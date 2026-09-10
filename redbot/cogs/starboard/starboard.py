"""Pin the messages a server thinks are worth keeping.

TrustyJAID's Starboard, ported to this fork. The engine - the reaction
handling, the embed builder, the entry model - is his, unchanged. What changed
is the surface: every setting moved to the dashboard, the way every other cog
in this fork works, leaving three slash commands for the things a member
actually does in the channel.

Upstream had twenty prefix commands for the same settings.
"""
from __future__ import annotations

import asyncio
import typing as t

import discord
from red_commons.logging import getLogger

from redbot.core import Config, app_commands, commands
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.chat_formatting import humanize_list

from .dashboard_integration import DashboardIntegration
from .events import StarboardEvents
from .starboard_entry import FakePayload, StarboardEntry

_ = Translator("Starboard", __file__)
log = getLogger("red.trusty-cogs.Starboard")


@cog_i18n(_)
class Starboard(DashboardIntegration, StarboardEvents, commands.Cog):
    """Create a starboard to *pin* those special comments indefinitely."""

    __version__ = "2.6.0"
    __author__ = ["TrustyJAID"]

    starboard = app_commands.Group(
        name="starboard",
        description="Messages the server thought were worth keeping.",
        guild_only=True,
        extras={"red_force_enable": True},
    )

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        # The identifier upstream uses, so a server that ran TrustyJAID's cog
        # keeps its starboards.
        self.config = Config.get_conf(self, 356488795)
        self.config.register_global(purge_time=None)
        self.config.register_guild(starboards={})
        self.starboards: dict[int, dict[str, StarboardEntry]] = {}
        self.ready = asyncio.Event()
        self.cleanup_loop: t.Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        log.debug("Started building starboards cache from config.")
        for guild_id in await self.config.all_guilds():
            self.starboards[guild_id] = {}
            all_data = await self.config.guild_from_id(int(guild_id)).starboards()
            for name, data in all_data.items():
                try:
                    self.starboards[guild_id][name] = await StarboardEntry.from_json(
                        data, guild_id
                    )
                except Exception:
                    log.exception("Error converting starboard %s in %s", name, guild_id)
        self.cleanup_loop = asyncio.create_task(self.cleanup_old_messages())
        self.ready.set()
        log.debug("Done building starboards cache from config.")

    async def cog_unload(self) -> None:
        self.ready.clear()
        if self.cleanup_loop:
            self.cleanup_loop.cancel()

    def format_help_for_context(self, ctx: commands.Context) -> str:
        pre_processed = super().format_help_for_context(ctx)
        return f"{pre_processed}\n\nCog Version: {self.__version__}"

    # ---------------- shared helpers ----------------

    def guild_starboards(self, guild: discord.Guild) -> dict[str, StarboardEntry]:
        return self.starboards.get(guild.id, {})

    async def starboard_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice]:
        """The starboards in this server, for a name argument.

        Upstream made people remember the name and type it; the list is right
        here, so offer it.
        """
        if interaction.guild is None:
            return []
        current = (current or "").lower()
        return [
            app_commands.Choice(name=name, value=name)
            for name in sorted(self.guild_starboards(interaction.guild))
            if current in name.lower()
        ][:25]

    async def _resolve_starboard(
        self, ctx: commands.Context, name: t.Optional[str]
    ) -> t.Optional[StarboardEntry]:
        """The named starboard, or the only one when a server has just one."""
        boards = self.guild_starboards(ctx.guild)
        if not boards:
            await ctx.send(
                _("There are no starboards set up here. Add one on the dashboard."),
                ephemeral=True,
            )
            return None
        if name:
            starboard = boards.get(name.lower())
            if starboard is None:
                await ctx.send(
                    _("There is no starboard called {name}.").format(name=name),
                    ephemeral=True,
                )
                return None
            return starboard
        if len(boards) > 1:
            await ctx.send(
                _("This server has more than one starboard. Say which: {names}.").format(
                    names=humanize_list([f"`{n}`" for n in sorted(boards)])
                ),
                ephemeral=True,
            )
            return None
        return next(iter(boards.values()))

    @staticmethod
    async def _fetch_message(
        ctx: commands.Context, reference: str
    ) -> t.Optional[discord.Message]:
        """A message from an id, a channel-message pair, or a link."""
        reference = (reference or "").strip()
        channel_id = None
        message_id = None
        if "/channels/" in reference:
            parts = reference.rstrip("/").split("/")
            if len(parts) >= 2 and parts[-1].isdigit() and parts[-2].isdigit():
                channel_id, message_id = int(parts[-2]), int(parts[-1])
        elif "-" in reference:
            left, _sep, right = reference.partition("-")
            if left.isdigit() and right.isdigit():
                channel_id, message_id = int(left), int(right)
        elif reference.isdigit():
            message_id = int(reference)

        if message_id is None:
            await ctx.send(
                _("That is not a message id, a link, or a channel-message pair."),
                ephemeral=True,
            )
            return None

        channels = (
            [ctx.guild.get_channel_or_thread(channel_id)]
            if channel_id
            else [ctx.channel, *ctx.guild.text_channels]
        )
        for channel in channels:
            if channel is None or not isinstance(channel, discord.abc.Messageable):
                continue
            try:
                return await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue
        await ctx.send(_("I could not find that message."), ephemeral=True)
        return None

    async def _manual_star(
        self, interaction: discord.Interaction, message_ref: str, name: t.Optional[str], add: bool
    ) -> None:
        ctx = await commands.Context.from_interaction(interaction)
        if not self.ready.is_set():
            return await ctx.send(_("Still starting up, try again shortly."), ephemeral=True)
        starboard = await self._resolve_starboard(ctx, name)
        if starboard is None:
            return
        message = await self._fetch_message(ctx, message_ref)
        if message is None:
            return
        if message.guild is None or message.guild.id != ctx.guild.id:
            return await ctx.send(
                _("I cannot star messages from another server."), ephemeral=True
            )
        if not starboard.enabled:
            return await ctx.send(
                _("Starboard {name} is switched off.").format(name=starboard.name),
                ephemeral=True,
            )
        if not starboard.check_roles(ctx.author):
            return await ctx.send(
                _(
                    "One of your roles is blocked on {name}, or you do not have one"
                    " that is allowed."
                ).format(name=starboard.name),
                ephemeral=True,
            )
        if not starboard.check_channel(self.bot, message.channel):
            return await ctx.send(
                _(
                    "That message's channel is blocked, missing from the allow list,"
                    " or marked NSFW while {name} is not."
                ).format(name=starboard.name),
                ephemeral=True,
            )
        await self._update_stars(
            FakePayload(
                guild_id=ctx.guild.id,
                message_id=message.id,
                channel_id=message.channel.id,
                user_id=ctx.author.id,
                emoji=starboard.emoji,
                event_type="REACTION_ADD" if add else "REACTION_REMOVE",
            )
        )
        verb = _("Starred") if add else _("Unstarred")
        await ctx.send(
            _("{verb} that message on **{name}**.").format(verb=verb, name=starboard.name),
            ephemeral=True,
        )

    # ---------------- commands ----------------

    @starboard.command(name="star", description="Star a message yourself.")
    @app_commands.describe(
        message="Its id, a channel-message pair, or a link to it.",
        name="Which starboard, when the server has more than one.",
    )
    @app_commands.autocomplete(name=starboard_autocomplete)
    async def star(
        self, interaction: discord.Interaction, message: str, name: t.Optional[str] = None
    ) -> None:
        """Star a message without reacting to it."""
        await self._manual_star(interaction, message, name, add=True)

    @starboard.command(name="unstar", description="Take your star off a message.")
    @app_commands.describe(
        message="Its id, a channel-message pair, or a link to it.",
        name="Which starboard, when the server has more than one.",
    )
    @app_commands.autocomplete(name=starboard_autocomplete)
    async def unstar(
        self, interaction: discord.Interaction, message: str, name: t.Optional[str] = None
    ) -> None:
        """Remove your star from a message."""
        await self._manual_star(interaction, message, name, add=False)

    @starboard.command(name="info", description="Show the starboards set up here.")
    async def info(self, interaction: discord.Interaction) -> None:
        """List this server's starboards and how they are set up."""
        ctx = await commands.Context.from_interaction(interaction)
        boards = self.guild_starboards(ctx.guild)
        if not boards:
            return await ctx.send(
                _("There are no starboards set up here. Add one on the dashboard."),
                ephemeral=True,
            )
        embed = discord.Embed(
            title=_("Starboards in {guild}").format(guild=ctx.guild.name),
            colour=await self.bot.get_embed_colour(ctx.channel),
        )
        for name, board in sorted(boards.items()):
            channel = ctx.guild.get_channel(board.channel)
            lines = [
                _("Channel: {channel}").format(
                    channel=channel.mention if channel else _("missing")
                ),
                _("Emoji: {emoji}").format(emoji=str(board.emoji)),
                _("Stars needed: {threshold}").format(threshold=board.threshold),
                _("Enabled: {state}").format(
                    state=_("yes") if board.enabled else _("no")
                ),
                _("Self-starring: {state}").format(
                    state=_("allowed") if board.selfstar else _("not allowed")
                ),
                _("Messages starred: {count}").format(count=board.starred_messages),
            ]
            embed.add_field(name=name, value="\n".join(lines), inline=False)
        embed.set_footer(text=_("Everything else is on the dashboard."))
        await ctx.send(embed=embed, ephemeral=True)
