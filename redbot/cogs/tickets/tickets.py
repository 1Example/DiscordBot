import asyncio
import datetime
import typing

import discord

from AAA3A_utils import Cog, Loop, Menu, Settings
from redbot.core import Config, app_commands, commands, modlog
from redbot.core.app_commands import checks as app_checks
from redbot.core.bot import Red
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.chat_formatting import humanize_list, pagify

from .converters import Emoji, ForumTagConverter, ModalConverter
from .dashboard_integration import DashboardIntegration
from .types import Ticket, get_non_animated_asset
from .views import (
    ClosedTicketControls,
    CreateTicketView,
    OwnerCloseConfirmation,
    TicketView,
)

# Credits:
# General repo credits.

_: Translator = Translator("Tickets", __file__)

DANK_MEMER_BOT_ID: int = 270904126974590976


def support_predicate(ignore_owner: bool = False):
    """May this person act on the ticket in this channel?

    Takes a Context or an Interaction, since the same rule guards the
    application commands and the buttons on a ticket.
    """

    async def predicate(ctx: commands.Context | discord.Interaction) -> bool:
        bot = ctx.client if isinstance(ctx, discord.Interaction) else ctx.bot
        author = ctx.user if isinstance(ctx, discord.Interaction) else ctx.author
        if (
            not ctx.channel.permissions_for(author).view_channel
            or not ctx.channel.permissions_for(ctx.guild.me).send_messages
        ) and author.id not in bot.owner_ids:
            return False
        cog = bot.get_cog("Tickets")
        if (
            ticket := (ctx.data if isinstance(ctx, discord.Interaction) else ctx.kwargs).get(
                "ticket",
            )
        ) is not None or (
            ticket := discord.utils.get(
                cog.tickets.get(ctx.guild.id, {}).values(),
                channel=ctx.channel,
            )
        ) is not None:
            profile = ticket.profile
        else:
            profile = "main"
        return (
            (ticket is not None and ticket.owner == author and not ignore_owner)
            or await bot.is_admin(author)
            or author.guild_permissions.manage_guild
            or any(
                author.get_role(role_id) is not None
                for role_id in await cog.config.guild(ctx.guild).profiles.get_raw(
                    profile,
                    "support_roles",
                    default=[],
                )
            )
        )

    return predicate

def support_any_profile_predicate():
    """As above, but a support role on any profile is enough."""

    async def predicate(ctx: commands.Context | discord.Interaction) -> bool:
        bot = ctx.client if isinstance(ctx, discord.Interaction) else ctx.bot
        author = ctx.user if isinstance(ctx, discord.Interaction) else ctx.author
        cog = bot.get_cog("Tickets")
        return await cog.support_predicate.__func__()(ctx) or any(
            author.get_role(role_id) is not None
            for data in (await cog.config.guild(ctx.guild).profiles()).values()
            for role_id in data["support_roles"]
        )

    return predicate

