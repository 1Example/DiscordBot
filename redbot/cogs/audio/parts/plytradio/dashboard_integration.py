from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands

from ...dashboard_integration import audio_pages
from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
)

log = logging.getLogger("red.plytradio.dashboard")

BUFFER_MIN = 1
BUFFER_MAX = 20


class YouTubeRadioDashboard:
    """Endless YouTube radio: toggle it and tune the lookahead buffer."""

    bot: t.Any
    _config: t.Any
    pylav: t.Any

    @dashboard_page(
        name="youtube-radio",
        description="Automatic YouTube radio settings.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_plytradio_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        staff = await is_staff(self.bot, user, member, guild)

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            if not staff:
                return {
                    "status": 1,
                    "error_title": "Forbidden",
                    "error_message": "Only server administrators can change the radio.",
                }
            notifications = await self._ytr_handle_post(guild, kwargs)

        settings = await self._ytradio_config.guild(guild).all()
        player = self.pylav.get_player(guild)

        current = None
        if player is not None and player.current is not None:
            try:
                current = {
                    "title": await player.current.title() or "Unknown",
                    "author": await player.current.author() or "",
                }
            except Exception:  # noqa: BLE001
                current = None

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": YTRADIO_TEMPLATE,
                "audio_pages": audio_pages(await self.bot.is_owner(user)),
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_staff": staff,
                "enabled": bool(settings.get("enabled")),
                "buffer": int(settings.get("buffer", 3) or 3),
                "buffer_min": BUFFER_MIN,
                "buffer_max": BUFFER_MAX,
                "connected": player is not None,
                "queue_length": len(getattr(player, "queue", []) or []) if player else 0,
                "current": current,
                # The cog keeps its own memory of what it has already served.
                "seen": len(getattr(self, "_played", {}).get(guild.id, []) or []),
                "seeds": len(getattr(self, "_seeds", {}).get(guild.id, []) or []),
            },
        }

    async def _ytr_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")
        conf = self._ytradio_config.guild(guild)

        try:
            if action == "save":
                enabled = field.checked("enabled")
                raw = (field("buffer") or "").strip()
                errors: list[dict] = []
                if raw:
                    try:
                        value = int(raw)
                    except ValueError:
                        errors.append(
                            {"message": f"Buffer '{raw}' is not a number.", "category": "danger"}
                        )
                    else:
                        if not BUFFER_MIN <= value <= BUFFER_MAX:
                            errors.append(
                                {
                                    "message": f"Buffer must be between {BUFFER_MIN} and {BUFFER_MAX}.",
                                    "category": "danger",
                                }
                            )
                        else:
                            await conf.buffer.set(value)
                            # The cog caches this per guild; keep it in step.
                            if hasattr(self, "_buffer_cache"):
                                self._buffer_cache[guild.id] = value

                await conf.enabled.set(enabled)
                if hasattr(self, "_enabled_cache"):
                    self._enabled_cache[guild.id] = enabled

                return errors + [
                    {
                        "message": f"Radio {'enabled' if enabled else 'disabled'}.",
                        "category": "success",
                    }
                ]

            if action == "clear_memory":
                # Lets the radio revisit tracks it has already played.
                for attr in ("_played", "_seeds"):
                    store = getattr(self, attr, None)
                    if store is not None and guild.id in store:
                        store[guild.id].clear()
                return [{"message": "Radio history cleared.", "category": "success"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("PyLavYouTubeRadio dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]


YTRADIO_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-random"></i> YouTube radio in {{ guild_name }}</h4>
    <p>
      {% if enabled %}Enabled &mdash; the queue refills automatically.
      {% else %}Disabled &mdash; playback stops when the queue empties.{% endif %}
      {% if connected %} &middot; {{ queue_length }} track(s) queued{% endif %}
    </p>
  </div>

  {{ subnav(name, audio_pages, 'youtube-radio', guild) }}

  {% if current %}
    <div class="dz-panel">
      <h5><i class="fa fa-play-circle"></i> Now playing</h5>
      <p style="margin:0;"><b>{{ current.title }}</b>
        {% if current.author %}<span style="opacity:.6;"> &mdash; {{ current.author }}</span>{% endif %}
      </p>
    </div>
  {% endif %}

  {% if is_staff %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-cog"></i> Settings</h5>
        <label class="dz-toggle">
          <input type="checkbox" name="enabled" {% if enabled %}checked{% endif %} />
          <span>Keep the queue topped up automatically</span>
        </label>

        <div class="dz-label" style="margin-top:11px;">Lookahead buffer</div>
        <input class="dz-input" type="number" min="{{ buffer_min }}" max="{{ buffer_max }}"
               name="buffer" value="{{ buffer }}" />
        <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
          How many tracks to keep queued ahead. Higher means fewer lookups but
          less responsiveness to skips. Allowed range {{ buffer_min }}&ndash;{{ buffer_max }}.
        </div>

        <div class="dz-row" style="margin-top:13px;">
          <button class="dz-btn primary" name="action" value="save">
            <i class="fa fa-save"></i> Save
          </button>
          <button class="dz-btn danger" name="action" value="clear_memory"
                  onclick="return confirm('Forget played tracks and seeds?');">
            <i class="fa fa-eraser"></i> Clear history
          </button>
        </div>
      </div>
    </form>
  {% endif %}

  <div class="dz-panel">
    <h5><i class="fa fa-history"></i> Radio memory</h5>
    <p class="dz-hint">
      Tracks already served are skipped so the radio keeps branching outward
      rather than looping.
    </p>
    <div class="dz-row">
      <span class="dz-tag">{{ seen }} track(s) remembered</span>
      <span class="dz-tag">{{ seeds }} seed(s) used</span>
    </div>
  </div>
</div>
"""
)
