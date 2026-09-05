from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    channel_options,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
    member_options,
    role_options,
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


# The seven settings that exist per server and again as a global ceiling.
# (key, label, help)
PLAYER_TOGGLES = (
    ("auto_play", "Autoplay when the queue ends",
     "Keep playing similar tracks once the queue runs out."),
    ("shuffle", "Shuffle", "Play the queue in a random order."),
    ("auto_shuffle", "Shuffle new tracks in",
     "Shuffle each batch of tracks as it is added."),
    ("self_deaf", "Deafen myself", "Join voice channels deafened."),
    ("empty_queue_dc", "Leave when the queue ends",
     "Disconnect once there is nothing left to play."),
    ("alone_dc", "Leave when alone",
     "Disconnect when the last person leaves the channel."),
)

# The pages of this module, in the order the subnav shows them. Owner-only ones
# are dropped for everybody else, so nobody is offered a link to a 403.
AUDIO_PAGES = (
    (None, "Now playing", "fa-music", False),
    ("player", "Player", "fa-play-circle", False),
    ("player-settings", "Player settings", "fa-sliders", False),
    ("playlists", "Playlists", "fa-list", False),
    ("effects", "Effects", "fa-sliders", False),
    ("radio", "Radio", "fa-broadcast-tower", False),
    ("youtube-radio", "YouTube radio", "fa-youtube-play", False),
    ("lyrics", "Lyrics", "fa-align-left", False),
    ("local-files", "Local files", "fa-folder-open-o", False),
    ("notifications", "Notifications", "fa-bell-o", False),
    ("nodes", "Nodes", "fa-server", True),
    ("managed-node", "Managed node", "fa-cogs", True),
    ("pylav", "PyLav", "fa-wrench", True),
    ("diagnostics", "Diagnostics", "fa-stethoscope", False),
)


def audio_pages(is_owner: bool) -> list:
    """The subnav entries this viewer should see."""
    return [
        (slug, label, icon)
        for slug, label, icon, owner_only in AUDIO_PAGES
        if not owner_only or is_owner
    ]

