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
)

log = logging.getLogger("red.plconfig.dashboard")


class PyLavConfigDashboard:
    """PyLav's global settings from the dashboard.

    Covers every ``[p]plset`` option: the managed node and its auto-updates, the
    bundled external node, the local tracks folder, Discord activity updates,
    playlist refresh schedules, and the live player overview.
    """

    bot: t.Any
    pylav: t.Any

    @dashboard_page(
        name="pylav",
        description="PyLav library settings and active players.",
        methods=("GET", "POST"),
        is_owner=True,
    )
    async def dashboard_plconfig_page(
        self, user: discord.User, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._plset_handle_post(kwargs)

        config = await self.pylav.lib_db_manager.get_config().fetch_all()
        player_defaults = await self.pylav.player_config_manager.get_global_config().fetch_all()

        players = []
        for player in self.pylav.player_manager.connected_players:
            guild = getattr(player, "guild", None)
            current = getattr(player, "current", None)
            channel = getattr(player, "channel", None)
            try:
                queue_length = len(list(player.queue.raw_queue))
            except Exception:  # noqa: BLE001 - queue internals vary by version
                queue_length = 0
            players.append(
                {
                    "guild": getattr(guild, "name", "Unknown"),
                    "guild_id": str(getattr(guild, "id", "")),
                    "channel": getattr(channel, "name", ""),
                    "listeners": sum(
                        1 for m in getattr(channel, "members", []) or [] if not m.bot
                    ),
                    # `title` and `author` are coroutine methods on a PyLav
                    # track; passing the bound method through breaks the RPC
                    # response, so they are resolved before rendering.
                    "track": (await current.title() if current else "")
                    or "Nothing playing",
                    "author": (await current.author() if current else "") or "",
                    "paused": bool(getattr(player, "paused", False)),
                    "volume": int(getattr(player, "volume", 0) or 0),
                    "queue": queue_length,
                    "node": getattr(getattr(player, "node", None), "name", ""),
                }
            )

        nodes = []
        for node in self.pylav.node_manager.nodes:
            nodes.append(
                {
                    "name": node.name,
                    "identifier": str(node.identifier),
                    "available": bool(getattr(node, "available", False)),
                    "managed": bool(getattr(node, "managed", False)),
                    "players": len(getattr(node, "players", []) or []),
                    "region": getattr(node, "region", "") or "",
                }
            )

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": PLCONFIG_TEMPLATE,
                "audio_pages": audio_pages(await self.bot.is_owner(user)),
                "spotify_set": bool(
                    await self.bot.get_shared_api_tokens("spotify")
                ),
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "lib_version": str(self.pylav.lib_version),
                "cog_version": getattr(self, "__version__", ""),
                "managed_node": bool(config.get("enable_managed_node")),
                "auto_update": bool(config.get("auto_update_managed_nodes")),
                "update_activity": bool(config.get("update_bot_activity")),
                "use_bundled_lava_link": bool(config.get("use_bundled_lava_link_external")),
                "config_folder": str(config.get("config_folder") or ""),
                "localtrack_folder": str(config.get("localtrack_folder") or ""),
                "next_bundled": self._plset_when(
                    config.get("next_execution_update_bundled_playlists")
                ),
                "next_bundled_external": self._plset_when(
                    config.get("next_execution_update_bundled_external_playlists")
                ),
                "next_external": self._plset_when(
                    config.get("next_execution_update_external_playlists")
                ),
                "default_volume": player_defaults.get("volume"),
                "max_volume": player_defaults.get("max_volume"),
                "auto_play": bool(player_defaults.get("auto_play")),
                "shuffle": bool(player_defaults.get("shuffle")),
                "auto_shuffle": bool(player_defaults.get("auto_shuffle")),
                "self_deaf": bool(player_defaults.get("self_deaf")),
                "players": players,
                "nodes": nodes,
            },
        }

    @staticmethod
    def _plset_when(value) -> str:
        try:
            return value.strftime("%Y-%m-%d %H:%M UTC")
        except AttributeError:
            return ""

    async def _plset_handle_post(self, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")
        global_config = self.pylav.lib_db_manager.get_config()

        try:
            if action == "save_node":
                # These three take effect after a restart, same as the commands.
                await global_config.update_enable_managed_node(field.checked("managed_node"))
                await global_config.update_auto_update_managed_nodes(
                    field.checked("auto_update")
                )
                return [
                    {
                        "message": "Managed node settings saved. They apply after I restart.",
                        "category": "success",
                    }
                ]

            if action == "save_activity":
                enabled = field.checked("update_activity")
                await global_config.update_update_bot_activity(enabled)
                if not enabled:
                    await self.bot.change_presence(activity=None)
                    return [
                        {"message": "I will no longer show the current track as my activity.",
                         "category": "success"}
                    ]
                return [
                    {"message": "I will show the current track as my activity.",
                     "category": "success"}
                ]

            if action == "remove_lavalink":
                # Once removed the bundled lava.link node cannot come back.
                if not await global_config.fetch_use_bundled_lava_link_external():
                    return [
                        {"message": "The bundled lava.link node is already gone.",
                         "category": "info"}
                    ]
                await global_config.update_use_bundled_lava_link_external(False)
                await self.pylav.remove_node(1001)
                return [
                    {"message": "The bundled lava.link external node was removed permanently.",
                     "category": "success"}
                ]

            if action == "set_tracks":
                import aiopath

                folder = (field("localtrack_folder") or "").strip()
                if not folder:
                    return [{"message": "Enter a folder path.", "category": "warning"}]
                path = aiopath.AsyncPath(folder)
                if await path.is_file():
                    return [
                        {"message": "That path is a file, not a folder.", "category": "warning"}
                    ]
                if not await path.exists():
                    if not field.checked("create_folder"):
                        return [
                            {
                                "message": "That folder does not exist. Tick 'create it' to "
                                "make it.",
                                "category": "warning",
                            }
                        ]
                    await path.mkdir(parents=True, exist_ok=True)
                await self.pylav.update_localtracks_folder(folder=path)
                return [
                    {"message": f"Local tracks folder set to {folder}.", "category": "success"}
                ]

            if action == "stop_player":
                guild = self.bot.get_guild(field.integer("guild_id", 0) or 0)
                if guild is None:
                    return [{"message": "That server is not available.", "category": "warning"}]
                player = self.pylav.player_manager.get(guild.id)
                if player is None:
                    return [
                        {"message": f"No player in {guild.name}.", "category": "info"}
                    ]
                await player.disconnect(requester=self.bot.user)
                return [
                    {"message": f"Disconnected the player in {guild.name}.",
                     "category": "success"}
                ]
        except Exception as exc:  # noqa: BLE001
            log.exception("PyLavConfigurator dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]


PLCONFIG_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-cogs"></i> PyLav settings</h4>
    <p>Library {{ lib_version }}{% if cog_version %}, cog {{ cog_version }}{% endif %}.
       Node changes apply after a restart.</p>
  </div>

  {{ subnav(name, audio_pages, 'pylav', guild) }}

  <div class="dz-panel">
    <h5><i class="fa fa-spotify"></i> Spotify credentials</h5>
    <p class="dz-hint">
      Without these, Spotify links resolve through a fallback that finds the
      wrong track more often. Currently
      <b>{{ 'set' if spotify_set else 'not set' }}</b>.
    </p>
    <details>
      <summary style="cursor:pointer;">How to get them</summary>
      <ol class="dz-hint" style="margin:8px 0 8px 18px;">
        <li>Sign in at
          <a href="https://developer.spotify.com/dashboard/applications"
             target="_blank" rel="noopener">developer.spotify.com</a>.</li>
        <li>Click <b>Create an App</b> and fill in a name and description.</li>
        <li>Answer <b>No</b> when asked about commercial integration.</li>
        <li>Accept the terms, then copy the client ID and client secret.</li>
      </ol>
      <p class="dz-hint" style="margin:0;">
        Then add them under <a href="{{ url_for('base_blueprint.admin', page='api') }}">Admin → API Keys</a>, under the service
        <code>spotify</code>: <code>client_id</code> and <code>client_secret</code>.
      </p>
      <p class="dz-hint" style="margin-top:7px;">
        Only the bot owner can see or change those keys.
      </p>
    </details>
  </div>

  {{ stats([('Nodes', nodes|length),
            ('Active players', players|length),
            ('Managed node', 'on' if managed_node else 'off'),
            ('Activity updates', 'on' if update_activity else 'off')]) }}

  <div class="dz-grid two">
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-server"></i> Managed node</h5>
        <p class="dz-hint">The Lavalink node the bot runs itself.</p>
        <label class="dz-toggle">
          <input type="checkbox" name="managed_node" {% if managed_node %}checked{% endif %} />
          <span>Run a managed Lavalink node</span>
        </label>
        <label class="dz-toggle">
          <input type="checkbox" name="auto_update" {% if auto_update %}checked{% endif %} />
          <span>Keep the managed node updated automatically</span>
        </label>
        <div class="dz-row dz-save">
          <button class="dz-btn primary" name="action" value="save_node">
            <i class="fa fa-save"></i> Save
          </button>
          {% if use_bundled_lava_link %}
            {{ confirm('Remove lava.link node', 'remove_lavalink',
                       'Permanently remove the bundled lava.link external node? This cannot be undone.') }}
          {% endif %}
        </div>
      </div>
    </form>

    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-gamepad"></i> Discord activity</h5>
        <p class="dz-hint">Show the current track as the bot's status.</p>
        <label class="dz-toggle">
          <input type="checkbox" name="update_activity"
                 {% if update_activity %}checked{% endif %} />
          <span>Update my activity while playing</span>
        </label>
        <div class="dz-save">
          <button class="dz-btn primary" name="action" value="save_activity">
            <i class="fa fa-save"></i> Save
          </button>
        </div>
      </div>
    </form>
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-folder-open"></i> Local tracks folder</h5>
      <p class="dz-hint">Where PyLav looks for local audio files.
         Applies after a restart.</p>
      <input class="dz-input" type="text" name="localtrack_folder"
             value="{{ localtrack_folder }}" placeholder="/path/to/music" />
      <label class="dz-toggle">
        <input type="checkbox" name="create_folder" />
        <span>Create the folder if it does not exist</span>
      </label>
      <p class="dz-hint">Settings folder: <code>{{ config_folder }}</code></p>
      <div class="dz-save">
        <button class="dz-btn primary" name="action" value="set_tracks">
          <i class="fa fa-save"></i> Save folder
        </button>
      </div>
    </div>
  </form>

  <div class="dz-panel">
    <h5><i class="fa fa-sliders"></i> Player defaults</h5>
    <p class="dz-hint">Applied to servers that have not overridden them.</p>
    <div class="dz-grid two">
      <table class="dz-t">
        <tr><th>Volume</th><td>{{ default_volume }}%</td></tr>
        <tr><th>Maximum volume</th><td>{{ max_volume }}%</td></tr>
        <tr><th>Autoplay</th><td>{{ 'on' if auto_play else 'off' }}</td></tr>
      </table>
      <table class="dz-t">
        <tr><th>Shuffle</th><td>{{ 'on' if shuffle else 'off' }}</td></tr>
        <tr><th>Auto shuffle</th><td>{{ 'on' if auto_shuffle else 'off' }}</td></tr>
        <tr><th>Self deafen</th><td>{{ 'on' if self_deaf else 'off' }}</td></tr>
      </table>
    </div>
  </div>

  <div class="dz-panel">
    <h5><i class="fa fa-calendar"></i> Playlist refresh schedule</h5>
    <table class="dz-t">
      <tr><th>Bundled playlists</th><td>{{ next_bundled or 'unknown' }}</td></tr>
      <tr><th>Bundled external playlists</th>
          <td>{{ next_bundled_external or 'unknown' }}</td></tr>
      <tr><th>External playlists</th><td>{{ next_external or 'unknown' }}</td></tr>
    </table>
  </div>

  <div class="dz-panel">
    <h5><i class="fa fa-sitemap"></i> Nodes</h5>
    {% if nodes %}
      <table class="dz-t">
        <tr><th>Name</th><th>ID</th><th>Region</th><th>Players</th><th>Status</th></tr>
        {% for n in nodes %}
          <tr>
            <td>{{ n.name }}</td>
            <td><code>{{ n.identifier }}</code></td>
            <td>{{ n.region }}</td>
            <td>{{ n.players }}</td>
            <td>
              {% if n.available %}<span class="dz-tag good">up</span>
              {% else %}<span class="dz-tag bad">down</span>{% endif %}
              {% if n.managed %}<span class="dz-tag">managed</span>{% endif %}
            </td>
          </tr>
        {% endfor %}
      </table>
    {% else %}
      <p class="dz-empty">No nodes registered.</p>
    {% endif %}
  </div>

  <div class="dz-panel">
    <h5><i class="fa fa-play-circle"></i> Active players</h5>
    {% if players %}
      <table class="dz-t">
        <tr><th>Server</th><th>Channel</th><th>Listeners</th><th>Playing</th>
            <th>Queue</th><th>Volume</th><th></th></tr>
        {% for p in players %}
          <tr>
            <td>{{ p.guild }}</td>
            <td>{{ p.channel }}</td>
            <td>{{ p.listeners }}</td>
            <td>{{ p.track }}{% if p.author %} &mdash; {{ p.author }}{% endif %}
                {% if p.paused %}<span class="dz-tag warn">paused</span>{% endif %}</td>
            <td>{{ p.queue }}</td>
            <td>{{ p.volume }}%</td>
            <td>
              <form method="POST">
                <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                <input type="hidden" name="guild_id" value="{{ p.guild_id }}" />
                {{ confirm('Stop', 'stop_player',
                           'Disconnect the player in ' ~ p.guild ~ '?') }}
              </form>
            </td>
          </tr>
        {% endfor %}
      </table>
    {% else %}
      <p class="dz-empty">No active players.</p>
    {% endif %}
  </div>
</div>
"""
)
