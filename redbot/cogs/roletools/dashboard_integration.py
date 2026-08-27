from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands

from redbot.core.utils.chat_formatting import humanize_timedelta
from redbot.core.commands import parse_timedelta
from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    member_options,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
    role_options,
)

log = logging.getLogger("red.roletools.dashboard")

# Per-role flags stored in config.role(...), with the label shown on the page.
# Shorthands `[p]roletools giverole` accepts for whole groups of members.
TARGET_GROUPS = (
    ("everyone", "Everyone in the server"),
    ("here", "Everyone currently online"),
    ("humans", "All humans"),
    ("bots", "All bots"),
)

ROLE_FLAGS = (
    ("selfassignable", "Members can self-assign"),
    ("selfremovable", "Members can self-remove"),
    ("sticky", "Sticky (reapplied on rejoin)"),
    ("auto", "Auto-granted on join"),
)


class DashboardIntegration:
    """RoleTools, end to end.

    Configures each role (self-assign, sticky, auto, cost, exclusive/inclusive/
    required sets), hands roles out with ``[p]roletools giverole``,
    ``removerole``, ``forcerole`` and ``forceroleremove``, shows who holds a role
    (``viewroles``), and covers the atomic assignment settings and the reaction
    role cleanup commands.
    """

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering RoleTools as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Manage self-assignable, sticky and automatic roles.",
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
        # Whatever role the form is pointed at: the one just saved, or the one
        # an Edit button asked for. Its current values pre-fill the form so a
        # change is an edit rather than a retype.
        editing = await self._rt_editing(guild, kwargs)

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
                "require_modes": (
                    ("all", "must have every one of them"),
                    ("any", "must have at least one of them"),
                ),
                "components": self._rt_components(guild, guild_data),
                "flags": [{"key": k, "label": v} for k, v in ROLE_FLAGS],
                "reaction_roles": self._rt_reaction_summary(guild, guild_data),
                "auto_roles": self._rt_names(guild, guild_data.get("auto_roles") or []),
                "temporary_roles": self._rt_names(guild, guild_data.get("temporary_roles") or []),
                "button_count": len(guild_data.get("buttons") or {}),
                "select_count": len(guild_data.get("select_menus") or {}),
                "bot_top": guild.me.top_role.position if guild.me else 0,
                "member_options": member_options(guild),
                "is_owner": await self.bot.is_owner(user),
                "atomic": guild_data.get("atomic"),
                "global_atomic": await self.config.atomic(),
                "targets": TARGET_GROUPS,
            },
        }

    async def _rt_editing(self, guild: discord.Guild, kwargs: dict) -> dict:
        """The role the form should show, with its settings already filled in.

        Returns {} when nothing is selected, which leaves the form blank for
        setting up a role that has never been configured.
        """
        field = form_reader(kwargs)
        raw = (field("role") or "").strip()
        if not raw.isdigit():
            return {}
        role = guild.get_role(int(raw))
        if role is None:
            return {}
        settings = await self.config.role(role).all()
        duration = settings.get("duration")
        return {
            "id": str(role.id),
            "name": role.name,
            # The role list itself, with this role already chosen, so the
            # picker keeps its selection across a save.
            "self": role_options(guild, selected=role.id),
            "colour": f"#{role.colour.value:06x}" if role.colour.value else "#99aab5",
            "flags": {key: bool(settings.get(key)) for key, _label in ROLE_FLAGS},
            "cost": settings.get("cost") or 0,
            "require_any": "any" if settings.get("require_any") else "all",
            "duration": self._rt_duration_text(duration),
            "exclusive": role_options(
                guild, selected_many=settings.get("exclusive_to") or []
            ),
            "required": role_options(guild, selected_many=settings.get("required") or []),
            "inclusive": role_options(
                guild, selected_many=settings.get("inclusive_with") or []
            ),
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

    def _rt_components(self, guild: discord.Guild, data: dict) -> dict:
        """Button sets and select menus, which the page previously only counted.

        They are built with the Discord commands, so this lists them and allows
        deleting - enough to see what exists and clear out what does not work,
        without re-implementing the whole builder on the web.
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

    def _rt_reaction_summary(self, guild: discord.Guild, data: dict) -> list[dict]:
        """reaction_roles maps 'messageid-emoji' to a role id."""
        rows = []
        for key, role_id in (data.get("reaction_roles") or {}).items():
            role = guild.get_role(int(role_id)) if str(role_id).isdigit() else None
            message_id, _, emoji = str(key).partition("-")
            rows.append(
                {
                    "message_id": message_id,
                    "emoji": emoji,
                    "role": role.name if role else f"(deleted {role_id})",
                    "broken": role is None,
                }
            )
        return sorted(rows, key=lambda r: r["message_id"])

    async def _rt_managed_roles(self, guild: discord.Guild) -> list[dict]:
        """Every role with at least one RoleTools flag or requirement set."""
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
            interesting = (
                any(settings.get(k) for k, _ in ROLE_FLAGS)
                or settings.get("cost")
                or settings.get("duration")
                or settings.get("exclusive_to")
                or settings.get("inclusive_with")
                or settings.get("required")
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
                    # The bot cannot hand out a role at or above its own top role.
                    "unmanageable": bool(guild.me and role.position >= guild.me.top_role.position),
                }
            )
        return sorted(rows, key=lambda r: r["name"].lower())

    async def _rt_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

        if action in ("save_atomic", "cleanup_reactions", "clear_reactions"):
            return await self._rt_settings_action(action, guild, field)

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

                # Temporary roles. Stored as whole seconds, so it round-trips
                # through the same humanised text the commands print.
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
                    elif delta.total_seconds() < 60:
                        warnings.append(
                            {
                                "message": "A temporary role shorter than a minute will not "
                                "work reliably; leave it blank for permanent.",
                                "category": "warning",
                            }
                        )
                    else:
                        await conf.duration.set(int(delta.total_seconds()))
                        async with self.config.guild(guild).temporary_roles() as temp:
                            if role.id not in temp:
                                temp.append(role.id)
                else:
                    await conf.duration.clear()
                    async with self.config.guild(guild).temporary_roles() as temp:
                        if role.id in temp:
                            temp.remove(role.id)

                return warnings + [
                    {"message": f"Saved settings for '{role.name}'.", "category": "success"}
                ]

            if action == "load_role":
                # Nothing to save - the page rebuilds with this role filled in.
                return [
                    {"message": f"Editing '{role.name}'.", "category": "info"}
                ]

            if action == "clear_role":
                for key, _label in ROLE_FLAGS:
                    await conf.get_attr(key).set(False)
                await conf.cost.set(0)
                await conf.require_any.set(False)
                await conf.duration.clear()
                for key in ("exclusive_to", "inclusive_with", "required"):
                    await conf.get_attr(key).set([])
                async with self.config.guild(guild).temporary_roles() as temp:
                    if role.id in temp:
                        temp.remove(role.id)
                return [{"message": f"Cleared '{role.name}'.", "category": "success"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("RoleTools dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]


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

    async def _rt_settings_action(
        self, action: str, guild: discord.Guild, field
    ) -> list[dict]:
        try:
            if action == "save_atomic":
                choice = field("atomic") or "default"
                if choice == "default":
                    await self.config.guild(guild).atomic.clear()
                    message = "This server now follows the global atomic setting."
                else:
                    await self.config.guild(guild).atomic.set(choice == "yes")
                    message = (
                        "Roles are now assigned one at a time."
                        if choice == "yes"
                        else "Roles are now assigned in a single call."
                    )
                if field.raw.get("global_atomic") is not None:
                    await self.config.atomic.set(field.checked("global_atomic"))
                return [{"message": message, "category": "success"}]

            if action == "clear_reactions":
                message_id = field.integer("message_id", 0) or 0
                if not message_id:
                    return [{"message": "Enter a message ID.", "category": "warning"}]
                removed = 0
                async with self.config.guild(guild).reaction_roles() as bindings:
                    for key in list(bindings):
                        if str(key).partition("-")[0] == str(message_id):
                            del bindings[key]
                            removed += 1
                return [
                    {"message": f"Removed {removed} reaction binding(s) from that message.",
                     "category": "success"}
                ]

            if action == "cleanup_reactions":
                # Drop bindings whose message or role is gone, the way
                # `[p]roletools reactroles cleanup` does.
                removed = 0
                async with self.config.guild(guild).reaction_roles() as bindings:
                    for key, role_id in list(bindings.items()):
                        if guild.get_role(int(role_id)) is None:
                            del bindings[key]
                            removed += 1
                return [
                    {"message": f"Removed {removed} binding(s) pointing at deleted roles.",
                     "category": "success"}
                ]
        except Exception as exc:  # noqa: BLE001
            log.exception("RoleTools dashboard settings action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]


ROLETOOLS_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-users"></i> RoleTools in {{ guild_name }}</h4>
    <p>
      {{ managed|length }} managed role(s) &middot;
      {{ reaction_roles|length }} reaction binding(s) &middot;
      {{ button_count }} button set(s) &middot; {{ select_count }} select menu(s)
    </p>
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

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-cogs"></i> Assignment behaviour</h5>
      <p class="dz-hint">
        Atomic assignment applies roles one at a time. It is slower but avoids
        race conditions when something else also changes roles.
      </p>
      <div class="dz-row">
        <select class="dz-select" name="atomic" style="max-width:280px;">
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
          <label class="dz-toggle">
            <input type="checkbox" name="global_atomic"
                   {% if global_atomic %}checked{% endif %} />
            <span>Global default: one at a time</span>
          </label>
        {% endif %}
        <button class="dz-btn primary" name="action" value="save_atomic">
          <i class="fa fa-save"></i> Save
        </button>
      </div>
      <label class="dz-label" style="margin-top:14px;">Reaction role maintenance</label>
      <div class="dz-row">
        <input class="dz-input" type="number" name="message_id"
               placeholder="message ID" style="max-width:220px;" />
        {{ confirm('Clear that message', 'clear_reactions',
                   'Remove every reaction role binding on that message?') }}
        {{ confirm('Remove dead bindings', 'cleanup_reactions',
                   'Remove reaction bindings whose role no longer exists?',
                   '', 'fa-broom') }}
      </div>
    </div>
  </form>

  <div class="dz-panel">
    <h5><i class="fa fa-pencil"></i> Configure a role</h5>
    <p class="dz-hint">Pick a role, set its flags, then save.</p>
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />

      <div class="dz-label">Role</div>
      {{ picker('role', editing.self if editing else roles, false, 8,
                'Search roles...', true, 'pick a role to configure') }}
      {% if editing %}
        <div style="font-size:.72rem; opacity:.55; margin-top:5px;">
          Showing the current settings for
          <b style="color:{{ editing.colour }};">{{ editing.name }}</b>.
          Change what you need and save.
        </div>
      {% else %}
        <div style="font-size:.72rem; opacity:.45; margin-top:5px;">
          Pick a role and press <b>Load</b> to see what it is set to now,
          or just fill this in to configure it from scratch.
        </div>
      {% endif %}

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
            Only applies when RoleTools hands the role out.
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
            <span style="font-size:.72rem; opacity:.55;">Member</span>
            <select class="dz-select" name="require_mode" style="width:auto; flex:1 1 190px;">
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
        <button class="dz-btn danger" name="action" value="clear_role"
                onclick="return confirm('Clear every RoleTools setting on this role?');">
          <i class="fa fa-eraser"></i> Clear role
        </button>
      </div>
    </form>
  </div>

  <div class="dz-grid two">
    <div class="dz-panel">
      <h5><i class="fa fa-hand-pointer-o"></i> Button sets</h5>
      <p class="dz-hint">
        Built with the <code>roletools buttons</code> commands. Listed here so you
        can see what exists and which are pointing at a deleted role.
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

  <div class="dz-panel">
    <h5><i class="fa fa-list"></i> Managed roles</h5>
    {% if managed %}
      <table class="dz-t">
        <thead>
          <tr><th>Role</th><th>Members</th><th>Flags</th><th>Cost</th>
              <th>Expires</th><th>Requirements</th><th></th></tr>
        </thead>
        <tbody>
          {% for r in managed %}
            <tr>
              <td>
                <span style="display:inline-block; width:9px; height:9px; border-radius:50%;
                             background:{{ r.colour }}; margin-right:7px;"></span>
                <b>{{ r.name }}</b>
                {% if r.unmanageable %}
                  <span class="dz-tag" style="color:#f0aa3c;">above my role</span>
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
                <form method="POST" style="display:inline;">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="role" value="{{ r.id }}" />
                  <button class="dz-btn" name="action" value="load_role"
                          title="Load this role into the form above">
                    <i class="fa fa-pencil"></i> Edit
                  </button>
                </form>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="dz-empty">No roles configured yet.</p>
    {% endif %}
  </div>

  <div class="dz-grid two">
    <div class="dz-panel">
      <h5><i class="fa fa-smile-o"></i> Reaction roles</h5>
      {% if reaction_roles %}
        <table class="dz-t">
          <thead><tr><th>Message</th><th>Emoji</th><th>Role</th></tr></thead>
          <tbody>
            {% for r in reaction_roles %}
              <tr>
                <td style="font-size:.78rem; opacity:.6;">{{ r.message_id }}</td>
                <td>{{ r.emoji }}</td>
                <td {% if r.broken %}style="color:#ff8b8b;"{% endif %}>{{ r.role }}</td>
              </tr>
            {% endfor %}
          </tbody>
        </table>
        <p class="dz-hint" style="margin-top:9px;">
          Bindings are created in Discord with the roletools reaction commands.
        </p>
      {% else %}
        <p class="dz-empty">No reaction roles bound.</p>
      {% endif %}
    </div>

    <div class="dz-panel">
      <h5><i class="fa fa-magic"></i> Automatic roles</h5>
      <div class="dz-label">Granted on join</div>
      {% if auto_roles %}
        {% for name in auto_roles %}<span class="dz-tag">{{ name }}</span> {% endfor %}
      {% else %}
        <p class="dz-hint" style="margin:0;">None.</p>
      {% endif %}
      <div class="dz-label" style="margin-top:12px;">Temporary roles</div>
      {% if temporary_roles %}
        {% for name in temporary_roles %}<span class="dz-tag">{{ name }}</span> {% endfor %}
      {% else %}
        <p class="dz-hint" style="margin:0;">None.</p>
      {% endif %}
    </div>
  </div>
</div>
"""
)
