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
)

log = logging.getLogger("red.plnodes.dashboard")

# Sources a node can be told to ignore, as offered by the add/manage menus.
KNOWN_SOURCES = (
    "youtube",
    "youtubemusic",
    "spotify",
    "applemusic",
    "deezer",
    "yandexmusic",
    "soundcloud",
    "bandcamp",
    "twitch",
    "vimeo",
    "http",
    "local",
    "speak",
    "tts",
    "gctts",
    "getyarn",
    "clypit",
    "pornhub",
    "reddit",
    "ocremix",
    "tiktok",
    "mixcloud",
    "soundgasm",
)


class NodesDashboard:
    """Lavalink node management from the dashboard.

    Replaces the interactive ``[p]plnode add``, ``[p]plnode manage``,
    ``[p]plnode remove`` and ``[p]plnode list`` menus with a form: add a node,
    edit its connection details, choose which sources it may serve, and remove it.
    """

    bot: t.Any
    pylav: t.Any

    @dashboard_page(
        name="nodes",
        description="Add, edit and remove Lavalink nodes.",
        methods=("GET", "POST"),
        is_owner=True,
    )
    async def dashboard_plnodes_page(
        self, user: discord.User, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._plnode_handle_post(kwargs)

        nodes = []
        for node in sorted(self.pylav.node_manager.nodes, key=lambda n: n.name.lower()):
            nodes.append(await self._plnode_row(node))

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": PLNODES_TEMPLATE,
                "audio_pages": audio_pages(await self.bot.is_owner(user)),
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "nodes": nodes,
                "sources": KNOWN_SOURCES,
                "available": sum(1 for n in nodes if n["available"]),
                "editable": sum(1 for n in nodes if not n["locked"]),
            },
        }

    async def _plnode_row(self, node) -> dict:
        from pylav.constants.builtin_nodes import BUNDLED_NODES_IDS_HOST_MAPPING

        # `fetch_all` is what `[p]plnode remove` dumps, so it holds every field
        # the menus can edit.
        try:
            data = await node.config.fetch_all()
        except Exception:  # noqa: BLE001 - a node can be unreachable
            data = {}
        server = (data.get("yaml") or {}).get("server") or {}
        disabled = sorted(data.get("disabled_sources") or [])
        search_only = bool(data.get("search_only"))
        ssl = bool(data.get("ssl"))
        timeout = int(data.get("resume_timeout") or 600)

        # PyLav owns its bundled nodes; the menus refuse to edit them too.
        locked = bool(
            getattr(node, "managed", False)
            or node.identifier in BUNDLED_NODES_IDS_HOST_MAPPING
            or node.identifier == 31415
        )
        return {
            "name": node.name,
            "identifier": str(node.identifier),
            "host": server.get("address") or server.get("host") or "",
            "port": server.get("port") or "",
            "available": bool(getattr(node, "available", False)),
            "managed": bool(getattr(node, "managed", False)),
            "locked": locked,
            "players": len(getattr(node, "players", []) or []),
            "region": getattr(node, "region", "") or "",
            "search_only": search_only,
            "ssl": ssl,
            "timeout": timeout,
            "disabled_sources": disabled,
            "source_options": [
                {
                    "id": source,
                    "name": source,
                    "group": "Disabled" if source in disabled else "Enabled",
                    "selected": source in disabled,
                    "warn": False,
                }
                for source in KNOWN_SOURCES
            ],
        }

    async def _plnode_handle_post(self, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

        try:
            if action == "add":
                name = (field("name") or "").strip()
                host = (field("host") or "").strip()
                password = (field("password") or "").strip()
                port = field.integer("port", 0) or 0
                if not (name and host and password and port):
                    return [
                        {
                            "message": "Name, host, port and password are all required.",
                            "category": "warning",
                        }
                    ]
                identifier = field.integer("identifier", 0) or 0
                if not identifier:
                    # Keep away from the low IDs PyLav reserves for its own nodes.
                    existing = {n.identifier for n in self.pylav.node_manager.nodes}
                    identifier = max([*existing, 100000]) + 1
                node = await self.pylav.add_node(
                    host=host,
                    password=password,
                    unique_identifier=identifier,
                    port=port,
                    name=name,
                    resume_timeout=field.integer("timeout", 600) or 600,
                    ssl=field.checked("ssl"),
                    reconnect_attempts=-1,
                    search_only=field.checked("search_only"),
                    managed=False,
                    disabled_sources=field.many("disabled_sources"),
                )
                if node is None:
                    return [{"message": "PyLav refused that node.", "category": "danger"}]
                return [
                    {
                        "message": f"Node {name} added. It may take a moment to connect.",
                        "category": "success",
                    }
                ]

            node = self._plnode_find(field("identifier"))
            if node is None:
                return [{"message": "That node no longer exists.", "category": "warning"}]

            from pylav.constants.builtin_nodes import BUNDLED_NODES_IDS_HOST_MAPPING

            locked = (
                getattr(node, "managed", False)
                or node.identifier in BUNDLED_NODES_IDS_HOST_MAPPING
                or node.identifier == 31415
            )
            if locked:
                return [
                    {
                        "message": f"{node.name} is managed by PyLav and cannot be changed.",
                        "category": "warning",
                    }
                ]

            if action == "remove":
                name = node.name
                await self.pylav.remove_node(node.identifier)
                return [{"message": f"Node {name} removed.", "category": "success"}]

            if action == "save":
                name = (field("name") or "").strip() or node.name
                await node.config.update_name(name)
                await node.config.update_search_only(field.checked("search_only"))
                await node.config.update_ssl(field.checked("ssl"))
                timeout = field.integer("timeout", 0) or 0
                if timeout:
                    await node.config.update_resume_timeout(timeout)

                yaml_data = await node.config.fetch_yaml()
                host = (field("host") or "").strip()
                port = field.integer("port", 0) or 0
                password = (field("password") or "").strip()
                if host:
                    yaml_data["server"]["address"] = host
                if port:
                    yaml_data["server"]["port"] = port
                if password:
                    yaml_data["lavalink"]["server"]["password"] = password
                await node.config.update_yaml(yaml_data)

                await node.update_disabled_sources(set(field.many("disabled_sources")))

                # Reconnect so the new connection details take effect now.
                await self.pylav.remove_node(node.identifier)
                await self.pylav.add_node(**(await node.config.get_connection_args()))
                return [
                    {"message": f"Node {name} updated and reconnected.", "category": "success"}
                ]
        except Exception as exc:  # noqa: BLE001
            log.exception("PyLavNodes dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    def _plnode_find(self, identifier: str | None):
        try:
            wanted = int(identifier)
        except (TypeError, ValueError):
            return None
        return next(
            (n for n in self.pylav.node_manager.nodes if n.identifier == wanted), None
        )


PLNODES_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-sitemap"></i> Lavalink nodes</h4>
    <p>Every node setting, as a form. Nodes PyLav
       manages itself are shown but cannot be edited.</p>
  </div>

  {{ subnav(name, audio_pages, 'nodes', guild) }}

  {{ stats([('Nodes', nodes|length),
            ('Online', available),
            ('Editable', editable)]) }}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-plus"></i> Add a node</h5>
      <div class="dz-grid three">
        <div>
          <label class="dz-label">Name</label>
          <input class="dz-input" type="text" name="name" placeholder="my-node" required />
        </div>
        <div>
          <label class="dz-label">Host</label>
          <input class="dz-input" type="text" name="host" placeholder="lavalink.example.com"
                 required />
        </div>
        <div>
          <label class="dz-label">Port</label>
          <input class="dz-input" type="number" name="port" value="2333" required />
        </div>
      </div>
      <div class="dz-grid three">
        <div>
          <label class="dz-label">Password</label>
          <input class="dz-input" type="password" name="password" required />
        </div>
        <div>
          <label class="dz-label">Resume timeout (seconds)</label>
          <input class="dz-input" type="number" name="timeout" value="600" />
        </div>
        <div>
          <label class="dz-label">Node ID <span class="dz-tag">optional</span></label>
          <input class="dz-input" type="number" name="identifier"
                 placeholder="auto" />
        </div>
      </div>
      <div class="dz-grid two">
        <div>
          <label class="dz-toggle">
            <input type="checkbox" name="ssl" />
            <span>Use SSL (wss / https)</span>
          </label>
          <label class="dz-toggle">
            <input type="checkbox" name="search_only" />
            <span>Search only &mdash; never play through this node</span>
          </label>
        </div>
        <div>
          <label class="dz-label">Sources to disable</label>
          <select class="dz-select" name="disabled_sources" multiple size="8">
            {% for s in sources %}<option value="{{ s }}">{{ s }}</option>{% endfor %}
          </select>
        </div>
      </div>
      <div class="dz-save">
        <button class="dz-btn primary" name="action" value="add">
          <i class="fa fa-plus"></i> Add node
        </button>
      </div>
    </div>
  </form>

  {% for n in nodes %}
    <div class="dz-panel">
      <h5>
        <i class="fa fa-server"></i> {{ n.name }}
        {% if n.available %}<span class="dz-tag good">online</span>
        {% else %}<span class="dz-tag bad">offline</span>{% endif %}
        {% if n.locked %}<span class="dz-tag">managed by PyLav</span>{% endif %}
        {% if n.search_only %}<span class="dz-tag">search only</span>{% endif %}
      </h5>
      <p class="dz-hint">
        ID <code>{{ n.identifier }}</code>
        {%- if n.region %} &middot; {{ n.region }}{% endif %}
        &middot; {{ n.players }} player(s)
        {%- if n.disabled_sources %} &middot; {{ n.disabled_sources|length }} source(s)
          disabled{% endif %}
      </p>

      {% if n.locked %}
        <table class="dz-t">
          <tr><th>Host</th><td>{{ n.host }}:{{ n.port }}</td></tr>
          <tr><th>SSL</th><td>{{ 'yes' if n.ssl else 'no' }}</td></tr>
          <tr><th>Disabled sources</th>
              <td>{{ n.disabled_sources|join(', ') or 'none' }}</td></tr>
        </table>
      {% else %}
        <form method="POST">
          <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
          <input type="hidden" name="identifier" value="{{ n.identifier }}" />
          <div class="dz-grid three">
            <div>
              <label class="dz-label">Name</label>
              <input class="dz-input" type="text" name="name" value="{{ n.name }}" />
            </div>
            <div>
              <label class="dz-label">Host</label>
              <input class="dz-input" type="text" name="host" value="{{ n.host }}" />
            </div>
            <div>
              <label class="dz-label">Port</label>
              <input class="dz-input" type="number" name="port" value="{{ n.port }}" />
            </div>
          </div>
          <div class="dz-grid three">
            <div>
              <label class="dz-label">Password <span class="dz-tag">leave blank to keep</span></label>
              <input class="dz-input" type="password" name="password" placeholder="unchanged" />
            </div>
            <div>
              <label class="dz-label">Resume timeout (seconds)</label>
              <input class="dz-input" type="number" name="timeout" value="{{ n.timeout }}" />
            </div>
            <div>
              <label class="dz-toggle">
                <input type="checkbox" name="ssl" {% if n.ssl %}checked{% endif %} />
                <span>Use SSL</span>
              </label>
              <label class="dz-toggle">
                <input type="checkbox" name="search_only"
                       {% if n.search_only %}checked{% endif %} />
                <span>Search only</span>
              </label>
            </div>
          </div>
          <label class="dz-label">Sources to disable</label>
          {{ picker('disabled_sources', n.source_options, true, 8, 'Search sources...') }}
          <div class="dz-row dz-save">
            <button class="dz-btn primary" name="action" value="save">
              <i class="fa fa-save"></i> Save and reconnect
            </button>
            {{ confirm('Remove node', 'remove',
                       'Remove the node ' ~ n.name ~ ' from PyLav?') }}
          </div>
        </form>
      {% endif %}
    </div>
  {% else %}
    <div class="dz-panel"><p class="dz-empty">No nodes registered.</p></div>
  {% endfor %}
</div>
"""
)
