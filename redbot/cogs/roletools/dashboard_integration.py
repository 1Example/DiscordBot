from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands

from redbot.core.commands import parse_timedelta
from redbot.core.utils.chat_formatting import humanize_timedelta
from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    channel_options,
    dashboard_page,
    emoji_options,
    emoji_problem,
    form_reader,
    guild_member,
    is_staff,
    member_options,
    query_reader,
    role_options,
)

log = logging.getLogger("red.roletools.dashboard")

# Shorthands `[p]roletools giverole` accepts for whole groups of members.
TARGET_GROUPS = (
    ("everyone", "Everyone in the server"),
    ("here", "Everyone currently online"),
    ("humans", "All humans"),
    ("bots", "All bots"),
)

# Per-role flags stored in config.role(...), with the label shown on the page.
ROLE_FLAGS = (
    ("selfassignable", "Members can self-assign"),
    ("selfremovable", "Members can self-remove"),
    ("sticky", "Sticky (reapplied on rejoin)"),
    ("auto", "Auto-granted on join"),
)

# How many messages the message picker offers per channel.
MESSAGE_HISTORY = 50


class DashboardIntegration:
    """RoleTools, end to end.

    Configures each role (self-assign, sticky, auto, cost, exclusive/inclusive/
    required sets, temporary duration), builds and removes reaction roles by
    picking a channel and a message rather than hunting for IDs, hands roles out
    through the same code paths as ``[p]roletools giverole``, and covers the
    atomic assignment setting.
    """

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering RoleTools as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    # ------------------------------------------------------------------ page --

    @dashboard_page(
        name=None,
        description="Manage self-assignable, sticky, automatic and reaction roles.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_roletools_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        if not await is_staff(self.bot, user, member, guild):
            return {
                "status": 1,
                "error_title": "Forbidden",
                "error_message": "Only server administrators can manage roles.",
            }

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._rt_handle_post(guild, kwargs)

        guild_data = await self.config.guild(guild).all()
        managed = await self._rt_managed_roles(guild)
        # Whichever role the form is pointed at: the one just saved, or the one
        # an Edit link asked for. Its current values pre-fill the form, so a
        # change is an edit rather than a retype.
        editing = await self._rt_editing(guild, kwargs)
        reactions = await self._rt_reaction_context(guild, guild_data, kwargs)

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": ROLETOOLS_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "managed": managed,
                "roles": role_options(guild),
                "editing": editing,
                "reactions": reactions,
                "require_modes": (
                    ("all", "must have every one of them"),
                    ("any", "must have at least one of them"),
                ),
                "components": self._rt_components(guild, guild_data),
                "flags": [{"key": k, "label": v} for k, v in ROLE_FLAGS],
                "auto_roles": self._rt_names(guild, guild_data.get("auto_roles") or []),
                "pending_temporary": self._rt_pending_temporary(guild, guild_data),
                "temporary_roles": [r for r in managed if r["duration"]],
                "member_options": member_options(guild),
                "is_owner": await self.bot.is_owner(user),
                "atomic": guild_data.get("atomic"),
                "global_atomic": await self.config.atomic(),
                "targets": TARGET_GROUPS,
            },
        }

    # --------------------------------------------------------------- helpers --

    async def _rt_cache(self, guild: discord.Guild) -> dict:
        """The cog's in-memory guild settings, loaded if this guild is missing.

        `events.py` answers every reaction from `self.settings`, never from
        config, so a binding written only to config does nothing until the cog
        is reloaded. Every guild-level write below goes through this.
        """
        cache = getattr(self, "settings", None)
        if cache is None:
            return {}
        if guild.id not in cache:
            cache[guild.id] = await self.config.guild(guild).all()
        return cache[guild.id]

    @staticmethod
    def _rt_split_key(key: str) -> tuple[str, str, str]:
        """A binding key back into (channel_id, message_id, emoji).

        Keys are ``channelid-messageid-emoji``. Very old bindings were stored
        without the channel id, so those come back with an empty channel.
        """
        parts = str(key).split("-", 2)
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        message_id, _, emoji = str(key).partition("-")
        return "", message_id, emoji

    async def _rt_editing(self, guild: discord.Guild, kwargs: dict) -> dict:
        """The role the form should show, with its settings already filled in.

        A posted `role` wins over `?edit=`, so changing the picker and saving
        does not snap back to whatever role the Edit link named.
        """
        raw = (form_reader(kwargs)("role") or "").strip()
        if not raw.isdigit():
            raw = str(query_reader(kwargs)("edit") or "").strip()
        if not raw.isdigit():
            return {}
        role = guild.get_role(int(raw))
        if role is None:
            return {}
        settings = await self.config.role(role).all()
        return {
            "id": str(role.id),
            "name": role.name,
            # The role list with this role already chosen, so the picker keeps
            # its selection across a save.
            "self": role_options(guild, selected=role.id),
            "colour": f"#{role.colour.value:06x}" if role.colour.value else "#99aab5",
            "flags": {key: bool(settings.get(key)) for key, _label in ROLE_FLAGS},
            "cost": settings.get("cost") or 0,
            "require_any": "any" if settings.get("require_any") else "all",
            "duration": self._rt_duration_text(settings.get("duration")),
            "exclusive": role_options(
                guild, selected_many=settings.get("exclusive_to") or []
            ),
            "required": role_options(guild, selected_many=settings.get("required") or []),
            "inclusive": role_options(
                guild, selected_many=settings.get("inclusive_with") or []
            ),
            "unmanageable": bool(guild.me and role.position >= guild.me.top_role.position),
        }

    @staticmethod
    def _rt_duration_text(seconds) -> str:
        """Seconds back into something a person would type, e.g. '2 hours'."""
        if not seconds:
            return ""
        try:
            return humanize_timedelta(seconds=int(seconds)) or ""
        except Exception:  # noqa: BLE001 - a bad stored value must not break the page
            return ""

    async def _rt_reaction_context(
        self, guild: discord.Guild, data: dict, kwargs: dict
    ) -> dict:
        """Everything the reaction-role panel needs, including the message list.

        The channel comes from the form (or `?channel=`), and its recent
        messages are fetched so a binding can be made by picking one instead of
        copying IDs out of Discord.
        """
        field = form_reader(kwargs)
        raw_channel = (field("channel") or "").strip()
        if not raw_channel.isdigit():
            raw_channel = str(query_reader(kwargs)("channel") or "").strip()
        channel = guild.get_channel(int(raw_channel)) if raw_channel.isdigit() else None
        if not isinstance(channel, discord.TextChannel):
            channel = None

        messages: list[dict] = []
        note = ""
        if channel is not None:
            messages, note = await self._rt_message_options(
                channel, selected=(field("message") or "").strip()
            )

        return {
            "channels": channel_options(
                guild, kinds=("text",), selected=channel.id if channel else None
            ),
            "channel_id": str(channel.id) if channel else "",
            "channel_name": f"#{channel.name}" if channel else "",
            "messages": messages,
            "note": note,
            "emojis": emoji_options(guild),
            "groups": self._rt_reaction_groups(guild, data),
        }

    async def _rt_message_options(
        self, channel: discord.TextChannel, *, selected: str = ""
    ) -> tuple[list[dict], str]:
        """Recent messages in `channel`, shaped for the picker.

        Returns (options, note); the note explains an empty list rather than
        leaving a silently empty dropdown.
        """
        me = channel.guild.me
        perms = channel.permissions_for(me) if me is not None else None
        if perms is not None and not perms.read_message_history:
            return [], f"I cannot read the history of #{channel.name}."
        can_react = bool(perms is None or perms.add_reactions)

        out: list[dict] = []
        try:
            async for message in channel.history(limit=MESSAGE_HISTORY):
                out.append(
                    {
                        "id": str(message.id),
                        "name": f"{message.author.display_name}: "
                        f"{self._rt_excerpt(message)}",
                        "group": message.created_at.strftime("%Y-%m-%d"),
                        "selected": str(message.id) == str(selected),
                        "warn": not can_react,
                    }
                )
        except discord.HTTPException as exc:
            log.warning("Could not read #%s: %s", channel.name, exc)
            return [], f"Discord refused to give me the history of #{channel.name}."

        if not out:
            return [], f"#{channel.name} has no messages I can see."
        if not can_react:
            return out, (
                f"I cannot add reactions in #{channel.name}, so you will have to put "
                "the emoji on the message yourself."
            )
        return out, ""

    @staticmethod
    def _rt_excerpt(message: discord.Message, limit: int = 70) -> str:
        """One readable line describing a message, for the picker label."""
        text = (message.content or "").replace("\n", " ").strip()
        if not text and message.embeds:
            embed = message.embeds[0]
            text = (embed.title or embed.description or "").replace("\n", " ").strip()
            text = text or "(embed)"
        if not text and message.attachments:
            text = f"({len(message.attachments)} attachment(s))"
        if not text:
            text = "(no text)"
        return text if len(text) <= limit else text[: limit - 3] + "..."

    def _rt_reaction_groups(self, guild: discord.Guild, data: dict) -> list[dict]:
        """Bindings grouped by the message they live on.

        A flat list is unreadable once a message carries eight emoji, and the
        useful unit of work is "this message", not "this binding".
        """
        groups: dict[str, dict] = {}
        for key, role_id in (data.get("reaction_roles") or {}).items():
            channel_id, message_id, emoji = self._rt_split_key(key)
            channel = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
            role = guild.get_role(int(role_id)) if str(role_id).isdigit() else None
            group = groups.setdefault(
                message_id,
                {
                    "message_id": message_id,
                    "channel_id": channel_id,
                    "channel": f"#{channel.name}"
                    if channel is not None
                    else ("channel deleted" if channel_id else "channel not recorded"),
                    "missing_channel": channel is None,
                    "jump": f"https://discord.com/channels/{guild.id}/{channel_id}/{message_id}"
                    if channel is not None
                    else "",
                    "rows": [],
                },
            )
            group["rows"].append(
                {
                    "key": key,
                    "emoji": emoji,
                    "custom": emoji.isdigit(),
                    "emoji_url": f"https://cdn.discordapp.com/emojis/{emoji}.png?size=32"
                    if emoji.isdigit()
                    else "",
                    "role": role.name if role is not None else f"(deleted {role_id})",
                    "broken": role is None,
                }
            )
        for group in groups.values():
            group["rows"].sort(key=lambda r: r["role"].lower())
        return sorted(groups.values(), key=lambda g: g["message_id"], reverse=True)

    def _rt_pending_temporary(self, guild: discord.Guild, data: dict) -> list[dict]:
        """Temporary roles waiting to be taken off somebody.

        `guild.temporary_roles` is a queue of pending removals, not a list of
        roles: each entry is {user_id, role_id, remove_at}. Writing role ids
        into it breaks the removal loop, so nothing here ever does.
        """
        out = []
        for entry in data.get("temporary_roles") or []:
            if not isinstance(entry, dict):
                continue
            role = guild.get_role(int(entry.get("role_id") or 0))
            member = guild.get_member(int(entry.get("user_id") or 0))
            out.append(
                {
                    "role": role.name if role is not None else "(deleted role)",
                    "member": member.display_name if member is not None else "(left)",
                    "when": int(entry.get("remove_at") or 0),
                }
            )
        return sorted(out, key=lambda e: e["when"])[:25]

    def _rt_components(self, guild: discord.Guild, data: dict) -> dict:
        """Button sets and select menus, which the page previously only counted.

        They are built with the Discord commands; this shows what exists and
        which are pointing at a role that has since been deleted.
        """
        buttons = []
        for name, cfg in (data.get("buttons") or {}).items():
            role = guild.get_role(int(cfg.get("role_id") or 0))
            buttons.append(
                {
                    "name": name,
                    "label": cfg.get("label") or name,
                    "emoji": cfg.get("emoji") or "",
                    "style": cfg.get("style") or "",
                    "role": role.name if role else f"(deleted {cfg.get('role_id')})",
                    "broken": role is None,
                    "messages": len(cfg.get("messages") or []),
                }
            )
        menus = []
        for name, cfg in (data.get("select_menus") or {}).items():
            menus.append(
                {
                    "name": name,
                    "placeholder": cfg.get("placeholder") or "",
                    "options": list(cfg.get("options") or []),
                    "messages": len(cfg.get("messages") or []),
                }
            )
        return {
            "buttons": sorted(buttons, key=lambda b: b["name"]),
            "menus": sorted(menus, key=lambda m: m["name"]),
        }

    def _rt_names(self, guild: discord.Guild, role_ids: list) -> list[str]:
        out = []
        for role_id in role_ids:
            role = guild.get_role(role_id)
            out.append(role.name if role else f"(deleted {role_id})")
        return out

    async def _rt_managed_roles(self, guild: discord.Guild) -> list[dict]:
        """Every role with at least one RoleTools setting on it."""
        try:
            all_roles = await self.config.all_roles()
        except Exception:  # noqa: BLE001
            log.exception("Could not read role settings")
            return []

        rows = []
        for role_id, settings in all_roles.items():
            role = guild.get_role(role_id)
            if role is None:
                continue
            # A role set up with only a relationship, a duration or a reaction
            # is still configured; checking the flags alone hid it completely.
            interesting = (
                any(settings.get(k) for k, _ in ROLE_FLAGS)
                or settings.get("cost")
                or settings.get("duration")
                or settings.get("exclusive_to")
                or settings.get("inclusive_with")
                or settings.get("required")
                or settings.get("reactions")
            )
            if not interesting:
                continue
            rows.append(
                {
                    "id": str(role.id),
                    "name": role.name,
                    "colour": f"#{role.colour.value:06x}" if role.colour.value else "#99aab5",
                    "members": len(role.members),
                    "cost": settings.get("cost") or 0,
                    "flags": {k: bool(settings.get(k)) for k, _ in ROLE_FLAGS},
                    "exclusive": self._rt_names(guild, settings.get("exclusive_to") or []),
                    "inclusive": self._rt_names(guild, settings.get("inclusive_with") or []),
                    "required": self._rt_names(guild, settings.get("required") or []),
                    "require_any": bool(settings.get("require_any")),
                    "duration": self._rt_duration_text(settings.get("duration")),
                    "reactions": len(settings.get("reactions") or []),
                    # The bot cannot hand out a role at or above its own top role.
                    "unmanageable": bool(guild.me and role.position >= guild.me.top_role.position),
                }
            )
        return sorted(rows, key=lambda r: r["name"].lower())

    # ----------------------------------------------------------------- posts --

    async def _rt_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

        if action in ("save_atomic", "cleanup_reactions", "clear_reactions"):
            return await self._rt_settings_action(action, guild, field)

        if action in ("pick_channel", "bind_reaction", "unbind_reaction"):
            return await self._rt_reaction_action(action, guild, field)

        if action in ("give", "remove", "force", "force_remove", "view"):
            return await self._rt_role_action(action, guild, field)

        raw_role = field("role") or ""
        if not raw_role.isdigit():
            return [{"message": "Pick a role.", "category": "warning"}]
        role = guild.get_role(int(raw_role))
        if role is None:
            return [{"message": "That role no longer exists.", "category": "danger"}]

        conf = self.config.role(role)

        try:
            if action == "save_role":
                warnings: list[dict] = []
                if guild.me and role.position >= guild.me.top_role.position:
                    warnings.append(
                        {
                            "message": f"'{role.name}' is at or above my highest role, "
                            f"so I will not be able to assign it.",
                            "category": "warning",
                        }
                    )
                for key, _label in ROLE_FLAGS:
                    await conf.get_attr(key).set(field.checked(f"f_{key}"))
                # `auto` on its own does nothing: the join handler reads the
                # guild's own list, so the two have to be kept in step.
                await self._rt_set_auto(guild, role, field.checked("f_auto"))

                raw_cost = (field("cost") or "").strip()
                if raw_cost:
                    try:
                        await conf.cost.set(max(0, int(raw_cost)))
                    except ValueError:
                        warnings.append(
                            {"message": f"Cost '{raw_cost}' is not a number.", "category": "danger"}
                        )
                else:
                    await conf.cost.set(0)

                for key, form_key in (
                    ("exclusive_to", "exclusive"),
                    ("inclusive_with", "inclusive"),
                    ("required", "required"),
                ):
                    ids = [int(x) for x in field.many(form_key) if str(x).isdigit()]
                    # Self-references silently break assignment logic.
                    ids = [i for i in ids if i != role.id]
                    await conf.get_attr(key).set(ids)

                # Whether the required roles mean ALL of them or any one.
                await conf.require_any.set(field("require_mode") == "any")

                # A temporary duration is stored on the role as whole seconds,
                # so it round-trips through the same humanised text the commands
                # print. The guild-level list is a queue of pending removals and
                # is deliberately left alone.
                raw_duration = (field("duration") or "").strip()
                if raw_duration:
                    delta = parse_timedelta(raw_duration)
                    if delta is None:
                        warnings.append(
                            {
                                "message": f"'{raw_duration}' is not a duration I understand. "
                                "Try something like '30 minutes', '2 hours' or '7 days'.",
                                "category": "danger",
                            }
                        )
                    elif delta.total_seconds() < 300:
                        warnings.append(
                            {
                                "message": "The removal loop only runs every 5 minutes, so "
                                "anything shorter than that will not be honoured. The "
                                "duration was not changed.",
                                "category": "warning",
                            }
                        )
                    else:
                        await conf.duration.set(int(delta.total_seconds()))
                else:
                    await conf.duration.clear()

                return warnings + [
                    {"message": f"Saved settings for '{role.name}'.", "category": "success"}
                ]

            if action == "load_role":
                # Nothing to save: the page rebuilds with this role filled in.
                return [{"message": f"Editing '{role.name}'.", "category": "info"}]

            if action == "clear_role":
                for key, _label in ROLE_FLAGS:
                    await conf.get_attr(key).set(False)
                await self._rt_set_auto(guild, role, False)
                await conf.cost.set(0)
                await conf.require_any.set(False)
                await conf.duration.clear()
                for key in ("exclusive_to", "inclusive_with", "required"):
                    await conf.get_attr(key).set([])
                return [
                    {
                        "message": f"Cleared '{role.name}'. Its reaction bindings were left "
                        "alone; remove those below if you want them gone too.",
                        "category": "success",
                    }
                ]
        except Exception as exc:  # noqa: BLE001
            log.exception("RoleTools dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    async def _rt_set_auto(
        self, guild: discord.Guild, role: discord.Role, enabled: bool
    ) -> None:
        """Keep `role.auto` and the guild's auto_roles list in step."""
        async with self.config.guild(guild).auto_roles() as current:
            if enabled and role.id not in current:
                current.append(role.id)
            elif not enabled and role.id in current:
                current.remove(role.id)
        listed = (await self._rt_cache(guild)).setdefault("auto_roles", [])
        if enabled and role.id not in listed:
            listed.append(role.id)
        elif not enabled and role.id in listed:
            listed.remove(role.id)

    # ------------------------------------------------------------- reactions --

    async def _rt_reaction_action(
        self, action: str, guild: discord.Guild, field
    ) -> list[dict]:
        try:
            if action == "pick_channel":
                raw = (field("channel") or "").strip()
                channel = guild.get_channel(int(raw)) if raw.isdigit() else None
                if channel is None:
                    return [{"message": "Pick a channel first.", "category": "warning"}]
                return [
                    {
                        "message": f"Showing the last {MESSAGE_HISTORY} messages in "
                        f"#{channel.name}.",
                        "category": "info",
                    }
                ]

            if action == "bind_reaction":
                return await self._rt_bind(guild, field)

            if action == "unbind_reaction":
                return await self._rt_unbind(guild, field)
        except Exception as exc:  # noqa: BLE001
            log.exception("RoleTools dashboard reaction action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    async def _rt_bind(self, guild: discord.Guild, field) -> list[dict]:
        raw_channel = (field("channel") or "").strip()
        channel = guild.get_channel(int(raw_channel)) if raw_channel.isdigit() else None
        if not isinstance(channel, discord.TextChannel):
            return [{"message": "Pick a channel.", "category": "warning"}]

        raw_message = (field("message") or "").strip()
        if not raw_message.isdigit():
            return [
                {
                    "message": "Pick a message. Press 'Show messages' if the list is empty.",
                    "category": "warning",
                }
            ]

        raw_role = (field("bind_role") or "").strip()
        role = guild.get_role(int(raw_role)) if raw_role.isdigit() else None
        if role is None:
            return [{"message": "Pick the role to hand out.", "category": "warning"}]
        if guild.me and role.position >= guild.me.top_role.position:
            return [
                {
                    "message": f"'{role.name}' is at or above my highest role, so I could "
                    "never hand it out. Move my role above it first.",
                    "category": "danger",
                }
            ]

        emoji, key_part, problem = self._rt_read_emoji(field)
        if problem:
            return [{"message": problem, "category": "danger"}]

        try:
            message = await channel.fetch_message(int(raw_message))
        except discord.NotFound:
            return [{"message": "That message no longer exists.", "category": "danger"}]
        except discord.Forbidden:
            return [
                {"message": f"I cannot read messages in #{channel.name}.", "category": "danger"}
            ]

        key = f"{channel.id}-{message.id}-{key_part}"
        existing = (await self.config.guild(guild).reaction_roles()).get(key)
        if existing is not None:
            old = guild.get_role(int(existing))
            return [
                {
                    "message": f"That emoji on that message already hands out "
                    f"{old.name if old else existing}. Remove it first to change it.",
                    "category": "warning",
                }
            ]

        out: list[dict] = []
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException as exc:
            if getattr(exc, "code", 0) in (10014, 50035):
                return [
                    {
                        "message": f"Discord rejected that emoji: {emoji_problem(str(emoji))}. "
                        "Nothing was saved.",
                        "category": "danger",
                    }
                ]
            out.append(
                {
                    "message": "I could not add the reaction myself, so put it on the "
                    "message by hand or the binding will never fire.",
                    "category": "warning",
                }
            )

        async with self.config.guild(guild).reaction_roles() as bindings:
            bindings[key] = role.id
        async with self.config.role(role).reactions() as reactions:
            if key not in reactions:
                reactions.append(key)
        (await self._rt_cache(guild)).setdefault("reaction_roles", {})[key] = role.id

        if field.checked("make_selfassignable"):
            await self.config.role(role).selfassignable.set(True)
            await self.config.role(role).selfremovable.set(True)
        elif not await self.config.role(role).selfassignable():
            out.append(
                {
                    "message": f"'{role.name}' is not self-assignable, so reacting will not "
                    "actually grant it. Tick the box, or set the flag above.",
                    "category": "warning",
                }
            )

        return out + [
            {
                "message": f"Reacting on that message in #{channel.name} now grants "
                f"'{role.name}'.",
                "category": "success",
            }
        ]

    @staticmethod
    def _rt_read_emoji(field) -> tuple[t.Any, str, str]:
        """The chosen emoji as (thing to react with, key fragment, problem).

        Custom emoji arrive from the picker as `<:name:id>` tokens; anything
        typed is treated as unicode. The key fragment has to match what
        `events.py` builds from a raw reaction payload, or nothing ever fires.
        """
        token = (field("emoji_token") or "").strip()
        typed = (field("emoji_text") or "").strip()
        if token:
            partial = discord.PartialEmoji.from_str(token)
            if partial.id is None:
                return None, "", "That custom emoji could not be read. Pick it again."
            return partial, str(partial.id), ""
        if not typed:
            return None, "", "Choose a custom emoji or type one."
        raw = typed.strip("\N{VARIATION SELECTOR-16}")
        looks_wrong = (
            raw.startswith(("http://", "https://"))
            or raw.isascii()
            or (len(raw) > 2 and raw.startswith(":") and raw.endswith(":"))
        )
        if looks_wrong:
            return None, "", f"That will not work as an emoji: {emoji_problem(raw)}."
        return raw, raw, ""

    async def _rt_unbind(self, guild: discord.Guild, field) -> list[dict]:
        key = (field("key") or "").strip()
        if not key:
            return [{"message": "Nothing to remove.", "category": "warning"}]

        async with self.config.guild(guild).reaction_roles() as bindings:
            role_id = bindings.pop(key, None)
        if role_id is None:
            return [{"message": "That binding is already gone.", "category": "info"}]

        (await self._rt_cache(guild)).get("reaction_roles", {}).pop(key, None)
        async with self.config.role_from_id(int(role_id)).reactions() as reactions:
            if key in reactions:
                reactions.remove(key)

        await self._rt_clear_reaction(guild, key)
        role = guild.get_role(int(role_id))
        return [
            {
                "message": f"Removed the binding for '{role.name if role else role_id}'.",
                "category": "success",
            }
        ]

    async def _rt_clear_reaction(self, guild: discord.Guild, key: str) -> None:
        """Take the now-meaningless reaction off the message, if we still can."""
        channel_id, message_id, emoji = self._rt_split_key(key)
        if not (channel_id.isdigit() and message_id.isdigit()):
            return
        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            return
        target = self.bot.get_emoji(int(emoji)) if emoji.isdigit() else emoji
        if target is None:
            return
        try:
            message = await channel.fetch_message(int(message_id))
            await message.clear_reaction(target)
        except discord.HTTPException:
            # Failing to tidy up is never a reason to fail the removal itself.
            pass

    # --------------------------------------------------------- handing roles --

    def _rt_targets(self, guild: discord.Guild, field) -> list[discord.Member]:
        """Members chosen individually plus any of the everyone/here/bots/humans groups."""
        members: dict[int, discord.Member] = {}
        for raw in field.many("member_ids"):
            if str(raw).isdigit() and (found := guild.get_member(int(raw))):
                members[found.id] = found
        for group in field.many("groups"):
            if group == "everyone":
                for m in guild.members:
                    members[m.id] = m
            elif group == "here":
                for m in guild.members:
                    if str(m.status) == "online":
                        members[m.id] = m
            elif group == "bots":
                for m in guild.members:
                    if m.bot:
                        members[m.id] = m
            elif group == "humans":
                for m in guild.members:
                    if not m.bot:
                        members[m.id] = m
        for raw in field.many("target_role_ids"):
            if str(raw).isdigit() and (role := guild.get_role(int(raw))):
                for m in role.members:
                    members[m.id] = m
        return list(members.values())

    async def _rt_role_action(
        self, action: str, guild: discord.Guild, field
    ) -> list[dict]:
        raw_role = field("target_role") or ""
        if not raw_role.isdigit():
            return [{"message": "Pick a role to act on.", "category": "warning"}]
        role = guild.get_role(int(raw_role))
        if role is None:
            return [{"message": "That role no longer exists.", "category": "danger"}]

        if action == "view":
            holders = sorted(role.members, key=lambda m: m.display_name.lower())
            return [
                {
                    "message": f"{len(holders)} member(s) have {role.name}"
                    + (": " + ", ".join(m.display_name for m in holders[:60]) if holders else "."),
                    "category": "info",
                }
            ]

        if guild.me and role.position >= guild.me.top_role.position:
            return [
                {
                    "message": f"{role.name} is at or above my highest role, so I "
                    "cannot assign it.",
                    "category": "warning",
                }
            ]

        targets = self._rt_targets(guild, field)
        if not targets:
            return [{"message": "Pick at least one member.", "category": "warning"}]

        reason = (field("reason") or "").strip() or "Changed from the dashboard"
        done = 0
        failed = 0
        try:
            for target in targets:
                try:
                    if action == "give":
                        # Goes through the exclusive/inclusive checks, like the command.
                        await self.give_roles(target, [role], reason=reason)
                    elif action == "remove":
                        await self.remove_roles(target, [role], reason=reason)
                    elif action == "force":
                        async with self.config.member(target).sticky_roles() as sticky:
                            if role.id not in sticky:
                                sticky.append(role.id)
                        await self.give_roles(target, [role], reason="Forced Sticky Role")
                    else:
                        async with self.config.member(target).sticky_roles() as sticky:
                            if role.id in sticky:
                                sticky.remove(role.id)
                        await self.remove_roles(
                            target, [role], reason="Force removed Sticky Role"
                        )
                except discord.HTTPException:
                    failed += 1
                    continue
                done += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("RoleTools dashboard role action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        verb = {
            "give": "given",
            "remove": "removed from",
            "force": "force-applied to",
            "force_remove": "force-removed from",
        }[action]
        out = [
            {"message": f"{role.name} {verb} {done} member(s).", "category": "success"}
        ]
        if failed:
            out.append(
                {"message": f"{failed} member(s) could not be updated.",
                 "category": "warning"}
            )
        return out

    # ------------------------------------------------------------- behaviour --

    async def _rt_settings_action(
        self, action: str, guild: discord.Guild, field
    ) -> list[dict]:
        try:
            if action == "save_atomic":
                choice = field("atomic") or "default"
                if choice == "default":
                    await self.config.guild(guild).atomic.clear()
                    value = None
                    message = "This server now follows the global atomic setting."
                else:
                    value = choice == "yes"
                    await self.config.guild(guild).atomic.set(value)
                    message = (
                        "Roles are now assigned one at a time."
                        if value
                        else "Roles are now assigned in a single call."
                    )
                (await self._rt_cache(guild))["atomic"] = value
                if field.raw.get("global_atomic") is not None:
                    await self.config.atomic.set(field.checked("global_atomic"))
                return [{"message": message, "category": "success"}]

            if action == "clear_reactions":
                raw_message = (field("message") or "").strip()
                if not raw_message.isdigit():
                    return [
                        {"message": "Pick the message to clear first.", "category": "warning"}
                    ]
                removed = await self._rt_drop_bindings(
                    guild, lambda key, role_id: self._rt_split_key(key)[1] == raw_message
                )
                return [
                    {"message": f"Removed {removed} binding(s) from that message.",
                     "category": "success"}
                ]

            if action == "cleanup_reactions":
                # Drop bindings whose role or channel is gone, the way
                # `[p]roletools reactroles cleanup` does.
                def dead(key: str, role_id) -> bool:
                    channel_id, _message_id, _emoji = self._rt_split_key(key)
                    if guild.get_role(int(role_id)) is None:
                        return True
                    return bool(channel_id) and guild.get_channel(int(channel_id)) is None

                removed = await self._rt_drop_bindings(guild, dead)
                return [
                    {
                        "message": f"Removed {removed} binding(s) pointing at something that "
                        "no longer exists.",
                        "category": "success",
                    }
                ]
        except Exception as exc:  # noqa: BLE001
            log.exception("RoleTools dashboard settings action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    async def _rt_drop_bindings(self, guild: discord.Guild, matches) -> int:
        """Delete every binding `matches(key, role_id)` accepts.

        Config, the role's own reaction list and the in-memory cache all have to
        agree, or a removed binding keeps firing until the cog is reloaded.
        """
        doomed: list[tuple[str, int]] = []
        async with self.config.guild(guild).reaction_roles() as bindings:
            for key, role_id in list(bindings.items()):
                try:
                    if not matches(key, role_id):
                        continue
                except Exception:  # noqa: BLE001 - a malformed key must not stop the sweep
                    continue
                del bindings[key]
                doomed.append((key, int(role_id)))

        cache = (await self._rt_cache(guild)).get("reaction_roles", {})
        for key, role_id in doomed:
            cache.pop(key, None)
            async with self.config.role_from_id(role_id).reactions() as reactions:
                if key in reactions:
                    reactions.remove(key)
        return len(doomed)


ROLETOOLS_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<style>
  .rt-msg { display:flex; align-items:center; gap:9px; flex-wrap:wrap;
            margin-bottom:7px; }
  .rt-msg code { font-size:.72rem; opacity:.5; }
  .rt-emoji { width:22px; height:22px; vertical-align:-5px; }
  .rt-bind { padding:11px 13px; border-radius:12px; margin-bottom:10px;
             background:rgba(0,0,0,.18); border:1px solid rgba(255,255,255,.07); }
  .rt-bind:last-child { margin-bottom:0; }
</style>

<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-users"></i> RoleTools in {{ guild_name }}</h4>
    <p>
      Everything below takes effect straight away &mdash; there is no need to run
      the Discord commands as well.
    </p>
  </div>

  {{ stats([
      ('managed roles', managed|length),
      ('reaction messages', reactions.groups|length),
      ('button sets', components.buttons|length),
      ('select menus', components.menus|length),
      ('auto on join', auto_roles|length),
      ('temporary', temporary_roles|length),
  ]) }}

  <div class="dz-panel" id="rt-role">
    <h5><i class="fa fa-pencil"></i> Configure a role</h5>
    {% if editing %}
      <p class="dz-hint">
        Editing <b style="color:{{ editing.colour }};">{{ editing.name }}</b>.
        These are its current settings; change what you need and save.
        {% if editing.unmanageable %}
          <span style="color:#f0aa3c;">
            This role sits above mine, so I cannot hand it out.
          </span>
        {% endif %}
      </p>
    {% else %}
      <p class="dz-hint">
        Pick a role and press <b>Load</b> to see how it is set up now, or use the
        <b>Edit</b> link on any row in the table below.
      </p>
    {% endif %}

    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />

      <div class="dz-label">Role</div>
      {{ picker('role', editing.self if editing else roles, false, 8,
                'Search roles...', true, 'pick a role to configure') }}

      <div class="dz-row" style="margin-top:10px;">
        {% for f in flags %}
          <label class="dz-toggle">
            <input type="checkbox" name="f_{{ f.key }}"
                   {% if editing and editing.flags[f.key] %}checked{% endif %} />
            <span>{{ f.label }}</span>
          </label>
        {% endfor %}
      </div>

      <div class="dz-grid two" style="margin-top:10px;">
        <div>
          <div class="dz-label">Cost to self-assign (0 = free)</div>
          <input class="dz-input" type="number" min="0" name="cost"
                 value="{{ editing.cost if editing else 0 }}" />
        </div>
        <div>
          <div class="dz-label">Remove automatically after</div>
          <input class="dz-input" type="text" name="duration"
                 placeholder="e.g. 30 minutes, 2 hours, 7 days - blank for permanent"
                 value="{{ editing.duration if editing else '' }}" />
          <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
            Only applies when RoleTools hands the role out. The removal loop runs
            every 5 minutes, so nothing shorter than that is worth setting.
          </div>
        </div>
      </div>

      <div class="dz-grid two" style="margin-top:12px;">
        <div>
          <div class="dz-label">Exclusive to</div>
          {{ picker('exclusive', editing.exclusive if editing else roles, true, 6,
                    'Search roles...') }}
          <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
            Cannot be held alongside these. Granting this one takes them away.
          </div>
        </div>
        <div>
          <div class="dz-label">Required</div>
          {{ picker('required', editing.required if editing else roles, true, 6,
                    'Search roles...') }}
          <div class="dz-row" style="margin-top:6px; align-items:center; gap:7px;">
            <span style="font-size:.72rem; opacity:.55; flex:none;">Member</span>
            <select class="dz-select" name="require_mode">
              {% for value, label in require_modes %}
                <option value="{{ value }}"
                        {% if editing and editing.require_any == value %}selected{% endif %}>
                  {{ label }}
                </option>
              {% endfor %}
            </select>
          </div>
        </div>
      </div>

      <div class="dz-label" style="margin-top:12px;">Inclusive with</div>
      {{ picker('inclusive', editing.inclusive if editing else roles, true, 5,
                'Search roles...') }}
      <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
        Granted together with this role.
      </div>

      <div class="dz-row" style="margin-top:13px;">
        <button class="dz-btn" name="action" value="load_role">
          <i class="fa fa-folder-open"></i> Load
        </button>
        <button class="dz-btn primary" name="action" value="save_role">
          <i class="fa fa-save"></i> Save role
        </button>
        {{ confirm('Clear role', 'clear_role',
                   'Clear every RoleTools setting on this role?', 'danger', 'fa-eraser') }}
      </div>
    </form>
  </div>

  <div class="dz-panel">
    <h5><i class="fa fa-list"></i> Managed roles</h5>
    {% if managed %}
      <div style="overflow-x:auto;">
      <table class="dz-t" style="min-width:760px;">
        <thead>
          <tr><th>Role</th><th>Members</th><th>Flags</th><th>Cost</th>
              <th>Expires</th><th>Reactions</th><th>Requirements</th><th></th></tr>
        </thead>
        <tbody>
          {% for r in managed %}
            <tr>
              <td>
                <span style="display:inline-block; width:9px; height:9px; border-radius:50%;
                             background:{{ r.colour }}; margin-right:7px;"></span>
                <b>{{ r.name }}</b>
                {% if r.unmanageable %}
                  <span class="dz-tag warn">above my role</span>
                {% endif %}
              </td>
              <td style="opacity:.7;">{{ r.members }}</td>
              <td>
                {% for key, on in r.flags.items() %}
                  {% if on %}<span class="dz-tag">{{ key }}</span> {% endif %}
                {% endfor %}
              </td>
              <td style="opacity:.7;">{% if r.cost %}{{ r.cost }}{% else %}-{% endif %}</td>
              <td style="opacity:.7; font-size:.78rem;">
                {% if r.duration %}{{ r.duration }}{% else %}-{% endif %}
              </td>
              <td style="opacity:.7;">{% if r.reactions %}{{ r.reactions }}{% else %}-{% endif %}</td>
              <td style="font-size:.78rem; opacity:.7;">
                {% if r.required %}
                  needs {{ 'any of' if r.require_any else 'all of' }}:
                  {{ r.required|join(", ") }}<br>
                {% endif %}
                {% if r.exclusive %}not with: {{ r.exclusive|join(", ") }}<br>{% endif %}
                {% if r.inclusive %}with: {{ r.inclusive|join(", ") }}{% endif %}
                {% if not r.required and not r.exclusive and not r.inclusive %}-{% endif %}
              </td>
              <td style="text-align:right; white-space:nowrap;">
                <a class="dz-btn" href="?edit={{ r.id }}#rt-role"
                   title="Load this role into the form above">
                  <i class="fa fa-pencil"></i> Edit
                </a>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
      </div>
    {% else %}
      <p class="dz-empty">No roles configured yet.</p>
    {% endif %}
  </div>

  <div class="dz-panel">
    <h5><i class="fa fa-smile-o"></i> Reaction roles</h5>
    <p class="dz-hint">
      Pick the channel, pick the message from the list, pick an emoji and a role.
      I add the reaction for you, so nobody has to copy message IDs out of Discord.
    </p>

    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />

      <div class="dz-grid two">
        <div>
          <div class="dz-label">Channel</div>
          {{ picker('channel', reactions.channels, false, 8, 'Search channels...',
                    true, 'pick a channel') }}
          <div class="dz-row" style="margin-top:8px;">
            <button class="dz-btn" name="action" value="pick_channel">
              <i class="fa fa-refresh"></i> Show messages
            </button>
          </div>
        </div>
        <div>
          <div class="dz-label">Message</div>
          {% if reactions.messages %}
            {{ picker('message', reactions.messages, false, 8,
                      'Search messages...', true, 'pick a message') }}
          {% else %}
            <select class="dz-select" name="message" disabled>
              <option>
                {% if reactions.channel_id %}
                  nothing to show
                {% else %}
                  choose a channel first
                {% endif %}
              </option>
            </select>
          {% endif %}
          {% if reactions.note %}
            <div style="font-size:.72rem; color:#f0aa3c; margin-top:5px;">
              {{ reactions.note }}
            </div>
          {% elif reactions.channel_name %}
            <div style="font-size:.72rem; opacity:.45; margin-top:5px;">
              The last {{ reactions.messages|length }} messages in
              {{ reactions.channel_name }}, newest first.
            </div>
          {% endif %}
        </div>
      </div>

      <div class="dz-grid two" style="margin-top:12px;">
        <div>
          <div class="dz-label">Emoji from this server</div>
          {{ picker('emoji_token', reactions.emojis, false, 8, 'Search emoji...',
                    true, 'none - I will type one') }}
          <div class="dz-label" style="margin-top:9px;">Or type any emoji</div>
          <input class="dz-input" type="text" name="emoji_text"
                 placeholder="paste a single emoji, e.g. 🎉" />
        </div>
        <div>
          <div class="dz-label">Role to grant</div>
          {{ picker('bind_role', roles, false, 8, 'Search roles...',
                    true, 'pick a role') }}
          <label class="dz-toggle" style="margin-top:9px;">
            <input type="checkbox" name="make_selfassignable" checked />
            <span>
              Also let members give and take this role themselves
              <span style="opacity:.55;">(required, or reacting does nothing)</span>
            </span>
          </label>
        </div>
      </div>

      <div class="dz-row" style="margin-top:13px;">
        <button class="dz-btn primary" name="action" value="bind_reaction">
          <i class="fa fa-plus"></i> Create binding
        </button>
        {{ confirm('Clear this message', 'clear_reactions',
                   'Remove every reaction role binding on the selected message?') }}
        {{ confirm('Remove dead bindings', 'cleanup_reactions',
                   'Remove bindings whose role or channel no longer exists?',
                   '', 'fa-magic') }}
      </div>
    </form>
  </div>

  <div class="dz-panel">
    <h5><i class="fa fa-link"></i> Existing bindings</h5>
    {% if reactions.groups %}
      {% for g in reactions.groups %}
        <div class="rt-bind">
          <div class="rt-msg">
            <b {% if g.missing_channel %}style="color:#ff8b8b;"{% endif %}>{{ g.channel }}</b>
            <code>{{ g.message_id }}</code>
            {% if g.jump %}
              <a class="dz-tag" href="{{ g.jump }}" target="_blank" rel="noopener">
                open in Discord
              </a>
            {% endif %}
          </div>
          <table class="dz-t">
            <tbody>
              {% for r in g.rows %}
                <tr>
                  <td style="width:56px;">
                    {% if r.custom %}
                      <img class="rt-emoji" src="{{ r.emoji_url }}" alt="" loading="lazy" />
                    {% else %}
                      <span style="font-size:1.1rem;">{{ r.emoji }}</span>
                    {% endif %}
                  </td>
                  <td {% if r.broken %}style="color:#ff8b8b;"{% endif %}>{{ r.role }}</td>
                  <td style="text-align:right;">
                    <form method="POST" style="display:inline;">
                      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                      <input type="hidden" name="key" value="{{ r.key }}" />
                      <button class="dz-btn danger" name="action" value="unbind_reaction"
                              title="Remove this binding and clear the reaction">
                        <i class="fa fa-times"></i> Remove
                      </button>
                    </form>
                  </td>
                </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      {% endfor %}
    {% else %}
      <p class="dz-empty">No reaction roles bound yet.</p>
    {% endif %}
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-user-plus"></i> Hand out a role</h5>
      <p class="dz-hint">
        Give and remove run the exclusive and inclusive rules, so other roles may
        change too. Force applies a sticky role that only a manual removal undoes.
      </p>
      <div class="dz-grid two">
        <div>
          <label class="dz-label">Role</label>
          {{ picker('target_role', roles, false, 8, 'Search roles...') }}
          <label class="dz-label" style="margin-top:10px;">Reason</label>
          <input class="dz-input" type="text" name="reason"
                 placeholder="shown in the audit log" />
        </div>
        <div>
          <label class="dz-label">Members</label>
          {{ picker('member_ids', member_options, true, 8, 'Search members...') }}
          <label class="dz-label" style="margin-top:10px;">Or whole groups</label>
          <div class="dz-grid two">
            {% for key, label in targets %}
              <label class="dz-toggle">
                <input type="checkbox" name="groups" value="{{ key }}" />
                <span>{{ label }}</span>
              </label>
            {% endfor %}
          </div>
          <label class="dz-label" style="margin-top:10px;">Or everyone with a role</label>
          {{ picker('target_role_ids', roles, true, 5, 'Search roles...') }}
        </div>
      </div>
      <div class="dz-row dz-save">
        <button class="dz-btn primary" name="action" value="give">
          <i class="fa fa-plus"></i> Give role
        </button>
        <button class="dz-btn" name="action" value="remove">
          <i class="fa fa-minus"></i> Remove role
        </button>
        <button class="dz-btn" name="action" value="view">
          <i class="fa fa-eye"></i> Who has it
        </button>
        {{ confirm('Force sticky', 'force',
                   'Force this role on the selected members as a sticky role?',
                   '', 'fa-thumb-tack') }}
        {{ confirm('Force remove', 'force_remove',
                   'Force-remove this sticky role from the selected members?') }}
      </div>
    </div>
  </form>

  <div class="dz-grid two">
    <div class="dz-panel">
      <h5><i class="fa fa-hand-pointer-o"></i> Button sets</h5>
      <p class="dz-hint">
        Built with the <code>roletools buttons</code> commands. Listed here so you
        can see what exists and which point at a deleted role.
      </p>
      {% if components.buttons %}
        <table class="dz-t">
          <thead><tr><th>Name</th><th>Role</th><th>On messages</th></tr></thead>
          <tbody>
            {% for b in components.buttons %}
              <tr>
                <td><b>{{ b.label }}</b>
                  <span style="opacity:.45; font-size:.74rem;">{{ b.name }}</span></td>
                <td {% if b.broken %}style="color:#ff8b8b;"{% endif %}>{{ b.role }}</td>
                <td style="opacity:.7;">{{ b.messages }}</td>
              </tr>
            {% endfor %}
          </tbody>
        </table>
      {% else %}
        <p class="dz-empty">No button sets.</p>
      {% endif %}
    </div>

    <div class="dz-panel">
      <h5><i class="fa fa-caret-square-o-down"></i> Select menus</h5>
      <p class="dz-hint">Built with the <code>roletools select</code> commands.</p>
      {% if components.menus %}
        <table class="dz-t">
          <thead><tr><th>Name</th><th>Options</th><th>On messages</th></tr></thead>
          <tbody>
            {% for m in components.menus %}
              <tr>
                <td><b>{{ m.name }}</b>
                  {% if m.placeholder %}
                    <div style="opacity:.45; font-size:.74rem;">{{ m.placeholder }}</div>
                  {% endif %}
                </td>
                <td style="opacity:.7; font-size:.78rem;">
                  {{ m.options|join(", ") if m.options else "-" }}
                </td>
                <td style="opacity:.7;">{{ m.messages }}</td>
              </tr>
            {% endfor %}
          </tbody>
        </table>
      {% else %}
        <p class="dz-empty">No select menus.</p>
      {% endif %}
    </div>
  </div>

  <div class="dz-grid two">
    <div class="dz-panel">
      <h5><i class="fa fa-magic"></i> Automatic and temporary</h5>
      <div class="dz-label">Granted on join</div>
      {% if auto_roles %}
        {% for name in auto_roles %}<span class="dz-tag">{{ name }}</span> {% endfor %}
      {% else %}
        <p class="dz-hint" style="margin:0;">
          None. Tick <b>Auto-granted on join</b> on a role above.
        </p>
      {% endif %}

      <div class="dz-label" style="margin-top:14px;">Removed after a while</div>
      {% if temporary_roles %}
        {% for r in temporary_roles %}
          <span class="dz-tag">{{ r.name }} &middot; {{ r.duration }}</span>
        {% endfor %}
      {% else %}
        <p class="dz-hint" style="margin:0;">
          None. Set <b>Remove automatically after</b> on a role above.
        </p>
      {% endif %}

      {% if pending_temporary %}
        <div class="dz-label" style="margin-top:14px;">Waiting to be removed</div>
        <table class="dz-t">
          <thead><tr><th>Member</th><th>Role</th></tr></thead>
          <tbody>
            {% for p in pending_temporary %}
              <tr><td style="opacity:.75;">{{ p.member }}</td><td>{{ p.role }}</td></tr>
            {% endfor %}
          </tbody>
        </table>
      {% endif %}
    </div>

    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-cogs"></i> Assignment behaviour</h5>
        <p class="dz-hint">
          Atomic assignment applies roles one at a time. It is slower but avoids
          race conditions when something else also changes roles.
        </p>
        <div class="dz-label">In this server</div>
        <select class="dz-select" name="atomic" style="max-width:320px;">
          <option value="default" {% if atomic is none %}selected{% endif %}>
            Follow the global setting
          </option>
          <option value="yes" {% if atomic is true %}selected{% endif %}>
            One role at a time
          </option>
          <option value="no" {% if atomic is false %}selected{% endif %}>
            All at once
          </option>
        </select>
        {% if is_owner %}
          <label class="dz-toggle" style="margin-top:8px;">
            <input type="checkbox" name="global_atomic"
                   {% if global_atomic %}checked{% endif %} />
            <span>Global default: one at a time</span>
          </label>
        {% endif %}
        <div class="dz-row" style="margin-top:10px;">
          <button class="dz-btn primary" name="action" value="save_atomic">
            <i class="fa fa-save"></i> Save
          </button>
        </div>
      </div>
    </form>
  </div>
</div>
"""
)
