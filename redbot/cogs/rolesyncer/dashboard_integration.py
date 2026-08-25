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

log = logging.getLogger("red.rolesyncer.dashboard")

# onesync: [source, target] - target follows source, one direction only.
# twosync: [a, b]           - either role mirrors onto the other.
MODES = {"onesync": "One-way", "twosync": "Two-way"}


class DashboardIntegration:
    """Visual editor for role synchronisation pairs."""

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering RoleSyncer as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Link roles so they stay in sync.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_rolesyncer_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        if not await is_staff(self.bot, user, member, guild):
            return {
                "status": 1,
                "error_title": "Forbidden",
                "error_message": "Only server administrators can edit role syncing.",
            }

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._rs_handle_post(guild, kwargs)

        data = await self.config.guild(guild).all()
        pairs = {mode: self._rs_render_pairs(guild, data.get(mode) or []) for mode in MODES}

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": ROLESYNCER_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "modes": MODES,
                "pairs": pairs,
                "roles": role_options(guild),
                "bot_top_role": guild.me.top_role.position if guild.me else 0,
            },
        }

    def _rs_render_pairs(self, guild: discord.Guild, raw: list) -> list[dict]:
        out = []
        for index, pair in enumerate(raw):
            try:
                first_id, second_id = pair[0], pair[1]
            except (TypeError, IndexError):
                continue
            first, second = guild.get_role(first_id), guild.get_role(second_id)
            # A deleted role leaves a dead entry; surface it so it can be removed.
            out.append(
                {
                    "index": index,
                    "first": first.name if first else f"(deleted {first_id})",
                    "second": second.name if second else f"(deleted {second_id})",
                    "broken": first is None or second is None,
                    # The bot cannot assign a role above its own highest role.
                    "unmanageable": bool(
                        second and guild.me and second.position >= guild.me.top_role.position
                    ),
                }
            )
        return out

    async def _rs_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")
        mode = field("mode")
        if mode not in MODES:
            return [{"message": "Unknown sync mode.", "category": "warning"}]

        try:
            if action == "add":
                return await self._rs_add(guild, mode, field("first"), field("second"))
            if action == "remove":
                return await self._rs_remove(guild, mode, field("index"))
        except Exception as exc:  # noqa: BLE001
            log.exception("RoleSyncer dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]
        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    async def _rs_add(self, guild, mode: str, first: str | None, second: str | None) -> list[dict]:
        try:
            first_id, second_id = int(first), int(second)
        except (TypeError, ValueError):
            return [{"message": "Pick two roles.", "category": "warning"}]
        if first_id == second_id:
            return [{"message": "A role cannot be synced with itself.", "category": "warning"}]

        first_role, second_role = guild.get_role(first_id), guild.get_role(second_id)
        if first_role is None or second_role is None:
            return [{"message": "One of those roles no longer exists.", "category": "danger"}]

        # Warn early rather than failing silently later in on_member_update.
        warnings = []
        if guild.me and second_role.position >= guild.me.top_role.position:
            warnings.append(
                {
                    "message": f"'{second_role.name}' sits above my highest role, "
                    f"so I will not be able to assign it.",
                    "category": "warning",
                }
            )

        async with self.config.guild(guild).get_attr(mode)() as pairs:
            if any(p[0] == first_id and p[1] == second_id for p in pairs):
                return [{"message": "That pair already exists.", "category": "warning"}]
            pairs.append([first_id, second_id])

        return warnings + [
            {
                "message": f"{MODES[mode]} sync added: {first_role.name} -> {second_role.name}.",
                "category": "success",
            }
        ]

    async def _rs_remove(self, guild, mode: str, index: str | None) -> list[dict]:
        try:
            position = int(index)
        except (TypeError, ValueError):
            return [{"message": "Bad pair index.", "category": "danger"}]
        async with self.config.guild(guild).get_attr(mode)() as pairs:
            if not 0 <= position < len(pairs):
                return [{"message": "That pair no longer exists.", "category": "warning"}]
            pairs.pop(position)
        return [{"message": "Pair removed.", "category": "success"}]


ROLESYNCER_TEMPLATE = (
    BASE_CSS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-link"></i> Role syncing in {{ guild_name }}</h4>
    <p>One-way: the second role follows the first. Two-way: either role mirrors the other.</p>
  </div>

  {% for mode, label in modes.items() %}
    <div class="dz-panel">
      <h5><i class="fa fa-{% if mode == 'onesync' %}long-arrow-right{% else %}exchange{% endif %}"></i>
          {{ label }} pairs</h5>
      <p class="dz-hint">{{ pairs[mode]|length }} configured.</p>

      {% if pairs[mode] %}
        <table class="dz-t">
          <thead><tr><th>From</th><th></th><th>To</th><th>Status</th><th></th></tr></thead>
          <tbody>
            {% for p in pairs[mode] %}
              <tr>
                <td><b>{{ p.first }}</b></td>
                <td style="opacity:.5;">
                  <i class="fa fa-{% if mode == 'onesync' %}long-arrow-right{% else %}exchange{% endif %}"></i>
                </td>
                <td><b>{{ p.second }}</b></td>
                <td>
                  {% if p.broken %}<span class="dz-tag" style="color:#ff8b8b;">deleted role</span>
                  {% elif p.unmanageable %}<span class="dz-tag" style="color:#f0aa3c;">above my role</span>
                  {% else %}<span class="dz-tag">active</span>{% endif %}
                </td>
                <td style="width:1%;">
                  <form method="POST">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                    <input type="hidden" name="mode" value="{{ mode }}" />
                    <input type="hidden" name="index" value="{{ p.index }}" />
                    <button class="dz-btn round danger" name="action" value="remove" title="Remove">
                      <i class="fa fa-times"></i>
                    </button>
                  </form>
                </td>
              </tr>
            {% endfor %}
          </tbody>
        </table>
      {% else %}
        <p class="dz-empty">No {{ label|lower }} pairs yet.</p>
      {% endif %}

      <form method="POST" class="dz-row" style="margin-top:11px;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <input type="hidden" name="mode" value="{{ mode }}" />
        <select class="dz-select" style="flex:1 1 190px;" name="first">
          {% for r in roles %}<option value="{{ r.id }}">{{ r.name }}</option>{% endfor %}
        </select>
        <span style="opacity:.5;">
          <i class="fa fa-{% if mode == 'onesync' %}long-arrow-right{% else %}exchange{% endif %}"></i>
        </span>
        <select class="dz-select" style="flex:1 1 190px;" name="second">
          {% for r in roles %}<option value="{{ r.id }}">{{ r.name }}</option>{% endfor %}
        </select>
        <button class="dz-btn primary" name="action" value="add">
          <i class="fa fa-plus"></i> Add pair
        </button>
      </form>
    </div>
  {% endfor %}
</div>
"""
)
