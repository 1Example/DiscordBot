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

log = logging.getLogger("red.plplaylists.dashboard")

MAX_TRACKS_SHOWN = 40


class DashboardIntegration:
    """Browse and manage the playlists visible to this server."""

    bot: t.Any
    pylav: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering PyLavPlaylists as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Browse, inspect and queue saved playlists.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_plplaylists_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        staff = await is_staff(self.bot, user, member, guild)

        notifications: list[dict] = []
        selected = None
        if kwargs.get("method") == "POST":
            notifications, selected = await self._plp_handle_post(member, guild, kwargs, staff)

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": PLAYLISTS_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_staff": staff,
                "groups": await self._plp_groups(member, guild),
                "detail": selected,
            },
        }

    async def _plp_groups(self, member: discord.Member, guild: discord.Guild) -> list[dict]:
        """Playlists the member can see, split by scope."""
        try:
            bundled, user_pl, guild_pl, channel_pl, vc_pl = await self.pylav.playlist_db_manager.get_all_for_user(
                requester=member.id, guild=guild
            )
        except Exception:  # noqa: BLE001
            log.exception("Could not list playlists")
            return []

        groups = []
        for title, playlists in (
            ("Server", guild_pl),
            ("Yours", user_pl),
            ("Global", bundled),
            ("Channel", channel_pl),
            ("Voice channel", vc_pl),
        ):
            rows = []
            for playlist in playlists or []:
                try:
                    rows.append(
                        {
                            "id": str(playlist.id),
                            "name": await playlist.fetch_name() or "Untitled",
                            "size": await playlist.size(),
                            "url": await playlist.fetch_url() or "",
                        }
                    )
                except Exception:  # noqa: BLE001 - one bad row shouldn't hide the rest
                    log.exception("Could not read a playlist")
            if rows:
                groups.append({"title": title, "playlists": sorted(rows, key=lambda r: r["name"].lower())})
        return groups

    async def _plp_handle_post(self, member, guild, kwargs: dict, staff: bool):
        field = form_reader(kwargs)
        action = field("action")
        playlist_id = (field("playlist") or "").strip()
        if not playlist_id:
            return [{"message": "Pick a playlist.", "category": "warning"}], None

        try:
            playlist = await self.pylav.playlist_db_manager.get_playlist_by_id(playlist_id)
        except Exception:  # noqa: BLE001
            return [{"message": "That playlist no longer exists.", "category": "danger"}], None

        try:
            name = await playlist.fetch_name() or "Untitled"

            if action == "inspect":
                return [], await self._plp_detail(playlist, name)

            if action in ("play", "queue"):
                player = self.pylav.get_player(guild)
                if player is None:
                    channel = getattr(getattr(member, "voice", None), "channel", None)
                    if channel is None:
                        return (
                            [{"message": "Join a voice channel first.", "category": "warning"}],
                            None,
                        )
                    player = await self.pylav.player_manager.create(
                        channel=channel, requester=member
                    )

                tracks = await playlist.fetch_tracks() or []
                if not tracks:
                    return [{"message": f"'{name}' is empty.", "category": "warning"}], None

                added = 0
                for index, encoded in enumerate(tracks):
                    identifier = encoded if isinstance(encoded, str) else (encoded or {}).get("encoded")
                    if not identifier:
                        continue
                    try:
                        track = await self.pylav.decode_track(identifier, raise_on_failure=False)
                        if track is None:
                            continue
                        if action == "play" and index == 0 and not player.current:
                            await player.play(track, None, member)
                        else:
                            await player.add(requester=member.id, track=track)
                        added += 1
                    except Exception:  # noqa: BLE001 - skip undecodable tracks
                        continue
                return (
                    [{"message": f"Queued {added} track(s) from '{name}'.", "category": "success"}],
                    None,
                )

            if action == "delete":
                if not staff:
                    return (
                        [{"message": "Only administrators can delete playlists.", "category": "warning"}],
                        None,
                    )
                await playlist.delete()
                return [{"message": f"Deleted '{name}'.", "category": "success"}], None
        except Exception as exc:  # noqa: BLE001
            log.exception("Playlist dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}], None

        return [{"message": f"Unknown action: {action}", "category": "warning"}], None

    async def _plp_detail(self, playlist, name: str) -> dict:
        tracks = await playlist.fetch_tracks() or []
        rows = []
        for encoded in tracks[:MAX_TRACKS_SHOWN]:
            identifier = encoded if isinstance(encoded, str) else (encoded or {}).get("encoded")
            title, author, uri = identifier, "", ""
            if identifier:
                try:
                    decoded = await self.pylav.decode_track(identifier, raise_on_failure=False)
                    info = getattr(decoded, "info", None)
                    if info is not None:
                        title = getattr(info, "title", None) or "Unknown"
                        author = getattr(info, "author", None) or ""
                        uri = getattr(info, "uri", None) or ""
                except Exception:  # noqa: BLE001
                    title = "Could not decode"
            rows.append({"title": title, "author": author, "uri": uri})
        return {
            "id": str(playlist.id),
            "name": name,
            "total": len(tracks),
            "shown": len(rows),
            "tracks": rows,
        }


PLAYLISTS_TEMPLATE = (
    BASE_CSS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-list-ul"></i> Playlists for {{ guild_name }}</h4>
    <p>Queue a saved playlist, or open one to see what is in it.</p>
  </div>

  {% if detail %}
    <div class="dz-panel">
      <h5><i class="fa fa-music"></i> {{ detail.name }}</h5>
      <p class="dz-hint">
        {{ detail.total }} track(s){% if detail.shown < detail.total %},
        showing the first {{ detail.shown }}{% endif %}.
      </p>
      <table class="dz-t">
        <thead><tr><th>#</th><th>Title</th><th>Artist</th></tr></thead>
        <tbody>
          {% for tr in detail.tracks %}
            <tr>
              <td style="opacity:.45; width:34px;">{{ loop.index }}</td>
              <td>
                {% if tr.uri %}<a href="{{ tr.uri }}" target="_blank" rel="noopener">{{ tr.title }}</a>
                {% else %}{{ tr.title }}{% endif %}
              </td>
              <td style="opacity:.7;">{{ tr.author }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  {% endif %}

  {% if not groups %}
    <p class="dz-empty">No playlists are visible to you here.</p>
  {% endif %}

  {% for g in groups %}
    <div class="dz-panel">
      <h5><i class="fa fa-folder-open-o"></i> {{ g.title }}</h5>
      <p class="dz-hint">{{ g.playlists|length }} playlist(s).</p>
      <table class="dz-t">
        <thead><tr><th>Name</th><th>Tracks</th><th>Source</th><th></th></tr></thead>
        <tbody>
          {% for p in g.playlists %}
            <tr>
              <td><b>{{ p.name }}</b></td>
              <td style="opacity:.7;">{{ p.size }}</td>
              <td style="max-width:220px; overflow:hidden; text-overflow:ellipsis;">
                {% if p.url %}<a href="{{ p.url }}" target="_blank" rel="noopener">link</a>
                {% else %}<span style="opacity:.4;">local</span>{% endif %}
              </td>
              <td style="white-space:nowrap; width:1%;">
                <form method="POST" style="display:inline;">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="playlist" value="{{ p.id }}" />
                  <button class="dz-btn round" name="action" value="inspect" title="Show tracks">
                    <i class="fa fa-eye"></i>
                  </button>
                </form>
                <form method="POST" style="display:inline;">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="playlist" value="{{ p.id }}" />
                  <button class="dz-btn round" name="action" value="queue" title="Add to queue">
                    <i class="fa fa-plus"></i>
                  </button>
                </form>
                <form method="POST" style="display:inline;">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="playlist" value="{{ p.id }}" />
                  <button class="dz-btn round primary" name="action" value="play" title="Play now">
                    <i class="fa fa-play"></i>
                  </button>
                </form>
                {% if is_staff %}
                  <form method="POST" style="display:inline;"
                        onsubmit="return confirm('Delete {{ p.name }}?');">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                    <input type="hidden" name="playlist" value="{{ p.id }}" />
                    <button class="dz-btn round danger" name="action" value="delete" title="Delete">
                      <i class="fa fa-trash-o"></i>
                    </button>
                  </form>
                {% endif %}
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  {% endfor %}
</div>
"""
)
