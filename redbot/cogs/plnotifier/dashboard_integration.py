from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    channel_options,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
)

log = logging.getLogger("red.plnotifier.dashboard")

# 58 flat toggles is unusable, so events are grouped for presentation only.
# Any key not listed here still appears, under "Other".
EVENT_GROUPS = (
    (
        "Playback",
        (
            "track_start", "track_end", "track_skipped", "track_seek", "track_replaced",
            "track_resumed", "track_autoplay", "track_previous_requested", "tracks_requested",
        ),
    ),
    (
        "Problems",
        ("track_stuck", "track_exception", "websocket_closed"),
    ),
    (
        "Economy",
        ("action_charged",),
    ),
    (
        "Queue",
        (
            "queue_shuffled", "queue_end", "queue_track_position_changed",
            "queue_tracks_removed",
        ),
    ),
    (
        "Player",
        (
            "player_paused", "player_stopped", "player_resumed", "player_moved",
            "player_disconnected", "player_connected", "player_restored", "volume_changed",
            "player_repeat", "filters_applied",
        ),
    ),
    (
        "Automatic actions",
        (
            "player_auto_paused", "player_auto_resumed", "player_auto_disconnected",
            "player_auto_disconnected_empty_queue",
        ),
    ),
    (
        "Nodes",
        ("node_connected", "node_disconnected", "node_changed"),
    ),
    (
        "SponsorBlock",
        ("segment_skipped", "segments_loaded"),
    ),
)

# Every track_start_* variant, collapsed into one section.
SOURCE_PREFIX = "track_start_"

# What "just the basics" means: the handful of events worth announcing, used by
# the reset button and matching the cog's registered defaults.
BASIC_EVENTS = frozenset(
    {
        "tracks_requested",
        "track_skipped",
        "track_start",
        "queue_end",
        "player_connected",
        "player_disconnected",
        "player_paused",
        "player_resumed",
        "track_stuck",
        "track_exception",
        "action_charged",
    }
)

PRETTY_SOURCES = {
    "youtube_music": "YouTube Music",
    "apple_music": "Apple Music",
    "localfile": "Local files",
    "http": "Direct HTTP",
    "gctts": "Google TTS",
    "flowery_tts": "Flowery TTS",
    "ocrmix": "OverClocked ReMix",
    "clypit": "Clyp.it",
    "getyarn": "getyarn.io",
}


