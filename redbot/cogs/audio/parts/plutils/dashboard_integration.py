from __future__ import annotations

import json
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
)

log = logging.getLogger("red.plutils.dashboard")

# Same mapping `[p]plutils logger` uses.
LOG_LEVELS = (
    (0, "Critical"),
    (1, "Error"),
    (2, "Warning"),
    (3, "Info"),
    (4, "Debug"),
    (5, "Verbose"),
    (6, "Trace"),
)


class UtilsDashboard:
    """PyLav's utility commands on the dashboard.

    Covers ``[p]plutils get`` (the current track's encoded string, title, author,
    source and raw player state), ``[p]plutils b64``, the ``[p]plutils cache``
    group, the slash command tree, and the PyLav logger level.
    """

    bot: t.Any
    pylav: t.Any

    @dashboard_page(
        name="diagnostics",
        description="Track inspection, the query cache and PyLav diagnostics.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_plutils_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        owner = await self.bot.is_owner(user)

        notifications: list[dict] = []
        result: dict = {}
        if kwargs.get("method") == "POST":
            notifications, result = await self._plutils_handle_post(guild, owner, kwargs)

        player = self.pylav.get_player(guild.id)
        current = getattr(player, "current", None)
        track = {}
        if current is not None:
            # These are all coroutine methods on a PyLav track, not plain
            # attributes; handing an un-awaited one to the template makes the
            # RPC layer fail to serialise the whole response.
            track = {
                "title": await current.title(),
                "author": await current.author(),
                "source": await current.source(),
                "encoded": current.encoded,
                "uri": await current.uri() or "",
                "artwork": await current.artworkUrl() or "",
            }

        cache_size = None
        if owner:
            try:
                cache_size = await self.pylav.query_cache_manager.size()
            except Exception:  # noqa: BLE001 - the cache backend may be down
                cache_size = None

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": PLUTILS_TEMPLATE,
                "audio_pages": audio_pages(await self.bot.is_owner(user)),
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_owner": owner,
                "connected": player is not None,
                "track": track,
                "cache_size": cache_size,
                "levels": LOG_LEVELS,
                "current_level": logging.getLevelName(
                    logging.getLogger("PyLav").getEffectiveLevel()
                ),
                "slash_tree": self._plutils_slash_tree() if owner else [],
                "result": result,
            },
        }

    def _plutils_slash_tree(self) -> list[dict]:
        """Flatten the app command tree, the way `[p]plutils slashes` renders it."""
        rows: list[dict] = []

        def walk(commands_, depth: int) -> None:
            for command in sorted(commands_, key=lambda c: c.name):
                if isinstance(command, discord.app_commands.Group):
                    rows.append({"name": command.name, "depth": depth, "kind": "group"})
                    walk(command.commands, depth + 1)
                elif isinstance(command, discord.app_commands.ContextMenu):
                    kind = {
                        discord.AppCommandType.user: "user menu",
                        discord.AppCommandType.message: "message menu",
                    }.get(command.type, "menu")
                    rows.append({"name": command.name, "depth": depth, "kind": kind})
                else:
                    rows.append({"name": command.name, "depth": depth, "kind": "command"})

        walk(self.bot.tree.get_commands(), 0)
        return rows

    async def _plutils_handle_post(
        self, guild: discord.Guild, owner: bool, kwargs: dict
    ) -> tuple[list[dict], dict]:
        field = form_reader(kwargs)
        action = field("action")

        try:
            if action == "decode":
                encoded = (field("b64") or "").strip()
                if not encoded:
                    return [{"message": "Paste a base64 track.", "category": "warning"}], {}
                try:
                    data = await self.pylav.decode_track(encoded, raise_on_failure=True)
                except Exception:  # noqa: BLE001
                    return [{"message": "That is not a valid base64 track string.",
                             "category": "warning"}], {}
                return [], {
                    "title": "Decoded track",
                    "json": json.dumps(data.info.to_dict(), indent=2, sort_keys=True),
                }

            if action == "player_state":
                player = self.pylav.get_player(guild.id)
                if player is None:
                    return [{"message": "No player in this server.", "category": "info"}], {}
                node_player = await player.fetch_node_player()
                payload = node_player.to_dict()
                return [], {
                    "title": "Raw player state",
                    "json": json.dumps(payload, indent=2, sort_keys=True, default=str),
                }

            if not owner:
                return [
                    {"message": "Only the bot owner can do that.", "category": "danger"}
                ], {}

            if action == "cache_clear":
                await self.pylav.query_cache_manager.wipe()
                return [{"message": "Query cache cleared.", "category": "success"}], {}

            if action == "cache_older":
                days = field.integer("days", 0) or 0
                if not 1 <= days <= 31:
                    return [
                        {"message": "Days must be between 1 and 31.", "category": "warning"}
                    ], {}
                await self.pylav.query_cache_manager.delete_older_than(days=days)
                return [
                    {"message": f"Cleared cache entries older than {days} day(s).",
                     "category": "success"}
                ], {}

            if action == "cache_query":
                from pylav.players.query.obj import Query

                raw = (field("query") or "").strip()
                if not raw:
                    return [{"message": "Enter a query.", "category": "warning"}], {}
                query = await Query.from_string(raw)
                await self.pylav.query_cache_manager.delete_query(query)
                return [
                    {"message": f"Cache cleared for {raw}.", "category": "success"}
                ], {}

            if action == "set_level":
                level = field.integer("level", -1)
                mapping = {
                    0: logging.CRITICAL,
                    1: logging.ERROR,
                    2: logging.WARNING,
                    3: logging.INFO,
                    4: logging.DEBUG,
                    5: logging.DEBUG - 3,
                    6: logging.DEBUG - 5,
                }
                if level not in mapping:
                    return [{"message": "Pick a valid level.", "category": "warning"}], {}
                logging.getLogger("PyLav").setLevel(mapping[level])
                name = logging.getLevelName(mapping[level])
                return [
                    {"message": f"PyLav log level set to {name}.", "category": "success"}
                ], {}
        except Exception as exc:  # noqa: BLE001
            log.exception("PyLavUtils dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}], {}

        return [{"message": f"Unknown action: {action}", "category": "warning"}], {}


PLUTILS_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-wrench"></i> PyLav utilities</h4>
    <p>Track details, the query cache and diagnostics for {{ guild_name }}.</p>
  </div>

  {{ subnav(name, audio_pages, 'diagnostics', guild) }}

  {% if result %}
    <div class="dz-panel">
      <h5><i class="fa fa-code"></i> {{ result.title }}</h5>
      <pre class="dz-text" style="max-height:420px; overflow:auto;">{{ result.json }}</pre>
    </div>
  {% endif %}

  <div class="dz-panel">
    <h5><i class="fa fa-music"></i> Current track</h5>
    {% if track %}
      <div class="dz-row" style="align-items:flex-start;">
        {% if track.artwork %}
          <img src="{{ track.artwork }}" alt=""
               style="width:120px; border-radius:10px;" />
        {% endif %}
        <table class="dz-t" style="flex:1 1 260px;">
          <tr><th>Title</th><td>{{ track.title }}</td></tr>
          <tr><th>Author</th><td>{{ track.author }}</td></tr>
          <tr><th>Source</th><td>{{ track.source }}</td></tr>
          {% if track.uri %}
            <tr><th>URL</th>
                <td><a href="{{ track.uri }}" target="_blank" rel="noopener">{{ track.uri }}</a></td></tr>
          {% endif %}
          <tr><th>Encoded</th>
              <td><code style="word-break:break-all;">{{ track.encoded }}</code></td></tr>
        </table>
      </div>
      <form method="POST" style="margin-top:12px;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <button class="dz-btn" name="action" value="player_state">
          <i class="fa fa-database"></i> Show raw player state
        </button>
      </form>
    {% else %}
      <p class="dz-empty">
        {% if connected %}The player is connected but nothing is playing.
        {% else %}No player in this server.{% endif %}
      </p>
    {% endif %}
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-code"></i> Decode a track</h5>
      <p class="dz-hint">Turn a base64 track string into readable JSON.</p>
      <div class="dz-row">
        <input class="dz-input" type="text" name="b64" placeholder="QAAA..."
               style="flex:1 1 260px;" />
        <button class="dz-btn primary" name="action" value="decode">
          <i class="fa fa-search"></i> Decode
        </button>
      </div>
    </div>
  </form>

  {% if is_owner %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-database"></i> Query cache</h5>
        <p class="dz-hint">
          {% if cache_size is not none %}{{ cache_size }} entries cached.
          {% else %}Cache size unavailable.{% endif %}
        </p>
        <div class="dz-row">
          <input class="dz-input" type="number" min="1" max="31" name="days"
                 placeholder="days" style="max-width:120px;" />
          <button class="dz-btn" name="action" value="cache_older">
            <i class="fa fa-clock-o"></i> Clear older than
          </button>
        </div>
        <div class="dz-row" style="margin-top:10px;">
          <input class="dz-input" type="text" name="query"
                 placeholder="a specific query or URL" style="flex:1 1 240px;" />
          <button class="dz-btn" name="action" value="cache_query">
            <i class="fa fa-eraser"></i> Clear that query
          </button>
          {{ confirm('Wipe the cache', 'cache_clear',
                     'Delete every cached query result?') }}
        </div>
      </div>
    </form>

    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-bug"></i> PyLav log level</h5>
        <p class="dz-hint">Currently {{ current_level }}.</p>
        <div class="dz-row">
          <select class="dz-select" name="level" style="max-width:200px;">
            {% for value, label in levels %}
              <option value="{{ value }}">{{ label }}</option>
            {% endfor %}
          </select>
          <button class="dz-btn primary" name="action" value="set_level">
            <i class="fa fa-save"></i> Apply
          </button>
        </div>
      </div>
    </form>

    <div class="dz-panel">
      <h5><i class="fa fa-list"></i> Slash commands</h5>
      <p class="dz-hint">{{ slash_tree|length }} entries registered on the tree.</p>
      <div style="max-height:420px; overflow-y:auto;">
        {% for row in slash_tree %}
          <div style="padding:3px 0; padding-left:{{ row.depth * 18 }}px; font-size:.85rem;">
            {% if row.kind == 'group' %}<i class="fa fa-folder-o"></i>
            {% else %}<i class="fa fa-terminal"></i>{% endif %}
            {{ row.name }}
            {% if row.kind not in ('group', 'command') %}
              <span class="dz-tag">{{ row.kind }}</span>
            {% endif %}
          </div>
        {% endfor %}
      </div>
    </div>
  {% endif %}
</div>
"""
)
