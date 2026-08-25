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

log = logging.getLogger("red.infochannel.dashboard")

# Human-readable blurb for each counter the cog supports.
COUNTER_HELP = {
    "members": "Everyone, humans and bots.",
    "humans": "Members that are not bots.",
    "boosters": "Members currently boosting.",
    "bots": "Bot accounts.",
    "roles": "Number of roles in the server.",
    "channels": "Number of channels in the server.",
    "online": "Members showing as online.",
    "offline": "Members showing as offline.",
}


class DashboardIntegration:
    """Toggle counter channels and edit their name templates."""

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering InfoChannel as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Configure the server counter channels.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_infochannel_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        if not await is_staff(self.bot, user, member, guild):
            return {
                "status": 1,
                "error_title": "Forbidden",
                "error_message": "Only server administrators can change counter channels.",
            }

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._ic_handle_post(guild, kwargs)

        data = await self.config.guild(guild).all()
        names = data.get("channel_names") or {}
        enabled = data.get("enabled_channels") or {}
        ids = data.get("channel_ids") or {}

        counters = []
        for key in self.default_channel_names:
            channel = guild.get_channel(ids.get(key)) if ids.get(key) else None
            counters.append(
                {
                    "key": key,
                    "label": key.capitalize(),
                    "help": COUNTER_HELP.get(key, ""),
                    "enabled": bool(enabled.get(key)),
                    "template": names.get(key) or self.default_channel_names[key],
                    "channel": channel.name if channel else None,
                    # An enabled counter with no live channel means the channel
                    # was deleted manually in Discord.
                    "orphaned": bool(enabled.get(key)) and channel is None,
                }
            )

        category = guild.get_channel(data.get("category_id")) if data.get("category_id") else None

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": INFOCHANNEL_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "counters": counters,
                "category": category.name if category else None,
                "active": sum(1 for c in counters if c["enabled"]),
            },
        }

    async def _ic_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        if field("action") != "save":
            return [{"message": "Unknown action.", "category": "warning"}]

        errors: list[dict] = []
        conf = self.config.guild(guild)

        async with conf.channel_names() as names:
            for key in self.default_channel_names:
                template = (field(f"n_{key}") or "").strip()
                if not template:
                    continue
                if "{count}" not in template:
                    errors.append(
                        {
                            "message": f"{key}: template must contain {{count}} - left unchanged.",
                            "category": "warning",
                        }
                    )
                    continue
                # Discord truncates channel names at 100 characters.
                names[key] = template[:100]

        # Toggling is applied through the cog so that channels are actually
        # created or deleted, rather than only flipping a Config flag.
        async with conf.enabled_channels() as enabled:
            for key in self.default_channel_names:
                enabled[key] = field.checked(f"t_{key}")

        try:
            await self.update_infochannel(guild)
        except Exception as exc:  # noqa: BLE001
            log.exception("InfoChannel update failed after a dashboard save")
            errors.append(
                {"message": f"Saved, but refreshing the channels failed: {exc}", "category": "warning"}
            )

        return errors + [{"message": "Counter settings saved.", "category": "success"}]


INFOCHANNEL_TEMPLATE = (
    BASE_CSS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-bar-chart"></i> Counter channels in {{ guild_name }}</h4>
    <p>
      {{ active }} active{% if category %} &middot; category: <b>{{ category }}</b>{% endif %}.
      Discord rate-limits channel renames, so counters refresh at most every 5 minutes.
    </p>
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />

    <div class="dz-grid two">
      {% for c in counters %}
        <div class="dz-panel">
          <div style="display:flex; align-items:center; justify-content:space-between; gap:10px;">
            <label class="dz-toggle" style="padding:0;">
              <input type="checkbox" name="t_{{ c.key }}" {% if c.enabled %}checked{% endif %} />
              <b>{{ c.label }}</b>
            </label>
            {% if c.orphaned %}
              <span class="dz-tag" style="color:#f0aa3c;">channel missing</span>
            {% elif c.channel %}
              <span class="dz-tag">{{ c.channel }}</span>
            {% endif %}
          </div>
          <p class="dz-hint" style="margin:7px 0 8px;">{{ c.help }}</p>
          <input class="dz-input" type="text" name="n_{{ c.key }}" value="{{ c.template }}"
                 maxlength="100" />
          <div style="font-size:.72rem; opacity:.45; margin-top:5px;">
            Must include <code>{count}</code>
          </div>
        </div>
      {% endfor %}
    </div>

    <div class="dz-save">
      <button class="dz-btn primary" name="action" value="save">
        <i class="fa fa-save"></i> Save counters
      </button>
    </div>
  </form>
</div>
"""
)