@cog_i18n(_)
class Tickets(DashboardIntegration, Cog):
    """Configure and manage a tickets system for your server!"""

    def __init__(self, bot: Red) -> None:
        super().__init__(bot=bot)

        self.config: Config = Config.get_conf(
            self,
            identifier=205192943327321000143939875896557571750,
            force_registration=True,
        )
        guild_settings = {
            "tickets": {},
            "last_id": 0,
            "profiles": {},
            "default_profile_settings": {
                "enabled": False,
                "max_open_tickets_by_member": 5,
                "creating_modal": None,
                "channel_name": None,
                "welcome_message": "Welcome {owner_mention}! 👋",
                "custom_message": "Don't ping staff members, they will answer you as soon as possible.",
                "close_reopen_reason_modal": True,
                "create_modlog_case": False,
                "transcripts": True,
                "always_include_item_label": False,
                "disable_default_open_modal": False,
                # Roles.
                "support_roles": [],
                "ping_roles": [],
                "speak_roles": [],
                "view_roles": [],
                "whitelist_roles": [],
                "blacklist_roles": [],
                "ticket_role": None,
                # Channels.
                "forum_channel": None,
                "forum_tags": [],
                "category_open": None,
                "category_closed": None,
                "logs_channel": None,
                # Checks.
                "owner_close_confirmation": True,
                "owner_can_close": True,
                "owner_can_reopen": True,
                "owner_can_add_members": False,
                "owner_can_remove_members": False,
                "close_on_leave": True,
                "close_after_dank_payout": False,
                "auto_delete_on_close": None,
                # Emojis.
                "emojis": {
                    "close": "✖️",
                    "reopen": "👐",
                    "claim": "👥",
                    "unclaim": "👤",
                    "lock": "🔒",
                    "unlock": "🔓",
                    "transcript": "📜",
                    "delete": "🗑️",
                    "approve_appeal": "🛡️",
                },
                # Appeal feature.
                "appeals": {
                    "enabled": False,
                    "guild_id": None,
                    "invite_code": None,
                },
            },
            "buttons_dropdowns": {},
        }
        guild_settings["profiles"]["main"] = guild_settings["default_profile_settings"]
        self.config.register_guild(**guild_settings)
        self.config.register_member(
            tickets_number=0,
            closed_tickets_number=0,
        )

        self.tickets: dict[int, dict[int, Tickets]] = {}

        _settings: dict[str, dict[str, typing.Any]] = {
            "enabled": {
                "converter": bool,
                "description": "Whether the profile is enabled or not.",
            },
            "creating_modal": {
                "converter": ModalConverter,
                "description": "Whether a modal will be sent to the ticket owner when they create a ticket.\n\n**Example:**\n```\n[p]settickets creatingmodal <profile>\n- label: What is the problem?\n  style: 2 #  short = 1, paragraph = 2\n  required: True\n  default: None\n  placeholder: None\n  min_length: None\n  max_length: None\n```",
                "no_slash": True,
            },
            "max_open_tickets_by_member": {
                "converter": commands.Range[int, 1, 50],
                "description": "Maximum number of open tickets a member can have.",
            },
            "channel_name": {
                "converter": commands.Range[str, 1, 500],
                "description": "Name of the channel where the tickets will be created, reduced to 100 characters. You can use the following placeholders: `{id}`, `{emoji}`, `{owner_display_name}`, `{owner_name}`, `{owner_mention}`, `{owner_id}`, `{guild_name}` and `{guild_id}`.",
                "no_slash": True,
            },
            "welcome_message": {
                "converter": commands.Range[str, 1, 1000],
                "description": "Welcome message that will be sent when a ticket is created. You can use the following placeholders: `{id}`, `{emoji}`, `{owner_display_name}`, `{owner_name}`, `{owner_mention}`, `{owner_id}`, `{guild_name}` and `{guild_id}`.",
                "no_slash": True,
            },
            "custom_message": {
                "converter": commands.Range[str, 1, 3000],
                "description": "Message sent when a ticket opens. Supports placeholders such as {id} and {owner_mention}.",
            },
            "close_reopen_reason_modal": {
                "converter": bool,
                "description": "Whether a modal will be sent to the ticket owner when they close or reopen a ticket for asking a reason.",
                "no_slash": True,
            },
            "create_modlog_case": {
                "converter": bool,
                "description": "Whether a modlog's case will be created when a ticket is created.",
                "no_slash": True,
            },
            "transcripts": {
                "converter": bool,
                "description": "Whether a transcript will be created when a ticket is deleted.",
                "no_slash": True,
            },
            "always_include_item_label": {
                "converter": bool,
                "description": "Whether the item label will always be included in the embeds.",
                "no_slash": True,
            },
            "disable_default_open_modal": {
                "converter": bool,
                "description": "Whether the default open modal will be disabled.",
                "no_slash": True,
            },
            # Roles.
            "support_roles": {
                "converter": commands.Greedy[discord.Role],
                "description": "Roles that can support tickets.",
            },
            "ping_roles": {
                "converter": commands.Greedy[discord.Role],
                "description": "Roles that will be pinged when a ticket is created.",
            },
            "speak_roles": {
                "converter": commands.Greedy[discord.Role],
                "description": "Roles that can speak in the ticket channel.",
                "no_slash": True,
            },
            "view_roles": {
                "converter": commands.Greedy[discord.Role],
                "description": "Roles that can view tickets.",
            },
            "whitelist_roles": {
                "converter": commands.Greedy[discord.Role],
                "description": "Roles that can create tickets.",
            },
            "blacklist_roles": {
                "converter": commands.Greedy[discord.Role],
                "description": "Roles that can't create tickets.",
            },
            "ticket_role": {
                "converter": discord.Role,
                "description": "Role that will be added to the ticket owner for the duration of the ticket.",
                "no_slash": True,
            },
            # Channels.
            "forum_channel": {
                "converter": typing.Union[discord.ForumChannel, discord.TextChannel],
                "description": "Forum/text channel where the tickets will be created as threads.",
            },
            "forum_tags": {
                "converter": commands.Greedy[ForumTagConverter],
                "description": "Tags that will be added to the threads in the forum channel.",
                "no_slash": True,
            },
            "category_open": {
                "converter": discord.CategoryChannel,
                "description": "Category where the open tickets will be created.",
            },
            "category_closed": {
                "converter": discord.CategoryChannel,
                "description": "Category where the closed tickets will be created.",
            },
            "logs_channel": {
                "converter": typing.Union[
                    discord.TextChannel,
                    discord.VoiceChannel,
                    discord.Thread,
                ],
                "description": "Channel where the logs will be sent.",
            },
            # Checks.
            "owner_close_confirmation": {
                "converter": bool,
                "description": "Whether the ticket owner get a message to confirm the closing of the ticket.",
                "no_slash": True,
            },
            "owner_can_close": {
                "converter": bool,
                "description": "Whether the ticket owner can close the ticket.",
                "no_slash": True,
            },
            "owner_can_reopen": {
                "converter": bool,
                "description": "Whether the ticket owner can reopen the ticket.",
                "no_slash": True,
            },
            "owner_can_add_members": {
                "converter": bool,
                "description": "Whether the ticket owner can add members to the ticket.",
                "no_slash": True,
            },
            "owner_can_remove_members": {
                "converter": bool,
                "description": "Whether the ticket owner can remove members from the ticket.",
                "no_slash": True,
            },
            "close_on_leave": {
                "converter": bool,
                "description": "Whether the ticket will be closed when the owner leaves the server.",
                "no_slash": True,
            },
            "close_after_dank_payout": {
                "converter": bool,
                "description": "Whether the ticket will be closed after a Dank Memer payout.",
                "no_slash": True,
            },
            "auto_delete_on_close": {
                "converter": commands.Range[int, 0, 30 * 24],
                "description": "Time in hours before the ticket is deleted after being closed. Set to 0 for an immediate deletion.",
                "no_slash": True,
            },
            # Emojis.
            "emoji_close": {
                "path": ["emojis", "close"],
                "converter": Emoji,
                "description": "Emoji of the `Close` buttons.",
                "no_slash": True,
            },
            "emoji_reopen": {
                "path": ["emojis", "reopen"],
                "converter": Emoji,
                "description": "Emoji of the `Reopen` buttons.",
                "no_slash": True,
            },
            "emoji_claim": {
                "path": ["emojis", "claim"],
                "converter": Emoji,
                "description": "Emoji of the `Claim` button.",
                "no_slash": True,
            },
            "emoji_unclaim": {
                "path": ["emojis", "unclaim"],
                "converter": Emoji,
                "description": "Emoji of the `Unclaim` button.",
                "no_slash": True,
            },
            "emoji_lock": {
                "path": ["emojis", "lock"],
                "converter": Emoji,
                "description": "Emoji of the `Lock` button.",
                "no_slash": True,
            },
            "emoji_unlock": {
                "path": ["emojis", "unlock"],
                "converter": Emoji,
                "description": "Emoji of the `Unlock` button.",
                "no_slash": True,
            },
            "emoji_transcript": {
                "path": ["emojis", "transcript"],
                "converter": Emoji,
                "description": "Emoji of the `Transcript` button.",
                "no_slash": True,
            },
            "emoji_delete": {
                "path": ["emojis", "delete"],
                "converter": Emoji,
                "description": "Emoji of the `Delete` button.",
                "no_slash": True,
            },
            "emoji_approve_appeal": {
                "path": ["emojis", "approve_appeal"],
                "converter": Emoji,
                "description": "Emoji of the `Approve Appeal` button.",
                "no_slash": True,
            },
        }
        self.settings: Settings = Settings(
            bot=self.bot,
            cog=self,
            config=self.config,
            group=self.config.GUILD,
            settings=_settings,
            global_path=["profiles"],
            use_profiles_system=True,
            can_edit=True,
            commands_group=self.settickets,
        )

    async def cog_load(self) -> None:
        await super().cog_load()
        await modlog.register_casetypes(
            [
                {
                    "name": "ticket_created",
                    "default_setting": True,
                    "image": "❓",
                    "case_str": "Ticket Created",
                },
                {
                    "name": "ticket_claimed",
                    "default_setting": True,
                    "image": "👥",
                    "case_str": "Ticket Claimed",
                },
                {
                    "name": "ticket_unclaimed",
                    "default_setting": True,
                    "image": "👤",
                    "case_str": "Ticket Unclaimed",
                },
                {
                    "name": "ticket_closed",
                    "default_setting": True,
                    "image": "❌",
                    "case_str": "Ticket Closed",
                },
                {
                    "name": "ticket_reopened",
                    "default_setting": True,
                    "image": "👐",
                    "case_str": "Ticket Reopened",
                },
                {
                    "name": "ticket_locked",
                    "default_setting": True,
                    "image": "🔒",
                    "case_str": "Ticket Locked",
                },
                {
                    "name": "ticket_unlocked",
                    "default_setting": True,
                    "image": "🔓",
                    "case_str": "Ticket Unlocked",
                },
                {
                    "name": "ticket_member_added",
                    "default_setting": True,
                    "image": "➕",
                    "case_str": "Member Added to Ticket",
                },
                {
                    "name": "ticket_member_removed",
                    "default_setting": True,
                    "image": "➖",
                    "case_str": "Member Removed from Ticket",
                },
            ],
        )
        await self.settings.add_commands()
        asyncio.create_task(self.load_tickets())

    async def cog_unload(self) -> None:
        await super().cog_unload()

    async def load_tickets(self) -> None:
        await self.bot.wait_until_red_ready()
        for guild_id, guild_data in (await self.config.all_guilds()).items():
            for ticket_id, ticket_data in guild_data["tickets"].items():
                ticket = Ticket(bot=self.bot, cog=self, **ticket_data)
                self.tickets.setdefault(guild_id, {})[int(ticket_id)] = ticket
                view: TicketView = TicketView(cog=self, ticket=ticket)
                view._message = ticket.message
                if ticket.guild is not None:
                    await view._update()
                else:
                    view.members.custom_id = f"Tickets_#{ticket.id}_members"
                    view.lock.custom_id = f"Tickets_#{ticket.id}_lock"
                    view.claim.custom_id = f"Tickets_#{ticket.id}_claim"
                    view.close.custom_id = f"Tickets_#{ticket.id}_close"
                    view.approve_appeal.custom_id = f"Tickets_#{ticket.id}_approve_appeal"
                self.bot.add_view(view, message_id=ticket.message_id)
                if ticket.message is not None:
                    self.views[ticket.message] = view
            for message, components in guild_data["buttons_dropdowns"].items():
                channel_id, message_id = (int(x) for x in str(message).split("-"))
                view: CreateTicketView = CreateTicketView(cog=self, components=components)
                self.bot.add_view(view, message_id=message_id)
                if (channel := self.bot.get_channel(channel_id)) is not None:
                    self.views[discord.PartialMessage(channel=channel, id=message_id)] = view
        view: OwnerCloseConfirmation = OwnerCloseConfirmation(cog=self)
        self.bot.add_view(view)
        self.views["OwnerCloseConfirmation"] = view
        view: ClosedTicketControls = ClosedTicketControls(cog=self)
        self.bot.add_view(view)
        self.views["ClosedTicketControls"] = view
        self.loops.append(
            Loop(
                cog=self,
                name="Check Tickets",
                function=self.check_tickets,
                minutes=1,
            ),
        )

    async def check_tickets(self) -> None:
        for guild_id, tickets in self.tickets.copy().items():
            profiles = await self.config.guild_from_id(guild_id).profiles()
            for ticket in tickets.copy().values():
                if ticket.guild is None:
                    try:
                        await self.bot.fetch_guild(guild_id)
                    except discord.NotFound:
                        await ticket.delete()
                    continue
                if ticket.channel is None:
                    try:
                        await ticket.guild.fetch_channel(ticket.channel_id)
                    except discord.NotFound:
                        await ticket.delete()
                    continue
                if ticket.profile not in profiles:
                    await ticket.delete()
                    continue
                config = profiles[ticket.profile]
                if not ticket.is_closed and ticket.owner is None and config["close_on_leave"]:
                    try:
                        await ticket.guild.fetch_member(ticket.owner_id)
                    except discord.NotFound:
                        await ticket.close()
                if (
                    ticket.is_closed
                    and config["auto_delete_on_close"] is not None
                    and datetime.datetime.now(tz=datetime.timezone.utc) - ticket.closed_at
                    > datetime.timedelta(hours=config["auto_delete_on_close"])
                ):
                    await ticket.delete_channel(None)  # That's a setting, so no deleter.

    @commands.Cog.listener("on_message_edit")
    async def close_after_dank_payout(
        self, before: discord.Message, after: discord.Message,
    ) -> None:
        message = after
        if (
            message.guild is None
            or not message.author.bot
            or message.author.id != DANK_MEMER_BOT_ID
            or message.interaction_metadata is None
            or message.content
            or message.embeds
            or not message.components
            or not isinstance(message.components[0], discord.components.Container)
            or not isinstance(message.components[0].children[0], discord.components.TextDisplay)
        ):
            return
        description = message.components[0].children[0].content
        if not description.startswith("Successfully paid ") or not description.endswith(
            " from the server's pool!",
        ):
            return
        if (
            ticket := discord.utils.get(
                self.tickets.get(message.guild.id, {}).values(),
                channel_id=message.channel.id,
            )
        ) is None:
            return
        if ticket.is_closed:
            return
        if not await self.config.guild(message.guild).profiles.get_raw(
            ticket.profile,
            "close_after_dank_payout",
            default=False,
        ):
            return
        await ticket.close(message.interaction_metadata.user)

    def is_support(ignore_owner: bool = False):  # noqa: N805
        """Text-command flavour. `types.py` reads `.predicate` off this."""
        return commands.check(support_predicate(ignore_owner))

    def is_support_app(ignore_owner: bool = False):  # noqa: N805
        """Application-command flavour of the same rule."""
        return app_commands.check(support_predicate(ignore_owner))

    def is_support_any_profile_app():
        return app_commands.check(support_any_profile_predicate())

    async def send_ticket_log(self, ticket: Ticket) -> None:
        if (
            (guild := ticket.guild) is not None
            and (
                logs_channel_id := await self.config.guild(guild).profiles.get_raw(
                    ticket.profile,
                    "logs_channel",
                    default=None,
                )
            )
            is not None
            and (logs_channel := guild.get_channel_or_thread(logs_channel_id)) is not None
        ):
            try:
                view: discord.ui.View = discord.ui.View()
                view.add_item(
                    discord.ui.Button(
                        label=_("Jump to Ticket"),
                        style=discord.ButtonStyle.link,
                        url=ticket.message.jump_url,
                    ),
                )
                await logs_channel.send(
                    embed=await ticket.get_embed(for_logging=True),
                    view=view,
                )
            except discord.HTTPException as e:
                self.logger.error(
                    f"Error when sending event creation log for the event #{ticket.id} in the guild `{ticket.channel.name}` ({ticket.channel.id}) in the channel `{ticket.guild.name}` ({ticket.guild.id}).",
                    exc_info=e,
                )

    async def create_ticket(
        self,
        ctx_interaction: commands.Context | discord.Interaction,
        profile: str,
        owner: discord.Member,
        reason: str | None = None,
        skip_modal: bool = False,
        **kwargs,
    ) -> Ticket | None:
        """Open a ticket.

        `skip_modal` is for callers that already have everything the modal
        would ask for - the Reports cog opens a ticket from a report the member
        has already written out, and a DM context has nowhere to show a modal
        anyway. The created ticket is returned so those callers can reference
        it back to the member; None means the caller's modal timed out.
        """
        guild = ctx_interaction.guild

        config = await self.config.guild(guild).profiles.get_raw(profile)
        creating_modal = config["creating_modal"]
        if (
            creating_modal is None
            and reason is None
            and isinstance(ctx_interaction, discord.Interaction)
            and not config.get("disable_default_open_modal", False)
        ):
            final_modal = [
                {
                    "label": "Reason:",
                    "style": discord.TextStyle.paragraph.value,
                    "required": True,
                    "default": None,
                    "placeholder": "Enter the reason for creating the ticket...",
                    "min_length": 1,
                    "max_length": 1000,
                },
            ]
        else:
            final_modal = creating_modal
        if skip_modal:
            final_modal = None
        owner_answers = {}
        if final_modal is not None:
            modal: discord.ui.Modal = discord.ui.Modal(
                title=_("Create a Ticket"),
            )
            modal.on_submit = lambda interaction: interaction.response.defer()
            text_inputs = []
            for text_input_kwargs in final_modal:
                if not text_input_kwargs["label"].endswith((":", "?")):
                    text_input_kwargs["label"] += ":"
                text_input_kwargs["style"] = discord.TextStyle(text_input_kwargs["style"])
                text_input: discord.ui.TextInput = discord.ui.TextInput(**text_input_kwargs)
                modal.add_item(text_input)
                text_inputs.append(text_input)
            if isinstance(ctx_interaction, discord.Interaction):
                await ctx_interaction.response.send_modal(modal)
            else:
                view: discord.ui.View = discord.ui.View(timeout=180)

                async def interaction_check(interaction: discord.Interaction):
                    if interaction.user != owner and interaction.user != (
                        ctx_interaction.user
                        if isinstance(ctx_interaction, discord.Interaction)
                        else ctx_interaction.author
                    ):
                        await interaction.response.send_message(
                            _("Only the ticket owner can fill the answers."),
                            ephemeral=True,
                        )
                        return False
                    return True

                view.interaction_check = interaction_check

                async def on_timeout():
                    try:
                        await view._message.delete()
                    except discord.HTTPException:
                        pass

                view.on_timeout = on_timeout
                button: discord.ui.Button = discord.ui.Button(
                    emoji="❓",
                    label=_("Fill Answers"),
                )

                async def callback(interaction: discord.Interaction):
                    await view.on_timeout()
                    view.stop()
                    await interaction.response.send_modal(modal)

                button.callback = callback
                view.add_item(button)
                view._message = await ctx_interaction.reply(view=view)
                timeout = await view.wait()
                if timeout:
                    return
            timeout = await modal.wait()
            if timeout:
                return
            if creating_modal is None:
                reason = text_inputs[0].value.strip() or None
            else:
                owner_answers = {
                    text_input.label: text_input.value.strip()
                    for text_input in text_inputs
                    if text_input.value.strip()
                }
        elif isinstance(ctx_interaction, discord.Interaction):
            await ctx_interaction.response.defer(ephemeral=True, thinking=True)

        id = await self.config.guild(guild).last_id() + 1
        ticket: Ticket = Ticket(
            bot=self.bot,
            cog=self,
            guild_id=guild.id,
            id=id,
            owner_id=owner.id,
            profile=profile,
            **kwargs,
            reason=reason,
            owner_answers=owner_answers,
        )
        try:
            await ticket.create()
        except RuntimeError as e:
            raise commands.UserFeedbackCheckFailure(str(e))
        await self.config.guild(guild).last_id.set(id)

        if isinstance(ctx_interaction, discord.Interaction):
            await ctx_interaction.followup.send(
                _(
                    "❓ Your ticket has been created! Please wait for a staff member to assist you in {channel.mention}.",
                ).format(channel=ticket.channel),
                ephemeral=True,
            )
        return ticket

    # ---------------------------------------------------------------------
    # /ticket
    # ---------------------------------------------------------------------

    ticket = app_commands.Group(
        name="ticket",
        description="Open a support ticket, and manage the ones you can see.",
        guild_only=True,
        extras={"red_force_enable": True},
    )

    async def _profile_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> typing.List[app_commands.Choice]:
        profiles = await self.config.guild(interaction.guild).profiles()
        return [
            app_commands.Choice(name=name, value=name)
            for name in sorted(profiles)
            if current.lower() in name.lower()
        ][:25]

    async def _ticket_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> typing.List[app_commands.Choice]:
        """Tickets this person can see, newest first."""
        choices = []
        for number, ticket in sorted(
            (self.tickets.get(interaction.guild.id) or {}).items(), reverse=True
        ):
            channel = ticket.channel
            if channel is not None and not channel.permissions_for(
                interaction.user
            ).view_channel:
                continue
            owner = ticket.owner
            label = f"#{number} - {getattr(owner, 'display_name', 'unknown')}"
            if ticket.is_closed:
                label += " (closed)"
            if current and current.lstrip("#").lower() not in label.lower():
                continue
            choices.append(app_commands.Choice(name=label[:100], value=number))
            if len(choices) == 25:
                break
        return choices

    async def _resolve_profile(self, guild: discord.Guild, name: typing.Optional[str]) -> str:
        """A profile name, checked the way the text converter used to."""
        name = (name or "main").strip()
        profiles = await self.config.guild(guild).profiles()
        if name not in profiles:
            raise commands.UserFeedbackCheckFailure(_("This profile doesn't exist."))
        return name

    async def _resolve_ticket(
        self, interaction: discord.Interaction, number: typing.Optional[int]
    ) -> Ticket:
        """A ticket by number, or the one this channel belongs to.

        The text converter also took a channel or message ID; the autocomplete
        offers numbers, and leaving it empty picks up the current channel,
        which is how these were used in practice.
        """
        guild_tickets = self.tickets.get(interaction.guild.id, {})
        if number is None:
            ticket = discord.utils.get(guild_tickets.values(), channel=interaction.channel)
        else:
            ticket = guild_tickets.get(number) or discord.utils.get(
                guild_tickets.values(), channel_id=number
            ) or discord.utils.get(guild_tickets.values(), message_id=number)
        if ticket is None:
            raise commands.UserFeedbackCheckFailure(_("No ticket found."))
        if (
            ticket.channel is not None
            and not ticket.channel.permissions_for(interaction.user).view_channel
            and interaction.user.id not in self.bot.owner_ids
        ):
            raise commands.UserFeedbackCheckFailure(
                _("You don't have permission to view the channel of this ticket."),
            )
        return ticket

    @ticket.command(name="create", description="Open a ticket.")
    @app_commands.describe(
        profile="Which kind of ticket. Defaults to the main one.",
        reason="What it is about.",
    )
    @app_commands.autocomplete(profile=_profile_autocomplete)
    async def create(
        self,
        interaction: discord.Interaction,
        profile: str = "main",
        reason: app_commands.Range[str, 1, 1000] = None,
    ) -> None:
        """Open a ticket."""
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        profile = await self._resolve_profile(interaction.guild, profile)
        await self.create_ticket(ctx, profile, ctx.author, reason=reason)

    @ticket.command(name="createfor", description="Open a ticket on someone's behalf.")
    @app_checks.admin_or_permissions(manage_guild=True)
    @app_commands.describe(
        owner="Who the ticket is for.",
        profile="Which kind of ticket. Defaults to the main one.",
        reason="What it is about.",
    )
    @app_commands.autocomplete(profile=_profile_autocomplete)
    async def createfor(
        self,
        interaction: discord.Interaction,
        owner: discord.Member,
        profile: str = "main",
        reason: app_commands.Range[str, 1, 1000] = None,
    ) -> None:
        """Open a ticket on someone's behalf."""
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        profile = await self._resolve_profile(interaction.guild, profile)
        await self.create_ticket(ctx, profile, owner, reason=reason)

    @ticket.command(name="settings", description="Show how a ticket profile is set up.")
    @app_checks.bot_has_permissions(embed_links=True)
    @is_support_app(ignore_owner=True)
    @app_commands.describe(profile="Which profile. Leave empty to list them all.")
    @app_commands.autocomplete(profile=_profile_autocomplete)
    async def settings(
        self, interaction: discord.Interaction, profile: str = None
    ) -> None:
        """Show how a ticket profile is set up."""
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        profiles = await self.config.guild(ctx.guild).profiles()
        if profile is None:
            embed: discord.Embed = discord.Embed(
                title=_("Tickets — Settings — Profiles"),
                description="\n".join([f"- `{profile}`" for profile in profiles]),
                color=await ctx.embed_color(),
            )
            await ctx.send(embed=embed)
            return
        if profile not in profiles:
            raise commands.UserFeedbackCheckFailure(_("This profile doesn't exist."))
        config = profiles[profile]
        embeds = [
            discord.Embed(
                title=_("Tickets — Settings — Profile `{profile}`").format(profile=profile),
                color=await ctx.embed_color(),
            ),
        ]
        forum_channel = (
            ctx.guild.get_channel_or_thread(config.get("forum_channel"))
            if config.get("forum_channel")
            else None
        )
        embeds.append(
            discord.Embed(
                title=_("Settings:"),
                description=_(
                    "- Enabled: {enabled}\n"
                    "- Max Open Tickets By Member: {max_open_tickets_by_member}\n"
                    "- Creating Modal: {creating_modal}\n"
                    "- Channel Name: {channel_name}\n"
                    "- Welcome Message: {welcome_message}\n"
                    "- Custom Message: {custom_message}\n"
                    "- Close/Reopen Reason Modal: {close_reopen_reason_modal}\n"
                    "- Create Modlog Case: {create_modlog_case}\n"
                    "- Transcripts: {transcripts}\n"
                    "- Always Include Item Label: {always_include_item_label}\n"
                    "- Disable Default Open Modal: {disable_default_open_modal}\n\n"
                    "- Support Roles: {support_roles}\n"
                    "- Ping Roles: {ping_roles}\n"
                    "- Speak Roles: {speak_roles}\n"
                    "- View Roles: {view_roles}\n"
                    "- Whitelist Roles: {whitelist_roles}\n"
                    "- Blacklist Roles: {blacklist_roles}\n\n"
                    "- Forum Channel: {forum_channel}\n"
                    "- Forum Tags: {forum_tags}\n"
                    "- Category Open: {category_open}\n"
                    "- Category Closed: {category_closed}\n"
                    "- Logs Channel: {logs_channel}\n\n"
                    "- Owner Close Confirmation: {owner_close_confirmation}\n"
                    "- Owner Can Close: {owner_can_close}\n"
                    "- Owner Can Reopen: {owner_can_reopen}\n"
                    "- Owner Can Add Members: {owner_can_add_members}\n"
                    "- Owner Can Remove Members: {owner_can_remove_members}\n"
                    "- Close On Leave: {close_on_leave}\n"
                    "- Auto Delete On Close: {auto_delete_on_close}\n\n"
                    "- Emoji Claim: {emoji_claim}\n"
                    "- Emoji Unclaim: {emoji_unclaim}\n"
                    "- Emoji Close: {emoji_close}\n"
                    "- Emoji Reopen: {emoji_reopen}\n"
                    "- Emoji Lock: {emoji_lock}\n"
                    "- Emoji Unlock: {emoji_unlock}\n"
                    "- Emoji Transcript: {emoji_transcript}\n"
                    "- Emoji Delete: {emoji_delete}",
                ).format(
                    enabled=config["enabled"],
                    max_open_tickets_by_member=config["max_open_tickets_by_member"],
                    creating_modal=_("Set.") if config["creating_modal"] is not None else "...",
                    channel_name=config["channel_name"] or "...",
                    welcome_message=config.get("welcome_message") or "...",
                    custom_message=config["custom_message"] or "...",
                    close_reopen_reason_modal=config["close_reopen_reason_modal"],
                    create_modlog_case=config["create_modlog_case"],
                    transcripts=config["transcripts"],
                    always_include_item_label=config["always_include_item_label"],
                    disable_default_open_modal=config.get("disable_default_open_modal", False),
                    support_roles=humanize_list(
                        [
                            role.mention
                            for role_id in config["support_roles"]
                            if (role := ctx.guild.get_role(role_id)) is not None
                        ],
                    )
                    or "...",
                    ping_roles=humanize_list(
                        [
                            role.mention
                            for role_id in config["ping_roles"]
                            if (role := ctx.guild.get_role(role_id)) is not None
                        ],
                    )
                    or "...",
                    speak_roles=humanize_list(
                        [
                            role.mention
                            for role_id in config.get("speak_roles", [])
                            if (role := ctx.guild.get_role(role_id)) is not None
                        ],
                    )
                    or "...",
                    view_roles=humanize_list(
                        [
                            role.mention
                            for role_id in config["view_roles"]
                            if (role := ctx.guild.get_role(role_id)) is not None
                        ],
                    )
                    or "...",
                    whitelist_roles=humanize_list(
                        [
                            role.mention
                            for role_id in config["whitelist_roles"]
                            if (role := ctx.guild.get_role(role_id)) is not None
                        ],
                    )
                    or "...",
                    blacklist_roles=humanize_list(
                        [
                            role.mention
                            for role_id in config["blacklist_roles"]
                            if (role := ctx.guild.get_role(role_id)) is not None
                        ],
                    )
                    or "...",
                    forum_channel=forum_channel.mention if forum_channel is not None else "...",
                    forum_tags=(
                        (
                            humanize_list(
                                [
                                    f"`{f'{tag.emoji} ' if tag.emoji is not None else ''}{tag.name}`"
                                    for tag_id in config["forum_tags"]
                                    if (tag := forum_channel.get_tag(tag_id)) is not None
                                ],
                            )
                            or "..."
                        )
                        if forum_channel is not None
                        else "..."
                    ),
                    category_open=(
                        category.mention
                        if (category := ctx.guild.get_channel(config["category_open"])) is not None
                        else "..."
                    ),
                    category_closed=(
                        category.mention
                        if (category := ctx.guild.get_channel(config["category_closed"]))
                        is not None
                        else "..."
                    ),
                    logs_channel=(
                        channel.mention
                        if (channel := ctx.guild.get_channel_or_thread(config["logs_channel"]))
                        is not None
                        else "..."
                    ),
                    owner_close_confirmation=config["owner_close_confirmation"],
                    owner_can_close=config["owner_can_close"],
                    owner_can_reopen=config["owner_can_reopen"],
                    owner_can_add_members=config["owner_can_add_members"],
                    owner_can_remove_members=config["owner_can_remove_members"],
                    close_on_leave=config["close_on_leave"],
                    auto_delete_on_close=config["auto_delete_on_close"],
                    emoji_claim=config["emojis"]["claim"],
                    emoji_unclaim=config["emojis"]["unclaim"],
                    emoji_close=config["emojis"]["close"],
                    emoji_reopen=config["emojis"]["reopen"],
                    emoji_lock=config["emojis"]["lock"],
                    emoji_unlock=config["emojis"]["unlock"],
                    emoji_transcript=config["emojis"]["transcript"],
                    emoji_delete=config["emojis"]["delete"],
                ),
                color=await ctx.embed_color(),
            ),
        )
        await Menu(pages=[{"embeds": embeds}]).start(ctx)

    @ticket.command(name="show", description="Show a ticket's details.")
    @is_support_any_profile_app()
    @app_commands.describe(
        ticket="Which ticket. Defaults to the one this channel belongs to."
    )
    @app_commands.autocomplete(ticket=_ticket_autocomplete)
    async def show(
        self,
        interaction: discord.Interaction,
        ticket: typing.Optional[int] = None,
    ) -> None:
        """Show a ticket's details."""
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        ticket = await self._resolve_ticket(interaction, ticket)
        await ctx.send(embed=await ticket.get_embed(for_logging=True))

    @ticket.command(name="list", description="List this server's tickets.")
    @is_support_any_profile_app()
    @app_commands.describe(
        short="One line per ticket instead of a full card.",
        claimed="Only tickets you have claimed.",
        status="Which tickets to include.",
        owner="Only tickets opened by this member.",
    )
    async def list(
        self,
        interaction: discord.Interaction,
        short: bool = False,
        claimed: bool = False,
        status: typing.Literal[
            "all",
            "open",
            "claimed",
            "unclaimed",
            "closed",
            "appeal_approved",
        ] = "open",
        owner: typing.Optional[discord.Member] = None,
    ) -> None:
        """List this server's tickets."""
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        if not (tickets := self.tickets.get(ctx.guild.id)):
            raise commands.UserFeedbackCheckFailure(_("No ticket found."))
        tickets_to_display = []
        for ticket in tickets.values():
            if (
                not ticket.channel.permissions_for(ctx.author).view_channel
                and ctx.author.id not in ctx.bot.owner_ids
            ):
                continue
            if owner is not None and ticket.owner != owner:
                continue
            if claimed and (
                ticket.is_closed or not ticket.is_claimed or ctx.author != ticket.claimed_by
            ):
                continue
            if (
                status == "open"
                and ticket.is_closed
                or status == "claimed"
                and (ticket.is_closed or not ticket.is_claimed)
                or status == "unclaimed"
                and (ticket.is_closed or ticket.is_claimed)
                or status == "closed"
                and not ticket.is_closed
                or status == "appeal_approved"
                and not ticket.is_appeal_approved
            ):
                continue
            tickets_to_display.append(ticket)
        if not short:
            embeds = [await ticket.get_embed(for_logging=True) for ticket in tickets_to_display]
        else:
            embed: discord.Embed = discord.Embed(
                title=_("{len_tickets} Ticket{s}").format(
                    len_tickets=len(tickets_to_display),
                    s="s" if len(tickets_to_display) > 1 else "",
                ),
                color=await ctx.embed_color(),
                timestamp=ctx.message.created_at,
            )
            embed.set_footer(text=ctx.guild.name, icon_url=get_non_animated_asset(ctx.guild.icon))
            BREAK_LINE = "\n"
            description = "\n".join(
                [
                    f"• **#{ticket.id}** **{'CLOSED' if ticket.is_closed else ('CLAIMED' if ticket.is_claimed else 'OPEN')}** - {ticket.owner.mention if ticket.owner is not None else '[Unknown]'} - {ticket.channel.mention} - {ticket.reason.split(BREAK_LINE)[0][:30]}"
                    for ticket in tickets_to_display
                ],
            )
            pages = list(pagify(description, page_length=6000))
            embeds = []
            for page in pages:
                e = embed.copy()
                e.description = page
                embeds.append(e)
        await Menu(pages=embeds, page_start=-1).start(ctx)

    @ticket.command(name="close", description="Close a ticket.")
    @is_support_app()
    @app_commands.describe(
        ticket="Which ticket. Defaults to the one this channel belongs to."
    )
    @app_commands.autocomplete(ticket=_ticket_autocomplete)
    async def close(
        self,
        interaction: discord.Interaction,
        ticket: typing.Optional[int] = None,
    ) -> None:
        """Close a ticket."""
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        ticket = await self._resolve_ticket(interaction, ticket)
        try:
            await ticket.close(ctx.author)
        except RuntimeError as e:
            raise commands.UserFeedbackCheckFailure(str(e))

    @ticket.command(name="reopen", description="Reopen a closed ticket.")
    @is_support_app()
    @app_commands.describe(
        ticket="Which ticket. Defaults to the one this channel belongs to."
    )
    @app_commands.autocomplete(ticket=_ticket_autocomplete)
    async def reopen(
        self,
        interaction: discord.Interaction,
        ticket: typing.Optional[int] = None,
    ) -> None:
        """Reopen a closed ticket."""
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        ticket = await self._resolve_ticket(interaction, ticket)
        try:
            await ticket.reopen(ctx.author)
        except RuntimeError as e:
            raise commands.UserFeedbackCheckFailure(str(e))

    @ticket.command(name="claim", description="Claim a ticket as yours to handle.")
    @is_support_app(ignore_owner=True)
    @app_commands.describe(
        ticket="Which ticket. Defaults to the one this channel belongs to."
    )
    @app_commands.autocomplete(ticket=_ticket_autocomplete)
    async def claim(
        self,
        interaction: discord.Interaction,
        ticket: typing.Optional[int] = None,
    ) -> None:
        """Claim a ticket as yours to handle."""
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        ticket = await self._resolve_ticket(interaction, ticket)
        try:
            await ticket.claim(ctx.author)
        except RuntimeError as e:
            raise commands.UserFeedbackCheckFailure(str(e))

    @ticket.command(name="unclaim", description="Give up a ticket you claimed.")
    @is_support_app(ignore_owner=True)
    @app_commands.describe(
        ticket="Which ticket. Defaults to the one this channel belongs to."
    )
    @app_commands.autocomplete(ticket=_ticket_autocomplete)
    async def unclaim(
        self,
        interaction: discord.Interaction,
        ticket: typing.Optional[int] = None,
    ) -> None:
        """Give up a ticket you claimed."""
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        ticket = await self._resolve_ticket(interaction, ticket)
        try:
            await ticket.unclaim()
        except RuntimeError as e:
            raise commands.UserFeedbackCheckFailure(str(e))


    @ticket.command(name="unlock", description="Unlock a locked ticket.")
    @is_support_app(ignore_owner=True)
    @app_commands.describe(
        ticket="Which ticket. Defaults to the one this channel belongs to."
    )
    @app_commands.autocomplete(ticket=_ticket_autocomplete)
    async def unlock(
        self,
        interaction: discord.Interaction,
        ticket: typing.Optional[int] = None,
    ) -> None:
        """Unlock a locked ticket."""
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        ticket = await self._resolve_ticket(interaction, ticket)
        try:
            await ticket.unlock(ctx.author)
        except RuntimeError as e:
            raise commands.UserFeedbackCheckFailure(str(e))

    @ticket.command(name="approveappeal", description="Approve a ban appeal and lift the ban.")
    @is_support_app(ignore_owner=True)
    @app_commands.describe(
        ticket="Which ticket. Defaults to the one this channel belongs to."
    )
    @app_commands.autocomplete(ticket=_ticket_autocomplete)
    async def approveappeal(
        self,
        interaction: discord.Interaction,
        ticket: typing.Optional[int] = None,
    ) -> None:
        """Approve a ban appeal and lift the ban."""
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        ticket = await self._resolve_ticket(interaction, ticket)
        try:
            await ticket.approve_appeal(ctx.author)
        except RuntimeError as e:
            raise commands.UserFeedbackCheckFailure(str(e))

    @ticket.command(name="addmember", description="Give a member access to a ticket.")
    @is_support_app()
    @app_commands.describe(
        member="Who to add.",
        ticket="Which ticket. Defaults to the one this channel belongs to.",
    )
    @app_commands.autocomplete(ticket=_ticket_autocomplete)
    async def addmember(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        ticket: typing.Optional[int] = None,
    ) -> None:
        """Give a member access to a ticket."""
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        ticket = await self._resolve_ticket(interaction, ticket)
        if (
            ticket is None
            and (
                ticket := discord.utils.get(
                    self.tickets.get(ctx.guild.id, {}).values(),
                    channel=ctx.channel,
                )
            )
            is None
        ):
            raise commands.UserFeedbackCheckFailure(_("No ticket found."))
        try:
            await ticket.add_member(member, ctx.author)
        except RuntimeError as e:
            raise commands.UserFeedbackCheckFailure(str(e))

    @ticket.command(name="removemember", description="Take a member off a ticket.")
    @is_support_app()
    @app_commands.describe(
        member="Who to remove.",
        ticket="Which ticket. Defaults to the one this channel belongs to.",
    )
    @app_commands.autocomplete(ticket=_ticket_autocomplete)
    async def removemember(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        ticket: typing.Optional[int] = None,
    ) -> None:
        """Take a member off a ticket."""
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        ticket = await self._resolve_ticket(interaction, ticket)
        if (
            ticket is None
            and (
                ticket := discord.utils.get(
                    self.tickets.get(ctx.guild.id, {}).values(),
                    channel=ctx.channel,
                )
            )
            is None
        ):
            raise commands.UserFeedbackCheckFailure(_("No ticket found."))
        try:
            await ticket.remove_member(member, ctx.author)
        except RuntimeError as e:
            raise commands.UserFeedbackCheckFailure(str(e))

    @ticket.command(name="export", description="Export a ticket's transcript.")
    @is_support_app()
    @app_commands.describe(
        ticket="Which ticket. Defaults to the one this channel belongs to."
    )
    @app_commands.autocomplete(ticket=_ticket_autocomplete)
    async def export(
        self,
        interaction: discord.Interaction,
        ticket: typing.Optional[int] = None,
    ) -> None:
        """Export a ticket's transcript."""
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        ticket = await self._resolve_ticket(interaction, ticket)
        transcript = await ticket.export()
        message = await ctx.send(
            _("📜 Here is the transcript of this ticket!"),
            file=transcript,
        )
        view: discord.ui.View = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label=_("View Transcript"),
                style=discord.ButtonStyle.link,
                url=f"https://mahto.id/chat-exporter?url={message.attachments[0].url}",
            ),
        )
        await message.edit(view=view)


    @ticket.command(
        name="recover",
        description="Rebuild a ticket I lost track of, from its channel.",
    )
    @app_checks.admin_or_permissions(manage_guild=True)
    @app_commands.describe(channel="The ticket channel. Defaults to this one.")
    async def recover(
        self,
        interaction: discord.Interaction,
        channel: typing.Union[discord.TextChannel, discord.Thread] = None,
    ) -> None:
        """Rebuild a ticket I lost track of, from its channel."""
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        channel = channel or interaction.channel
        if (
            discord.utils.get(
                self.tickets.get(ctx.guild.id, {}).values(),
                channel=channel,
            )
            is not None
        ):
            raise commands.UserFeedbackCheckFailure(
                _("That channel is already linked to a ticket."),
            )
        if (
            first_message := await anext(
                (message async for message in channel.history(limit=1, oldest_first=True)),
                None,
            )
        ) is None:
            raise commands.UserFeedbackCheckFailure(_("No messages found in this channel."))
        if first_message.author != ctx.guild.me:
            raise commands.UserFeedbackCheckFailure(
                _("The first message in this channel must be sent by me."),
            )
        if not first_message.embeds:
            raise commands.UserFeedbackCheckFailure(
                _("The first message in this channel must contain an embed."),
            )
        embed = first_message.embeds[0]
        try:
            owner_id = int(embed.author.name.split(" (")[-1].split(")")[0])
        except (IndexError, ValueError):
            raise commands.UserFeedbackCheckFailure(
                _(
                    "The embed of the first message in this channel must have the author set to the ticket owner.",
                ),
            )
        profiles = await self.config.guild(ctx.guild).profiles()
        if (profile := embed.title.split("[")[1].split("]")[0]) not in profiles:
            raise commands.UserFeedbackCheckFailure(
                _("The profile of this ticket doesn't exist anymore."),
            )
        reason_field = discord.utils.get(embed.fields, name=_("Reason:"))
        ticket: Ticket = Ticket(
            bot=self.bot,
            cog=self,
            guild_id=ctx.guild.id,
            id=int(embed.title.split("#")[1].split(" ")[0]),
            owner_id=owner_id,
            channel_id=channel.id,
            message_id=first_message.id,
            profile=profile,
            reason=(reason_field.value[4:] if reason_field is not None else None),
            category_label=(
                first_message.embeds[1].title if len(first_message.embeds) == 3 else None
            ),
            owner_answers=(
                {field.name: field.value[4:] for field in first_message.embeds[1].fields}
                if len(first_message.embeds) == 3
                else {}
            ),
            opened_at_timestamp=int(embed.timestamp.timestamp()),
            is_claimed=embed.color == discord.Color.blue(),
            is_closed=embed.color == discord.Color.red(),
        )
        try:
            view: TicketView = self.views[first_message]
        except KeyError:
            view: TicketView = TicketView(cog=self, ticket=ticket)
        await view._update()
        try:
            await first_message.edit(
                embeds=await ticket.get_embeds(),
                view=view,
            )
            self.views[first_message] = view
        except discord.HTTPException:
            pass
        try:
            await channel.edit(
                name=await ticket.channel_name(
                    forum_channel=isinstance(ticket.channel, discord.Thread),
                ),
                overwrites=await ticket.get_channel_overwrites(),
                reason=_("Reverting channel name and overwrites for ticket recovery."),
            )
        except discord.HTTPException:
            pass
        await ticket.save()


