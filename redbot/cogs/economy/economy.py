import asyncio
import calendar
import logging
import time
from collections import namedtuple
from datetime import datetime, timezone, timedelta
from math import ceil
from typing import Literal

import discord
from discord import app_commands

from redbot.core import Config, bank, commands, errors
from redbot.core.bot import Red
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils import AsyncIter
from redbot.core.utils.chat_formatting import box, humanize_number, humanize_timedelta
from redbot.core.utils.menus import menu
from .dashboard_integration import DashboardIntegration

_ = T_ = Translator("Economy", __file__)

logger = logging.getLogger("red.economy")

# How often the auto-payday loop wakes up. Paydays are configured in minutes at
# the shortest, so a minute of granularity is plenty and costs almost nothing.
AUTO_PAYDAY_INTERVAL = 60

# A voice clock lives in memory while someone is talking. Banking it every few
# passes means a restart costs a few minutes of their evening, not all of it.
VOICE_FLUSH_EVERY = 5

# How long the voice rules are trusted before being read again. Voice events
# are frequent; guild settings barely ever change.
VOICE_RULES_TTL = 60

SECONDS_PER_HOUR = 3600

# Discord renders at most 1024 characters per field, and a payslip nobody reads
# is worse than a short one.
MAX_PAYSLIP_LEADERBOARD = 15

PAYSLIP_MEDALS = (
    "\N{FIRST PLACE MEDAL}",
    "\N{SECOND PLACE MEDAL}",
    "\N{THIRD PLACE MEDAL}",
)

DEFAULT_PAYDAY_TITLE = "\N{MONEY WITH WINGS} Payslip's here"
DEFAULT_PAYDAY_MESSAGE = "**{bot}** pays **{total} {currency}** to **{members}** members."
DEFAULT_VOICE_PAYDAY_MESSAGE = (
    "**{bot}** pays **{total} {currency}** to **{members}** members "
    "for **{voice}** in voice."
)

NUM_ENC = "\N{COMBINING ENCLOSING KEYCAP}"
VARIATION_SELECTOR = "\N{VARIATION SELECTOR-16}"
MOCK_MEMBER = namedtuple("Member", "id guild")


def guild_only_check():
    async def pred(ctx: commands.Context):
        if await bank.is_global():
            return True
        elif ctx.guild is not None and not await bank.is_global():
            return True
        else:
            return False

    return commands.check(pred)


class SetParser:
    def __init__(self, argument):
        allowed = ("+", "-")
        try:
            self.sum = int(argument)
        except ValueError:
            raise commands.BadArgument(
                _(
                    "Invalid value, the argument must be an integer,"
                    " optionally preceded with a `+` or `-` sign."
                )
            )
        if argument and argument[0] in allowed:
            if self.sum < 0:
                self.operation = "withdraw"
            elif self.sum > 0:
                self.operation = "deposit"
            else:
                raise commands.BadArgument(
                    _(
                        "Invalid value, the amount of currency to increase or decrease"
                        " must be an integer different from zero."
                    )
                )
            self.sum = abs(self.sum)
        else:
            self.operation = "set"


