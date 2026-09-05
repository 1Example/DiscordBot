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

log = logging.getLogger("red.pllocal.dashboard")

RESULT_LIMIT = 200


class LocalFilesDashboard:
    """Local file playback from the dashboard.

    Replaces ``/local`` and ``[p]pllocalset update``: browse or search the local
    track cache, queue an entry (recursively for folders), and refresh the cache.
    """

    bot: t.Any
    pylav: t.Any

    @dashboard_page(
        name="local-files",
        description="Browse and queue local files.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_pllocal_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._pllocal_handle_post(member, guild, kwargs)

        cache = getattr(self.pylav, "local_tracks_cache", None)
        ready = bool(getattr(cache, "is_ready", False))
        entries = []
        query = (form_reader(kwargs)("search") or "").strip().lower()
        if ready:
            import os

            for digest, entry in cache.hexdigest_to_query.items():
                # `_query` is the filesystem path the cache indexed.
                path = f"{entry._query}"
                if query and query not in path.lower():
                    continue
                is_folder = os.path.isdir(path)
                entries.append(
                    {
                        "id": digest,
                        "name": await entry.query_to_string(
                            max_length=110, with_emoji=True, no_extension=True,
                            add_ellipsis=True,
                        ),
                        "group": "Folders" if is_folder else "Files",
                        "selected": False,
                        "warn": False,
                    }
                )
                if len(entries) >= RESULT_LIMIT:
                    break
            entries.sort(key=lambda e: (e["group"], e["name"].lower()))

        player = self.pylav.get_player(guild.id)
        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": PLLOCAL_TEMPLATE,
                "audio_pages": audio_pages(await self.bot.is_owner(user)),
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_owner": await self.bot.is_owner(user),
                "ready": ready,
                "total": len(getattr(cache, "path_to_track", {}) or {}) if cache else 0,
                "entries": entries,
                "shown": len(entries),
                "limit": RESULT_LIMIT,
                "search": query,
                "connected": player is not None,
                "channel": getattr(getattr(player, "channel", None), "name", ""),
            },
        }

    async def _pllocal_handle_post(
        self, member: discord.Member, guild: discord.Guild, kwargs: dict
    ) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

        try:
            if action == "refresh":
                if not await self.bot.is_owner(member):
                    return [
                        {"message": "Only the bot owner can refresh the cache.",
                         "category": "danger"}
                    ]
                await self.pylav.local_tracks_cache.update()
                count = len(self.pylav.local_tracks_cache.path_to_track)
                return [
                    {"message": f"Local track cache refreshed; {count} tracks found.",
                     "category": "success"}
                ]

            if action == "search":
                # The search term is read back on the GET-style render below.
                return []

            if action == "play":
                digest = field("entry")
                cache = getattr(self.pylav, "local_tracks_cache", None)
                if not cache or digest not in cache.hexdigest_to_query:
                    return [
                        {"message": "That entry is no longer in the cache.",
                         "category": "warning"}
                    ]
                entry = cache.hexdigest_to_query[digest]
                entry._recursive = field.checked("recursive")

                player = self.pylav.get_player(guild.id)
                if player is None:
                    channel = getattr(getattr(member, "voice", None), "channel", None)
                    if channel is None:
                        return [
                            {
                                "message": "Join a voice channel first so I know where "
                                "to connect.",
                                "category": "warning",
                            }
                        ]
                    permission = channel.permissions_for(guild.me)
                    if not (permission.connect and permission.speak):
                        return [
                            {
                                "message": f"I cannot connect or speak in {channel.name}.",
                                "category": "warning",
                            }
                        ]
                    player = await self.pylav.connect_player(
                        channel=channel, requester=member
                    )

                successful, count, _failed = await self.pylav.get_all_tracks_for_queries(
                    entry, requester=member, player=player
                )
                if not count:
                    return [{"message": "Nothing playable was found.", "category": "warning"}]
                if count == 1:
                    await player.add(requester=member.id, track=successful[0])
                else:
                    await player.bulk_add(
                        requester=member.id, tracks_and_queries=successful
                    )
                if not (player.is_active or player.queue.empty()):
                    await player.next(requester=member)
                return [
                    {"message": f"Queued {count} track(s).", "category": "success"}
                ]
        except Exception as exc:  # noqa: BLE001
            log.exception("PyLavLocalFiles dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]


PLLOCAL_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-folder-open"></i> Local files</h4>
    <p>
      {% if ready %}{{ total }} track(s) cached.
      {% else %}The local track cache is not ready yet.{% endif %}
      {% if connected %} Player connected to {{ channel }}.{% endif %}
    </p>
  </div>

  {{ subnav(name, audio_pages, 'local-files', guild) }}

  {% if not ready %}
    <div class="dz-panel">
      <p class="dz-empty">Nothing to show until the cache has been built.</p>
      {% if is_owner %}
        <form method="POST">
          <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
          <button class="dz-btn primary" name="action" value="refresh">
            <i class="fa fa-refresh"></i> Build the cache
          </button>
        </form>
      {% endif %}
    </div>
  {% else %}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-search"></i> Find and queue</h5>
      <p class="dz-hint">
        Showing {{ shown }} entr{{ 'y' if shown == 1 else 'ies' }}
        {%- if shown >= limit %} (capped at {{ limit }}; narrow the search){% endif %}.
      </p>
      <div class="dz-row" style="margin-bottom:12px;">
        <input class="dz-input" type="text" name="search" value="{{ search }}"
               placeholder="filter by name" style="flex:1 1 220px;" />
        <button class="dz-btn" name="action" value="search">
          <i class="fa fa-search"></i> Filter
        </button>
        {% if is_owner %}
          <button class="dz-btn" name="action" value="refresh">
            <i class="fa fa-refresh"></i> Refresh cache
          </button>
        {% endif %}
      </div>
      {% if entries %}
        {{ picker('entry', entries, false, 12, 'Search local files...') }}
        <label class="dz-toggle">
          <input type="checkbox" name="recursive" />
          <span>For a folder, include everything inside it</span>
        </label>
        <div class="dz-save">
          <button class="dz-btn primary" name="action" value="play">
            <i class="fa fa-play"></i> Queue selection
          </button>
        </div>
      {% else %}
        <p class="dz-empty">Nothing matches that filter.</p>
      {% endif %}
    </div>
  </form>

  {% endif %}
</div>
"""
)
