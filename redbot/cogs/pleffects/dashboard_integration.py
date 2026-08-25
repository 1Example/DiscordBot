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

log = logging.getLogger("red.pleffects.dashboard")

# Filters PyLav exposes on a player, with a friendly label and the attribute
# that indicates the filter is doing something.
FILTERS = (
    ("nightcore", "Nightcore", "Speeds up and raises the pitch."),
    ("vibrato", "Vibrato", "Wobbles the pitch."),
    ("tremolo", "Tremolo", "Wobbles the volume."),
    ("karaoke", "Karaoke", "Attempts to remove vocals."),
    ("timescale", "Timescale", "Speed, pitch and rate."),
    ("distortion", "Distortion", "Adds harmonic distortion."),
    ("low_pass", "Low pass", "Muffles the high end."),
    ("rotation", "Rotation", "Rotates audio between channels."),
    ("channel_mix", "Channel mix", "Blends left and right."),
    ("echo", "Echo", "Repeats the signal."),
)


class DashboardIntegration:
    """Audio effect persistence, plus what is currently applied."""

    bot: t.Any
    _config: t.Any
    pylav: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering PyLavEffects as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Audio effects and whether they persist.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_pleffects_page(
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
                    "error_message": "Only server administrators can change effect settings.",
                }
            notifications = await self._fx_handle_post(guild, kwargs)

        settings = await self._config.guild(guild).all()
        player = self.pylav.get_player(guild)

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": EFFECTS_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_staff": staff,
                "persist_fx": bool(settings.get("persist_fx")),
                "persist_eq": bool(settings.get("persist_eq")),
                "connected": player is not None,
                "filters": self._fx_state(player),
                "volume": int(getattr(player, "volume", 0) or 0) if player else None,
            },
        }

    def _fx_state(self, player) -> list[dict]:
        """Which filters are currently active on the live player."""
        rows = []
        for key, label, blurb in FILTERS:
            active = False
            if player is not None:
                obj = getattr(player, key, None)
                # PyLav filter objects expose .changed when they differ from default.
                if obj is not None:
                    active = bool(getattr(obj, "changed", False))
            rows.append({"key": key, "label": label, "help": blurb, "active": active})
        return rows

    async def _fx_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

        try:
            if action == "save":
                await self._config.guild(guild).persist_fx.set(field.checked("persist_fx"))
                await self._config.guild(guild).persist_eq.set(field.checked("persist_eq"))
                return [{"message": "Persistence settings saved.", "category": "success"}]

            if action == "reset":
                player = self.pylav.get_player(guild)
                if player is None:
                    return [{"message": "I am not connected to a voice channel.", "category": "warning"}]
                # set_filters with no arguments and reset=True clears everything.
                await player.set_filters(requester=self.bot.user, reset_not_set=True)
                return [{"message": "All effects cleared.", "category": "success"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("PyLavEffects dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]


EFFECTS_TEMPLATE = (
    BASE_CSS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-sliders"></i> Audio effects in {{ guild_name }}</h4>
    <p>
      {% if connected %}Player connected{% if volume is not none %} at {{ volume }}% volume{% endif %}.
      {% else %}No active player.{% endif %}
    </p>
  </div>

  {% if is_staff %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-thumb-tack"></i> Persistence</h5>
        <p class="dz-hint">Whether settings survive the player disconnecting.</p>
        <label class="dz-toggle">
          <input type="checkbox" name="persist_fx" {% if persist_fx %}checked{% endif %} />
          <span>Keep effects between sessions</span>
        </label>
        <label class="dz-toggle">
          <input type="checkbox" name="persist_eq" {% if persist_eq %}checked{% endif %} />
          <span>Keep the equaliser between sessions</span>
        </label>
        <div class="dz-row" style="margin-top:12px;">
          <button class="dz-btn primary" name="action" value="save">
            <i class="fa fa-save"></i> Save
          </button>
          {% if connected %}
            <button class="dz-btn danger" name="action" value="reset"
                    onclick="return confirm('Clear every active effect?');">
              <i class="fa fa-undo"></i> Clear all effects
            </button>
          {% endif %}
        </div>
      </div>
    </form>
  {% endif %}

  <div class="dz-panel">
    <h5><i class="fa fa-magic"></i> Current filters</h5>
    <p class="dz-hint">
      {% if connected %}Applied with the effects commands in Discord.
      {% else %}Connect a player to see live state.{% endif %}
    </p>
    <div class="dz-grid two">
      {% for f in filters %}
        <div style="display:flex; align-items:center; gap:10px; padding:7px 0;
                    border-bottom:1px solid rgba(255,255,255,.05);">
          <span style="width:9px; height:9px; border-radius:50%; flex:0 0 auto;
                       background:{% if f.active %}#3ba55d{% else %}rgba(255,255,255,.16){% endif %};"></span>
          <div style="min-width:0;">
            <div style="font-size:.87rem; font-weight:600;">
              {{ f.label }}
              {% if f.active %}<span class="dz-tag" style="margin-left:5px;">on</span>{% endif %}
            </div>
            <div style="font-size:.72rem; opacity:.45;">{{ f.help }}</div>
          </div>
        </div>
      {% endfor %}
    </div>
  </div>
</div>
"""
)
