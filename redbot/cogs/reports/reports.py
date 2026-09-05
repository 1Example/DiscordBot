import logging
import asyncio
from typing import Union, List, Literal
from datetime import timedelta
from copy import copy
from types import SimpleNamespace
import contextlib
import discord

from redbot.core import app_commands, Config, commands
from redbot.core.utils import AsyncIter
from redbot.core.utils.chat_formatting import pagify, box
from redbot.core.utils.antispam import AntiSpam
from redbot.core.bot import Red
from redbot.core.i18n import Translator, cog_i18n, set_contextual_locales_from_guild
from redbot.core.utils.predicates import MessagePredicate
from redbot.core.utils.tunnel import Tunnel
from .dashboard_integration import DashboardIntegration


_ = Translator("Reports", __file__)

log = logging.getLogger("red.reports")


@cog_i18n(_)
class Reports(DashboardIntegration, commands.Cog):
    """Create user reports that server staff can respond to.

    Users can open reports using `[p]report`. These are then sent
    to a channel in the server for staff, and the report creator
    gets a DM. Both can be used to communicate.
    """

    default_guild_settings = {
        "output_channel": None,
        "active": False,
        "next_ticket": 1,
        # When set, a report opens a real ticket under this Tickets profile
        # instead of being forwarded into `output_channel`. The channel path is
        # kept as the fallback for servers that have not set up Tickets.
        "ticket_profile": None,
    }

    default_report = {"report": {}}

    # This can be made configurable later if it
    # becomes an issue.
    # Intervals should be a list of tuples in the form
    # (period: timedelta, max_frequency: int)
    # see redbot/core/utils/antispam.py for more details

    intervals = [
        (timedelta(seconds=5), 1),
        (timedelta(minutes=5), 3),
        (timedelta(hours=1), 10),
        (timedelta(days=1), 24),
    ]

    def __init__(self, bot: Red):
        super().__init__()
        self.bot = bot
        self.config = Config.get_conf(self, 78631113035100160, force_registration=True)
        self.config.register_guild(**self.default_guild_settings)
        self.config.init_custom("REPORT", 2)
        self.config.register_custom("REPORT", **self.default_report)
        self.antispam = {}
        self.user_cache = []
        self.tunnel_store = {}
        # (guild, ticket#):
        #   {'tun': Tunnel, 'msgs': List[int]}

    async def red_delete_data_for_user(
        self,
        *,
        requester: Literal["discord_deleted_user", "owner", "user", "user_strict"],
        user_id: int,
    ):
        if requester != "discord_deleted_user":
            return

        all_reports = await self.config.custom("REPORT").all()

        steps = 0
        paths = []

        # this doesn't use async iter intentionally due to the nested iterations
        for guild_id_str, tickets in all_reports.items():
            for ticket_number, ticket in tickets.items():
                steps += 1
                if not steps % 100:
                    await asyncio.sleep(0)  # yield context

            if ticket.get("report", {}).get("user_id", 0) == user_id:
                paths.append((guild_id_str, ticket_number))

        async with self.config.custom("REPORT").all() as all_reports:
            async for guild_id_str, ticket_number in AsyncIter(paths, steps=100):
                r = all_reports[guild_id_str][ticket_number]["report"]
                r["user_id"] = 0xDE1
                # this might include EUD, and a report of a deleted user
                # that's been unhandled for long enough for the
                # user to be deleted and the bot receive a request like this...
                r["report"] = "[REPORT DELETED DUE TO DISCORD REQUEST]"

    @property
    def tunnels(self):
        return [x["tun"] for x in self.tunnel_store.values()]


    async def internal_filter(self, m: discord.Member, mod=False, perms=None):
        if perms is not None and m.guild_permissions >= perms:
            return True
        if mod and await self.bot.is_mod(m):
            return True
        # The following line is for consistency with how perms are handled
        # in Red, though I'm not sure it makes sense to use here.
        if await self.bot.is_owner(m):
            return True

    async def discover_guild(
        self,
        author: discord.User,
        *,
        mod: bool = False,
        permissions: Union[discord.Permissions, dict] = None,
        prompt: str = "",
    ):
        """
        discovers which of shared guilds between the bot
        and provided user based on conditions (mod or permissions is an or)

        prompt is for providing a user prompt for selection
        """
        shared_guilds = []
        if permissions is None:
            perms = discord.Permissions()
        elif isinstance(permissions, discord.Permissions):
            perms = permissions
        else:
            perms = discord.Permissions(**permissions)

        async for guild in AsyncIter(self.bot.guilds, steps=100):
            x = guild.get_member(author.id)
            if x is not None:
                if await self.internal_filter(x, mod, perms):
                    shared_guilds.append(guild)
        if len(shared_guilds) == 0:
            raise ValueError("No Qualifying Shared Guilds")
        if len(shared_guilds) == 1:
            return shared_guilds[0]
        output = ""
        guilds = sorted(shared_guilds, key=lambda g: g.name)
        for i, guild in enumerate(guilds, 1):
            output += "{}: {}\n".format(i, guild.name)
        output += "\n{}".format(prompt)

        for page in pagify(output, delims=["\n"]):
            await author.send(box(page))

        try:
            message = await self.bot.wait_for(
                "message",
                check=MessagePredicate.same_context(channel=author.dm_channel, user=author),
                timeout=45,
            )
        except asyncio.TimeoutError:
            await author.send(_("You took too long to select. Try again later."))
            return None

        try:
            message = int(message.content.strip())
            guild = guilds[message - 1]
        except (ValueError, IndexError):
            await author.send(_("That wasn't a valid choice."))
            return None
        else:
            return guild

    async def open_ticket_for_report(
        self, msg: discord.Message, guild: discord.Guild, profile: str
    ):
        """Open a Tickets ticket for a report, if that is how this server is set up.

        Returns the ticket, or None if it could not be opened - unloaded cog,
        deleted profile, a profile that is disabled or misconfigured. Every one
        of those is a reason to fall through to the output channel rather than
        to lose the report.
        """
        tickets_cog = self.bot.get_cog("Tickets")
        if tickets_cog is None:
            log.warning("Reports is set to open tickets but the Tickets cog is not loaded.")
            return None
        author = guild.get_member(msg.author.id)
        if author is None:
            return None
        profiles = await tickets_cog.config.guild(guild).profiles()
        if profile not in profiles:
            log.warning(
                "Reports is set to use the Tickets profile %r, which no longer exists in %s.",
                profile,
                guild.id,
            )
            return None

        # `create_ticket` normally opens a modal to collect the reason. The
        # member has already written the report, and a report can be filed from
        # a DM where there is nothing to attach a modal to, so it is passed
        # straight through with `skip_modal` instead.
        #
        # With the modal skipped, `create_ticket` reads only `.guild` off the
        # context and checks that it is not an Interaction. A report from a DM
        # has no Context and `msg.guild` is None, so it gets a stand-in that
        # names the guild the report is *for*.
        stand_in = SimpleNamespace(
            guild=guild, author=author, channel=msg.channel, bot=self.bot
        )
        try:
            return await tickets_cog.create_ticket(
                stand_in,
                profile=profile,
                owner=author,
                reason=msg.clean_content,
                skip_modal=True,
            )
        except Exception:
            log.exception("Could not open a ticket for a report in %s", guild.id)
            return None

    async def send_report(self, ctx: commands.Context, msg: discord.Message, guild: discord.Guild):
        author = guild.get_member(msg.author.id)
        report = msg.clean_content

        profile = await self.config.guild(guild).ticket_profile()
        if profile:
            ticket = await self.open_ticket_for_report(msg, guild, profile)
            if ticket is not None:
                # The conversation now lives in the ticket channel, so no
                # tunnel is opened and nothing is stored in this cog's own
                # report store. The Ticket comes back rather than a number so
                # the caller can point the member at the channel.
                return ticket

        channel_id = await self.config.guild(guild).output_channel()
        channel = guild.get_channel(channel_id)
        if channel is None:
            return None

        files: List[discord.File] = await Tunnel.files_from_attach(msg)

        ticket_number = await self.config.guild(guild).next_ticket()
        await self.config.guild(guild).next_ticket.set(ticket_number + 1)

        if await self.bot.embed_requested(channel):
            em = discord.Embed(description=report, colour=await ctx.embed_colour())
            em.set_author(
                name=_("Report from {author}{maybe_nick}").format(
                    author=author, maybe_nick=(f" ({author.nick})" if author.nick else "")
                ),
                icon_url=author.display_avatar,
            )
            em.set_footer(text=_("Report #{}").format(ticket_number))
            send_content = None
        else:
            em = None
            send_content = _("Report from {author.mention} (Ticket #{number})").format(
                author=author, number=ticket_number
            )
            send_content += "\n" + report

        try:
            await Tunnel.message_forwarder(
                destination=channel, content=send_content, embed=em, files=files
            )
        except (discord.Forbidden, discord.HTTPException):
            return None

        await self.config.custom("REPORT", guild.id, ticket_number).report.set(
            {"user_id": author.id, "report": report}
        )
        return ticket_number

    @app_commands.command(
        name="report",
        description="Send a report to the moderators.",
        extras={"red_force_enable": True},
    )
    @app_commands.describe(
        _report="What you want to report. Leave empty and I will ask."
    )
    async def report(
        self, interaction: discord.Interaction, _report: str = ""
    ):
        """Send a report.

        Use without arguments for interactive reporting, or do
        `[p]report [text]` to use it non-interactively.
        """
        ctx = await commands.Context.from_interaction(interaction)
        author = ctx.author
        guild = ctx.guild
        if guild is None:
            guild = await self.discover_guild(
                author, prompt=_("Select a server to make a report in by number.")
            )
        if guild is None:
            return
        g_active = await self.config.guild(guild).active()
        if not g_active:
            return await author.send(_("Reporting has not been enabled for this server"))
        if guild.id not in self.antispam:
            self.antispam[guild.id] = {}
        if author.id not in self.antispam[guild.id]:
            self.antispam[guild.id][author.id] = AntiSpam(self.intervals)
        if self.antispam[guild.id][author.id].spammy:
            return await author.send(
                _(
                    "You've sent too many reports recently. "
                    "Please contact a server admin if this is important matter, "
                    "or please wait and try again later."
                )
            )
        if author.id in self.user_cache:
            return await author.send(
                _(
                    "Please finish making your prior report before trying to make an "
                    "additional one!"
                )
            )
        self.user_cache.append(author.id)
        try:
            await self._run_report(ctx, author, guild, _report)
        finally:
            # The old after_invoke released this; every early return below
            # would otherwise leave the author unable to report again.
            if author.id in self.user_cache:
                self.user_cache.remove(author.id)

    async def _run_report(self, ctx, author, guild, _report: str) -> None:
        """The body of /report, so the in-flight guard is always released."""

        if _report:
            _m = copy(ctx.message)
            _m.content = _report
            _m.content = _m.clean_content
            val = await self.send_report(ctx, _m, guild)
        else:
            try:
                await author.send(
                    _(
                        "Please respond to this message with your Report."
                        "\nYour report should be a single message"
                    )
                )
            except discord.Forbidden:
                return await ctx.send(_("This requires DMs enabled."))

            try:
                message = await self.bot.wait_for(
                    "message",
                    check=MessagePredicate.same_context(ctx, channel=author.dm_channel),
                    timeout=180,
                )
            except asyncio.TimeoutError:
                return await author.send(_("You took too long. Try again later."))
            else:
                val = await self.send_report(ctx, message, guild)

        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            if val is None:
                if (
                    await self.config.guild(guild).output_channel() is None
                    and await self.config.guild(guild).ticket_profile() is None
                ):
                    await author.send(
                        _(
                            "This server has no reports channel set up. Please contact a server admin."
                        )
                    )
                else:
                    await author.send(
                        _("There was an error sending your report, please contact a server admin.")
                    )
            elif isinstance(val, int):
                await author.send(_("Your report was submitted. (Ticket #{})").format(val))
                self.antispam[guild.id][author.id].stamp()
            else:
                # A Tickets ticket: the member can follow it in its channel.
                channel = getattr(val, "channel", None)
                if channel is not None:
                    await author.send(
                        _("Your report opened ticket #{id} in {channel}.").format(
                            id=val.id, channel=channel.mention
                        )
                    )
                else:
                    await author.send(
                        _("Your report opened ticket #{id}.").format(id=val.id)
                    )
                self.antispam[guild.id][author.id].stamp()

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """
        oh dear....
        """

        if not str(payload.emoji) == "\N{NEGATIVE SQUARED CROSS MARK}":
            return

        _id = payload.message_id
        t = next(filter(lambda x: _id in x[1]["msgs"], self.tunnel_store.items()), None)

        if t is None:
            return
        guild = t[0][0]
        tun = t[1]["tun"]
        if payload.user_id in [x.id for x in tun.members]:
            await set_contextual_locales_from_guild(self.bot, guild)
            await tun.react_close(
                uid=payload.user_id, message=_("{closer} has closed the correspondence")
            )
            self.tunnel_store.pop(t[0], None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        to_remove = []

        for k, v in self.tunnel_store.items():
            guild, ticket_number = k
            if await self.bot.cog_disabled_in_guild(self, guild):
                to_remove.append(k)
                continue

            await set_contextual_locales_from_guild(self.bot, guild)
            topic = _("Re: ticket# {ticket_number} in {guild.name}").format(
                ticket_number=ticket_number, guild=guild
            )
            # Tunnels won't forward unintended messages, this is safe
            msgs = await v["tun"].communicate(message=message, topic=topic)
            if msgs:
                self.tunnel_store[k]["msgs"] = msgs

        for key in to_remove:
            if tun := self.tunnel_store.pop(key, None):
                guild, ticket = key
                await set_contextual_locales_from_guild(self.bot, guild)
                await tun["tun"].close_because_disabled(
                    _(
                        "Correspondence about ticket# {ticket_number} in "
                        "{guild.name} has been ended due "
                        "to reports being disabled in that server."
                    ).format(ticket_number=ticket, guild=guild)
                )

