import asyncio
import calendar
import logging
from collections import namedtuple
from datetime import datetime, timezone, timedelta
from math import ceil
from typing import Literal

import discord

from redbot.core import Config, bank, commands, errors
from redbot.core.commands.converter import TimedeltaConverter, positive_int
from redbot.core.bot import Red
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils import AsyncIter
from redbot.core.utils.chat_formatting import box, humanize_number
from redbot.core.utils.menus import menu
from .dashboard_integration import DashboardIntegration

_ = T_ = Translator("Economy", __file__)

logger = logging.getLogger("red.economy")

# How often the auto-payday loop wakes up. Paydays are configured in minutes at
# the shortest, so a minute of granularity is plenty and costs almost nothing.
AUTO_PAYDAY_INTERVAL = 60

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
    }

    default_global_settings = default_guild_settings

    default_member_settings = {"next_payday": 0, "last_slot": 0}

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

    async def cog_load(self) -> None:
        self._auto_payday_task = asyncio.create_task(self._auto_payday_loop())

    def cog_unload(self) -> None:
        if self._auto_payday_task is not None:
            self._auto_payday_task.cancel()

    # ------------------------------------------------------------ auto payday

    async def _auto_payday_loop(self) -> None:
        """Run a payday pass for every guild that wants one."""
        await self.bot.wait_until_red_ready()
        while True:
            try:
                await asyncio.sleep(AUTO_PAYDAY_INTERVAL)
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

    async def _payday_amount_for(self, member: discord.Member, base: int, role_credits: dict) -> int:
        """What this member earns, taking the best of their role payouts."""
        amount = base
        for role in member.roles:
            role_amount = (role_credits.get(role.id) or {}).get("PAYDAY_CREDITS", 0)
            if role_amount > amount:
                amount = role_amount
        return amount

    async def run_auto_payday(self, guild: discord.Guild) -> dict:
        """Pay everyone who is due, and announce it. Returns a summary."""
        settings = await self.config.guild(guild).all()
        is_global = await bank.is_global()
        cur_time = calendar.timegm(datetime.now(timezone.utc).utctimetuple())

        # A global bank keeps one timer per user; a per-guild bank keeps one per
        # member. Reading them in bulk keeps a 500-member guild to two queries.
        if is_global:
            payday_time = await self.config.PAYDAY_TIME()
            base_credits = await self.config.PAYDAY_CREDITS()
            timers = await self.config.all_users()
        else:
            payday_time = settings["PAYDAY_TIME"]
            base_credits = settings["PAYDAY_CREDITS"]
            timers = await self.config.all_members(guild)
        role_credits = {} if is_global else await self.config.all_roles()

        required_role = settings.get("AUTO_PAYDAY_ROLE") or 0
        total = 0
        paid: list[discord.Member] = []
        capped = 0

        for member in guild.members:
            if member.bot:
                continue
            if required_role and not member.get_role(required_role):
                continue
            last = (timers.get(member.id) or {}).get("next_payday", 0)
            if cur_time < last + payday_time:
                continue

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

            if is_global:
                await self.config.user(member).next_payday.set(cur_time)
            else:
                await self.config.member(member).next_payday.set(cur_time)
            # Payroll is not urgent; let the rest of the bot breathe.
            await asyncio.sleep(0)

        summary = {
            "paid": len(paid),
            "total": total,
            "capped": capped,
            "next": cur_time + payday_time,
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
        fields = {
            "bot": me.display_name,
            "guild": guild.name,
            "currency": currency,
            "total": humanize_number(total),
            "members": humanize_number(members),
            "average": humanize_number(total // members if members else 0),
        }

        template = settings.get("AUTO_PAYDAY_MESSAGE") or DEFAULT_PAYDAY_MESSAGE
        try:
            description = template.format(**fields)
        except (KeyError, IndexError, ValueError):
            # A typo in the template is not worth losing the announcement over.
            description = DEFAULT_PAYDAY_MESSAGE.format(**fields)

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

    @guild_only_check()
    @commands.group(name="bank")
    async def _bank(self, ctx: commands.Context):
        """Base command to manage the bank."""
        pass

    @_bank.command()
    async def balance(self, ctx: commands.Context, user: discord.Member = commands.Author):
        """Show the user's account balance.

        Example:
        - `[p]bank balance`
        - `[p]bank balance @Twentysix`

        **Arguments**

        - `<user>` The user to check the balance of. If omitted, defaults to your own balance.
        """
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

    @_bank.command()
    async def transfer(self, ctx: commands.Context, to: discord.Member, amount: int):
        """Transfer currency to other users.

        This will come out of your balance, so make sure you have enough.

        Example:
        - `[p]bank transfer @Twentysix 500`

        **Arguments**

        - `<to>` The user to give currency to.
        - `<amount>` The amount of currency to give.
        """
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

    @bank.is_owner_if_bank_global()
    @commands.admin_or_permissions(manage_guild=True)
    @_bank.command(name="set")
    async def _set(self, ctx: commands.Context, to: discord.Member, creds: SetParser):
        """Set the balance of a user's bank account.

        Putting + or - signs before the amount will add/remove currency on the user's bank account instead.

        Examples:
        - `[p]bank set @Twentysix 26` - Sets balance to 26
        - `[p]bank set @Twentysix +2` - Increases balance by 2
        - `[p]bank set @Twentysix -6` - Decreases balance by 6

        **Arguments**

        - `<to>` The user to set the currency of.
        - `<creds>` The amount of currency to set their balance to.
        """
        author = ctx.author
        currency = await bank.get_currency_name(ctx.guild)

        try:
            if creds.operation == "deposit":
                await bank.deposit_credits(to, creds.sum)
                msg = _("{author} added {num} {currency} to {user}'s account.").format(
                    author=author.display_name,
                    num=humanize_number(creds.sum),
                    currency=currency,
                    user=to.display_name,
                )
            elif creds.operation == "withdraw":
                await bank.withdraw_credits(to, creds.sum)
                msg = _("{author} removed {num} {currency} from {user}'s account.").format(
                    author=author.display_name,
                    num=humanize_number(creds.sum),
                    currency=currency,
                    user=to.display_name,
                )
            else:
                await bank.set_balance(to, creds.sum)
                msg = _("{author} set {user}'s account balance to {num} {currency}.").format(
                    author=author.display_name,
                    num=humanize_number(creds.sum),
                    currency=currency,
                    user=to.display_name,
                )
        except (ValueError, errors.BalanceTooHigh) as e:
            await ctx.send(str(e))
        else:
            await ctx.send(msg)

    @bank.is_owner_if_bank_global()
    @commands.admin_or_permissions(manage_guild=True)
    @_bank.command(name="add")
    async def _add(self, ctx: commands.Context, to: discord.Member, creds: positive_int):
        """Add currency to a user's bank account.

        Example:
        - `[p]bank add @Twentysix 100` - Increases balance by 100

        **Arguments**

        - `<to>` The user to give currency to.
        - `<creds>` The amount of currency to add.
        """
        author = ctx.author
        currency = await bank.get_currency_name(ctx.guild)

        try:
            await bank.deposit_credits(to, creds)
        except (ValueError, errors.BalanceTooHigh) as e:
            await ctx.send(str(e))
        else:
            await ctx.send(
                _("{author} added {num} {currency} to {user}'s account.").format(
                    author=author.display_name,
                    num=humanize_number(creds),
                    currency=currency,
                    user=to.display_name,
                )
            )

    @bank.is_owner_if_bank_global()
    @commands.admin_or_permissions(manage_guild=True)
    @_bank.command(name="sub", aliases=["subtract"])
    async def _sub(self, ctx: commands.Context, to: discord.Member, creds: positive_int):
        """Remove currency from a user's bank account.

        Example:
        - `[p]bank sub @Twentysix 50` - Decreases balance by 50

        **Arguments**

        - `<to>` The user to remove currency from.
        - `<creds>` The amount of currency to remove.
        """
        author = ctx.author
        currency = await bank.get_currency_name(ctx.guild)

        try:
            await bank.withdraw_credits(to, creds)
        except ValueError as e:
            await ctx.send(str(e))
        else:
            await ctx.send(
                _("{author} removed {num} {currency} from {user}'s account.").format(
                    author=author.display_name,
                    num=humanize_number(creds),
                    currency=currency,
                    user=to.display_name,
                )
            )

    async def _too_soon(self, ctx: commands.Context, relative_time: str) -> str:
        """Explain the wait, and say so differently when payday is automatic."""
        if ctx.guild is not None and await self.config.guild(ctx.guild).AUTO_PAYDAY():
            return _(
                "{author.mention} You do not need this command here — "
                "your payday lands on its own. The next one is {relative_time}."
            ).format(author=ctx.author, relative_time=relative_time)
        return _("{author.mention} Too soon. Your next payday is {relative_time}.").format(
            author=ctx.author, relative_time=relative_time
        )

    @guild_only_check()
    @commands.command()
    async def payday(self, ctx: commands.Context):
        """Get some free currency.

        The amount awarded and frequency can be configured.
        """
        author = ctx.author
        guild = ctx.guild

        cur_time = calendar.timegm(ctx.message.created_at.utctimetuple())
        credits_name = await bank.get_currency_name(ctx.guild)
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

    @commands.command()
    @guild_only_check()
    async def leaderboard(self, ctx: commands.Context, top: int = 10, show_global: bool = False):
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

    @guild_only_check()
    @bank.is_owner_if_bank_global()
    @commands.admin_or_permissions(manage_guild=True)
    @commands.group()
    async def economyset(self, ctx: commands.Context):
        """Base command to manage Economy settings."""

    @economyset.command(name="showsettings")
    async def economyset_showsettings(self, ctx: commands.Context):
        """
        Shows the current economy settings
        """
        role_paydays = []
        guild = ctx.guild
        if await bank.is_global():
            conf = self.config
        else:
            conf = self.config.guild(guild)
            for role in guild.roles:
                rolepayday = await self.config.role(role).PAYDAY_CREDITS()
                if rolepayday:
                    role_paydays.append(f"{role}: {rolepayday}")
        await ctx.send(
            box(
                _(
                    "---Economy Settings---\n"
                    "Payday amount: {payday_amount}\n"
                    "Payday cooldown: {payday_time}\n"
                ).format(
                    payday_time=humanize_number(await conf.PAYDAY_TIME()),
                    payday_amount=humanize_number(await conf.PAYDAY_CREDITS()),
                )
            )
        )
        if role_paydays:
            await ctx.send(box(_("---Role Payday Amounts---\n") + "\n".join(role_paydays)))

    @economyset.command()
    async def paydaytime(
        self, ctx: commands.Context, *, duration: TimedeltaConverter(default_unit="seconds")
    ):
        """Set the cooldown for the payday command.

        Examples:
        - `[p]economyset paydaytime 86400`
        - `[p]economyset paydaytime 1d`

        **Arguments**

        - `<duration>` The new duration to wait in between uses of payday. Default is 5 minutes.
        Accepts: seconds, minutes, hours, days, weeks (if no unit is specified, the duration is assumed to be given in seconds)
        """
        seconds = int(duration.total_seconds())
        guild = ctx.guild
        if await bank.is_global():
            await self.config.PAYDAY_TIME.set(seconds)
        else:
            await self.config.guild(guild).PAYDAY_TIME.set(seconds)
        await ctx.send(
            _("Value modified. At least {num} seconds must pass between each payday.").format(
                num=seconds
            )
        )

    @economyset.command()
    async def paydayamount(self, ctx: commands.Context, creds: int):
        """Set the amount earned each payday.

        Example:
        - `[p]economyset paydayamount 400`

        **Arguments**

        - `<creds>` The new amount to give when using the payday command. Default is 120.
        """
        guild = ctx.guild
        max_balance = await bank.get_max_balance(ctx.guild)
        if creds <= 0 or creds > max_balance:
            return await ctx.send(
                _("Amount must be greater than zero and less than {maxbal}.").format(
                    maxbal=humanize_number(max_balance)
                )
            )
        credits_name = await bank.get_currency_name(guild)
        if await bank.is_global():
            await self.config.PAYDAY_CREDITS.set(creds)
        else:
            await self.config.guild(guild).PAYDAY_CREDITS.set(creds)
        await ctx.send(
            _("Every payday will now give {num} {currency}.").format(
                num=humanize_number(creds), currency=credits_name
            )
        )

    @economyset.command()
    async def rolepaydayamount(self, ctx: commands.Context, role: discord.Role, creds: int):
        """Set the amount earned each payday for a role.

        Set to `0` to remove the payday amount you set for that role.

        Only available when not using a global bank.

        Example:
        - `[p]economyset rolepaydayamount @Members 400`

        **Arguments**

        - `<role>` The role to assign a custom payday amount to.
        - `<creds>` The new amount to give when using the payday command.
        """
        guild = ctx.guild
        max_balance = await bank.get_max_balance(ctx.guild)
        if creds >= max_balance:
            return await ctx.send(
                _(
                    "The bank requires that you set the payday to be less than"
                    " its maximum balance of {maxbal}."
                ).format(maxbal=humanize_number(max_balance))
            )
        credits_name = await bank.get_currency_name(guild)
        if await bank.is_global():
            await ctx.send(_("The bank must be per-server for per-role paydays to work."))
        else:
            if creds <= 0:  # Because I may as well...
                default_creds = await self.config.guild(guild).PAYDAY_CREDITS()
                await self.config.role(role).clear()
                await ctx.send(
                    _(
                        "The payday value attached to role has been removed. "
                        "Users with this role will now receive the default pay "
                        "of {num} {currency}."
                    ).format(num=humanize_number(default_creds), currency=credits_name)
                )
            else:
                await self.config.role(role).PAYDAY_CREDITS.set(creds)
                await ctx.send(
                    _(
                        "Every payday will now give {num} {currency} "
                        "to people with the role {role_name}."
                    ).format(
                        num=humanize_number(creds), currency=credits_name, role_name=role.name
                    )
                )
