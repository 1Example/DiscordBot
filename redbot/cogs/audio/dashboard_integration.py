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

log = logging.getLogger("red.audio.dashboard")

MAX_QUEUE_SHOWN = 30


def _fmt_ms(milliseconds: float | int | None) -> str:
    if not milliseconds or milliseconds < 0:
        return "0:00"
    total = int(milliseconds // 1000)
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


class DashboardIntegration:
    """Player status, queue overview and per-server command settings."""

    bot: t.Any
    _config: t.Any
    pylav: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering PyLavPlayer as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Player status, queue and audio command settings.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_audio_page(
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
                    "error_message": "Only server administrators can change audio settings.",
                }
            notifications = await self._au_handle_post(guild, kwargs)

        settings = await self._config.guild(guild).all()
        player = self.pylav.get_player(guild)

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": AUDIO_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_staff": staff,
                "enable_slash": bool(settings.get("enable_slash", True)),
                "enable_context": bool(settings.get("enable_context", True)),
                "state": await self._au_state(player),
                "nodes": self._au_nodes(),
            },
        }

    def _au_nodes(self) -> list[dict]:
        rows = []
        try:
            nodes = list(self.pylav.node_manager.nodes)
        except Exception:  # noqa: BLE001
            return []
        for node in nodes:
            rows.append(
                {
                    "name": getattr(node, "name", "unnamed"),
                    "available": bool(getattr(node, "available", False)),
                    "players": len(getattr(node, "players", []) or []),
                }
            )
        return rows

    async def _au_state(self, player) -> dict:
        if player is None:
            return {"connected": False}

        try:
            raw_queue = list(player.queue.raw_queue)
        except Exception:  # noqa: BLE001 - queue internals vary between versions
            raw_queue = []

        queue = []
        for index, track in enumerate(raw_queue[:MAX_QUEUE_SHOWN], start=1):
            queue.append(
                {
                    "position": index,
                    "title": await self._au_field(track, "title", "Unknown"),
                    "author": await self._au_field(track, "author", ""),
                    "duration": _fmt_ms(await self._au_field(track, "duration", 0)),
                }
            )

        try:
            position = await player.position()
        except Exception:  # noqa: BLE001
            position = 0

        state = {
            "connected": True,
            "channel": getattr(getattr(player, "channel", None), "name", ""),
            "paused": bool(player.paused),
            "playing": bool(player.is_playing),
            "volume": int(player.volume),
            "position": _fmt_ms(position),
            "queue_length": len(raw_queue),
            "queue": queue,
            "current": None,
        }

        if player.current is not None:
            state["current"] = {
                "title": await self._au_field(player.current, "title", "Unknown"),
                "author": await self._au_field(player.current, "author", ""),
                "duration": _fmt_ms(await self._au_field(player.current, "duration", 0)),
                "uri": await self._au_field(player.current, "uri", ""),
            }
        return state

    @staticmethod
    async def _au_field(track, name: str, default):
        """PyLav track fields are coroutines, and any one of them can fail."""
        method = getattr(track, name, None)
        if method is None:
            return default
        try:
            value = await method()
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    async def _au_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        if field("action") != "save":
            return [{"message": "Unknown action.", "category": "warning"}]
        conf = self._config.guild(guild)
        await conf.enable_slash.set(field.checked("enable_slash"))
        await conf.enable_context.set(field.checked("enable_context"))
        return [
            {
                "message": "Saved. Command visibility changes apply after the next sync.",
                "category": "success",
            }
        ]


AUDIO_TEMPLATE = (
    BASE_CSS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-headphones"></i> Audio in {{ guild_name }}</h4>
    <p>
      {% if state.connected %}
        Connected to <b>{{ state.channel }}</b> &middot;
        {% if state.paused %}paused{% elif state.playing %}playing{% else %}idle{% endif %}
        &middot; volume {{ state.volume }}% &middot; {{ state.queue_length }} queued
      {% else %}Not connected to a voice channel.{% endif %}
    </p>
  </div>

  {% if state.current %}
    <div class="dz-panel">
      <h5><i class="fa fa-music"></i> Now playing</h5>
      <p style="margin:0 0 4px;">
        {% if state.current.uri %}
          <a href="{{ state.current.uri }}" target="_blank" rel="noopener">
            <b>{{ state.current.title }}</b></a>
        {% else %}<b>{{ state.current.title }}</b>{% endif %}
      </p>
      <p class="dz-hint" style="margin:0;">
        {{ state.current.author }} &middot; {{ state.position }} / {{ state.current.duration }}
      </p>
      <p class="dz-hint" style="margin:9px 0 0;">
        Playback controls live on the <b>PyLavController</b> page.
      </p>
    </div>
  {% endif %}

  {% if state.queue %}
    <div class="dz-panel">
      <h5><i class="fa fa-list-ol"></i> Queue</h5>
      <p class="dz-hint">
        Showing {{ state.queue|length }} of {{ state.queue_length }}.
      </p>
      <table class="dz-t">
        <thead><tr><th>#</th><th>Title</th><th>Artist</th><th>Length</th></tr></thead>
        <tbody>
          {% for tr in state.queue %}
            <tr>
              <td style="opacity:.45; width:34px;">{{ tr.position }}</td>
              <td>{{ tr.title }}</td>
              <td style="opacity:.7;">{{ tr.author }}</td>
              <td style="opacity:.7;">{{ tr.duration }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  {% endif %}

  {% if is_staff %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-cog"></i> Command settings</h5>
        <p class="dz-hint">Applies to this server only.</p>
        <label class="dz-toggle">
          <input type="checkbox" name="enable_slash" {% if enable_slash %}checked{% endif %} />
          <span>Enable slash commands</span>
        </label>
        <label class="dz-toggle">
          <input type="checkbox" name="enable_context" {% if enable_context %}checked{% endif %} />
          <span>Enable right-click "Play from..." menus</span>
        </label>
        <div style="margin-top:12px;">
          <button class="dz-btn primary" name="action" value="save">
            <i class="fa fa-save"></i> Save
          </button>
        </div>
      </div>
    </form>
  {% endif %}

  <div class="dz-panel">
    <h5><i class="fa fa-server"></i> Nodes</h5>
    {% if nodes %}
      <table class="dz-t">
        <thead><tr><th>Node</th><th>Status</th><th>Players</th></tr></thead>
        <tbody>
          {% for n in nodes %}
            <tr>
              <td><b>{{ n.name }}</b></td>
              <td>
                {% if n.available %}<span class="dz-tag">online</span>
                {% else %}<span class="dz-tag" style="color:#ff8b8b;">offline</span>{% endif %}
              </td>
              <td style="opacity:.7;">{{ n.players }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="dz-empty">No nodes reported.</p>
    {% endif %}
  </div>
</div>
"""
)
