from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    dashboard_page,
    form_reader,
    guild_member,
)

log = logging.getLogger("red.pllyrics.dashboard")


class DashboardIntegration:
    """Lyrics on the dashboard.

    Covers ``[p]pllyrics np``, ``[p]pllyrics track`` and ``[p]pllyrics b64``:
    read the lyrics of what is playing, or of any track you search for, with the
    whole text on one page instead of paginated in chat.
    """

    bot: t.Any
    pylav: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering PyLavLyrics as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Lyrics for the current track or any search.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_pllyrics_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error

        notifications: list[dict] = []
        result: dict = {}
        if kwargs.get("method") == "POST":
            notifications, result = await self._lyrics_handle_post(guild, kwargs)

        player = self.pylav.get_player(guild.id)
        current = getattr(player, "current", None)
        node = await self.pylav.node_manager.find_best_node(feature="lavalyrics")

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": PLLYRICS_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "supported": node is not None,
                "playing": await current.get_track_display_name() if current else "",
                "result": result,
            },
        }

    async def _lyrics_handle_post(
        self, guild: discord.Guild, kwargs: dict
    ) -> tuple[list[dict], dict]:
        field = form_reader(kwargs)
        action = field("action")

        try:
            if action == "current":
                player = self.pylav.get_player(guild.id)
                if player is None or not player.current:
                    return (
                        [{"message": "Nothing is playing right now.", "category": "info"}],
                        {},
                    )
                lyrics = await player.get_lyrics()
                if not lyrics:
                    return (
                        [{"message": "No lyrics were found for that track.",
                          "category": "info"}],
                        {},
                    )
                return [], {
                    "title": await player.current.get_track_display_name(),
                    "artwork": await player.current.artworkUrl() or "",
                    "text": lyrics if isinstance(lyrics, str) else getattr(lyrics, "text", ""),
                }

            if action in ("search", "b64"):
                node = await self.pylav.node_manager.find_best_node(feature="lavalyrics")
                if node is None:
                    return (
                        [{"message": "No node has the lyrics feature enabled.",
                          "category": "warning"}],
                        {},
                    )

                if action == "b64":
                    encoded = (field("b64") or "").strip()
                    if not encoded:
                        return (
                            [{"message": "Enter a base64 track string.",
                              "category": "warning"}],
                            {},
                        )
                    title = "Encoded track"
                    artwork = ""
                else:
                    from pylav.players.query.obj import Query

                    search = (field("search") or "").strip()
                    if not search:
                        return [{"message": "Enter a search.", "category": "warning"}], {}
                    query = await Query.from_string(search)
                    response = await self.pylav.get_tracks(query)
                    match response.loadType:
                        case "track":
                            tracks = [response.data]
                        case "search":
                            tracks = response.data
                        case "playlist":
                            tracks = response.data.tracks
                        case __:
                            tracks = []
                    if not tracks:
                        return (
                            [{"message": f"No track matched {search}.",
                              "category": "warning"}],
                            {},
                        )
                    encoded = tracks[0].encoded
                    title = getattr(getattr(tracks[0], "info", None), "title", search)
                    artwork = getattr(getattr(tracks[0], "info", None), "artworkUrl", "") or ""

                lyrics = await node.fetch_lyrics(encoded, True)
                text = getattr(lyrics, "text", None)
                if not text:
                    return (
                        [{"message": "No lyrics were found for that track.",
                          "category": "info"}],
                        {},
                    )
                return [], {"title": title, "artwork": artwork, "text": text}
        except Exception as exc:  # noqa: BLE001
            log.exception("PyLavLyrics dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}], {}

        return [{"message": f"Unknown action: {action}", "category": "warning"}], {}


PLLYRICS_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-music"></i> Lyrics</h4>
    <p>
      {% if supported %}A node with the lyrics feature is available.
      {% else %}No node currently offers the lyrics feature, so only the player's
        own lyrics source can be used.{% endif %}
    </p>
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-search"></i> Look up lyrics</h5>
      <p class="dz-hint">
        {% if playing %}Now playing: <b>{{ playing }}</b>.
        {% else %}Nothing is playing in {{ guild_name }} right now.{% endif %}
      </p>
      <div class="dz-row">
        <button class="dz-btn primary" name="action" value="current">
          <i class="fa fa-play"></i> Lyrics for the current track
        </button>
      </div>
      <label class="dz-label" style="margin-top:14px;">Search for a track</label>
      <div class="dz-row">
        <input class="dz-input" type="text" name="search"
               placeholder="artist - title" style="flex:1 1 240px;" />
        <button class="dz-btn" name="action" value="search">
          <i class="fa fa-search"></i> Find lyrics
        </button>
      </div>
      <label class="dz-label" style="margin-top:14px;">
        Or paste an encoded track <span class="dz-tag">base64</span>
      </label>
      <div class="dz-row">
        <input class="dz-input" type="text" name="b64"
               placeholder="QAAA..." style="flex:1 1 240px;" />
        <button class="dz-btn" name="action" value="b64">
          <i class="fa fa-code"></i> Decode and find
        </button>
      </div>
    </div>
  </form>

  {% if result %}
    <div class="dz-panel">
      <h5><i class="fa fa-file-text-o"></i> {{ result.title }}</h5>
      {% if result.artwork %}
        <img src="{{ result.artwork }}" alt=""
             style="max-width:180px; border-radius:10px; margin-bottom:12px;" />
      {% endif %}
      <div class="dz-text" style="max-height:620px; overflow-y:auto;">{{ result.text }}</div>
    </div>
  {% endif %}
</div>
"""
)
