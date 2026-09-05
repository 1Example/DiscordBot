from __future__ import annotations

import logging
import typing as t
from datetime import datetime, timezone

import discord
from redbot.core import commands
from redbot.core.bot import Red

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    channel_options,
    dashboard_page,
    fake_context,
    form_reader,
    guild_member,
    is_staff,
    member_options,
    role_options,
)

log = logging.getLogger("red.tickets.dashboard")

# Profile settings grouped the way the page renders them. Each entry is
# (config key, label, help text).
TOGGLES = (
    ("enabled", "Profile enabled", "Members can open tickets with this profile."),
    (
        "close_reopen_reason_modal",
        "Ask for a reason",
        "Prompt for a reason when a ticket is closed or reopened.",
    ),
    ("create_modlog_case", "Create modlog cases", "File a case for each ticket action."),
    ("transcripts", "Save transcripts", "Attach a transcript to the log when closing."),
    (
        "always_include_item_label",
        "Include the item label",
        "Add the button or dropdown label to the ticket channel name.",
    ),
    (
        "disable_default_open_modal",
        "Skip the open modal",
        "Do not show the default modal when a ticket is opened.",
    ),
    (
        "owner_close_confirmation",
        "Confirm before the owner closes",
        "Ask the ticket owner to confirm when they close their own ticket.",
    ),
    ("owner_can_close", "Owner can close", "The ticket owner may close their ticket."),
    ("owner_can_reopen", "Owner can reopen", "The ticket owner may reopen their ticket."),
    (
        "owner_can_add_members",
        "Owner can add members",
        "The ticket owner may pull other members into the ticket.",
    ),
    (
        "owner_can_remove_members",
        "Owner can remove members",
        "The ticket owner may remove members from the ticket.",
    ),
    (
        "close_on_leave",
        "Close when the owner leaves",
        "Close the ticket automatically if its owner leaves the server.",
    ),
    (
        "close_after_dank_payout",
        "Close after a Dank Memer payout",
        "Close the ticket once a Dank Memer payout is confirmed in it.",
    ),
)

TEXTS = (
    (
        "channel_name",
        "Channel name",
        500,
        "Placeholders: {id}, {emoji}, {owner_display_name}, {owner_name}, "
        "{owner_mention}, {owner_id}, {guild_name}, {guild_id}.",
    ),
    ("welcome_message", "Welcome message", 1000, "Sent to the owner when the ticket opens."),
    ("custom_message", "Custom message", 3000, "Extra text posted in every new ticket."),
)

ROLE_LISTS = (
    ("support_roles", "Support roles", "Can manage every ticket on this profile."),
    ("ping_roles", "Ping roles", "Pinged when a ticket is opened."),
    ("speak_roles", "Speak roles", "Can talk in ticket channels."),
    ("view_roles", "View roles", "Can read ticket channels."),
    ("whitelist_roles", "Whitelist roles", "Only these roles may open tickets."),
    ("blacklist_roles", "Blacklist roles", "These roles may never open tickets."),
)

EMOJIS = (
    ("close", "Close"),
    ("reopen", "Reopen"),
    ("claim", "Claim"),
    ("unclaim", "Unclaim"),
    ("lock", "Lock"),
    ("unlock", "Unlock"),
    ("transcript", "Transcript"),
    ("delete", "Delete"),
    ("approve_appeal", "Approve appeal"),
)