@cog_i18n(_)
class Economy(DashboardIntegration, commands.Cog):
    """Get rich and have fun with imaginary currency!"""

    default_guild_settings = {
        "PAYDAY_TIME": 300,
        "PAYDAY_CREDITS": 120,
        # Kept registered so old saved values are not orphaned; the slot
        # machine itself now lives in the SimpleCasino cog.
        "SLOT_MIN": 5,
        "SLOT_MAX": 100,
        "SLOT_TIME": 5,
        "REGISTER_CREDITS": 0,
        # Pay everyone on a timer instead of waiting for them to ask.
        "AUTO_PAYDAY": False,
        # 0 pays every member; otherwise only members holding this role.
        "AUTO_PAYDAY_ROLE": 0,
        "AUTO_PAYDAY_ANNOUNCE": True,
        "AUTO_PAYDAY_CHANNEL": 0,
        "AUTO_PAYDAY_TITLE": "",
        "AUTO_PAYDAY_MESSAGE": "",
        "AUTO_PAYDAY_IMAGE": "",
        "AUTO_PAYDAY_COLOUR": 0,
        # Who is holding the most, listed under the payslip.
        "AUTO_PAYDAY_LEADERBOARD": True,
        "AUTO_PAYDAY_LEADERBOARD_SIZE": 5,
        # One clock for the whole server. Without this each member drifts onto
        # their own timer and a server ends up with a payslip per person.
        "AUTO_PAYDAY_LAST": 0,
        # "fixed" pays everyone the same; "voice" pays for time spent talking.
        "AUTO_PAYDAY_MODE": "fixed",
        # Credits per hour of voice, and the bounds around it.
        "AUTO_PAYDAY_VOICE_RATE": 100,
        "AUTO_PAYDAY_VOICE_MIN": 5,
        "AUTO_PAYDAY_VOICE_MAX": 0,
        # What still counts as being present.
        "AUTO_PAYDAY_VOICE_AFK": False,
        "AUTO_PAYDAY_VOICE_ALONE": False,
        "AUTO_PAYDAY_VOICE_DEAF": False,
    }

    default_global_settings = default_guild_settings

    # voice_seconds is what has been earned since the last payday, so a voice
    # payday pays for the day just gone rather than for all of history.
    default_member_settings = {"next_payday": 0, "last_slot": 0, "voice_seconds": 0}

    default_role_settings = {"PAYDAY_CREDITS": 0}

    default_user_settings = default_member_settings

    def __init__(self, bot: Red):
        super().__init__()
        self.bot = bot
        self.config = Config.get_conf(self, 1256844281)
        self.config.register_guild(**self.default_guild_settings)
        self.config.register_global(**self.default_global_settings)
        self.config.register_member(**self.default_member_settings)
        self.config.register_user(**self.default_user_settings)
        self.config.register_role(**self.default_role_settings)
        self._auto_payday_task: asyncio.Task | None = None
        # guild id -> member id -> monotonic clock start
        self._voice_open: dict[int, dict[int, float]] = {}
        self._voice_rules_cache: dict[int, tuple[float, dict]] = {}

    async def cog_load(self) -> None:
        self._auto_payday_task = asyncio.create_task(self._auto_payday_loop())

    def cog_unload(self) -> None:
        if self._auto_payday_task is not None:
            self._auto_payday_task.cancel()
        # Best effort: bank whatever is on the clock before we go. The periodic
        # flush is what actually guarantees the time survives a restart.
        if self._voice_open:
            asyncio.create_task(self._voice_flush())

    # ------------------------------------------------------------ auto payday

    async def _auto_payday_loop(self) -> None:
        """Run a payday pass for every guild that wants one."""
        await self.bot.wait_until_red_ready()
        await self._voice_seed()
        tick = 0
        while True:
            try:
                await asyncio.sleep(AUTO_PAYDAY_INTERVAL)
                tick += 1
                if tick % VOICE_FLUSH_EVERY == 0:
                    await self._voice_flush()
                for guild in list(self.bot.guilds):
                    if await self.bot.cog_disabled_in_guild(self, guild):
                        continue
                    if not await self.config.guild(guild).AUTO_PAYDAY():
                        continue
                    try:
                        await self.run_auto_payday(guild)
                    except Exception:  # noqa: BLE001 - one guild must not stop the rest
                        logger.exception("Auto payday failed in %s (%s)", guild, guild.id)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 - the loop itself must never die
                logger.exception("Auto payday loop error")

    # ---------------------------------------------------------- voice clocks

    async def _voice_rules(self, guild: discord.Guild) -> dict:
        """What still counts as being present, cached so events stay cheap."""
        now = time.monotonic()
        cached = self._voice_rules_cache.get(guild.id)
        if cached is not None and cached[0] > now:
            return cached[1]
        conf = await self.config.guild(guild).all()
        rules = {
            "voice": (conf.get("AUTO_PAYDAY_MODE") or "fixed") == "voice",
            "afk": bool(conf.get("AUTO_PAYDAY_VOICE_AFK")),
            "alone": bool(conf.get("AUTO_PAYDAY_VOICE_ALONE")),
            "deaf": bool(conf.get("AUTO_PAYDAY_VOICE_DEAF")),
        }
        self._voice_rules_cache[guild.id] = (now + VOICE_RULES_TTL, rules)
        return rules

    def forget_voice_rules(self, guild: discord.Guild) -> None:
        """Drop the cached rules after they have been edited."""
        self._voice_rules_cache.pop(guild.id, None)

    async def resync_voice(self, guild: discord.Guild) -> None:
        """Re-check every clock in one server after its rules changed.

        Turning voice pay on should start counting the people already talking,
        not wait for them to move channel.
        """
        self.forget_voice_rules(guild)
        for member in guild.members:
            if member.bot:
                continue
            if member.voice is not None or member.id in self._voice_open.get(guild.id, {}):
                try:
                    await self._voice_sync(member)
                except Exception:  # noqa: BLE001 - one member must not stop the rest
                    logger.exception("Could not resync the voice clock for %s", member.id)

    @staticmethod
    def _voice_counts(member: discord.Member, state, rules: dict) -> bool:
        """Whether this member's clock should be running right now."""
        if member.bot or state is None or state.channel is None:
            return False
        afk = member.guild.afk_channel
        if not rules["afk"] and afk is not None and state.channel.id == afk.id:
            return False
        if not rules["deaf"] and (state.self_deaf or state.deaf):
            return False
        if not rules["alone"]:
            # Sitting alone in a channel is idling, not taking part.
            if not any(m for m in state.channel.members if not m.bot and m.id != member.id):
                return False
        return True

    async def _voice_bank(self, member: discord.Member, seconds: float) -> None:
        if seconds < 1:
            return
        conf = self.config.member(member)
        await conf.voice_seconds.set(int(await conf.voice_seconds()) + int(seconds))

    async def _voice_sync(self, member: discord.Member, state=None) -> None:
        """Start or stop one member's clock to match what they are doing."""
        open_here = self._voice_open.setdefault(member.guild.id, {})
        started = open_here.get(member.id)
        rules = await self._voice_rules(member.guild)
        counts = rules["voice"] and self._voice_counts(
            member, state if state is not None else member.voice, rules
        )
        if counts:
            if started is None:
                open_here[member.id] = time.monotonic()
        elif started is not None:
            del open_here[member.id]
            await self._voice_bank(member, time.monotonic() - started)

    async def _voice_flush(self, guild: discord.Guild = None) -> None:
        """Bank what is on the clock so far, leaving running clocks running."""
        now = time.monotonic()
        guild_ids = [guild.id] if guild is not None else list(self._voice_open)
        for guild_id in guild_ids:
            open_here = self._voice_open.get(guild_id)
            if not open_here:
                continue
            found = self.bot.get_guild(guild_id)
            if found is None:
                self._voice_open.pop(guild_id, None)
                continue
            for member_id, started in list(open_here.items()):
                member = found.get_member(member_id)
                if member is None:
                    del open_here[member_id]
                    continue
                open_here[member_id] = now
                await self._voice_bank(member, now - started)

    async def _voice_seed(self) -> None:
        """Pick up whoever is already talking when the cog loads."""
        for guild in list(self.bot.guilds):
            for member in guild.members:
                if member.voice is not None and member.voice.channel is not None:
                    try:
                        await self._voice_sync(member)
                    except Exception:  # noqa: BLE001 - one member must not stop the rest
                        logger.exception("Could not start the voice clock for %s", member.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after) -> None:
        if member.bot:
            return
        guild = member.guild
        try:
            if await self.bot.cog_disabled_in_guild(self, guild):
                return
            # Anyone else in either channel may have just become alone, or
            # stopped being alone, so their clock needs a look as well.
            touched = {member.id: member}
            for channel in (before.channel, after.channel):
                if channel is not None:
                    for other in channel.members:
                        if not other.bot:
                            touched[other.id] = other
            for one in touched.values():
                await self._voice_sync(one)
        except Exception:  # noqa: BLE001 - never break the event for the rest of the bot
            logger.exception("Voice clock update failed in %s", guild.id)

    async def _payday_amount_for(self, member: discord.Member, base: int, role_credits: dict) -> int:
        """What this member earns, taking the best of their role payouts.

        In voice mode the role amounts are read as per-hour rates instead, so a
        role that pays more keeps paying more without a second set of settings.
        """
        amount = base
        for role in member.roles:
            role_amount = (role_credits.get(role.id) or {}).get("PAYDAY_CREDITS", 0)
            if role_amount > amount:
                amount = role_amount
        return amount

    async def run_auto_payday(self, guild: discord.Guild, *, force: bool = False) -> dict:
        """Pay everyone who is due, and announce it. Returns a summary.

        The whole server shares one clock: a payday happens for everyone at
        once or not at all. Paying people off their own timers is what leaves a
        server with a second payslip for one member an hour after the real one.
        """
        settings = await self.config.guild(guild).all()
        is_global = await bank.is_global()
        cur_time = calendar.timegm(datetime.now(timezone.utc).utctimetuple())

        if is_global:
            payday_time = await self.config.PAYDAY_TIME()
            base_credits = await self.config.PAYDAY_CREDITS()
        else:
            payday_time = settings["PAYDAY_TIME"]
            base_credits = settings["PAYDAY_CREDITS"]
        role_credits = {} if is_global else await self.config.all_roles()

        last_run = settings.get("AUTO_PAYDAY_LAST") or 0
        if not force and cur_time < last_run + payday_time:
            return {
                "paid": 0,
                "total": 0,
                "capped": 0,
                "next": last_run + payday_time,
                "voice": False,
                "voice_seconds": 0,
                "top": [],
                "skipped": True,
            }

        voice_mode = (settings.get("AUTO_PAYDAY_MODE") or "fixed") == "voice"
        rate = max(0, int(settings.get("AUTO_PAYDAY_VOICE_RATE") or 0))
        min_seconds = max(0, int(settings.get("AUTO_PAYDAY_VOICE_MIN") or 0)) * 60
        cap = max(0, int(settings.get("AUTO_PAYDAY_VOICE_MAX") or 0))
        earned: dict = {}
        if voice_mode:
            # Bank the live clocks first, so the payslip counts the minutes
            # somebody is in the middle of right now.
            await self._voice_flush(guild)
            earned = await self.config.all_members(guild)

        required_role = settings.get("AUTO_PAYDAY_ROLE") or 0
        total = 0
        paid: list[discord.Member] = []
        capped = 0
        voice_seconds = 0
        top: list[tuple[str, int, int]] = []

        for member in guild.members:
            if member.bot:
                continue
            if required_role and not member.get_role(required_role):
                continue

            seconds = 0
            if voice_mode:
                seconds = int((earned.get(member.id) or {}).get("voice_seconds", 0) or 0)
                if seconds < min_seconds or rate <= 0:
                    continue
                member_rate = (
                    rate
                    if is_global
                    else await self._payday_amount_for(member, rate, role_credits)
                )
                amount = int(seconds * member_rate / SECONDS_PER_HOUR)
                if cap:
                    amount = min(amount, cap)
            else:
                amount = (
                    base_credits
                    if is_global
                    else await self._payday_amount_for(member, base_credits, role_credits)
                )
            if amount <= 0:
                continue
            try:
                await bank.deposit_credits(member, amount)
            except errors.BalanceTooHigh as exc:
                # Top them up to the ceiling rather than skipping them, which is
                # what the command does, then stop counting them as earning.
                await bank.set_balance(member, exc.max_balance)
                capped += 1
            except Exception:  # noqa: BLE001 - a bad account must not stop payroll
                logger.exception("Could not pay %s (%s)", member, member.id)
                continue
            else:
                total += amount
                paid.append(member)
                voice_seconds += seconds
                top.append((member.display_name, amount, seconds))

            if is_global:
                await self.config.user(member).next_payday.set(cur_time)
            else:
                await self.config.member(member).next_payday.set(cur_time)
            # Payroll is not urgent; let the rest of the bot breathe.
            await asyncio.sleep(0)

        if voice_mode:
            # The day is settled, so everyone starts the next one from zero,
            # including the people who did not talk enough to earn anything.
            for member_id, data in earned.items():
                if data.get("voice_seconds"):
                    await self.config.member_from_ids(guild.id, member_id).voice_seconds.set(0)
            now = time.monotonic()
            for member_id in self._voice_open.get(guild.id, {}):
                self._voice_open[guild.id][member_id] = now

        await self.config.guild(guild).AUTO_PAYDAY_LAST.set(cur_time)

        top.sort(key=lambda row: row[1], reverse=True)
        summary = {
            "paid": len(paid),
            "total": total,
            "capped": capped,
            "next": cur_time + payday_time,
            "voice": voice_mode,
            "voice_seconds": voice_seconds,
            "top": top,
            "skipped": False,
        }
        if paid and settings.get("AUTO_PAYDAY_ANNOUNCE"):
            await self._announce_payday(guild, settings, summary)
        if paid or capped:
            logger.info(
                "Auto payday in %s: %s paid %s, %s at the cap",
                guild.id,
                len(paid),
                total,
                capped,
            )
        return summary

    async def _payslip_leaderboard(self, guild: discord.Guild, size: int) -> str:
        """The richest members, as a block for the payslip embed.

        Returns an empty string when there is nothing worth showing, so the
        caller can leave the field off entirely rather than print a header over
        nothing.
        """
        size = max(1, min(int(size or 0), MAX_PAYSLIP_LEADERBOARD))
        try:
            is_global = await bank.is_global()
            raw = await bank.get_leaderboard(positions=size, guild=guild)
        except Exception:  # noqa: BLE001 - the payslip matters more than the extra
            logger.exception("Could not build the payslip leaderboard for %s", guild.id)
            return ""

        lines = []
        for position, (user_id, data) in enumerate(raw, start=1):
            balance = (data or {}).get("balance", 0)
            if balance <= 0:
                continue
            member = guild.get_member(user_id)
            name = (
                member.display_name
                if member is not None
                else (data or {}).get("name") or _("Unknown")
            )
            rank = (
                PAYSLIP_MEDALS[position - 1]
                if position <= len(PAYSLIP_MEDALS)
                else f"`{position}.`"
            )
            # Names are user-controlled; bolding them would let anyone smuggle
            # markdown into the payslip, so only the balance is styled.
            lines.append(f"{rank} {discord.utils.escape_markdown(name)} \u2014 "
                         f"**{humanize_number(balance)}**")
            # A field caps at 1024 characters; stop well short of it.
            if sum(len(line) + 1 for line in lines) > 900:
                break

        if not lines:
            return ""
        if is_global:
            lines.append(_("*Balances are shared across every server.*"))
        return "\n".join(lines)

    async def _announce_payday(self, guild: discord.Guild, settings: dict, summary: dict) -> None:
        channel = guild.get_channel(settings.get("AUTO_PAYDAY_CHANNEL") or 0)
        if channel is None or not hasattr(channel, "send"):
            return
        me = guild.me
        if me is None or not channel.permissions_for(me).embed_links:
            return

        currency = await bank.get_currency_name(guild)
        members = summary["paid"]
        total = summary["total"]
        voice_mode = summary.get("voice")
        voice_time = humanize_timedelta(seconds=summary.get("voice_seconds", 0)) or _("none")
        fields = {
            "bot": me.display_name,
            "guild": guild.name,
            "currency": currency,
            "total": humanize_number(total),
            "members": humanize_number(members),
            "average": humanize_number(total // members if members else 0),
            "voice": voice_time,
        }

        template = settings.get("AUTO_PAYDAY_MESSAGE") or (
            DEFAULT_VOICE_PAYDAY_MESSAGE if voice_mode else DEFAULT_PAYDAY_MESSAGE
        )
        try:
            description = template.format(**fields)
        except (KeyError, IndexError, ValueError):
            # A typo in the template is not worth losing the announcement over.
            description = (
                DEFAULT_VOICE_PAYDAY_MESSAGE if voice_mode else DEFAULT_PAYDAY_MESSAGE
            ).format(**fields)

        colour_value = settings.get("AUTO_PAYDAY_COLOUR") or 0
        try:
            colour = discord.Colour(colour_value) if colour_value else discord.Colour.gold()
        except (TypeError, ValueError):
            colour = discord.Colour.gold()

        embed = discord.Embed(
            title=(settings.get("AUTO_PAYDAY_TITLE") or DEFAULT_PAYDAY_TITLE).format(**fields),
            description=description,
            colour=colour,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name=_("Members paid"), value=fields["members"])
        embed.add_field(name=_("Average"), value=f"{fields['average']} {currency}")
        if voice_mode:
            embed.add_field(name=_("Voice time"), value=voice_time)
        embed.add_field(
            name=_("Next payday"),
            value=discord.utils.format_dt(
                datetime.fromtimestamp(summary["next"], tz=timezone.utc), "R"
            ),
        )
        if summary["capped"]:
            embed.add_field(
                name=_("At the cap"),
                value=_("{count} already hold the maximum.").format(
                    count=humanize_number(summary["capped"])
                ),
                inline=False,
            )
        if voice_mode and summary.get("top"):
            # In voice mode who talked most is the news, so it goes above the
            # standing leaderboard rather than replacing it.
            lines = []
            for position, (name, amount, seconds) in enumerate(summary["top"][:5], start=1):
                rank = (
                    PAYSLIP_MEDALS[position - 1]
                    if position <= len(PAYSLIP_MEDALS)
                    else f"`{position}.`"
                )
                spent = humanize_timedelta(seconds=seconds) or _("a moment")
                lines.append(
                    f"{rank} {discord.utils.escape_markdown(name)} — "
                    f"**{humanize_number(amount)}** ({spent})"
                )
            embed.add_field(
                name=_("Most time in voice"), value="\n".join(lines), inline=False
            )

        if settings.get("AUTO_PAYDAY_LEADERBOARD", True):
            board = await self._payslip_leaderboard(
                guild, settings.get("AUTO_PAYDAY_LEADERBOARD_SIZE", 5)
            )
            if board:
                embed.add_field(
                    name=_("Richest in {guild}").format(guild=guild.name),
                    value=board,
                    inline=False,
                )

        image = (settings.get("AUTO_PAYDAY_IMAGE") or "").strip()
        if image.startswith(("http://", "https://")):
            embed.set_image(url=image)
        embed.set_footer(text=guild.name, icon_url=guild.icon.url if guild.icon else None)

        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            logger.warning("Could not announce the payday in %s", channel.id, exc_info=True)

    async def red_delete_data_for_user(
        self,
        *,
        requester: Literal["discord_deleted_user", "owner", "user", "user_strict"],
        user_id: int,
    ):
        if requester != "discord_deleted_user":
            return

        await self.config.user_from_id(user_id).clear()

        all_members = await self.config.all_members()

        async for guild_id, guild_data in AsyncIter(all_members.items(), steps=100):
            if user_id in guild_data:
                await self.config.member_from_ids(guild_id, user_id).clear()

    @app_commands.command(
        name="balance",
        description="Show an account balance.",
        extras={"red_force_enable": True},
    )
    @app_commands.guild_only()
    @app_commands.describe(user="Whose balance to show. Defaults to yours.")
    async def balance(
        self, interaction: discord.Interaction, user: discord.Member = None
    ):
        """Show the user's account balance.

        Example:
        - `[p]bank balance`
        - `[p]bank balance @Twentysix`

        **Arguments**

        - `<user>` The user to check the balance of. If omitted, defaults to your own balance.
        """
        ctx = await commands.Context.from_interaction(interaction)
        user = user or interaction.user
        bal = await bank.get_balance(user)
        currency = await bank.get_currency_name(ctx.guild)
        max_bal = await bank.get_max_balance(ctx.guild)
        if bal > max_bal:
            bal = max_bal
            await bank.set_balance(user, bal)
        await ctx.send(
            _("{user}'s balance is {num} {currency}").format(
                user=user.display_name, num=humanize_number(bal), currency=currency
            )
        )

    @app_commands.command(
        name="transfer",
        description="Send some of your currency to someone else.",
        extras={"red_force_enable": True},
    )
    @app_commands.guild_only()
    @app_commands.describe(
        to="Who to send it to.",
        amount="How much to send.",
    )
    async def transfer(
        self,
        interaction: discord.Interaction,
        to: discord.Member,
        amount: app_commands.Range[int, 1, None],
    ):
        """Transfer currency to other users.

        This will come out of your balance, so make sure you have enough.

        Example:
        - `[p]bank transfer @Twentysix 500`

        **Arguments**

        - `<to>` The user to give currency to.
        - `<amount>` The amount of currency to give.
        """
        ctx = await commands.Context.from_interaction(interaction)
        from_ = ctx.author
        currency = await bank.get_currency_name(ctx.guild)

        try:
            await bank.transfer_credits(from_, to, amount)
        except (ValueError, errors.BalanceTooHigh) as e:
            return await ctx.send(str(e))

        await ctx.send(
            _("{user} transferred {num} {currency} to {other_user}").format(
                user=from_.display_name,
                num=humanize_number(amount),
                currency=currency,
                other_user=to.display_name,
            )
        )

    @app_commands.command(
        name="payday",
        description="Claim your free currency.",
        extras={"red_force_enable": True},
    )
    @app_commands.guild_only()
    async def payday(self, interaction: discord.Interaction):
        """Get some free currency.

        The amount awarded and frequency can be configured.
        """
        ctx = await commands.Context.from_interaction(interaction)
        author = ctx.author
        guild = ctx.guild

        cur_time = calendar.timegm(ctx.message.created_at.utctimetuple())
        credits_name = await bank.get_currency_name(ctx.guild)

        if guild is not None:
            auto = await self.config.guild(guild).all()
            if auto.get("AUTO_PAYDAY"):
                # The server pays everybody together; claiming by hand on top of
                # that is what puts members back on their own timers.
                every = (
                    await self.config.PAYDAY_TIME()
                    if await bank.is_global()
                    else auto["PAYDAY_TIME"]
                )
                due = (auto.get("AUTO_PAYDAY_LAST") or cur_time) + every
                relative_time = discord.utils.format_dt(
                    datetime.fromtimestamp(max(due, cur_time), tz=timezone.utc), "R"
                )
                await ctx.send(await self._too_soon(ctx, relative_time))
                return
        if await bank.is_global():  # Role payouts will not be used
            # Gets the latest time the user used the command successfully and adds the global payday time
            next_payday = (
                await self.config.user(author).next_payday() + await self.config.PAYDAY_TIME()
            )
            if cur_time >= next_payday:
                try:
                    await bank.deposit_credits(author, await self.config.PAYDAY_CREDITS())
                except errors.BalanceTooHigh as exc:
                    await bank.set_balance(author, exc.max_balance)
                    await ctx.send(
                        _(
                            "You've reached the maximum amount of {currency}! "
                            "Please spend some more \N{GRIMACING FACE}\n\n"
                            "You currently have {new_balance} {currency}."
                        ).format(
                            currency=credits_name, new_balance=humanize_number(exc.max_balance)
                        )
                    )
                    return
                # Sets the current time as the latest payday
                await self.config.user(author).next_payday.set(cur_time)

                pos = await bank.get_leaderboard_position(author)
                await ctx.send(
                    _(
                        "{author.mention} Here, take some {currency}. "
                        "Enjoy! (+{amount} {currency}!)\n\n"
                        "You currently have {new_balance} {currency}.\n\n"
                        "You are currently #{pos} on the global leaderboard!"
                    ).format(
                        author=author,
                        currency=credits_name,
                        amount=humanize_number(await self.config.PAYDAY_CREDITS()),
                        new_balance=humanize_number(await bank.get_balance(author)),
                        pos=humanize_number(pos) if pos else pos,
                    )
                )

            else:
                relative_time = discord.utils.format_dt(
                    datetime.now(timezone.utc) + timedelta(seconds=next_payday - cur_time), "R"
                )
                await ctx.send(await self._too_soon(ctx, relative_time))
        else:
            # Gets the users latest successfully payday and adds the guilds payday time
            next_payday = (
                await self.config.member(author).next_payday()
                + await self.config.guild(guild).PAYDAY_TIME()
            )
            if cur_time >= next_payday:
                credit_amount = await self.config.guild(guild).PAYDAY_CREDITS()
                for role in author.roles:
                    role_credits = await self.config.role(
                        role
                    ).PAYDAY_CREDITS()  # Nice variable name
                    if role_credits > credit_amount:
                        credit_amount = role_credits
                try:
                    await bank.deposit_credits(author, credit_amount)
                except errors.BalanceTooHigh as exc:
                    await bank.set_balance(author, exc.max_balance)
                    await ctx.send(
                        _(
                            "You've reached the maximum amount of {currency}! "
                            "Please spend some more \N{GRIMACING FACE}\n\n"
                            "You currently have {new_balance} {currency}."
                        ).format(
                            currency=credits_name, new_balance=humanize_number(exc.max_balance)
                        )
                    )
                    return

                # Sets the latest payday time to the current time
                next_payday = cur_time

                await self.config.member(author).next_payday.set(next_payday)
                pos = await bank.get_leaderboard_position(author)
                await ctx.send(
                    _(
                        "{author.mention} Here, take some {currency}. "
                        "Enjoy! (+{amount} {currency}!)\n\n"
                        "You currently have {new_balance} {currency}.\n\n"
                        "You are currently #{pos} on the global leaderboard!"
                    ).format(
                        author=author,
                        currency=credits_name,
                        amount=humanize_number(credit_amount),
                        new_balance=humanize_number(await bank.get_balance(author)),
                        pos=humanize_number(pos) if pos else pos,
                    )
                )
            else:
                relative_time = discord.utils.format_dt(
                    datetime.now(timezone.utc) + timedelta(seconds=next_payday - cur_time), "R"
                )
                await ctx.send(await self._too_soon(ctx, relative_time))

    @app_commands.command(
        name="leaderboard",
        description="Show the richest members.",
        extras={"red_force_enable": True},
    )
    @app_commands.guild_only()
    @app_commands.describe(
        top="How many places to show. Defaults to 10.",
        show_global="Show the whole bot rather than this server.",
    )
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        top: app_commands.Range[int, 1, 100] = 10,
        show_global: bool = False,
    ):
        """Print the leaderboard.

        Defaults to top 10.

        Examples:
        - `[p]leaderboard`
        - `[p]leaderboard 50` - Shows the top 50 instead of top 10.
        - `[p]leaderboard 100 yes` - Shows the top 100 from all servers.

        **Arguments**

        - `<top>` How many positions on the leaderboard to show. Defaults to 10 if omitted.
        - `<show_global>` Whether to include results from all servers. This will default to false unless specified.
        """
        ctx = await commands.Context.from_interaction(interaction)
        guild = ctx.guild
        author = ctx.author
        embed_requested = await ctx.embed_requested()
        footer_message = _("Page {page_num}/{page_len}.")
        max_bal = await bank.get_max_balance(ctx.guild)

        if top < 1:
            top = 10

        base_embed = discord.Embed(title=_("Economy Leaderboard"))
        if show_global and await bank.is_global():
            # show_global is only applicable if bank is global
            bank_sorted = await bank.get_leaderboard(positions=top, guild=None)
            base_embed.set_author(
                name=ctx.bot.user.display_name, icon_url=ctx.bot.user.display_avatar
            )
        else:
            bank_sorted = await bank.get_leaderboard(positions=top, guild=guild)
            if guild:
                base_embed.set_author(name=guild.name, icon_url=guild.icon)

        try:
            bal_len = len(humanize_number(bank_sorted[0][1]["balance"]))
            bal_len_max = len(humanize_number(max_bal))
            if bal_len > bal_len_max:
                bal_len = bal_len_max
            # first user is the largest we'll see
        except IndexError:
            return await ctx.send(_("There are no accounts in the bank."))
        pound_len = len(str(len(bank_sorted)))
        header = "{pound:{pound_len}}{score:{bal_len}}{name:2}\n".format(
            pound="#",
            name=_("Name"),
            score=_("Score"),
            bal_len=bal_len + 6,
            pound_len=pound_len + 3,
        )
        highscores = []
        pos = 1
        temp_msg = header
        for acc in bank_sorted:
            try:
                name = guild.get_member(acc[0]).display_name
            except AttributeError:
                user_id = ""
                if await ctx.bot.is_owner(ctx.author):
                    user_id = f"({str(acc[0])})"
                name = f"{acc[1]['name']} {user_id}"

            balance = acc[1]["balance"]
            if balance > max_bal:
                balance = max_bal
                await bank.set_balance(MOCK_MEMBER(acc[0], guild), balance)
            balance = humanize_number(balance)
            if acc[0] != author.id:
                temp_msg += (
                    f"{f'{humanize_number(pos)}.': <{pound_len+2}} "
                    f"{balance: <{bal_len + 5}} {name}\n"
                )

            else:
                temp_msg += (
                    f"{f'{humanize_number(pos)}.': <{pound_len+2}} "
                    f"{balance: <{bal_len + 5}} "
                    f"<<{author.display_name}>>\n"
                )
            if pos % 10 == 0:
                if embed_requested:
                    embed = base_embed.copy()
                    embed.description = box(temp_msg, lang="md")
                    embed.set_footer(
                        text=footer_message.format(
                            page_num=len(highscores) + 1,
                            page_len=ceil(len(bank_sorted) / 10),
                        )
                    )
                    highscores.append(embed)
                else:
                    highscores.append(box(temp_msg, lang="md"))
                temp_msg = header
            pos += 1

        if temp_msg != header:
            if embed_requested:
                embed = base_embed.copy()
                embed.description = box(temp_msg, lang="md")
                embed.set_footer(
                    text=footer_message.format(
                        page_num=len(highscores) + 1,
                        page_len=ceil(len(bank_sorted) / 10),
                    )
                )
                highscores.append(embed)
            else:
                highscores.append(box(temp_msg, lang="md"))

        if highscores:
            await menu(ctx, highscores)
        else:
            await ctx.send(_("No balances found."))
