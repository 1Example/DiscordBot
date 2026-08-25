from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
)

log = logging.getLogger("red.permissions.dashboard")

COG = "COG"
COMMAND = "COMMAND"


class DashboardIntegration:
    """View the permission rules that apply in this server, and clear them."""

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
            },
        }

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
        category = field("category")
        name = (field("name") or "").strip()

        if category not in (COG, COMMAND):
            return [{"message": "Unknown rule category.", "category": "warning"}]

        try:
            if action == "clear_rule":
                model_id = (field("model_id") or "").strip()
                group = self.config.custom(category, name)
                async with group.all() as scopes:
                    guild_rules = scopes.get(str(guild.id))
                    if not guild_rules or model_id not in guild_rules:
                        return [{"message": "That rule no longer exists.", "category": "warning"}]
                    del guild_rules[model_id]
                    if not guild_rules:
                        del scopes[str(guild.id)]
                await self._perm_reload()
                return [{"message": f"Rule cleared from '{name}'.", "category": "success"}]

            if action == "clear_all_for":
                group = self.config.custom(category, name)
                async with group.all() as scopes:
                    if scopes.pop(str(guild.id), None) is None:
                        return [{"message": "No rules to clear.", "category": "warning"}]
                await self._perm_reload()
                return [{"message": f"All rules cleared from '{name}'.", "category": "success"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("Permissions dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    async def _perm_reload(self) -> None:
        """Push the edited rules back into the live permission cache."""
        loader = getattr(self, "_load_all_rules", None) or getattr(self, "initialize", None)
        if loader is None:
            log.warning("No rule reload hook found; a bot restart may be needed.")
            return
        try:
            await loader()
        except Exception:  # noqa: BLE001
            log.exception("Could not reload permission rules")


PERMISSIONS_TEMPLATE = (
    BASE_CSS
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
    <p class="dz-hint" style="margin:0;">
      Rules are read-only here apart from clearing. Create them in Discord with
      the permissions commands, or by uploading a YAML ACL &mdash; those support
      ordering and defaults this view cannot express safely.
    </p>
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
                      {% if r.allowed %}
                        <span class="dz-tag" style="color:#3ba55d;">allow</span>
                      {% else %}
                        <span class="dz-tag" style="color:#ff8b8b;">deny</span>
                      {% endif %}
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