class DashboardIntegration:
    """Ticket management and configuration from the dashboard.

    Covers ``[p]ticket`` (close, reopen, claim, unclaim, lock, unlock, add and
    remove members, approve appeals, export transcripts, delete) and
    ``[p]settickets`` (every profile setting, profile creation and removal, and
    the panel messages ``addbutton``/``adddropdownoption`` created).
    """

    bot: Red
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog: commands.Cog) -> None:
        log.info("Dashboard cog found, registering Tickets as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Manage tickets and configure ticket profiles.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_tickets_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        staff = await is_staff(self.bot, user, member, guild)
        support = staff or await self._tk_is_support(member, guild)

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            if not support:
                return {
                    "status": 1,
                    "error_title": "Forbidden",
                    "error_message": "Only ticket support staff can use this page.",
                }
            notifications = await self._tk_handle_post(member, guild, staff, kwargs)

        settings = await self.config.guild(guild).all()
        profiles = settings.get("profiles") or {}

        tickets = []
        for ticket in (self.tickets.get(guild.id) or {}).values():
            tickets.append(self._tk_row(guild, ticket))
        tickets.sort(key=lambda t_: (-t_["id"]))

        panels = []
        for key, components in (settings.get("buttons_dropdowns") or {}).items():
            channel_id, message_id = (int(x) for x in str(key).split("-"))
            channel = guild.get_channel(channel_id)
            if channel is None:
                continue
            panels.append(
                {
                    "key": key,
                    "channel": channel.name,
                    "link": f"https://discord.com/channels/{guild.id}/"
                    f"{channel_id}/{message_id}",
                    # A panel is {"buttons": {...}, "dropdown_options": {...}};
                    # iterating the outer dict listed those two containers
                    # rather than the items on them.
                    "items": [
                        {
                            "key": identifier,
                            "kind": kind,
                            "label": data.get("label")
                            or data.get("emoji")
                            or "(no label)",
                            "emoji": data.get("emoji") or "",
                            "profile": data.get("profile", "main"),
                            "style": self.BUTTON_STYLES.get(
                                str(data.get("style", "")), ""
                            ),
                            "description": data.get("description") or "",
                        }
                        for kind in ("buttons", "dropdown_options")
                        for identifier, data in (
                            (components.get(kind) or {}).items()
                            if isinstance(components, dict)
                            else []
                        )
                        if isinstance(data, dict)
                    ],
                }
            )

        profile_rows = []
        for name in sorted(profiles):
            profile_rows.append(self._tk_profile_row(guild, name, profiles[name]))

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": TICKETS_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_support": support,
                "is_admin": staff,
                "profiles": profile_rows,
                "profile_names": sorted(profiles),
                "tickets": tickets,
                "open_count": sum(1 for t_ in tickets if not t_["closed"]),
                "closed_count": sum(1 for t_ in tickets if t_["closed"]),
                "claimed_count": sum(1 for t_ in tickets if t_["claimed"]),
                "panels": panels,
                "button_styles": [
                    {"value": value, "label": label}
                    for value, label in self.BUTTON_STYLES.items()
                ],
                "channel_options": channel_options(guild),
                "member_options": member_options(guild, humans_only=True),
                "toggles": TOGGLES,
                "texts": TEXTS,
                "role_lists": ROLE_LISTS,
                "emoji_keys": EMOJIS,
            },
        }

    async def _tk_is_support(self, member: discord.Member, guild: discord.Guild) -> bool:
        """True when the member holds a support role on any profile."""
        profiles = await self.config.guild(guild).profiles()
        for data in profiles.values():
            for role_id in data.get("support_roles") or []:
                if member.get_role(role_id) is not None:
                    return True
        return False

    def _tk_row(self, guild: discord.Guild, ticket) -> dict:
        owner = guild.get_member(ticket.owner_id)
        claimer = guild.get_member(ticket.claimed_by_id or 0)
        channel = guild.get_channel_or_thread(ticket.channel_id or 0)

        def when(stamp) -> str:
            if not stamp:
                return ""
            return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime(
                "%d %b %Y, %H:%M"
            )

        return {
            "id": ticket.id,
            "profile": ticket.profile or "main",
            "owner": getattr(owner, "display_name", None) or f"ID {ticket.owner_id}",
            "owner_id": str(ticket.owner_id),
            "channel": getattr(channel, "name", ""),
            "channel_id": str(ticket.channel_id or ""),
            "link": f"https://discord.com/channels/{guild.id}/{ticket.channel_id}"
            if ticket.channel_id
            else "",
            "reason": ticket.reason or "",
            "category": ticket.category_label or "",
            "opened": when(ticket.opened_at_timestamp),
            "closed": bool(ticket.is_closed),
            "closed_at": when(ticket.closed_at_timestamp),
            "claimed": bool(ticket.is_claimed),
            "claimed_by": getattr(claimer, "display_name", "") or "",
            "locked": bool(ticket.is_locked),
            "appeal_approved": bool(ticket.appeal_approved),
            "members": [
                getattr(guild.get_member(i), "display_name", f"ID {i}")
                for i in (ticket.members_ids or [])
            ],
            "answers": dict(ticket.owner_answers or {}),
        }

    def _tk_profile_row(self, guild: discord.Guild, name: str, data: dict) -> dict:
        appeals = data.get("appeals") or {}
        return {
            "name": name,
            "enabled": bool(data.get("enabled")),
            "values": data,
            "max_open": data.get("max_open_tickets_by_member") or 5,
            "auto_delete": data.get("auto_delete_on_close"),
            "emojis": data.get("emojis") or {},
            "appeals_enabled": bool(appeals.get("enabled")),
            "appeals_guild_id": appeals.get("guild_id") or "",
            "appeals_invite": appeals.get("invite_code") or "",
            "toggle_values": {key: bool(data.get(key)) for key, _l, _h in TOGGLES},
            "text_values": {key: data.get(key) or "" for key, _l, _m, _h in TEXTS},
            "role_options": {
                key: role_options(guild, selected_many=data.get(key) or [])
                for key, _l, _h in ROLE_LISTS
            },
            "ticket_role_options": role_options(guild, selected=data.get("ticket_role")),
            "category_open_options": channel_options(
                guild, kinds=("category",), selected=data.get("category_open")
            ),
            "category_closed_options": channel_options(
                guild, kinds=("category",), selected=data.get("category_closed")
            ),
            "logs_options": channel_options(
                guild, selected=data.get("logs_channel"), require_send=True
            ),
            "forum_options": channel_options(
                guild, kinds=("forum", "text"), selected=data.get("forum_channel")
            ),
        }

    async def _tk_handle_post(
        self, member: discord.Member, guild: discord.Guild, staff: bool, kwargs: dict
    ) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

        try:
            if action == "create_for":
                return await self._tk_create_for(member, guild, field)

            if action.startswith("ticket_"):
                return await self._tk_ticket_action(action, member, guild, field)

            # Everything below edits the configuration.
            if not staff:
                return [
                    {
                        "message": "Only server administrators can change ticket settings.",
                        "category": "danger",
                    }
                ]

            if action == "profile_add":
                name = (field("profile_name") or "").strip().lower()
                if not name:
                    return [{"message": "Enter a profile name.", "category": "warning"}]
                profiles = await self.config.guild(guild).profiles()
                if name in profiles:
                    return [
                        {"message": f"A profile named {name} already exists.",
                         "category": "warning"}
                    ]
                # The cog registers the template under `default_profile_settings`,
                # so a new profile always starts from the same shape the commands use.
                defaults = await self.config.guild(guild).default_profile_settings()
                await self.config.guild(guild).profiles.set_raw(name, value=defaults)
                return [{"message": f"Profile {name} created.", "category": "success"}]

            if action == "profile_delete":
                name = field("profile_name")
                profiles = await self.config.guild(guild).profiles()
                if name not in profiles:
                    return [{"message": "That profile no longer exists.",
                             "category": "warning"}]
                if len(profiles) == 1:
                    return [
                        {"message": "At least one profile must exist.",
                         "category": "warning"}
                    ]
                await self.config.guild(guild).profiles.clear_raw(name)
                return [{"message": f"Profile {name} deleted.", "category": "success"}]

            if action == "profile_save":
                return await self._tk_save_profile(guild, field)

            if action == "panel_add_button":
                return await self._tk_add_panel_item(guild, field, dropdown=False)
            if action == "panel_add_option":
                return await self._tk_add_panel_item(guild, field, dropdown=True)
            if action == "panel_item_remove":
                return await self._tk_remove_panel_item(guild, field)
            if action == "appeals_configure":
                return await self._tk_configure_appeals(member, guild, field)

            if action == "panel_remove":
                key = field("panel_key")
                async with self.config.guild(guild).buttons_dropdowns() as panels:
                    if key not in panels:
                        return [{"message": "That panel no longer exists.",
                                 "category": "warning"}]
                    del panels[key]
                return [
                    {
                        "message": "Panel removed. The message itself stays in Discord; "
                        "delete it there if you no longer want it.",
                        "category": "success",
                    }
                ]
        except discord.Forbidden:
            return [
                {"message": "Discord refused that action; check my permissions.",
                 "category": "danger"}
            ]
        except RuntimeError as exc:
            return [{"message": str(exc), "category": "warning"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("Tickets dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    async def _tk_create_for(
        self, member: discord.Member, guild: discord.Guild, field
    ) -> list[dict]:
        """Open a ticket on someone else's behalf, like `[p]ticket createfor`."""
        owner = guild.get_member(field.integer("owner_id", 0) or 0)
        if owner is None:
            return [{"message": "Pick the member the ticket is for.", "category": "warning"}]
        profile = (field("profile") or "main").strip()
        profiles = await self.config.guild(guild).profiles()
        if profile not in profiles:
            return [{"message": f"No profile named {profile}.", "category": "warning"}]
        reason = (field("reason") or "").strip() or None
        if reason and len(reason) > 1000:
            return [
                {"message": "A reason cannot exceed 1000 characters.",
                 "category": "warning"}
            ]

        # `create_ticket` sends into the context it is handed, so it needs a real one.
        context = await fake_context(self.bot, member, "ticket createfor")
        if context is None:
            return [
                {
                    "message": "I could not open a ticket; there is no channel I can "
                    "talk in.",
                    "category": "danger",
                }
            ]
        await self.create_ticket(context, profile, owner, reason=reason)
        return [
            {
                "message": f"Ticket opened for {owner.display_name} on the "
                f"{profile} profile.",
                "category": "success",
            }
        ]

    async def _tk_ticket_action(
        self, action: str, member: discord.Member, guild: discord.Guild, field
    ) -> list[dict]:
        ticket_id = field.integer("ticket_id", 0) or 0
        ticket = (self.tickets.get(guild.id) or {}).get(ticket_id)
        if ticket is None:
            return [{"message": f"Ticket #{ticket_id} no longer exists.",
                     "category": "warning"}]

        reason = (field("reason") or "").strip() or None
        what = action.removeprefix("ticket_")

        if what == "close":
            await ticket.close(member, reason)
            return [{"message": f"Ticket #{ticket_id} closed.", "category": "success"}]
        if what == "reopen":
            await ticket.reopen(member, reason)
            return [{"message": f"Ticket #{ticket_id} reopened.", "category": "success"}]
        if what == "claim":
            await ticket.claim(member)
            return [{"message": f"Ticket #{ticket_id} claimed.", "category": "success"}]
        if what == "unclaim":
            await ticket.unclaim()
            return [{"message": f"Ticket #{ticket_id} unclaimed.", "category": "success"}]
        if what == "lock":
            await ticket.lock(member)
            return [{"message": f"Ticket #{ticket_id} locked.", "category": "success"}]
        if what == "unlock":
            await ticket.unlock(member)
            return [{"message": f"Ticket #{ticket_id} unlocked.", "category": "success"}]
        if what == "approve":
            await ticket.approve_appeal(member)
            return [
                {"message": f"Appeal on ticket #{ticket_id} approved.",
                 "category": "success"}
            ]
        if what == "delete":
            await ticket.delete_channel(member)
            return [
                {"message": f"Ticket #{ticket_id} deleted.", "category": "success"}
            ]
        if what in ("addmember", "removemember"):
            target = guild.get_member(field.integer("member_id", 0) or 0)
            if target is None:
                return [{"message": "Pick a member.", "category": "warning"}]
            if what == "addmember":
                await ticket.add_member(target, member)
                return [
                    {"message": f"{target.display_name} added to ticket #{ticket_id}.",
                     "category": "success"}
                ]
            await ticket.remove_member(target, member)
            return [
                {"message": f"{target.display_name} removed from ticket #{ticket_id}.",
                 "category": "success"}
            ]

        return [{"message": f"Unknown ticket action: {what}", "category": "warning"}]


    # ------------------------------------------------------------- panels

    BUTTON_STYLES = {
        "1": "Blurple",
        "2": "Grey",
        "3": "Green",
        "4": "Red",
    }

    async def _tk_resolve_message(self, guild: discord.Guild, field):
        """The message a panel is attached to, from a link or a pair of IDs.

        The commands took a message converter; a link is the equivalent here,
        since it is what Discord's own "Copy Message Link" puts on the
        clipboard.
        """
        raw = (field("message_link") or "").strip()
        channel_id = message_id = 0
        if raw:
            parts = [p for p in raw.rstrip("/").split("/") if p.isdigit()]
            if len(parts) >= 3:
                _guild_id, channel_id, message_id = (int(p) for p in parts[-3:])
        else:
            channel_id = field.integer("panel_channel", 0) or 0
            message_id = field.integer("panel_message", 0) or 0
        if not channel_id or not message_id:
            return None, [
                {
                    "message": "Paste the message link, or give a channel and message ID.",
                    "category": "warning",
                }
            ]

        channel = guild.get_channel_or_thread(channel_id)
        if channel is None:
            return None, [
                {"message": "That channel is not in this server.", "category": "warning"}
            ]
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            return None, [{"message": "No such message there.", "category": "warning"}]
        except discord.Forbidden:
            return None, [
                {"message": f"I cannot read #{channel.name}.", "category": "danger"}
            ]
        if message.author.id != self.bot.user.id:
            return None, [
                {
                    "message": "The message has to be one I posted - Discord only lets a "
                    "bot put components on its own messages.",
                    "category": "warning",
                }
            ]
        return message, None

    async def _tk_panel_write(
        self,
        guild: discord.Guild,
        message: discord.Message,
        *,
        profile: str,
        emoji: str | None = None,
        label: str | None = None,
        style: int = 2,
        description: str | None = None,
        dropdown: bool = False,
    ) -> str:
        """Put one button or dropdown option on a message.

        This is what `[p]settickets addbutton` and `adddropdownoption` did, and
        the appeal setup uses it for the button it posts.

        Raises
        ------
        ValueError
            With the reason, for anything Discord or the cog will not accept.
        """
        from AAA3A_utils import CogsUtils

        from .views import CreateTicketView

        if emoji is None and label is None:
            raise ValueError("Give the item an emoji, a label, or both.")
        if label is not None and len(label) > 100:
            raise ValueError("A label is at most 100 characters.")

        key = f"{message.channel.id}-{message.id}"
        async with self.config.guild(guild).buttons_dropdowns() as panels:
            if key not in panels:
                if message.components:
                    raise ValueError(
                        "That message already has components on it that Tickets did"
                        " not add."
                    )
                panels[key] = {"buttons": {}, "dropdown_options": {}}
            components = panels[key]

            if dropdown:
                if label is None:
                    raise ValueError("A dropdown option needs a label.")
                if len(components["dropdown_options"]) >= 25:
                    raise ValueError("A dropdown holds at most 25 options.")
                if description is not None and len(description) > 100:
                    raise ValueError("A description is at most 100 characters.")
                identifier = CogsUtils.generate_key(
                    length=5, existing_keys=components["dropdown_options"]
                )
                components["dropdown_options"][identifier] = {
                    "emoji": emoji,
                    "label": label,
                    "description": description,
                    "profile": profile,
                }
                added = f"Dropdown option {label}"
            else:
                if len(components["buttons"]) >= 20:
                    raise ValueError("A message holds at most 20 buttons.")
                identifier = CogsUtils.generate_key(
                    length=5, existing_keys=components["buttons"]
                )
                components["buttons"][identifier] = {
                    "emoji": emoji,
                    "label": label,
                    "style": int(style),
                    "profile": profile,
                }
                added = f"Button {label or emoji}"

            snapshot = {
                "buttons": dict(components["buttons"]),
                "dropdown_options": dict(components["dropdown_options"]),
            }

        # Rebuilding the view is what actually puts it on the message; the
        # stored copy alone would only take effect after a restart.
        if message in self.views:
            self.views.pop(message).stop()
        view = CreateTicketView(cog=self, components=snapshot)
        message = await message.edit(view=view)
        self.views[message] = view
        return added

    async def _tk_add_panel_item(
        self, guild: discord.Guild, field, *, dropdown: bool
    ) -> list[dict]:
        """The page's form around `_tk_panel_write`."""
        message, warning = await self._tk_resolve_message(guild, field)
        if warning:
            return warning

        profile = (field("panel_profile") or "main").strip() or "main"
        profiles = await self.config.guild(guild).profiles()
        if profile not in profiles:
            return [{"message": f"No profile named {profile}.", "category": "warning"}]

        style = (field("panel_style") or "2").strip()
        if not dropdown and style not in self.BUTTON_STYLES:
            return [{"message": "Pick a button colour.", "category": "warning"}]

        try:
            added = await self._tk_panel_write(
                guild,
                message,
                profile=profile,
                emoji=(field("panel_emoji") or "").strip() or None,
                label=(field("panel_label") or "").strip() or None,
                style=int(style),
                description=(field("panel_description") or "").strip() or None,
                dropdown=dropdown,
            )
        except ValueError as exc:
            return [{"message": str(exc), "category": "warning"}]
        return [{"message": f"{added} added to that message.", "category": "success"}]

    async def _tk_remove_panel_item(self, guild: discord.Guild, field) -> list[dict]:
        """Take one button or option off a panel, leaving the rest."""
        from .views import CreateTicketView

        key = field("panel_key")
        kind = field("item_kind")
        identifier = field("item_key")
        if kind not in ("buttons", "dropdown_options"):
            return [{"message": "Unknown item type.", "category": "warning"}]

        async with self.config.guild(guild).buttons_dropdowns() as panels:
            if key not in panels or identifier not in panels[key].get(kind, {}):
                return [{"message": "That item is already gone.", "category": "info"}]
            del panels[key][kind][identifier]
            empty = not panels[key]["buttons"] and not panels[key]["dropdown_options"]
            if empty:
                del panels[key]
                snapshot = None
            else:
                snapshot = {
                    "buttons": dict(panels[key]["buttons"]),
                    "dropdown_options": dict(panels[key]["dropdown_options"]),
                }

        channel_id, message_id = (int(x) for x in str(key).split("-"))
        channel = guild.get_channel_or_thread(channel_id)
        if channel is not None:
            try:
                message = await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden):
                message = None
            if message is not None:
                if message in self.views:
                    self.views.pop(message).stop()
                if snapshot is None:
                    await message.edit(view=None)
                else:
                    view = CreateTicketView(cog=self, components=snapshot)
                    message = await message.edit(view=view)
                    self.views[message] = view
        return [{"message": "Item removed.", "category": "success"}]

    # ------------------------------------------------------------ appeals

    async def _tk_configure_appeals(
        self, member: discord.Member, guild: discord.Guild, field
    ) -> list[dict]:
        """The appeal feature, as `[p]settickets configureappeals` set it up.

        The checks are the command's, in the same order: the appeal server has
        to be one the bot is in and the operator can moderate, and the invite
        has to point at it.
        """
        profile = (field("appeal_profile") or "main").strip() or "main"
        profiles = await self.config.guild(guild).profiles()
        if profile not in profiles:
            return [{"message": f"No profile named {profile}.", "category": "warning"}]

        appeal_guild_id = field.integer("appeal_guild_id", 0) or 0
        appeal_guild = self.bot.get_guild(appeal_guild_id)
        if appeal_guild is None:
            return [
                {"message": "I am not in a server with that ID.", "category": "warning"}
            ]
        if appeal_guild.id == guild.id:
            return [
                {
                    "message": "Appeals are for a different server - the one people are "
                    "banned from cannot be the one they appeal in.",
                    "category": "warning",
                }
            ]
        operator = appeal_guild.get_member(member.id)
        if operator is None:
            return [
                {"message": "You are not in that server.", "category": "warning"}
            ]
        if not (
            operator.guild_permissions.manage_guild
            and operator.guild_permissions.ban_members
        ):
            return [
                {
                    "message": "You need Manage Server and Ban Members there.",
                    "category": "warning",
                }
            ]
        if not appeal_guild.me.guild_permissions.ban_members:
            return [
                {
                    "message": "I need Ban Members there, to lift the ban when an "
                    "appeal is approved.",
                    "category": "warning",
                }
            ]

        code = (field("appeal_invite") or "").strip()
        code = code.removeprefix("https://").removeprefix("http://")
        code = code.removeprefix("discord.gg/").removeprefix("discord.com/invite/")
        if not code:
            return [{"message": "Enter an invite to the appeal server.", "category": "warning"}]
        try:
            invite = await self.bot.fetch_invite(code)
        except discord.NotFound:
            return [{"message": "That invite does not exist.", "category": "warning"}]
        except discord.HTTPException as exc:
            return [{"message": f"Discord refused that invite: {exc}", "category": "danger"}]
        if invite.guild is None or invite.guild.id != appeal_guild.id:
            return [
                {"message": "That invite is not for the appeal server.", "category": "warning"}
            ]

        moderator_role = appeal_guild.get_role(
            field.integer("appeal_moderator_role", 0) or 0
        )
        category_open = appeal_guild.get_channel(field.integer("appeal_category_open", 0) or 0)
        category_closed = (
            appeal_guild.get_channel(field.integer("appeal_category_closed", 0) or 0)
            or category_open
        )
        logs_channel = appeal_guild.get_channel(field.integer("appeal_logs_channel", 0) or 0)
        button_channel = appeal_guild.get_channel(
            field.integer("appeal_button_channel", 0) or 0
        )

        # The shape the command wrote, unchanged - an appeal profile is one
        # open ticket per person, with a fixed three-question modal.
        config = await self.config.guild(guild).profiles.get_raw(profile)
        config["enabled"] = True
        config["max_open_tickets_by_member"] = 1
        config["creating_modal"] = [
            {
                "label": question,
                "style": 2,
                "required": True,
                "default": "",
                "placeholder": "",
                "min_length": None,
                "max_length": None,
            }
            for question in (
                "Why were you banned?",
                "Why should we unban you?",
                "How can you guarantee it won't happen again?",
            )
        ]
        if moderator_role is not None:
            if moderator_role.id not in config["support_roles"]:
                config["support_roles"].append(moderator_role.id)
            if moderator_role.id not in config["ping_roles"]:
                config["ping_roles"].append(moderator_role.id)
        if category_open is not None:
            config["category_open"] = category_open.id
            config["category_closed"] = category_closed.id
        config["owner_close_confirmation"] = False
        config["owner_can_reopen"] = False
        config["close_on_leave"] = True
        config["transcripts"] = True
        if logs_channel is not None:
            config["logs_channel"] = logs_channel.id
        config["appeals"] = {
            "enabled": True,
            "guild_id": appeal_guild.id,
            "invite_code": invite.code,
        }
        await self.config.guild(guild).profiles.set_raw(profile, value=config)

        notes = [
            {
                "message": f"Appeals configured on {profile}, for {appeal_guild.name}.",
                "category": "success",
            }
        ]
        if button_channel is not None:
            embed = discord.Embed(
                title="Appeal Ticket",
                description=(
                    "Click the button below to create an appeal ticket. If our team"
                    " accepts your appeal, you will be unbanned from the server."
                ),
                color=discord.Color.red(),
            )
            embed.set_footer(text=guild.name)
            try:
                posted = await button_channel.send(embed=embed)
                await self._tk_panel_write(
                    guild,
                    posted,
                    profile=profile,
                    emoji="\N{SHIELD}",
                    label="Appeal",
                    style=4,
                )
            except (discord.Forbidden, ValueError) as exc:
                notes.append(
                    {
                        "message": f"Configured, but the appeal button could not be posted: {exc}",
                        "category": "warning",
                    }
                )
        return notes

    async def _tk_save_profile(self, guild: discord.Guild, field) -> list[dict]:
        name = field("profile_name")
        profiles = await self.config.guild(guild).profiles()
        if name not in profiles:
            return [{"message": "That profile no longer exists.", "category": "warning"}]
        data = dict(profiles[name])

        for key, _label, _help in TOGGLES:
            data[key] = field.checked(key)

        for key, _label, maximum, _help in TEXTS:
            value = (field(key) or "").strip()
            if len(value) > maximum:
                return [
                    {
                        "message": f"{key} cannot be longer than {maximum} characters.",
                        "category": "warning",
                    }
                ]
            data[key] = value or None

        max_open = field.integer("max_open_tickets_by_member", 5) or 5
        if not 1 <= max_open <= 50:
            return [
                {"message": "Maximum open tickets must be between 1 and 50.",
                 "category": "warning"}
            ]
        data["max_open_tickets_by_member"] = max_open

        raw_delete = (field("auto_delete_on_close") or "").strip()
        if raw_delete == "":
            data["auto_delete_on_close"] = None
        else:
            try:
                hours = int(raw_delete)
            except ValueError:
                return [
                    {"message": "Auto-delete must be a whole number of hours.",
                     "category": "warning"}
                ]
            if not 0 <= hours <= 30 * 24:
                return [
                    {"message": "Auto-delete must be between 0 and 720 hours.",
                     "category": "warning"}
                ]
            data["auto_delete_on_close"] = hours

        for key, _label, _help in ROLE_LISTS:
            data[key] = [int(i) for i in field.many(key) if str(i).isdigit()]

        for key in ("ticket_role",):
            value = field.integer(key, 0) or 0
            data[key] = value or None

        for key in ("category_open", "category_closed", "logs_channel", "forum_channel"):
            value = field.integer(key, 0) or 0
            data[key] = value or None

        emojis = dict(data.get("emojis") or {})
        for emoji_key, _label in EMOJIS:
            value = (field(f"emoji_{emoji_key}") or "").strip()
            if value:
                emojis[emoji_key] = value
        data["emojis"] = emojis

        appeals = dict(data.get("appeals") or {})
        appeals["enabled"] = field.checked("appeals_enabled")
        appeal_guild = field.integer("appeals_guild_id", 0) or 0
        appeals["guild_id"] = appeal_guild or None
        appeals["invite_code"] = (field("appeals_invite") or "").strip() or None
        data["appeals"] = appeals

        await self.config.guild(guild).profiles.set_raw(name, value=data)
        return [{"message": f"Profile {name} saved.", "category": "success"}]


TICKETS_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-ticket"></i> Tickets in {{ guild_name }}</h4>
    <p>Work every open ticket and configure each profile without leaving the dashboard.</p>
  </div>

  {{ stats([('Open', open_count),
            ('Closed', closed_count),
            ('Claimed', claimed_count),
            ('Profiles', profiles|length)]) }}

  {% if not is_support %}
    <div class="dz-panel">
      <p class="dz-empty">You need a support role to manage tickets here.</p>
    </div>
  {% else %}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-plus"></i> Open a ticket for someone</h5>
      <p class="dz-hint">Creates the ticket as if that member had opened it themselves.</p>
      <div class="dz-grid three">
        <div>
          <label class="dz-label">Member</label>
          {{ picker('owner_id', member_options, false, 8, 'Search members...') }}
        </div>
        <div>
          <label class="dz-label">Profile</label>
          <select class="dz-select" name="profile">
            {% for name in profile_names %}
              <option value="{{ name }}">{{ name }}</option>
            {% endfor %}
          </select>
        </div>
        <div>
          <label class="dz-label">Reason</label>
          <input class="dz-input" type="text" name="reason" maxlength="1000"
                 placeholder="why the ticket is being opened" />
        </div>
      </div>
      <div class="dz-save">
        <button class="dz-btn primary" name="action" value="create_for">
          <i class="fa fa-ticket"></i> Open ticket
        </button>
      </div>
    </div>
  </form>

  <div class="dz-panel">
    <h5><i class="fa fa-inbox"></i> Tickets</h5>
    {% if tickets %}
      {% for t in tickets %}
        <div style="padding:12px 0; border-bottom:1px solid rgba(255,255,255,.06);">
          <div class="dz-row">
            <b>#{{ t.id }}</b>
            <span class="dz-tag">{{ t.profile }}</span>
            {% if t.closed %}<span class="dz-tag bad">closed</span>
            {% else %}<span class="dz-tag good">open</span>{% endif %}
            {% if t.claimed %}<span class="dz-tag">claimed by {{ t.claimed_by }}</span>{% endif %}
            {% if t.locked %}<span class="dz-tag warn">locked</span>{% endif %}
            {% if t.appeal_approved %}<span class="dz-tag good">appeal approved</span>{% endif %}
            {% if t.link %}
              <a class="dz-hint" href="{{ t.link }}" target="_blank" rel="noopener">
                open in Discord</a>
            {% endif %}
          </div>
          <p class="dz-hint" style="margin:6px 0;">
            Opened by {{ t.owner }}{% if t.opened %} on {{ t.opened }}{% endif %}
            {%- if t.category %} &middot; {{ t.category }}{% endif %}
            {%- if t.closed_at %} &middot; closed {{ t.closed_at }}{% endif %}
            {%- if t.members %} &middot; also in: {{ t.members|join(', ') }}{% endif %}
          </p>
          {% if t.reason %}<div class="dz-text">{{ t.reason }}</div>{% endif %}
          {% for question, answer in t.answers.items() %}
            <div class="efield"><b>{{ question }}</b>{{ answer }}</div>
          {% endfor %}

          <form method="POST" style="margin-top:8px;">
            <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
            <input type="hidden" name="ticket_id" value="{{ t.id }}" />
            <div class="dz-row">
              <input class="dz-input" type="text" name="reason" placeholder="reason"
                     style="flex:1 1 180px;" />
              {% if t.closed %}
                <button class="dz-btn primary" name="action" value="ticket_reopen">
                  <i class="fa fa-folder-open"></i> Reopen
                </button>
              {% else %}
                <button class="dz-btn" name="action" value="ticket_close">
                  <i class="fa fa-times"></i> Close
                </button>
                {% if t.claimed %}
                  <button class="dz-btn" name="action" value="ticket_unclaim">
                    <i class="fa fa-user"></i> Unclaim
                  </button>
                {% else %}
                  <button class="dz-btn" name="action" value="ticket_claim">
                    <i class="fa fa-users"></i> Claim
                  </button>
                {% endif %}
                {% if t.locked %}
                  <button class="dz-btn" name="action" value="ticket_unlock">
                    <i class="fa fa-unlock"></i> Unlock
                  </button>
                {% else %}
                  <button class="dz-btn" name="action" value="ticket_lock">
                    <i class="fa fa-lock"></i> Lock
                  </button>
                {% endif %}
                {% if not t.appeal_approved %}
                  <button class="dz-btn" name="action" value="ticket_approve">
                    <i class="fa fa-shield"></i> Approve appeal
                  </button>
                {% endif %}
              {% endif %}
              {{ confirm('Delete', 'ticket_delete',
                         'Delete ticket #' ~ t.id ~ ' and its channel?') }}
            </div>
            <div class="dz-row" style="margin-top:8px;">
              {{ picker('member_id', member_options, false, 6, 'Search members...') }}
              <button class="dz-btn" name="action" value="ticket_addmember">
                <i class="fa fa-user-plus"></i> Add
              </button>
              <button class="dz-btn" name="action" value="ticket_removemember">
                <i class="fa fa-user-times"></i> Remove
              </button>
            </div>
          </form>
        </div>
      {% endfor %}
    {% else %}
      <p class="dz-empty">No tickets yet.</p>
    {% endif %}
  </div>

  <div class="dz-panel">
    <h5><i class="fa fa-th-large"></i> Ticket panels</h5>
    <p class="dz-hint">
      The messages carrying the open-a-ticket buttons and dropdowns. Members
      use these; without one there is no way into a ticket.
    </p>

    {% if panels %}
      <table class="dz-t">
        <tr><th>Channel</th><th>Items</th><th></th></tr>
        {% for p in panels %}
          <tr>
            <td><a href="{{ p.link }}" target="_blank" rel="noopener">#{{ p.channel }}</a></td>
            <td>
              {% if not p.items %}
                <span class="dz-hint" style="margin:0;">empty</span>
              {% endif %}
              {% for item in p.items %}
                <span class="dz-tag" title="{{ item.description }}">
                  {% if item.emoji %}{{ item.emoji }} {% endif %}{{ item.label }}
                  &rarr; {{ item.profile }}
                  {% if item.style %}({{ item.style }}){% endif %}
                  {% if is_admin %}
                    <form method="POST" style="display:inline;">
                      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                      <input type="hidden" name="panel_key" value="{{ p.key }}" />
                      <input type="hidden" name="item_kind" value="{{ item.kind }}" />
                      <input type="hidden" name="item_key" value="{{ item.key }}" />
                      <button class="dz-btn round danger" name="action"
                              value="panel_item_remove" title="Remove this item">
                        <i class="fa fa-times"></i>
                      </button>
                    </form>
                  {% endif %}
                </span>
              {% endfor %}
            </td>
            <td style="white-space:nowrap; width:1%;">
              {% if is_admin %}
                <form method="POST">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="panel_key" value="{{ p.key }}" />
                  {{ confirm('', 'panel_remove',
                             'Stop this message from opening tickets?') }}
                </form>
              {% endif %}
            </td>
          </tr>
        {% endfor %}
      </table>
    {% else %}
      <p class="dz-empty">No panels yet.</p>
    {% endif %}

    {% if is_admin %}
      <form method="POST" style="margin-top:12px;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <div class="dz-label">Add to a message</div>
        <p class="dz-hint">
          Paste the link to a message I posted &mdash; right-click it in Discord
          and choose Copy Message Link. Discord only lets a bot put buttons on
          its own messages.
        </p>
        <input class="dz-input" type="text" name="message_link"
               placeholder="https://discord.com/channels/.../.../..." />
        <div class="dz-grid two" style="margin-top:9px;">
          <div>
            <div class="dz-label">Profile</div>
            <select class="dz-select" name="panel_profile">
              {% for name in profile_names %}
                <option value="{{ name }}">{{ name }}</option>
              {% endfor %}
            </select>
          </div>
          <div>
            <div class="dz-label">Button colour</div>
            <select class="dz-select" name="panel_style">
              {% for s in button_styles %}
                <option value="{{ s.value }}"
                        {% if s.value == '2' %}selected{% endif %}>{{ s.label }}</option>
              {% endfor %}
            </select>
          </div>
        </div>
        <div class="dz-grid two" style="margin-top:9px;">
          <div>
            <div class="dz-label">Emoji</div>
            <input class="dz-input" type="text" name="panel_emoji" placeholder="\N{TICKET}" />
          </div>
          <div>
            <div class="dz-label">Label</div>
            <input class="dz-input" type="text" name="panel_label" maxlength="100"
                   placeholder="Open a ticket" />
          </div>
        </div>
        <div class="dz-label" style="margin-top:9px;">Description (dropdown options only)</div>
        <input class="dz-input" type="text" name="panel_description" maxlength="100"
               placeholder="Shown under the option in the dropdown" />
        <div class="dz-row" style="margin-top:11px;">
          <button class="dz-btn primary" name="action" value="panel_add_button">
            <i class="fa fa-hand-pointer-o"></i> Add a button
          </button>
          <button class="dz-btn" name="action" value="panel_add_option">
            <i class="fa fa-caret-square-o-down"></i> Add a dropdown option
          </button>
          <span class="dz-hint" style="margin:0 0 0 6px;">
            An item needs an emoji or a label; a dropdown option needs a label.
          </span>
        </div>
      </form>
    {% endif %}
  </div>

  {% if is_admin %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-gavel"></i> Ban appeals</h5>
        <p class="dz-hint">
          Lets people banned from another server appeal from this one. Pick the
          profile to turn into an appeal profile: it gets a three-question form,
          one open ticket per person, transcripts on, and approving the appeal
          lifts the ban over there. You need Manage Server and Ban Members in
          the other server, and so does the bot.
        </p>
        <div class="dz-grid two">
          <div>
            <div class="dz-label">Profile to use</div>
            <select class="dz-select" name="appeal_profile">
              {% for name in profile_names %}
                <option value="{{ name }}">{{ name }}</option>
              {% endfor %}
            </select>
          </div>
          <div>
            <div class="dz-label">Server people are banned from (ID)</div>
            <input class="dz-input" type="text" name="appeal_guild_id"
                   placeholder="000000000000000000" />
          </div>
        </div>
        <div class="dz-label" style="margin-top:9px;">Invite back to that server</div>
        <input class="dz-input" type="text" name="appeal_invite"
               placeholder="discord.gg/example" />
        <div class="dz-grid two" style="margin-top:9px;">
          <div>
            <div class="dz-label">Moderator role there (ID)</div>
            <input class="dz-input" type="text" name="appeal_moderator_role"
                   placeholder="optional" />
          </div>
          <div>
            <div class="dz-label">Logs channel there (ID)</div>
            <input class="dz-input" type="text" name="appeal_logs_channel"
                   placeholder="optional" />
          </div>
        </div>
        <div class="dz-grid two" style="margin-top:9px;">
          <div>
            <div class="dz-label">Open category there (ID)</div>
            <input class="dz-input" type="text" name="appeal_category_open"
                   placeholder="optional" />
          </div>
          <div>
            <div class="dz-label">Closed category there (ID)</div>
            <input class="dz-input" type="text" name="appeal_category_closed"
                   placeholder="optional, defaults to the open one" />
          </div>
        </div>
        <div class="dz-label" style="margin-top:9px;">Post an appeal button in (ID)</div>
        <input class="dz-input" type="text" name="appeal_button_channel"
               placeholder="optional, a channel in the appeal server" />
        <p class="dz-hint" style="margin-top:7px;">
          These are IDs rather than pickers because they name channels and roles
          in the other server, which this page cannot list.
        </p>
        <div class="dz-save">
          <button class="dz-btn primary" name="action" value="appeals_configure">
            <i class="fa fa-check"></i> Configure appeals
          </button>
        </div>
      </div>
    </form>
  {% endif %}

  {% if is_admin %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-plus"></i> New profile</h5>
        <p class="dz-hint">A profile is one ticket type with its own roles,
           categories and messages.</p>
        <div class="dz-row">
          <input class="dz-input" type="text" name="profile_name"
                 placeholder="appeals" style="max-width:240px;" />
          <button class="dz-btn primary" name="action" value="profile_add">
            <i class="fa fa-plus"></i> Create profile
          </button>
        </div>
      </div>
    </form>

    {% for p in profiles %}
      <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <input type="hidden" name="profile_name" value="{{ p.name }}" />
        <div class="dz-panel">
          <h5>
            <i class="fa fa-sliders"></i> Profile: {{ p.name }}
            {% if p.enabled %}<span class="dz-tag good">enabled</span>
            {% else %}<span class="dz-tag">disabled</span>{% endif %}
          </h5>

          <div class="dz-grid two">
            <div>
              {% for key, label, help_text in toggles %}
                <label class="dz-toggle" title="{{ help_text }}">
                  <input type="checkbox" name="{{ key }}"
                         {% if p.toggle_values[key] %}checked{% endif %} />
                  <span>{{ label }}</span>
                </label>
              {% endfor %}
            </div>
            <div>
              <label class="dz-label">Maximum open tickets per member</label>
              <input class="dz-input" type="number" min="1" max="50"
                     name="max_open_tickets_by_member" value="{{ p.max_open }}" />
              <label class="dz-label">Delete a closed ticket after (hours)</label>
              <input class="dz-input" type="number" min="0" max="720"
                     name="auto_delete_on_close"
                     value="{{ p.auto_delete if p.auto_delete is not none else '' }}"
                     placeholder="never" />
              <label class="dz-label">Role given to ticket owners</label>
              {{ picker('ticket_role', p.ticket_role_options, false, 6,
                        'Search roles...', true, 'none') }}
            </div>
          </div>

          {% for key, label, maximum, help_text in texts %}
            <label class="dz-label">{{ label }}</label>
            <p class="dz-hint">{{ help_text }}</p>
            <textarea class="dz-area" name="{{ key }}"
                      maxlength="{{ maximum }}">{{ p.text_values[key] }}</textarea>
          {% endfor %}

          <div class="dz-grid two" style="margin-top:14px;">
            {% for key, label, help_text in role_lists %}
              <div>
                <label class="dz-label">{{ label }}</label>
                <p class="dz-hint">{{ help_text }}</p>
                {{ picker(key, p.role_options[key], true, 6, 'Search roles...') }}
              </div>
            {% endfor %}
          </div>

          <div class="dz-grid two" style="margin-top:14px;">
            <div>
              <label class="dz-label">Category for open tickets</label>
              {{ picker('category_open', p.category_open_options, false, 6,
                        'Search categories...', true, 'none') }}
              <label class="dz-label">Category for closed tickets</label>
              {{ picker('category_closed', p.category_closed_options, false, 6,
                        'Search categories...', true, 'none') }}
            </div>
            <div>
              <label class="dz-label">Log channel</label>
              {{ picker('logs_channel', p.logs_options, false, 6,
                        'Search channels...', true, 'no logging') }}
              <label class="dz-label">Forum channel for tickets</label>
              {{ picker('forum_channel', p.forum_options, false, 6,
                        'Search channels...', true, 'use categories instead') }}
            </div>
          </div>

          <label class="dz-label" style="margin-top:14px;">Button emojis</label>
          <div class="dz-grid three">
            {% for key, label in emoji_keys %}
              <div>
                <label class="dz-label">{{ label }}</label>
                <input class="dz-input" type="text" name="emoji_{{ key }}"
                       value="{{ p.emojis.get(key, '') }}" />
              </div>
            {% endfor %}
          </div>

          <label class="dz-label" style="margin-top:14px;">Ban appeals</label>
          <label class="dz-toggle">
            <input type="checkbox" name="appeals_enabled"
                   {% if p.appeals_enabled %}checked{% endif %} />
            <span>Treat tickets on this profile as ban appeals</span>
          </label>
          <div class="dz-grid two">
            <div>
              <label class="dz-label">Server the appeal is for</label>
              <input class="dz-input" type="number" name="appeals_guild_id"
                     value="{{ p.appeals_guild_id }}" placeholder="server ID" />
            </div>
            <div>
              <label class="dz-label">Invite code sent on approval</label>
              <input class="dz-input" type="text" name="appeals_invite"
                     value="{{ p.appeals_invite }}" placeholder="abcd1234" />
            </div>
          </div>

          <div class="dz-row dz-save">
            <button class="dz-btn primary" name="action" value="profile_save">
              <i class="fa fa-save"></i> Save profile
            </button>
            {{ confirm('Delete profile', 'profile_delete',
                       'Delete the profile ' ~ p.name ~ '? Its tickets stop working.') }}
          </div>
        </div>
      </form>
    {% endfor %}
  {% endif %}

  {% endif %}
</div>
"""
)
