from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    channel_options,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
    role_options,
)

log = logging.getLogger("red.admin.dashboard")


class DashboardIntegration:
    """Self-assignable roles and the announcement channel."""

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Admin as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Selfroles and the announcement channel.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_admin_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        if not await is_staff(self.bot, user, member, guild):
            return {
                "status": 1,
                "error_title": "Forbidden",
                "error_message": "Only server administrators can change these settings.",
            }

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._admin_handle_post(guild, kwargs)

        settings = await self.config.guild(guild).all()
        selfrole_ids = settings.get("selfroles") or []

        selfroles = []
        for role_id in selfrole_ids:
            role = guild.get_role(role_id)
            selfroles.append(
                {
                    "id": str(role_id),
                    "name": role.name if role else f"(deleted {role_id})",
                    "colour": f"#{role.colour.value:06x}" if role and role.colour.value else "#99aab5",
                    "members": len(role.members) if role else 0,
                    "broken": role is None,
                    # Assigning a role at or above the bot's top role always fails.
                    "unmanageable": bool(
                        role and guild.me and role.position >= guild.me.top_role.position
                    ),
                }
            )

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": ADMIN_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "selfroles": sorted(selfroles, key=lambda r: r["name"].lower()),
                "roles": [
                    r for r in role_options(guild) if r["id"] not in {s["id"] for s in selfroles}
                ],
                "channels": channel_options(guild, selected=settings.get("announce_channel")),
                "announce_set": settings.get("announce_channel") is not None,
            },
        }

    async def _admin_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")
        conf = self.config.guild(guild)

        try:
            if action == "add_selfrole":
                raw = field("role") or ""
                if not raw.isdigit():
                    return [{"message": "Pick a role.", "category": "warning"}]
                role = guild.get_role(int(raw))
                if role is None:
                    return [{"message": "That role no longer exists.", "category": "danger"}]

                warnings = []
                if guild.me and role.position >= guild.me.top_role.position:
                    warnings.append(
                        {
                            "message": f"'{role.name}' is at or above my highest role, "
                            f"so members will not actually receive it.",
                            "category": "warning",
                        }
                    )
                async with conf.selfroles() as selfroles:
                    if role.id in selfroles:
                        return [{"message": "Already a selfrole.", "category": "warning"}]
                    selfroles.append(role.id)
                return warnings + [
                    {"message": f"'{role.name}' is now self-assignable.", "category": "success"}
                ]

            if action == "remove_selfrole":
                raw = field("role") or ""
                if not raw.isdigit():
                    return [{"message": "Bad role id.", "category": "danger"}]
                role_id = int(raw)
                async with conf.selfroles() as selfroles:
                    if role_id not in selfroles:
                        return [{"message": "Not a selfrole.", "category": "warning"}]
                    selfroles.remove(role_id)
                role = guild.get_role(role_id)
                return [
                    {
                        "message": f"'{role.name if role else role_id}' removed.",
                        "category": "success",
                    }
                ]

            if action == "save_announce":
                raw = field("announce_channel") or ""
                channel_id = int(raw) if raw.isdigit() else None
                warnings = []
                if channel_id is not None:
                    channel = guild.get_channel(channel_id)
                    if channel is not None and not channel.permissions_for(guild.me).send_messages:
                        warnings.append(
                            {
                                "message": f"I cannot send messages in #{channel.name}.",
                                "category": "warning",
                            }
                        )
                await conf.announce_channel.set(channel_id)
                return warnings + [
                    {"message": "Announcement channel saved.", "category": "success"}
                ]
        except Exception as exc:  # noqa: BLE001
            log.exception("Admin dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]


ADMIN_TEMPLATE = (
    BASE_CSS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-shield"></i> Admin for {{ guild_name }}</h4>
    <p>
      {{ selfroles|length }} self-assignable role(s) &middot;
      {% if announce_set %}announcements configured{% else %}no announcement channel{% endif %}
    </p>
  </div>

  <div class="dz-panel">
    <h5><i class="fa fa-user-plus"></i> Selfroles</h5>
    <p class="dz-hint">Members can grant these to themselves with the selfrole command.</p>

    {% if selfroles %}
      <table class="dz-t">
        <thead><tr><th>Role</th><th>Members</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {% for r in selfroles %}
            <tr>
              <td>
                <span style="display:inline-block; width:9px; height:9px; border-radius:50%;
                             background:{{ r.colour }}; margin-right:7px;"></span>
                <b>{{ r.name }}</b>
              </td>
              <td style="opacity:.7;">{{ r.members }}</td>
              <td>
                {% if r.broken %}<span class="dz-tag" style="color:#ff8b8b;">deleted</span>
                {% elif r.unmanageable %}<span class="dz-tag" style="color:#f0aa3c;">above my role</span>
                {% else %}<span class="dz-tag">ok</span>{% endif %}
              </td>
              <td style="width:1%;">
                <form method="POST">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="role" value="{{ r.id }}" />
                  <button class="dz-btn round danger" name="action" value="remove_selfrole"
                          title="Remove"><i class="fa fa-times"></i></button>
                </form>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="dz-empty">No selfroles yet.</p>
    {% endif %}

    <form method="POST" class="dz-row" style="margin-top:11px;">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <select class="dz-select" style="flex:1 1 220px;" name="role">
        {% for r in roles %}<option value="{{ r.id }}">{{ r.name }}</option>{% endfor %}
      </select>
      <button class="dz-btn primary" name="action" value="add_selfrole">
        <i class="fa fa-plus"></i> Add selfrole
      </button>
    </form>
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-bullhorn"></i> Announcements</h5>
      <p class="dz-hint">Where bot-owner announcements are posted in this server.</p>
      <div class="dz-row">
        <select class="dz-select" style="flex:1 1 260px;" name="announce_channel">
          <option value="">&mdash; none &mdash;</option>
          {% for c in channels %}
            <option value="{{ c.id }}" {% if c.selected %}selected{% endif %}>{{ c.name }}</option>
          {% endfor %}
        </select>
        <button class="dz-btn primary" name="action" value="save_announce">
          <i class="fa fa-save"></i> Save
        </button>
      </div>
    </div>
  </form>
</div>
"""
)
