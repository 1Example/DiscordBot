from __future__ import annotations

import logging
import re
import typing as t

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    dashboard_page,
    form_reader,
)

log = logging.getLogger("red.plmanagednode.dashboard")

# The plugin names `[p]plmanaged settings plugins enable/disable` accepts.
PLUGINS = (
    "lavasrc",
    "skybot",
    "sponsorblock",
    "lavalink-filter",
    "lava-xm",
    "lavasearch",
    "youtube",
    "lavalyrics",
)

# `[p]plmanaged settings server` settings, with their accepted values and the
# help text the command prints for each.
SERVER_SETTINGS: dict[str, dict[str, t.Any]] = {
    "bufferDurationMs": {
        "kind": "int",
        "range": (40, 2000),
        "help": "Length of the NAS buffer in milliseconds. Higher values survive longer "
        "GC pauses but use more RAM. 0 disables JDA-NAS.",
    },
    "frameBufferDurationMs": {
        "kind": "int",
        "range": (1000, 10000),
        "help": "How many milliseconds of audio to keep buffered. Higher values use more RAM.",
    },
    "trackStuckThresholdMs": {
        "kind": "int",
        "range": (5000, 20000),
        "help": "How long a track may return no audio before it counts as stuck.",
    },
    "youtubePlaylistLoadLimit": {
        "kind": "int",
        "range": (5, 100),
        "help": "Pages to load for a YouTube playlist; each page holds 100 songs.",
    },
    "opusEncodingQuality": {
        "kind": "int",
        "range": (0, 10),
        "help": "Opus encoder quality. 10 sounds best and costs the most CPU.",
    },
    "playerUpdateInterval": {
        "kind": "int",
        "range": (1, 84600),
        "help": "How often, in seconds, to send player updates to clients.",
    },
    "resamplingQuality": {
        "kind": "choice",
        "choices": ("LOW", "MEDIUM", "HIGH"),
        "help": "Quality of resampling operations. HIGH uses the most CPU.",
    },
    "useSeekGhosting": {
        "kind": "bool",
        "help": "Keep playing the buffered audio while a seek is still in progress.",
    },
    "youtubeSearchEnabled": {
        "kind": "bool",
        "help": "Allow YouTube searches on this node. Apple Music and Spotify depend on it.",
    },
    "soundcloudSearchEnabled": {
        "kind": "bool",
        "help": "Allow SoundCloud searches on this node.",
    },
}