class DashboardIntegration:
    """Which player events produce a notification, and where."""

    bot: t.Any
    _config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering PyLavNotifier as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Choose which music events are announced.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_plnotifier_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        if not await is_staff(self.bot, user, member, guild):
            return {
                "status": 1,
                "error_title": "Forbidden",
                "error_message": "Only server administrators can change notifications.",
            }

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._pln_handle_post(guild, kwargs)

        settings = await self._config.guild(guild).all()
        grouped, seen = [], set()

        for title, keys in EVENT_GROUPS:
            events = []
            for key in keys:
                data = settings.get(key)
                if not isinstance(data, dict):
                    continue
                seen.add(key)
                events.append(self._pln_event(key, data, key.replace("_", " ").capitalize()))
            if events:
                grouped.append({"title": title, "events": events})

        sources = []
        for key, data in settings.items():
            if not key.startswith(SOURCE_PREFIX) or not isinstance(data, dict):
                continue
            seen.add(key)
            raw = key[len(SOURCE_PREFIX):]
            sources.append(self._pln_event(key, data, PRETTY_SOURCES.get(raw, raw.capitalize())))
        if sources:
            grouped.append(
                {"title": "Track start by source", "events": sorted(sources, key=lambda e: e["label"])}
            )

        other = [
            self._pln_event(k, v, k.replace("_", " ").capitalize())
            for k, v in settings.items()
            if k not in seen and isinstance(v, dict) and "enabled" in v
        ]
        if other:
            grouped.append({"title": "Other", "events": sorted(other, key=lambda e: e["label"])})

        total = sum(len(g["events"]) for g in grouped)
        active = sum(1 for g in grouped for e in g["events"] if e["enabled"])

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": PLNOTIFIER_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "groups": grouped,
                "total": total,
                "active": active,
                "channels": channel_options(guild, selected=settings.get("notify_channel_id")),
                "auto_delete_after": settings.get("auto_delete_after", 0) or 0,
            },
        }

    @staticmethod
    def _pln_event(key: str, data: dict, label: str) -> dict:
        return {
            "key": key,
            "label": label,
            "enabled": bool(data.get("enabled")),
            "mention": bool(data.get("mention")),
        }

    async def _pln_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")
        conf = self._config.guild(guild)

        try:
            if action == "save_group":
                keys = [k for k in (field("keys") or "").split(",") if k]
                if not keys:
                    return [{"message": "Nothing to save.", "category": "warning"}]
                for key in keys:
                    # Each event is a dict; write the whole mapping so a missing
                    # sub-key never leaves a half-updated entry behind.
                    await conf.get_attr(key).set(
                        {
                            "enabled": field.checked(f"{key}__enabled"),
                            "mention": field.checked(f"{key}__mention"),
                        }
                    )
                return [{"message": f"Saved {len(keys)} event(s).", "category": "success"}]

            if action == "reset_basics":
                changed = 0
                async with self._config.guild(guild).all() as stored:
                    for key, value in stored.items():
                        if not isinstance(value, dict) or "enabled" not in value:
                            continue
                        wanted = key in BASIC_EVENTS
                        if value["enabled"] != wanted:
                            value["enabled"] = wanted
                            changed += 1
                return [
                    {
                        "message": f"Reset to the basic notifications; "
                        f"{changed} event(s) changed.",
                        "category": "success",
                    }
                ]

            if action == "save_cleanup":
                seconds = field.integer("auto_delete_after", 0) or 0
                if seconds < 0 or seconds > 3600:
                    return [
                        {"message": "Choose between 0 and 3600 seconds.",
                         "category": "warning"}
                    ]
                await self._config.guild(guild).auto_delete_after.set(seconds)
                if seconds:
                    return [
                        {"message": f"Notifications will delete themselves after "
                                    f"{seconds} seconds.", "category": "success"}
                    ]
                return [
                    {"message": "Notifications will stay in the channel.",
                     "category": "success"}
                ]

            if action == "save_channel":
                raw = field("notify_channel_id") or ""
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
                await conf.notify_channel_id.set(channel_id)
                return warnings + [{"message": "Notification channel saved.", "category": "success"}]

            if action in ("enable_all", "disable_all", "mentions_off"):
                target = action == "enable_all"
                settings = await conf.all()
                changed = 0
                for key, data in settings.items():
                    if not isinstance(data, dict) or "enabled" not in data:
                        continue
                    if action == "mentions_off":
                        if not data.get("mention"):
                            continue
                        await conf.get_attr(key).set({**data, "mention": False})
                    else:
                        if data.get("enabled") is target:
                            continue
                        await conf.get_attr(key).set({**data, "enabled": target})
                    changed += 1
                verb = {"enable_all": "Enabled", "disable_all": "Disabled",
                        "mentions_off": "Removed mentions from"}[action]
                return [{"message": f"{verb} {changed} event(s).", "category": "success"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("PyLavNotifier dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]


PLNOTIFIER_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-bell"></i> Music notifications in {{ guild_name }}</h4>
    <p>{{ active }} of {{ total }} events are announced.</p>
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-clock-o"></i> Tidying up</h5>
      <p class="dz-hint">
        Notifications are chatter rather than a record, so they can remove
        themselves once they have been seen. Set 0 to keep them.
      </p>
      <div class="dz-row">
        <input class="dz-input" type="number" min="0" max="3600"
               name="auto_delete_after" value="{{ auto_delete_after }}"
               style="max-width:180px;" />
        <button class="dz-btn primary" name="action" value="save_cleanup">
          <i class="fa fa-save"></i> Save
        </button>
        {{ confirm('Just the basics', 'reset_basics',
                   'Turn off every event except the handful worth announcing (play, queue, skip, connect, problems, charges)?',
                   '', 'fa-magic') }}
      </div>
    </div>
  </form>

  <div class="dz-panel">
      <h5><i class="fa fa-hashtag"></i> Notification channel</h5>
      <p class="dz-hint">Leave unset to announce in the channel the player was summoned to.</p>
      <div class="dz-row">
        <select class="dz-select" style="flex:1 1 260px;" name="notify_channel_id">
          <option value="">&mdash; player's channel &mdash;</option>
          {% for c in channels %}
            <option value="{{ c.id }}" {% if c.selected %}selected{% endif %}>{{ c.name }}</option>
          {% endfor %}
        </select>
        <button class="dz-btn primary" name="action" value="save_channel">
          <i class="fa fa-save"></i> Save channel
        </button>
      </div>
    </div>
  </form>

  <div class="dz-panel">
    <div class="dz-row">
      <form method="POST" style="display:inline;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <button class="dz-btn" name="action" value="enable_all">
          <i class="fa fa-check-square-o"></i> Announce everything
        </button>
      </form>
      <form method="POST" style="display:inline;"
            onsubmit="return confirm('Silence every event?');">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <button class="dz-btn danger" name="action" value="disable_all">
          <i class="fa fa-bell-slash"></i> Silence everything
        </button>
      </form>
      <form method="POST" style="display:inline;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <button class="dz-btn" name="action" value="mentions_off">
          <i class="fa fa-at"></i> Turn off all mentions
        </button>
      </form>
    </div>
  </div>

  {% for g in groups %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <input type="hidden" name="keys"
             value="{% for e in g.events %}{{ e.key }}{% if not loop.last %},{% endif %}{% endfor %}" />
      <div class="dz-panel">
        <h5><i class="fa fa-music"></i> {{ g.title }}</h5>
        <p class="dz-hint">
          {{ g.events|length }} event(s). &ldquo;Mention&rdquo; pings the requester.
        </p>
        <table class="dz-t">
          <thead><tr><th>Event</th><th style="width:110px;">Announce</th>
                     <th style="width:110px;">Mention</th></tr></thead>
          <tbody>
            {% for e in g.events %}
              <tr>
                <td>{{ e.label }}</td>
                <td>
                  <input type="checkbox" name="{{ e.key }}__enabled"
                         {% if e.enabled %}checked{% endif %} />
                </td>
                <td>
                  <input type="checkbox" name="{{ e.key }}__mention"
                         {% if e.mention %}checked{% endif %} />
                </td>
              </tr>
            {% endfor %}
          </tbody>
        </table>
        <div style="margin-top:11px;">
          <button class="dz-btn primary" name="action" value="save_group">
            <i class="fa fa-save"></i> Save {{ g.title|lower }}
          </button>
        </div>
      </div>
    </form>
  {% endfor %}
</div>
"""
)
