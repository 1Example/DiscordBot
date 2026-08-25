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
    role_options,
)

log = logging.getLogger("red.roletools.dashboard")

# Per-role flags stored in config.role(...), with the label shown on the page.
ROLE_FLAGS = (
    ("selfassignable", "Members can self-assign"),
    ("selfremovable", "Members can self-remove"),
    ("sticky", "Sticky (reapplied on rejoin)"),
    ("auto", "Auto-granted on join"),
)


class DashboardIntegration:
    """Self-assignable roles, requirements and reaction-role overview."""

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

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": ROLETOOLS_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "managed": managed,
                "roles": role_options(guild),
                "flags": [{"key": k, "label": v} for k, v in ROLE_FLAGS],
                "reaction_roles": self._rt_reaction_summary(guild, guild_data),
                "auto_roles": self._rt_names(guild, guild_data.get("auto_roles") or []),
                "temporary_roles": self._rt_names(guild, guild_data.get("temporary_roles") or []),
                "button_count": len(guild_data.get("buttons") or {}),
                "select_count": len(guild_data.get("select_menus") or {}),
                "bot_top": guild.me.top_role.position if guild.me else 0,
            },
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
            interesting = any(settings.get(k) for k, _ in ROLE_FLAGS) or settings.get("cost")
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
                    # The bot cannot hand out a role at or above its own top role.
                    "unmanageable": bool(guild.me and role.position >= guild.me.top_role.position),
                }
            )
        return sorted(rows, key=lambda r: r["name"].lower())

    async def _rt_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

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

                return warnings + [
                    {"message": f"Saved settings for '{role.name}'.", "category": "success"}
                ]

            if action == "clear_role":
                for key, _label in ROLE_FLAGS:
                    await conf.get_attr(key).set(False)
                await conf.cost.set(0)
                for key in ("exclusive_to", "inclusive_with", "required"):
                    await conf.get_attr(key).set([])
                return [{"message": f"Cleared '{role.name}'.", "category": "success"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("RoleTools dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]


ROLETOOLS_TEMPLATE = (
    BASE_CSS
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

  <div class="dz-panel">
    <h5><i class="fa fa-pencil"></i> Configure a role</h5>
    <p class="dz-hint">Pick a role, set its flags, then save.</p>
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />

      <div class="dz-label">Role</div>
      <select class="dz-select" name="role">
        {% for r in roles %}<option value="{{ r.id }}">{{ r.name }}</option>{% endfor %}
      </select>

      <div class="dz-row" style="margin-top:10px;">
        {% for f in flags %}
          <label class="dz-toggle">
            <input type="checkbox" name="f_{{ f.key }}" />
            <span>{{ f.label }}</span>
          </label>
        {% endfor %}
      </div>

      <div class="dz-label" style="margin-top:10px;">Cost to self-assign (0 = free)</div>
      <input class="dz-input" type="number" min="0" name="cost" value="0" />

      <div class="dz-grid two" style="margin-top:12px;">
        <div>
          <div class="dz-label">Exclusive to</div>
          <select class="dz-select" name="exclusive" multiple size="5">
            {% for r in roles %}<option value="{{ r.id }}">{{ r.name }}</option>{% endfor %}
          </select>
          <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
            Cannot be held alongside these.
          </div>
        </div>
        <div>
          <div class="dz-label">Required</div>
          <select class="dz-select" name="required" multiple size="5">
            {% for r in roles %}<option value="{{ r.id }}">{{ r.name }}</option>{% endfor %}
          </select>
          <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
            Member must already have these.
          </div>
        </div>
      </div>

      <div class="dz-label" style="margin-top:12px;">Inclusive with</div>
      <select class="dz-select" name="inclusive" multiple size="4">
        {% for r in roles %}<option value="{{ r.id }}">{{ r.name }}</option>{% endfor %}
      </select>
      <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
        Granted together with this role.
      </div>

      <div class="dz-row" style="margin-top:13px;">
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

  <div class="dz-panel">
    <h5><i class="fa fa-list"></i> Managed roles</h5>
    {% if managed %}
      <table class="dz-t">
        <thead>
          <tr><th>Role</th><th>Members</th><th>Flags</th><th>Cost</th><th>Requirements</th></tr>
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
              <td style="font-size:.78rem; opacity:.7;">
                {% if r.required %}needs: {{ r.required|join(", ") }}<br>{% endif %}
                {% if r.exclusive %}not with: {{ r.exclusive|join(", ") }}<br>{% endif %}
                {% if r.inclusive %}with: {{ r.inclusive|join(", ") }}{% endif %}
                {% if not r.required and not r.exclusive and not r.inclusive %}-{% endif %}
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
