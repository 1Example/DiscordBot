from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands

from .converters import CogOrCommand

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    channel_options,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
    member_options,
    role_options,
)

log = logging.getLogger("red.permissions.dashboard")

COG = "COG"
COMMAND = "COMMAND"


class DashboardIntegration:
    """Create, edit and clear this server's permission rules.

    Writes go through the cog's own ``_add_rule`` / ``_remove_rule`` /
    ``_set_default_rule``, which update the live rule objects on the cog or
    command as well as config. Touching config directly would leave the running
    bot enforcing the old rules until a reload.

    Ordering and multi-guild defaults are still YAML-only; this page covers the
    per-rule editing that ``[p]permissions addguildrule`` and friends do.
    """

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Permissions as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Permission rules for this server.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_permissions_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        if not await is_staff(self.bot, user, member, guild):
            return {
                "status": 1,
                "error_title": "Forbidden",
                "error_message": "Only server administrators can view permission rules.",
            }

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._perm_handle_post(guild, kwargs)

        cogs = await self._perm_rules(guild, COG)
        commands_ = await self._perm_rules(guild, COMMAND)

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": PERMISSIONS_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "cogs": cogs,
                "commands": commands_,
                "total": sum(len(g["rules"]) for g in cogs + commands_),
                "subjects": self._perm_subject_options(),
                "targets": self._perm_target_options(guild),
            },
        }

    def _perm_subject_options(self) -> list[dict]:
        """Every cog and command a rule can be attached to.

        The value carries the category so one select covers both, and
        `_RuleDropper` cogs and commands are left out for the same reason the
        `CogOrCommand` converter rejects them: rules on them do nothing.
        """
        out: list[dict] = []
        for name, cog in self.bot.cogs.items():
            if isinstance(cog, commands.commands._RuleDropper):
                continue
            out.append({"id": f"{COG}:{name}", "name": name, "group": "Cogs"})
        for command in self.bot.walk_commands():
            if isinstance(command, commands.commands._RuleDropper):
                continue
            out.append(
                {
                    "id": f"{COMMAND}:{command.qualified_name}",
                    "name": command.qualified_name,
                    "group": "Commands",
                }
            )
        out.sort(key=lambda o: (o["group"], o["name"].lower()))
        return out

    def _perm_target_options(self, guild: discord.Guild) -> list[dict]:
        """Who a rule applies to: the server default, or a role/member/channel."""
        out = [{"id": "default", "name": "everyone (server default)", "group": "Default"}]
        for role in role_options(guild, skip_default=False):
            out.append({"id": role["id"], "name": role["name"], "group": "Roles"})
        for channel in channel_options(
            guild, kinds=("text", "voice", "stage", "forum", "category")
        ):
            out.append({"id": channel["id"], "name": f"#{channel['name']}",
                        "group": "Channels"})
        for member in member_options(guild):
            out.append({"id": member["id"], "name": member["name"], "group": "Members"})
        return out

    def _perm_subject(self, category: str, name: str):
        """Resolve a (category, name) pair back to the live cog or command.

        Returns None when it is gone - unloaded cog, removed command - which is
        a normal state for a stored rule, not an error.
        """
        if category == COG:
            return self.bot.get_cog(name)
        return self.bot.get_command(name)

    async def _perm_rules(self, guild: discord.Guild, category: str) -> list[dict]:
        """Rules stored for this guild, resolved to readable names.

        Config layout is custom(category, name) -> {guild_id: {model_id: bool}},
        where a model_id of "default" is the catch-all for that target.
        """
        try:
            everything = await self.config.custom(category).all()
        except Exception:  # noqa: BLE001
            log.exception("Could not read %s rules", category)
            return []

        groups = []
        for name, scopes in sorted(everything.items()):
            rules_for_guild = (scopes or {}).get(str(guild.id)) or {}
            if not rules_for_guild:
                continue
            rules = []
            for model_id, allowed in rules_for_guild.items():
                rules.append(
                    {
                        "model_id": str(model_id),
                        "target": self._perm_target(guild, model_id),
                        "allowed": bool(allowed),
                    }
                )
            rules.sort(key=lambda r: (r["model_id"] != "default", r["target"].lower()))
            groups.append({"name": name, "category": category, "rules": rules})
        return groups

    def _perm_target(self, guild: discord.Guild, model_id: str) -> str:
        if str(model_id) == "default":
            return "everyone (default)"
        try:
            target_id = int(model_id)
        except (TypeError, ValueError):
            return str(model_id)
        role = guild.get_role(target_id)
        if role is not None:
            return f"role: {role.name}"
        member = guild.get_member(target_id)
        if member is not None:
            return f"member: {member.display_name}"
        channel = guild.get_channel(target_id)
        if channel is not None:
            return f"channel: #{channel.name}"
        if target_id == guild.id:
            return "this server"
        return f"unknown ({target_id})"

    async def _perm_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

        # `set_rule` carries its subject in one combined "CATEGORY:name" value so
        # a single picker can list cogs and commands together; every other action
        # is dispatched from a row that already knows both halves.
        if action == "set_rule":
            subject = (field("subject") or "").strip()
            category, _sep, name = subject.partition(":")
        else:
            category = field("category")
            name = (field("name") or "").strip()

        if category not in (COG, COMMAND):
            return [{"message": "Unknown rule category.", "category": "warning"}]
        if not name:
            return [{"message": "Pick a cog or command.", "category": "warning"}]

        try:
            if action == "set_rule":
                target = (field("target") or "").strip()
                rule = field("rule")
                if not target:
                    return [{"message": "Pick who the rule applies to.",
                             "category": "warning"}]
                if rule not in ("allow", "deny", "unset"):
                    return [{"message": "Pick allow, deny or unset.",
                             "category": "warning"}]
                return await self._perm_write(guild, category, name, target, rule)

            if action == "toggle_rule":
                target = (field("model_id") or "").strip()
                # The row posts what the rule is now; flip it.
                rule = "deny" if field("allowed") == "1" else "allow"
                return await self._perm_write(guild, category, name, target, rule)

            if action == "clear_rule":
                target = (field("model_id") or "").strip()
                return await self._perm_write(guild, category, name, target, "unset")

            if action == "clear_all_for":
                cleared = await self._perm_clear_subject(guild, category, name)
                if not cleared:
                    return [{"message": "No rules to clear.", "category": "warning"}]
                return [{"message": f"{cleared} rule(s) cleared from '{name}'.",
                         "category": "success"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("Permissions dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    async def _perm_write(
        self, guild: discord.Guild, category: str, name: str, target: str, rule: str
    ) -> list[dict]:
        """Apply one rule change through the cog's own rule handling.

        `_add_rule` and friends update the live `commands.Command` / `Cog` rule
        objects *and* config together. Writing config on its own would leave the
        running bot enforcing the previous rules, which is what made this page
        read-only before.
        """
        obj = self._perm_subject(category, name)
        if obj is None:
            kind = "cog" if category == COG else "command"
            return [{"message": f"The {kind} '{name}' is not loaded right now.",
                     "category": "warning"}]

        cog_or_cmd = CogOrCommand(type=category, name=name, obj=obj)
        readable = self._perm_target(guild, target)

        if target == "default":
            # The server default is its own concept: None means "no default",
            # which is not the same as denying everyone.
            value = None if rule == "unset" else (rule == "allow")
            await self._set_default_rule(value, cog_or_cmd, guild.id)
            if value is None:
                return [{"message": f"Default rule for '{name}' removed.",
                         "category": "success"}]
            return [{"message": f"'{name}' now defaults to {rule} in this server.",
                     "category": "success"}]

        try:
            model_id = int(target)
        except (TypeError, ValueError):
            return [{"message": "That target is not a valid ID.", "category": "warning"}]

        if rule == "unset":
            await self._remove_rule(cog_or_cmd=cog_or_cmd, model_id=model_id,
                                    guild_id=guild.id)
            return [{"message": f"Rule for {readable} cleared from '{name}'.",
                     "category": "success"}]

        await self._add_rule(
            rule=(rule == "allow"), cog_or_cmd=cog_or_cmd, model_id=model_id,
            guild_id=guild.id,
        )
        return [{"message": f"'{name}' now has {rule} for {readable}.",
                 "category": "success"}]

    async def _perm_clear_subject(
        self, guild: discord.Guild, category: str, name: str
    ) -> int:
        """Drop every rule this server has for one cog or command."""
        scopes = await self.config.custom(category, name).all()
        existing = list((scopes.get(str(guild.id)) or {}).keys())
        if not existing:
            return 0
        for model_id in existing:
            await self._perm_write(guild, category, name, model_id, "unset")
        return len(existing)

PERMISSIONS_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-key"></i> Permission rules in {{ guild_name }}</h4>
    <p>
      {{ total }} rule(s) across {{ cogs|length }} cog(s) and
      {{ commands|length }} command(s).
    </p>
  </div>

  <div class="dz-panel">
    <h5><i class="fa fa-plus-circle"></i> Add or change a rule</h5>
    <p class="dz-hint">
      Sets this server's rules and its default. Rule <b>ordering</b> and
      global rules are still YAML-only &mdash; upload an ACL for those.
    </p>
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-grid two">
        <div>
          <label class="dz-label">Cog or command</label>
          {{ picker("subject", subjects, placeholder="Search cogs and commands...") }}
        </div>
        <div>
          <label class="dz-label">Applies to</label>
          {{ picker("target", targets, placeholder="Search roles, channels, members...") }}
        </div>
      </div>
      <div class="dz-row" style="margin-top:14px;">
        <button class="dz-btn primary" name="rule" value="allow">
          <i class="fa fa-check"></i> Allow
        </button>
        <button class="dz-btn danger" name="rule" value="deny">
          <i class="fa fa-ban"></i> Deny
        </button>
        <button class="dz-btn" name="rule" value="unset"
                title="Remove any rule for this pair">
          <i class="fa fa-eraser"></i> Unset
        </button>
        <input type="hidden" name="action" value="set_rule" />
      </div>
    </form>
  </div>

  {% for section, groups in [("Cogs", cogs), ("Commands", commands)] %}
    <div class="dz-panel">
      <h5>
        <i class="fa fa-{% if section == 'Cogs' %}cubes{% else %}terminal{% endif %}"></i>
        {{ section }}
      </h5>
      {% if groups %}
        {% for g in groups %}
          <div style="padding:11px 0; border-top:1px solid rgba(255,255,255,.06);">
            <div class="dz-row" style="justify-content:space-between;">
              <b>{{ g.name }}</b>
              <form method="POST"
                    onsubmit="return confirm('Clear every rule for {{ g.name }}?');">
                <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                <input type="hidden" name="category" value="{{ g.category }}" />
                <input type="hidden" name="name" value="{{ g.name }}" />
                <button class="dz-btn danger" name="action" value="clear_all_for">
                  <i class="fa fa-eraser"></i> Clear all
                </button>
              </form>
            </div>
            <table class="dz-t" style="margin-top:7px;">
              <thead><tr><th>Applies to</th><th>Rule</th><th></th></tr></thead>
              <tbody>
                {% for r in g.rules %}
                  <tr>
                    <td>{{ r.target }}</td>
                    <td>
                      {# The badge is the button: clicking it flips the rule. #}
                      <form method="POST" style="display:inline;">
                        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                        <input type="hidden" name="category" value="{{ g.category }}" />
                        <input type="hidden" name="name" value="{{ g.name }}" />
                        <input type="hidden" name="model_id" value="{{ r.model_id }}" />
                        <input type="hidden" name="allowed" value="{{ 1 if r.allowed else 0 }}" />
                        <button class="dz-btn" name="action" value="toggle_rule"
                                style="height:30px; padding:0 11px; font-size:.75rem;
                                       color:{{ '#3ba55d' if r.allowed else '#ff8b8b' }};"
                                title="Switch to {{ 'deny' if r.allowed else 'allow' }}">
                          {{ 'allow' if r.allowed else 'deny' }}
                          <i class="fa fa-exchange" style="opacity:.55;"></i>
                        </button>
                      </form>
                    </td>
                    <td style="width:1%;">
                      <form method="POST">
                        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                        <input type="hidden" name="category" value="{{ g.category }}" />
                        <input type="hidden" name="name" value="{{ g.name }}" />
                        <input type="hidden" name="model_id" value="{{ r.model_id }}" />
                        <button class="dz-btn round danger" name="action" value="clear_rule"
                                title="Clear this rule"><i class="fa fa-times"></i></button>
                      </form>
                    </td>
                  </tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        {% endfor %}
      {% else %}
        <p class="dz-empty">No {{ section|lower }} rules in this server.</p>
      {% endif %}
    </div>
  {% endfor %}
</div>
"""
)
