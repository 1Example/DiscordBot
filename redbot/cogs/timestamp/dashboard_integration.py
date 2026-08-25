from __future__ import annotations

import logging
import typing as t
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import BASE_CSS, dashboard_page, form_reader

log = logging.getLogger("red.timestamp.dashboard")

# Shown at the top of the picker; the full list follows.
COMMON = (
    "UTC", "Europe/London", "Europe/Bucharest", "Europe/Berlin", "Europe/Paris",
    "Europe/Madrid", "Europe/Moscow", "America/New_York", "America/Chicago",
    "America/Denver", "America/Los_Angeles", "America/Sao_Paulo", "Asia/Tokyo",
    "Asia/Shanghai", "Asia/Kolkata", "Asia/Dubai", "Australia/Sydney",
)

# Discord's timestamp styles, with what each renders as.
STYLES = (
    ("t", "Short time", "16:20"),
    ("T", "Long time", "16:20:30"),
    ("d", "Short date", "20/04/2021"),
    ("D", "Long date", "20 April 2021"),
    ("f", "Short date/time", "20 April 2021 16:20"),
    ("F", "Long date/time", "Tuesday, 20 April 2021 16:20"),
    ("R", "Relative", "2 months ago"),
)


class DashboardIntegration:
    """Personal timezone, used when the cog renders timestamps for you."""

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Timestamp as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Set the timezone used for your timestamps.",
        methods=("GET", "POST"),
        context_ids=["user_id"],
    )
    async def dashboard_timestamp_page(self, user: discord.User, **kwargs: t.Any) -> dict[str, t.Any]:
        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._ts_handle_post(user, kwargs)

        current = await self.config.user(user).timezone()
        # self.at is cached by the cog because listing zones opens many files.
        zones = sorted(getattr(self, "at", None) or [])

        preview, offset = self._ts_preview(current)

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": TIMESTAMP_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "current": current or "",
                "common": [z for z in COMMON if z in zones],
                "zones": zones,
                "count": len(zones),
                "preview": preview,
                "offset": offset,
                "styles": STYLES,
            },
        }

    @staticmethod
    def _ts_preview(zone: str | None) -> tuple[str, str]:
        if not zone:
            return "", ""
        try:
            now = datetime.now(ZoneInfo(zone))
        except Exception:  # noqa: BLE001 - an unknown zone should not break the page
            return "", ""
        delta = now.utcoffset()
        if delta is None:
            return now.strftime("%Y-%m-%d %H:%M"), ""
        total = int(delta.total_seconds())
        sign = "+" if total >= 0 else "-"
        hours, minutes = divmod(abs(total) // 60, 60)
        return now.strftime("%Y-%m-%d %H:%M"), f"UTC{sign}{hours:02d}:{minutes:02d}"

    async def _ts_handle_post(self, user: discord.User, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

        try:
            if action == "clear":
                await self.config.user(user).timezone.set(None)
                return [{"message": "Timezone cleared.", "category": "success"}]

            if action == "save":
                zone = (field("timezone") or "").strip()
                if not zone:
                    return [{"message": "Pick a timezone.", "category": "warning"}]
                # Validate against the real database, not just the dropdown.
                try:
                    ZoneInfo(zone)
                except Exception:  # noqa: BLE001
                    return [{"message": f"'{zone}' is not a known timezone.", "category": "danger"}]
                await self.config.user(user).timezone.set(zone)
                return [{"message": f"Timezone set to {zone}.", "category": "success"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("Timestamp dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]


TIMESTAMP_TEMPLATE = (
    BASE_CSS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-clock-o"></i> Your timezone</h4>
    <p>
      {% if current %}
        Set to <b>{{ current }}</b>
        {% if offset %}({{ offset }}){% endif %}
        {% if preview %}&middot; local time now: <b>{{ preview }}</b>{% endif %}
      {% else %}
        Not set. Timestamps fall back to UTC.
      {% endif %}
    </p>
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-globe"></i> Pick a timezone</h5>
      <p class="dz-hint">{{ count }} zones available. Common ones are listed first.</p>
      <select class="dz-select" name="timezone">
        <optgroup label="Common">
          {% for z in common %}
            <option value="{{ z }}" {% if z == current %}selected{% endif %}>{{ z }}</option>
          {% endfor %}
        </optgroup>
        <optgroup label="All">
          {% for z in zones %}
            <option value="{{ z }}" {% if z == current %}selected{% endif %}>{{ z }}</option>
          {% endfor %}
        </optgroup>
      </select>
      <div class="dz-row" style="margin-top:12px;">
        <button class="dz-btn primary" name="action" value="save">
          <i class="fa fa-save"></i> Save timezone
        </button>
        {% if current %}
          <button class="dz-btn danger" name="action" value="clear">
            <i class="fa fa-times"></i> Clear
          </button>
        {% endif %}
      </div>
    </div>
  </form>

  <div class="dz-panel">
    <h5><i class="fa fa-code"></i> Discord timestamp styles</h5>
    <p class="dz-hint">
      What each style looks like. Discord renders these in every viewer's own
      timezone, so your setting only affects how the cog reads times you give it.
    </p>
    <table class="dz-t">
      <thead><tr><th>Style</th><th>Name</th><th>Example</th></tr></thead>
      <tbody>
        {% for code, name, example in styles %}
          <tr>
            <td><code>{{ code }}</code></td>
            <td>{{ name }}</td>
            <td style="opacity:.7;">{{ example }}</td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>
"""
)