class DashboardIntegration:
    """Player status, queue overview and per-server command settings."""

    bot: t.Any
    _config: t.Any
    pylav: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Audio as a third party.")
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
        is_owner = await self.bot.is_owner(user)

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            if not staff:
                return {
                    "status": 1,
                    "error_title": "Forbidden",
                    "error_message": "Only server administrators can change audio settings.",
                }
            notifications = await self._au_handle_post(guild, is_owner, kwargs)

        settings = await self._config.guild(guild).all()
        player = self.pylav.get_player(guild)
        # One read: the DJ pickers need the same data the panels render.
        player_settings = await self._au_player_settings(guild, is_owner)

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": AUDIO_TEMPLATE,
                "audio_pages": audio_pages(is_owner),
                "player": player_settings,
                "player_toggles": [
                    {"key": k, "label": lbl, "help": h}
                    for k, lbl, h in PLAYER_TOGGLES
                ],
                "command_channels": channel_options(
                    guild, kinds=("text", "voice", "stage"), require_send=True
                ),
                "voice_channels": channel_options(guild, kinds=("voice", "stage")),
                "dj_role_options": role_options(
                    guild,
                    selected_many=[
                        int(r["id"]) for r in player_settings["dj_roles"]
                    ],
                ),
                "dj_member_options": member_options(
                    guild,
                    humans_only=True,
                    selected_many=[
                        int(u["id"]) for u in player_settings["dj_users"]
                    ],
                ),
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


    # --------------------------------------------------------- player setup

    async def _au_player_settings(self, guild: discord.Guild, is_owner: bool) -> dict:
        """What [p]playerset server and [p]playerset global report."""
        config = self.pylav.player_config_manager.get_config(guild.id)
        server = {key: bool(await getattr(config, f"fetch_{key}")()) for key, _l, _h in PLAYER_TOGGLES}
        server["max_volume"] = await config.fetch_max_volume()
        server["text_channel_id"] = await config.fetch_text_channel_id()
        server["forced_channel_id"] = await config.fetch_forced_channel_id()
        server["auto_play_playlist_id"] = await config.fetch_auto_play_playlist_id()

        dj_roles, dj_users = [], []
        for role_id in await config.fetch_dj_roles():
            role = guild.get_role(role_id)
            dj_roles.append({"id": str(role_id), "name": role.name if role else str(role_id)})
        for user_id in await config.fetch_dj_users():
            member = guild.get_member(user_id)
            dj_users.append(
                {"id": str(user_id), "name": member.display_name if member else str(user_id)}
            )

        out = {
            "server": server,
            "dj_roles": sorted(dj_roles, key=lambda r: r["name"].lower()),
            "dj_users": sorted(dj_users, key=lambda u: u["name"].lower()),
        }
        if is_owner:
            glob = self.pylav.player_manager.global_config
            out["global"] = {
                key: bool(await getattr(glob, f"fetch_{key}")()) for key, _l, _h in PLAYER_TOGGLES
            }
            out["global"]["max_volume"] = await glob.fetch_max_volume()
        return out

    async def _au_save_player(self, guild: discord.Guild, field, *, scope: str) -> list[dict]:
        """Write the player defaults, for this server or for every server."""
        if scope == "global":
            config = self.pylav.player_manager.global_config
        else:
            config = self.pylav.player_config_manager.get_config(guild.id)

        for key, _label, _help in PLAYER_TOGGLES:
            await getattr(config, f"update_{key}")(field.checked(f"{scope}_{key}"))

        volume = field.integer(f"{scope}_max_volume", 0) or 0
        if not 1 <= volume <= 1000:
            return [
                {"message": "The maximum volume has to be between 1% and 1000%.",
                 "category": "warning"}
            ]
        await config.update_max_volume(volume)

        if scope == "global":
            return [
                {"message": "Saved. These are the defaults for every server.",
                 "category": "success"}
            ]

        # Per-server only: which channels the player is pinned to, and what it
        # autoplays from.
        notes = []
        for form_key, setter, kind in (
            ("text_channel_id", "update_text_channel_id", "commands"),
            ("forced_channel_id", "update_forced_channel_id", "voice"),
        ):
            raw = field.integer(form_key, 0) or 0
            if raw:
                channel = guild.get_channel(raw)
                if channel is None:
                    notes.append(
                        {"message": f"That {kind} channel is not in this server.",
                         "category": "warning"}
                    )
                    continue
                if kind == "commands" and not channel.permissions_for(guild.me).send_messages:
                    notes.append(
                        {"message": f"I cannot send messages in #{channel.name}, so "
                                    "locking commands there would silence me.",
                         "category": "warning"}
                    )
                    continue
            await getattr(config, setter)(raw or None)

        playlist = (field("auto_play_playlist_id") or "").strip()
        await config.update_auto_play_playlist_id(int(playlist) if playlist.isdigit() else None)
        return notes + [{"message": "Player settings saved.", "category": "success"}]

    async def _au_dj(self, guild: discord.Guild, field, *, action: str) -> list[dict]:
        """The disc jockey lists, as [p]playerset server dj managed them."""
        config = self.pylav.player_config_manager.get_config(guild.id)

        if action == "dj_clear":
            for role_id in list(await config.fetch_dj_roles()):
                await config.remove_from_dj_roles(discord.Object(id=role_id))
            for user_id in list(await config.fetch_dj_users()):
                await config.remove_from_dj_users(discord.Object(id=user_id))
            return [
                {"message": "Cleared. With no DJs set, everyone can use the player.",
                 "category": "success"}
            ]

        added = removed = 0
        wanted_roles = {int(x) for x in field.many("dj_roles") if str(x).isdigit()}
        current_roles = set(await config.fetch_dj_roles())
        for role_id in wanted_roles - current_roles:
            await config.add_to_dj_roles(discord.Object(id=role_id))
            added += 1
        for role_id in current_roles - wanted_roles:
            await config.remove_from_dj_roles(discord.Object(id=role_id))
            removed += 1

        wanted_users = {int(x) for x in field.many("dj_users") if str(x).isdigit()}
        current_users = set(await config.fetch_dj_users())
        for user_id in wanted_users - current_users:
            await config.add_to_dj_users(discord.Object(id=user_id))
            added += 1
        for user_id in current_users - wanted_users:
            await config.remove_from_dj_users(discord.Object(id=user_id))
            removed += 1

        return [
            {"message": f"DJs saved: {added} added, {removed} removed.",
             "category": "success"}
        ]

    async def _au_handle_post(
        self, guild: discord.Guild, is_owner: bool, kwargs: dict
    ) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

        try:
            if action == "save_player":
                return await self._au_save_player(guild, field, scope="server")
            if action == "save_player_global":
                if not is_owner:
                    return [
                        {"message": "Only the bot owner can change the global defaults.", "category": "danger"}
                    ]
                return await self._au_save_player(guild, field, scope="global")
            if action in ("dj_save", "dj_clear"):
                return await self._au_dj(guild, field, action=action)
        except Exception as exc:  # noqa: BLE001
            log.exception("Audio dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        if action != "save":
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
    + MACROS
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

  {{ subnav(name, audio_pages, none, guild) }}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-sliders"></i> Player settings for this server</h5>
      <p class="dz-hint">How the player behaves here. The bot owner sets a
         ceiling for the volume on every server further down.</p>
      {% for t in player_toggles %}
        <label class="dz-toggle">
          <input type="checkbox" name="server_{{ t.key }}"
                 {% if player.server[t.key] %}checked{% endif %} />
          <span>{{ t.label }}</span>
        </label>
        <div class="dz-hint" style="margin:0 0 7px 30px;">{{ t.help }}</div>
      {% endfor %}

      <div class="dz-grid two" style="margin-top:9px;">
        <div>
          <div class="dz-label">Maximum volume (%)</div>
          <input class="dz-input" type="number" min="1" max="1000"
                 name="server_max_volume" value="{{ player.server.max_volume }}" />
        </div>
        <div>
          <div class="dz-label">Autoplay playlist ID</div>
          <input class="dz-input" type="text" name="auto_play_playlist_id"
                 value="{{ player.server.auto_play_playlist_id or '' }}"
                 placeholder="empty = PyLav picks" />
        </div>
      </div>

      <div class="dz-grid two" style="margin-top:9px;">
        <div>
          <div class="dz-label">Only accept commands in</div>
          {{ picker('text_channel_id', command_channels, allow_none=true,
                    none_label='anywhere', placeholder='Search channels...') }}
        </div>
        <div>
          <div class="dz-label">Only play in</div>
          {{ picker('forced_channel_id', voice_channels, allow_none=true,
                    none_label='any voice channel', placeholder='Search channels...') }}
        </div>
      </div>

      <div class="dz-save">
        <button class="dz-btn primary" name="action" value="save_player">
          <i class="fa fa-save"></i> Save player settings
        </button>
      </div>
    </div>
  </form>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-headphones"></i> Disc jockeys</h5>
      <p class="dz-hint">
        With nobody set, everyone can use the player. Set anyone here and the
        player is theirs, plus anyone with Manage Server.
        {% if player.dj_roles or player.dj_users %}
          Currently
          {{ player.dj_roles|length }} role(s) and {{ player.dj_users|length }} member(s).
        {% endif %}
      </p>
      <div class="dz-grid two">
        <div>
          <div class="dz-label">DJ roles</div>
          {{ picker('dj_roles', dj_role_options, multiple=true, size=7,
                    placeholder='Search roles...') }}
        </div>
        <div>
          <div class="dz-label">DJ members</div>
          {{ picker('dj_users', dj_member_options, multiple=true, size=7,
                    placeholder='Search members...') }}
        </div>
      </div>
      <div class="dz-row" style="margin-top:11px;">
        <button class="dz-btn primary" name="action" value="dj_save">
          <i class="fa fa-save"></i> Save DJs
        </button>
        <button class="dz-btn danger" name="action" value="dj_clear"
                onclick="return confirm('Let everyone use the player again?');">
          <i class="fa fa-times"></i> Clear
        </button>
      </div>
    </div>
  </form>

  {% if player.global %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-globe"></i> Defaults for every server</h5>
        <p class="dz-hint">Owner only. A server cannot exceed the volume set here.</p>
        {% for t in player_toggles %}
          <label class="dz-toggle">
            <input type="checkbox" name="global_{{ t.key }}"
                   {% if player.global[t.key] %}checked{% endif %} />
            <span>{{ t.label }}</span>
          </label>
        {% endfor %}
        <div class="dz-label" style="margin-top:9px;">Maximum volume anywhere (%)</div>
        <input class="dz-input" type="number" min="1" max="1000" style="max-width:200px;"
               name="global_max_volume" value="{{ player.global.max_volume }}" />
        <div class="dz-save">
          <button class="dz-btn primary" name="action" value="save_player_global">
            <i class="fa fa-save"></i> Save global defaults
          </button>
        </div>
      </div>
    </form>
  {% endif %}

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