class ManagedNodeDashboard:
    """The managed Lavalink node's configuration, as a form.

    Covers every ``[p]plmanaged`` command: enabling the node and its auto-updates,
    updating the jar, the heap size, host and port, plugins, sources, filters, the
    server tuning settings, and resetting IP rotation or the HTTP proxy.
    """

    bot: t.Any
    pylav: t.Any

    @dashboard_page(
        name="managed-node",
        description="Configure the managed Lavalink node.",
        methods=("GET", "POST"),
        is_owner=True,
    )
    async def dashboard_plmanaged_page(
        self, user: discord.User, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._plmanaged_handle_post(kwargs)

        global_config = self.pylav.lib_db_manager.get_config()
        extras = await global_config.fetch_extras()
        node_config = self.pylav._node_config_manager.bundled_node_config()
        data = await node_config.fetch_yaml()

        server = data.get("server") or {}
        lavalink_server = (data.get("lavalink") or {}).get("server") or {}
        plugins = (data.get("lavalink") or {}).get("plugins") or []
        plugin_sources = data.get("plugins") or {}

        sources = dict(lavalink_server.get("sources") or {})
        for key in ("lavasrc", "dunctebot"):
            sources.update((plugin_sources.get(key) or {}).get("sources") or {})

        settings_rows = []
        for name, spec in SERVER_SETTINGS.items():
            settings_rows.append(
                {
                    "name": name,
                    "kind": spec["kind"],
                    "help": spec["help"],
                    "value": lavalink_server.get(name),
                    "min": spec.get("range", (None, None))[0],
                    "max": spec.get("range", (None, None))[1],
                    "choices": spec.get("choices", ()),
                }
            )

        controller = getattr(self.pylav, "managed_node_controller", None)
        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": PLMANAGED_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "enabled": bool(await global_config.fetch_enable_managed_node()),
                "auto_update": bool(await global_config.fetch_auto_update_managed_nodes()),
                "heap_size": extras.get("max_ram") or "",
                "java_path": str(await global_config.fetch_java_path() or ""),
                "download_id": await global_config.fetch_download_id(),
                "running": bool(getattr(controller, "is_running", False)),
                "host": server.get("address") or server.get("host") or "",
                "port": server.get("port") or "",
                "installed_plugins": [
                    {
                        "dependency": p.get("dependency", ""),
                        "repository": p.get("repository", ""),
                    }
                    for p in plugins
                ],
                "plugins": PLUGINS,
                "sources": sorted(
                    ({"name": k, "enabled": bool(v)} for k, v in sources.items()),
                    key=lambda s: s["name"],
                ),
                "filters": sorted(
                    (
                        {"name": k, "enabled": bool(v)}
                        for k, v in (lavalink_server.get("filters") or {}).items()
                    ),
                    key=lambda f: f["name"],
                ),
                "settings": settings_rows,
            },
        }

    async def _plmanaged_handle_post(self, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")
        global_config = self.pylav.lib_db_manager.get_config()
        node_config = self.pylav._node_config_manager.bundled_node_config()

        try:
            if action == "save_toggles":
                await global_config.update_enable_managed_node(field.checked("enabled"))
                await global_config.update_auto_update_managed_nodes(
                    field.checked("auto_update")
                )
                return [
                    {"message": "Saved. These apply after I restart.", "category": "success"}
                ]

            if action == "check_update":
                controller = self.pylav.managed_node_controller
                controller._up_to_date = False
                upstream = await controller.get_ci_latest_info()
                number = upstream["number"]
                if number == await self.pylav._config.fetch_download_id():
                    return [
                        {"message": "The managed node is already up to date.",
                         "category": "info"}
                    ]
                return [
                    {
                        "message": f"Build {number} is available; use Update now to "
                        "download it.",
                        "category": "warning",
                    }
                ]

            if action == "update":
                controller = self.pylav.managed_node_controller
                controller._up_to_date = False
                await controller._download_jar(forced=True)
                return [
                    {
                        "message": "The managed node jar was updated. Restart the bot to "
                        "run it.",
                        "category": "success",
                    }
                ]

            if action == "heapsize":
                size = (field("heap_size") or "").strip().upper()
                if not re.match(r"^\d+[MG]$", size):
                    return [
                        {"message": "Heap size must look like 512M or 2G.",
                         "category": "warning"}
                    ]
                amount = int(size[:-1]) * (1024**2 if size[-1] == "M" else 1024**3)
                if amount < 64 * 1024**2:
                    return [
                        {"message": "Heap size must be at least 64M; 1G or more is "
                                    "recommended.", "category": "warning"}
                    ]
                extras = await global_config.fetch_extras()
                extras["max_ram"] = size
                await global_config.update_extras(extras)
                return [
                    {"message": f"Managed node heap size set to {size}. Restart to apply.",
                     "category": "success"}
                ]

            if action == "save_connection":
                host = (field("host") or "").strip()
                port = field.integer("port", 0) or 0
                if not host and not port:
                    return [{"message": "Nothing to change.", "category": "warning"}]
                data = await node_config.fetch_yaml()
                if host:
                    data["server"]["host"] = host
                if port:
                    data["server"]["port"] = port
                await node_config.update_yaml(data)
                return [
                    {"message": "Managed node address saved. Restart to apply.",
                     "category": "success"}
                ]

            if action == "save_sources":
                enabled = set(field.many("sources"))
                data = await node_config.fetch_yaml()
                changed = self._plmanaged_apply_flags(
                    data, "sources", enabled, field.many("all_sources")
                )
                await node_config.update_yaml(data)
                return [
                    {"message": f"{changed} source(s) updated. Restart to apply.",
                     "category": "success"}
                ]

            if action == "save_filters":
                enabled = set(field.many("filters"))
                data = await node_config.fetch_yaml()
                changed = 0
                for name in field.many("all_filters"):
                    want = name in enabled
                    if data["lavalink"]["server"]["filters"].get(name) != want:
                        data["lavalink"]["server"]["filters"][name] = want
                        changed += 1
                await node_config.update_yaml(data)
                return [
                    {"message": f"{changed} filter(s) updated. Restart to apply.",
                     "category": "success"}
                ]

            if action == "save_settings":
                data = await node_config.fetch_yaml()
                changed, problems = self._plmanaged_apply_settings(data, field)
                if problems:
                    return [{"message": p, "category": "warning"} for p in problems]
                await node_config.update_yaml(data)
                return [
                    {"message": f"{changed} setting(s) updated. Restart to apply.",
                     "category": "success"}
                ]

            if action in ("plugin_enable", "plugin_disable"):
                plugin = (field("plugin") or "").lower().strip()
                if plugin not in PLUGINS:
                    return [{"message": f"Unknown plugin {plugin}.", "category": "warning"}]
                command = self.bot.get_command(
                    "plmanaged settings plugins "
                    + ("enable" if action == "plugin_enable" else "disable")
                )
                if command is None:
                    return [
                        {"message": "The plugin commands are not available.",
                         "category": "danger"}
                    ]
                # The plugin commands build the maven coordinates themselves, so
                # reuse them rather than duplicating that mapping here.
                await command.callback(self, _PluginContext(self.bot), plugin=plugin)
                verb = "enabled" if action == "plugin_enable" else "disabled"
                return [
                    {"message": f"Plugin {plugin} {verb}. Restart to apply.",
                     "category": "success"}
                ]

            if action in ("reset_iprotation", "reset_httpproxy"):
                from pylav.constants.node import NODE_DEFAULT_SETTINGS

                data = await node_config.fetch_yaml()
                key = "ratelimit" if action == "reset_iprotation" else "httpConfig"
                data["lavalink"]["server"][key] = NODE_DEFAULT_SETTINGS["lavalink"][
                    "server"
                ][key]
                await node_config.update_yaml(data)
                label = "IP rotation" if key == "ratelimit" else "HTTP proxy"
                return [
                    {"message": f"{label} reset to defaults. Restart to apply.",
                     "category": "success"}
                ]
        except Exception as exc:  # noqa: BLE001
            log.exception("PyLavManagedNode dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    @staticmethod
    def _plmanaged_apply_flags(data: dict, key: str, enabled: set, names: list) -> int:
        """Flip source flags wherever they live in the node's YAML."""
        changed = 0
        lavalink = data["lavalink"]["server"].get(key) or {}
        plugins = data.get("plugins") or {}
        for name in names:
            want = name in enabled
            if name in lavalink:
                if lavalink[name] != want:
                    lavalink[name] = want
                    changed += 1
                continue
            for plugin in ("lavasrc", "dunctebot"):
                section = (plugins.get(plugin) or {}).get(key)
                if section is not None and name in section:
                    if section[name] != want:
                        section[name] = want
                        changed += 1
                    break
        return changed

    @staticmethod
    def _plmanaged_apply_settings(data: dict, field) -> tuple[int, list[str]]:
        server = data["lavalink"]["server"]
        changed = 0
        problems: list[str] = []
        for name, spec in SERVER_SETTINGS.items():
            if spec["kind"] == "bool":
                value: t.Any = field.checked(f"setting_{name}")
            else:
                raw = field(f"setting_{name}")
                if raw in (None, ""):
                    continue
                if spec["kind"] == "int":
                    try:
                        value = int(raw)
                    except (TypeError, ValueError):
                        problems.append(f"{name} must be a whole number.")
                        continue
                    low, high = spec["range"]
                    if not low <= value <= high:
                        problems.append(f"{name} must be between {low} and {high}.")
                        continue
                else:
                    value = str(raw).upper()
                    if value not in spec["choices"]:
                        problems.append(
                            f"{name} must be one of {', '.join(spec['choices'])}."
                        )
                        continue
            if server.get(name) != value:
                server[name] = value
                changed += 1
        return changed, problems


class _PluginContext:
    """Minimal context for reusing the plugin enable/disable command callbacks."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.interaction = None
        self.pylav = bot.pylav
        # `construct_embed` passes this object to `bot.get_embed_color`, which
        # only ever looks for a guild on it.
        self.guild = None
        self.channel = None

    async def send(self, *args: t.Any, **kwargs: t.Any) -> None:
        return None

    async def defer(self, *args: t.Any, **kwargs: t.Any) -> None:
        return None


PLMANAGED_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-server"></i> Managed Lavalink node</h4>
    <p>The node the bot runs itself. Almost everything here takes effect after a
       restart.</p>
  </div>

  {{ stats([('Node', 'enabled' if enabled else 'disabled'),
            ('Auto update', 'on' if auto_update else 'off'),
            ('Build', download_id or 'unknown'),
            ('Heap size', heap_size or 'default')]) }}

  <div class="dz-grid two">
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-power-off"></i> Node</h5>
        <label class="dz-toggle">
          <input type="checkbox" name="enabled" {% if enabled %}checked{% endif %} />
          <span>Run the managed node</span>
        </label>
        <label class="dz-toggle">
          <input type="checkbox" name="auto_update" {% if auto_update %}checked{% endif %} />
          <span>Update it automatically</span>
        </label>
        <div class="dz-row dz-save">
          <button class="dz-btn primary" name="action" value="save_toggles">
            <i class="fa fa-save"></i> Save
          </button>
          <button class="dz-btn" name="action" value="check_update">
            <i class="fa fa-search"></i> Check for updates
          </button>
          {{ confirm('Update now', 'update',
                     'Download the latest managed node build?', '', 'fa-download') }}
        </div>
        <p class="dz-hint" style="margin-top:10px;">
          Java: <code>{{ java_path or 'default' }}</code>
        </p>
      </div>
    </form>

    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-plug"></i> Address and memory</h5>
        <label class="dz-label">Host</label>
        <input class="dz-input" type="text" name="host" value="{{ host }}"
               placeholder="localhost" />
        <label class="dz-label">Port</label>
        <input class="dz-input" type="number" name="port" value="{{ port }}" />
        <div class="dz-save">
          <button class="dz-btn primary" name="action" value="save_connection">
            <i class="fa fa-save"></i> Save address
          </button>
        </div>
        <label class="dz-label" style="margin-top:14px;">Maximum heap size</label>
        <div class="dz-row">
          <input class="dz-input" type="text" name="heap_size" value="{{ heap_size }}"
                 placeholder="2G" style="max-width:160px;" />
          <button class="dz-btn" name="action" value="heapsize">
            <i class="fa fa-microchip"></i> Set
          </button>
        </div>
        <p class="dz-hint">A ceiling, not a reservation. At least 64M; 1G or more
           is recommended.</p>
      </div>
    </form>
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-music"></i> Sources</h5>
      <p class="dz-hint">Which services this node may resolve tracks from.</p>
      <div class="dz-grid three">
        {% for s in sources %}
          <label class="dz-toggle">
            <input type="checkbox" name="sources" value="{{ s.name }}"
                   {% if s.enabled %}checked{% endif %} />
            <input type="hidden" name="all_sources" value="{{ s.name }}" />
            <span>{{ s.name }}</span>
          </label>
        {% endfor %}
      </div>
      <div class="dz-save">
        <button class="dz-btn primary" name="action" value="save_sources">
          <i class="fa fa-save"></i> Save sources
        </button>
      </div>
    </div>
  </form>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-sliders"></i> Filters</h5>
      <p class="dz-hint">Audio effects the node is allowed to apply.</p>
      <div class="dz-grid three">
        {% for f in filters %}
          <label class="dz-toggle">
            <input type="checkbox" name="filters" value="{{ f.name }}"
                   {% if f.enabled %}checked{% endif %} />
            <input type="hidden" name="all_filters" value="{{ f.name }}" />
            <span>{{ f.name }}</span>
          </label>
        {% endfor %}
      </div>
      <div class="dz-save">
        <button class="dz-btn primary" name="action" value="save_filters">
          <i class="fa fa-save"></i> Save filters
        </button>
      </div>
    </div>
  </form>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-cog"></i> Server tuning</h5>
      <p class="dz-hint">The settings <code>[p]plmanaged settings server</code>
         exposes, with their accepted ranges.</p>
      {% for s in settings %}
        <div style="padding:9px 0; border-bottom:1px solid rgba(255,255,255,.06);">
          <label class="dz-label">
            {{ s.name }}
            {% if s.kind == 'int' %}<span class="dz-tag">{{ s.min }} &ndash; {{ s.max }}</span>{% endif %}
          </label>
          <p class="dz-hint" style="margin:0 0 6px;">{{ s.help }}</p>
          {% if s.kind == 'bool' %}
            <label class="dz-toggle">
              <input type="checkbox" name="setting_{{ s.name }}"
                     {% if s.value %}checked{% endif %} />
              <span>Enabled</span>
            </label>
          {% elif s.kind == 'choice' %}
            <select class="dz-select" name="setting_{{ s.name }}" style="max-width:200px;">
              {% for c in s.choices %}
                <option value="{{ c }}" {% if s.value == c %}selected{% endif %}>{{ c }}</option>
              {% endfor %}
            </select>
          {% else %}
            <input class="dz-input" type="number" min="{{ s.min }}" max="{{ s.max }}"
                   name="setting_{{ s.name }}" value="{{ s.value if s.value is not none else '' }}"
                   style="max-width:200px;" />
          {% endif %}
        </div>
      {% endfor %}
      <div class="dz-row dz-save">
        <button class="dz-btn primary" name="action" value="save_settings">
          <i class="fa fa-save"></i> Save settings
        </button>
        {{ confirm('Reset IP rotation', 'reset_iprotation',
                   'Reset the node IP rotation config to defaults?') }}
        {{ confirm('Reset HTTP proxy', 'reset_httpproxy',
                   'Reset the node HTTP proxy config to defaults?') }}
      </div>
    </div>
  </form>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-puzzle-piece"></i> Plugins</h5>
      <p class="dz-hint">{{ installed_plugins|length }} plugin(s) configured.</p>
      {% if installed_plugins %}
        <table class="dz-t">
          <tr><th>Dependency</th><th>Repository</th></tr>
          {% for p in installed_plugins %}
            <tr><td><code>{{ p.dependency }}</code></td><td>{{ p.repository }}</td></tr>
          {% endfor %}
        </table>
      {% endif %}
      <div class="dz-row" style="margin-top:12px;">
        <select class="dz-select" name="plugin" style="max-width:220px;">
          {% for p in plugins %}<option value="{{ p }}">{{ p }}</option>{% endfor %}
        </select>
        <button class="dz-btn primary" name="action" value="plugin_enable">
          <i class="fa fa-plus"></i> Enable
        </button>
        <button class="dz-btn danger" name="action" value="plugin_disable">
          <i class="fa fa-minus"></i> Disable
        </button>
      </div>
    </div>
  </form>
</div>
"""
)
