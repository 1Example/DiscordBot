from __future__ import annotations

import logging
import typing as t

import discord

from ...dashboard_integration import audio_pages
from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    dashboard_page,
    form_reader,
    guild_member,
)

log = logging.getLogger("red.plradio.dashboard")

RESULT_LIMIT = 100


class RadioDashboard:
    """Radio stations on the dashboard.

    Replaces the ``/radio`` slash command and its filter arguments: search the
    Radio Browser directory by name, country, language, tag or codec, then queue
    a station without touching Discord.
    """

    bot: t.Any
    pylav: t.Any

    @dashboard_page(
        name="radio",
        description="Search and queue radio stations.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_plradio_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error

        field = form_reader(kwargs)
        notifications: list[dict] = []
        stations: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications, stations = await self._radio_handle_post(member, guild, kwargs)

        player = self.pylav.get_player(guild.id)
        browser = getattr(self.pylav, "radio_browser", None)

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": PLRADIO_TEMPLATE,
                "audio_pages": audio_pages(await self.bot.is_owner(user)),
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "available": browser is not None,
                "stations": stations,
                "limit": RESULT_LIMIT,
                "connected": player is not None,
                "channel": getattr(getattr(player, "channel", None), "name", ""),
                "search": field("search") or "",
                "country": field("country") or "",
                "language": field("language") or "",
                "tag": field("tag") or "",
                "codec": field("codec") or "",
            },
        }

    async def _radio_handle_post(
        self, member: discord.Member, guild: discord.Guild, kwargs: dict
    ) -> tuple[list[dict], list[dict]]:
        field = form_reader(kwargs)
        action = field("action")
        browser = getattr(self.pylav, "radio_browser", None)

        try:
            if action == "search":
                if browser is None:
                    return (
                        [{"message": "The Radio Browser service is unavailable.",
                          "category": "warning"}],
                        [],
                    )
                filters = {
                    "name": (field("search") or "").strip() or None,
                    "country": (field("country") or "").strip() or None,
                    "language": (field("language") or "").strip() or None,
                    "tag": (field("tag") or "").strip() or None,
                    "codec": (field("codec") or "").strip() or None,
                }
                filters = {k: v for k, v in filters.items() if v}
                if not filters:
                    return (
                        [{"message": "Enter at least one filter.", "category": "warning"}],
                        [],
                    )
                found = await browser.search(limit=RESULT_LIMIT, **filters)
                rows = [self._radio_row(s) for s in (found or [])]
                if not rows:
                    return [{"message": "No stations matched.", "category": "info"}], []
                return (
                    [{"message": f"{len(rows)} station(s) found.", "category": "success"}],
                    rows,
                )

            if action == "play":
                url = (field("station_url") or "").strip()
                name = (field("station_name") or "").strip() or url
                if not url:
                    return [{"message": "Pick a station first.", "category": "warning"}], []

                player = self.pylav.get_player(guild.id)
                if player is None:
                    channel = getattr(getattr(member, "voice", None), "channel", None)
                    if channel is None:
                        return (
                            [{"message": "Join a voice channel so I know where to connect.",
                              "category": "warning"}],
                            [],
                        )
                    permission = channel.permissions_for(guild.me)
                    if not (permission.connect and permission.speak):
                        return (
                            [{"message": f"I cannot connect or speak in {channel.name}.",
                              "category": "warning"}],
                            [],
                        )
                    player = await self.pylav.connect_player(
                        channel=channel, requester=member
                    )

                from pylav.players.query.obj import Query

                query = await Query.from_string(url)
                successful, count, _failed = await self.pylav.get_all_tracks_for_queries(
                    query, requester=member, player=player, bypass_cache=True
                )
                if not count:
                    return (
                        [{"message": f"{name} could not be played.", "category": "warning"}],
                        [],
                    )
                await player.add(requester=member.id, track=successful[0])
                if not player.is_active and not player.queue.empty():
                    await player.next(requester=member)
                return (
                    [{"message": f"Queued {name}.", "category": "success"}],
                    [],
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("PyLavRadio dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}], []

        return [{"message": f"Unknown action: {action}", "category": "warning"}], []

    @staticmethod
    def _radio_row(station) -> dict:
        return {
            "name": getattr(station, "name", "Unknown"),
            "url": getattr(station, "url_resolved", None) or getattr(station, "url", ""),
            "homepage": getattr(station, "homepage", "") or "",
            "country": getattr(station, "country", "") or "",
            "language": getattr(station, "language", "") or "",
            "codec": getattr(station, "codec", "") or "",
            "bitrate": getattr(station, "bitrate", 0) or 0,
            "tags": getattr(station, "tags", "") or "",
            "votes": getattr(station, "votes", 0) or 0,
        }


PLRADIO_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-feed"></i> Radio</h4>
    <p>
      {% if available %}Search the Radio Browser directory and queue a station.
      {% else %}The Radio Browser service is currently unavailable.{% endif %}
      {% if connected %} Player connected to {{ channel }}.{% endif %}
    </p>
  </div>

  {{ subnav(name, audio_pages, 'radio', guild) }}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-search"></i> Find a station</h5>
      <p class="dz-hint">Every filter is optional, but give at least one.
         Up to {{ limit }} results.</p>
      <div class="dz-grid three">
        <div>
          <label class="dz-label">Name</label>
          <input class="dz-input" type="text" name="search" value="{{ search }}"
                 placeholder="BBC Radio 1" />
        </div>
        <div>
          <label class="dz-label">Country</label>
          <input class="dz-input" type="text" name="country" value="{{ country }}"
                 placeholder="United Kingdom" />
        </div>
        <div>
          <label class="dz-label">Language</label>
          <input class="dz-input" type="text" name="language" value="{{ language }}"
                 placeholder="english" />
        </div>
      </div>
      <div class="dz-grid two">
        <div>
          <label class="dz-label">Tag</label>
          <input class="dz-input" type="text" name="tag" value="{{ tag }}"
                 placeholder="rock" />
        </div>
        <div>
          <label class="dz-label">Codec</label>
          <input class="dz-input" type="text" name="codec" value="{{ codec }}"
                 placeholder="MP3" />
        </div>
      </div>
      <div class="dz-save">
        <button class="dz-btn primary" name="action" value="search">
          <i class="fa fa-search"></i> Search
        </button>
      </div>
    </div>
  </form>

  {% if stations %}
    <div class="dz-panel">
      <h5><i class="fa fa-list"></i> Results</h5>
      <table class="dz-t">
        <tr><th>Station</th><th>Country</th><th>Language</th><th>Quality</th>
            <th>Tags</th><th></th></tr>
        {% for s in stations %}
          <tr>
            <td>
              {{ s.name }}
              {% if s.homepage %}
                <br /><a class="dz-hint" href="{{ s.homepage }}" target="_blank"
                         rel="noopener">homepage</a>
              {% endif %}
            </td>
            <td>{{ s.country }}</td>
            <td>{{ s.language }}</td>
            <td>{{ s.codec }}{% if s.bitrate %} {{ s.bitrate }}kbps{% endif %}</td>
            <td>{{ s.tags }}</td>
            <td>
              <form method="POST">
                <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                <input type="hidden" name="station_url" value="{{ s.url }}" />
                <input type="hidden" name="station_name" value="{{ s.name }}" />
                <button class="dz-btn primary" name="action" value="play">
                  <i class="fa fa-play"></i> Queue
                </button>
              </form>
            </td>
          </tr>
        {% endfor %}
      </table>
    </div>
  {% endif %}
</div>
"""
)
