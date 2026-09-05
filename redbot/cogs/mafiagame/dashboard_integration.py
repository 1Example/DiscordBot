from __future__ import annotations

import asyncio
import logging
import typing as t

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    channel_options,
    dashboard_page,
    fake_context,
    form_reader,
    guild_member,
    is_staff,
    role_options,
)

log = logging.getLogger("red.mafiagame.dashboard")

# The cog declares every guild setting in one `_settings` map, with a converter
# per entry. Rather than restate all 39 here - which would drift the moment one
# is added - the page reads that map and renders each entry by its converter.
# Only the grouping and the ordering live here.
GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "General",
        "Where games happen and who may join.",
        (
            "category",
            "ping_role",
            "blacklisted_roles",
            "allow_spectators",
            "add_reactions",
            "channel_auto_delete",
            "game_logs",
            "display_roles_when_starting",
        ),
    ),
    (
        "Game rules",
        "How a round plays out.",
        (
            "default_mode",
            "theme",
            "show_dead_role",
            "dying_message",
            "anonymous_voting",
            "defend_judgement",
            "anonymous_judgement",
            "mafia_communication",
            "town_traitor",
            "town_vip",
            "poll_threshold",
        ),
    ),
    (
        "Roles",
        "Which roles can come up, and how a few of them behave.",
        (
            "more_roles",
            "disabled_roles",
            "custom_roles",
            "vigilante_shoot_night_1",
            "alchemist_lethal_potion_night_1",
            "hoarder_hoard_same_player_if_failed",
            "judge_prosecute_day_1",
        ),
    ),
    (
        "Anomalies",
        "Random events that shake up a round.",
        ("anomalies", "disabled_anomalies"),
    ),
    (
        "Timers",
        "Seconds each phase waits before moving on.",
        (
            "perform_action_timeout",
            "talk_timeout",
            "voting_timeout",
            "defend_timeout",
            "judgement_timeout",
        ),
    ),
    (
        "Away players",
        "What happens to someone who stops responding.",
        ("afk_days_before_kick", "afk_temp_ban_duration"),
    ),
    (
        "Economy",
        "Charge to play and pay out to winners, using Red's bank.",
        (
            "red_economy",
            "cost_to_play",
            "reward_for_winning",
            "reward_for_winning_based_on_costs",
        ),
    ),
)


class DashboardIntegration:
    """Set up Mafia, watch a live round, and see who is winning.

    Covers every ``[p]setmafia`` option, plus the parts that were previously
    only visible in Discord: what a running game is doing right now, and the
    per-player win record.
    """

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering MafiaGame as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    # ------------------------------------------------------------------ pages

    @dashboard_page(
        name=None,
        description="Live games, settings and the win record.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_mafia_page(
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
                    "error_message": "Only server administrators can change Mafia settings.",
                }
            notifications = await self._mafia_handle_post(guild, member, kwargs)

        settings = await self.config.guild(guild).all()
        game = self._mafia_live_game(guild)

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": MAFIA_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_staff": staff,
                "stat_items": self._mafia_stats(guild, game),
                "game": game,
                "groups": self._mafia_groups(guild, settings),
                "leaderboard": await self._mafia_leaderboard(guild),
                "modes": self._mafia_mode_cards(),
                "text_channels": channel_options(guild, require_send=True),
                "mode_options": [
                    {"id": name, "name": name, "group": group, "selected": False}
                    for name, group in self._mafia_names("modes")
                ],
                "can_manage": bool(guild.me and guild.me.guild_permissions.manage_channels),
            },
        }

    # ------------------------------------------------------------------- data

    def _mafia_live_game(self, guild: discord.Guild) -> dict:
        """What the running game is doing, or {} when nothing is running.

        Every read is defensive: the game object is mutated from a background
        task, so a field can be half-set while a phase changes over.
        """
        game = (getattr(self, "games", None) or {}).get(guild)
        if game is None:
            return {}
        try:
            players = list(getattr(game, "players", None) or [])
            alive = [p for p in players if getattr(p, "is_alive", True)]
            channel = getattr(game, "channel", None)
            anomaly = getattr(game, "current_anomaly", None)
            mode = getattr(game, "mode", None)
            return {
                "running": True,
                "mode": getattr(mode, "name", "Unknown"),
                "channel": f"#{channel.name}" if channel is not None else None,
                "channel_id": getattr(channel, "id", 0),
                "number": getattr(game, "current_number", 0),
                "players": len(players),
                "alive": len(alive),
                "dead": len(players) - len(alive),
                "anomaly": getattr(anomaly, "name", None),
                "roster": [
                    {
                        "name": getattr(
                            getattr(p, "member", None), "display_name", "Unknown"
                        ),
                        "alive": bool(getattr(p, "is_alive", True)),
                        # The role is deliberately not shown: the page is open to
                        # any member of the guild, and a live game is meant to be
                        # played with hidden roles.
                    }
                    for p in players
                ],
            }
        except Exception:  # noqa: BLE001 - a wobbly read must not break the page
            log.exception("Could not read the live Mafia game in %s", guild.id)
            return {"running": True, "mode": "Unknown", "players": 0, "alive": 0,
                    "dead": 0, "roster": [], "channel": None, "number": 0,
                    "anomaly": None}

    def _mafia_stats(self, guild: discord.Guild, game: dict) -> list:
        """(label, value) pairs, which is what the `stats` macro renders."""
        if not game:
            return [("Game running", "no")]
        return [
            ("Round", game.get("number", 0)),
            ("Still in", game.get("alive", 0)),
            ("Out", game.get("dead", 0)),
        ]

    def _mafia_option(self, guild: discord.Guild, key: str, value) -> dict:
        """Describe one setting well enough for the template to render it.

        The converter recorded by the cog decides the control: a bool becomes a
        checkbox, a channel or role becomes a picker, a Range becomes a number
        with its own bounds, a Literal or name-converter becomes a dropdown.
        """
        import typing as _t

        from redbot.core import commands as _commands

        spec = getattr(self, "_settings", {}).get(key, {})
        converter = spec.get("converter")
        description = spec.get("description", "")
        option = {
            "key": key,
            "label": key.replace("_", " ").capitalize(),
            "help": description,
            "kind": "text",
            "value": value,
        }

        if converter is bool:
            option["kind"] = "bool"
            option["value"] = bool(value)
            return option

        if converter is discord.CategoryChannel:
            option["kind"] = "picker"
            option["options"] = channel_options(guild, kinds=("category",), selected=value)
            option["none_label"] = "no category"
            return option

        if converter is discord.Role:
            option["kind"] = "picker"
            option["options"] = role_options(guild, selected=value)
            option["none_label"] = "nobody"
            return option

        origin = _t.get_origin(converter)
        args = _t.get_args(converter)

        if origin is _t.Literal:
            option["kind"] = "picker"
            option["options"] = [
                {"id": str(a), "name": str(a), "group": option["label"],
                 "selected": value == a}
                for a in args
            ]
            option["none_label"] = "none"
            return option

        # commands.Range[int, lo, hi] -> a number box that knows its own limits.
        # Range is a real object with `min`/`max` attributes, not a typing
        # construct, so `get_args` finds nothing on it - reading the attributes
        # is what actually gets the bounds.
        if "Range" in repr(converter) or hasattr(converter, "annotation"):
            option["kind"] = "number"
            low = getattr(converter, "min", None)
            high = getattr(converter, "max", None)
            if low is None and high is None:
                numbers = [a for a in args if isinstance(a, int)]
                low = numbers[0] if numbers else None
                high = numbers[1] if len(numbers) > 1 else None
            option["min"] = low
            option["max"] = high
            option["value"] = "" if value is None else value
            return option

        if origin is _commands.Greedy or "Greedy" in repr(converter):
            option["kind"] = "many"
            option["options"] = self._mafia_choices(guild, key, value)
            return option

        if key == "default_mode":
            option["kind"] = "picker"
            option["options"] = [
                {"id": name, "name": name, "group": group, "selected": value == name}
                for name, group in self._mafia_names("modes")
            ]
            option["none_label"] = "none"
            return option

        option["value"] = "" if value is None else value
        return option

    def _mafia_choices(self, guild: discord.Guild, key: str, value) -> list[dict]:
        """The options behind a multi-select, per setting."""
        chosen = {str(v) for v in (value or [])}
        if key == "blacklisted_roles":
            return role_options(guild, selected_many=list(value or []))
        source = "anomalies" if "anomal" in key else "roles"
        return [
            {"id": name, "name": name, "group": group, "selected": name in chosen}
            for name, group in self._mafia_names(source)
        ]

    @staticmethod
    def _mafia_names(kind: str) -> list[tuple[str, str]]:
        """(name, group) for each role, mode or anomaly.

        The picker macro groups by the second value, and Mafia has around a
        hundred roles - sorting them under their own side is the difference
        between a usable list and a wall.
        """
        try:
            if kind == "modes":
                from .modes import MODES

                return [(m.name, "Modes") for m in MODES]
            if kind == "anomalies":
                from .anomalies import ANOMALIES

                return [(a.name, "Anomalies") for a in ANOMALIES]
            from .roles import ROLES

            seen: dict[str, str] = {}
            for role in ROLES:
                name = getattr(role, "name", None)
                if name and name not in seen:
                    seen[name] = getattr(role, "side", None) or "Other"
            return sorted(seen.items(), key=lambda pair: (pair[1], pair[0]))
        except Exception:  # noqa: BLE001
            log.exception("Could not list the Mafia %s", kind)
            return []

    def _mafia_groups(self, guild: discord.Guild, settings: dict) -> list[dict]:
        groups = []
        for title, blurb, keys in GROUPS:
            options = [
                self._mafia_option(guild, key, settings.get(key))
                for key in keys
                if key in settings
            ]
            if options:
                groups.append({"title": title, "blurb": blurb, "options": options})
        return groups

    def _mafia_mode_cards(self) -> list[dict]:
        try:
            from .modes import MODES

            return [
                {
                    "name": m.name,
                    "emoji": getattr(m, "emoji", ""),
                    "description": getattr(m, "description", ""),
                }
                for m in MODES
            ]
        except Exception:  # noqa: BLE001
            log.exception("Could not list the Mafia modes")
            return []

    async def _mafia_leaderboard(self, guild: discord.Guild, limit: int = 15) -> list[dict]:
        """Wins and games played, for members of this guild only."""
        try:
            everyone = await self.config.all_users()
        except Exception:  # noqa: BLE001
            log.exception("Could not read the Mafia win record")
            return []

        rows = []
        for user_id, data in everyone.items():
            member = guild.get_member(user_id)
            if member is None:
                continue
            wins = sum((data.get("wins") or {}).values())
            games = sum((data.get("games") or {}).values())
            if not games:
                continue
            rows.append(
                {
                    "name": member.display_name,
                    "wins": wins,
                    "games": games,
                    "rate": round(wins / games * 100) if games else 0,
                    "achievements": len(data.get("achievements") or {}),
                }
            )
        rows.sort(key=lambda r: (-r["wins"], -r["rate"], r["name"].lower()))
        for position, row in enumerate(rows[:limit], start=1):
            row["position"] = position
        return rows[:limit]

    # ------------------------------------------------------------- post logic

    async def _mafia_handle_post(
        self, guild: discord.Guild, actor: discord.Member, kwargs: dict
    ) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")
        try:
            if action == "start_game":
                return await self._mafia_start_game(guild, actor, field)
            if action == "end_game":
                return await self._mafia_end_game(guild, actor)
            if action and action.startswith("save_"):
                return await self._mafia_save_group(guild, field, action[len("save_"):])
        except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
            log.exception("Mafia dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]
        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    async def _mafia_start_game(self, guild: discord.Guild, actor, field) -> list[dict]:
        """Open a join lobby in the chosen channel.

        The cog's own `start` command runs the lobby and blocks until it fills,
        so it goes on the event loop as a task. What this returns is "the lobby
        is open", which is all the web request can honestly say.
        """
        if (getattr(self, "games", None) or {}).get(guild) is not None:
            return [
                {"message": "A game is already running in this server.",
                 "category": "warning"}
            ]

        me = guild.me
        if me is None or not me.guild_permissions.manage_channels:
            return [
                {"message": "I need Manage Channels to create the game channel.",
                 "category": "danger"}
            ]

        channel = guild.get_channel(field.integer("channel", 0) or 0)
        if channel is None or not hasattr(channel, "send"):
            return [{"message": "Pick a channel for the lobby.", "category": "warning"}]
        if not channel.permissions_for(me).send_messages:
            return [
                {"message": f"I cannot post in #{channel.name}.", "category": "danger"}
            ]

        mode_name = (field("mode") or "").strip()
        mode = None
        if mode_name:
            try:
                from .modes import MODES

                mode = discord.utils.get(MODES, name=mode_name)
            except Exception:  # noqa: BLE001
                log.exception("Could not resolve the Mafia mode %r", mode_name)
            if mode is None:
                return [
                    {"message": f"There is no mode called '{mode_name}'.",
                     "category": "warning"}
                ]

        context = await fake_context(
            self.bot, actor, f"{guild.me.mention} mafia start", channel=channel
        )
        if context is None:
            return [
                {"message": "I could not open a lobby in that channel.",
                 "category": "danger"}
            ]

        async def run() -> None:
            try:
                await context.invoke(self.start, mode=mode)
            except Exception:  # noqa: BLE001 - nobody is waiting on this task
                log.exception("Starting a Mafia game from the dashboard failed")

        asyncio.create_task(run())
        return [
            {
                "message": f"Lobby opened in #{channel.name}"
                + (f" on {mode_name}." if mode_name else " on the default mode.")
                + " Players join from there.",
                "category": "success",
            }
        ]

    async def _mafia_end_game(self, guild: discord.Guild, actor) -> list[dict]:
        game = (getattr(self, "games", None) or {}).get(guild)
        if game is None:
            return [
                {"message": "No game is running in this server.", "category": "warning"}
            ]
        try:
            await game.end()
        except Exception as exc:  # noqa: BLE001
            log.exception("Ending the Mafia game from the dashboard failed")
            return [{"message": f"Could not end the game: {exc}", "category": "danger"}]
        return [{"message": "The game has been ended.", "category": "success"}]

    async def _mafia_save_group(self, guild, field, slug: str) -> list[dict]:
        wanted = next(
            (keys for title, _b, keys in GROUPS if self._slug(title) == slug), None
        )
        if wanted is None:
            return [{"message": f"Unknown section: {slug}", "category": "warning"}]

        conf = self.config.guild(guild)
        settings = await conf.all()
        problems: list[dict] = []
        saved = 0

        for key in wanted:
            if key not in settings:
                continue
            option = self._mafia_option(guild, key, settings.get(key))
            value, problem = self._mafia_read(field, option)
            if problem is not None:
                problems.append({"message": f"{option['label']}: {problem}",
                                 "category": "warning"})
                continue
            if value == settings.get(key):
                continue
            await conf.get_attr(key).set(value)
            saved += 1

        if problems and not saved:
            return problems
        return problems + [
            {
                "message": f"Saved {saved} setting(s)." if saved else "Nothing changed.",
                "category": "success" if saved else "info",
            }
        ]

    @staticmethod
    def _mafia_read(field, option: dict):
        """Pull one setting out of the form. Returns (value, problem)."""
        key, kind = option["key"], option["kind"]

        if kind == "bool":
            return field.checked(key), None

        if kind == "many":
            return field.many(key), None

        if kind == "number":
            raw = (field(key) or "").strip()
            if raw == "":
                # These are all optional; blank means "no limit".
                return None, None
            try:
                value = int(raw)
            except ValueError:
                return None, f"'{raw}' is not a number."
            low, high = option.get("min"), option.get("max")
            if low is not None and value < low:
                return None, f"must be at least {low}."
            if high is not None and value > high:
                return None, f"must be at most {high}."
            return value, None

        raw = (field(key) or "").strip()
        if kind == "picker":
            if not raw:
                return None, None
            # Channel and role pickers hand back ids; everything else is a name.
            return (int(raw) if raw.isdigit() else raw), None

        return (raw or None), None

    @staticmethod
    def _slug(title: str) -> str:
        return title.lower().replace(" ", "_")


MAFIA_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<style>
  .mf-roster { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }
  .mf-p { font-size:.78rem; padding:4px 10px; border-radius:999px;
          background:rgba(255,255,255,.06); border:1px solid rgba(255,255,255,.10); }
  .mf-p.out { opacity:.4; text-decoration:line-through; }
  .mf-mode { padding:11px 13px; border-radius:10px; background:rgba(255,255,255,.04);
             border:1px solid rgba(255,255,255,.08); }
  .mf-mode b { display:block; margin-bottom:3px; }
</style>

<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-user-secret"></i> Mafia in {{ guild_name }}</h4>
    <p>
      {% if game %}
        A <b>{{ game.mode }}</b> game is running
        {% if game.channel %}in {{ game.channel }}{% endif %}
        &mdash; {{ game.alive }} still in, {{ game.dead }} out.
      {% else %}
        No game is running right now.
      {% endif %}
    </p>
  </div>

  {{ stats(stat_items) }}

  {% if game %}
    <div class="dz-panel">
      <h5><i class="fa fa-play-circle"></i> Live game</h5>
      <p class="dz-hint">
        Round {{ game.number }}{% if game.anomaly %} &middot;
        anomaly: <b>{{ game.anomaly }}</b>{% endif %}.
        Roles are not shown here &mdash; the page is open to everyone in the server.
      </p>
      <div class="mf-roster">
        {% for p in game.roster %}
          <span class="mf-p{% if not p.alive %} out{% endif %}">{{ p.name }}</span>
        {% endfor %}
      </div>
    </div>
  {% endif %}

  {% if is_staff %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-play"></i> Run a game</h5>
        {% if not can_manage %}
          <p style="margin:0 0 9px; color:#ff8b8b;">
            <i class="fa fa-exclamation-circle"></i>
            I do not have <b>Manage Channels</b>, so I cannot create the game channel.
          </p>
        {% endif %}
        {% if game %}
          <p class="dz-hint">
            A game is already running. End it before starting another.
          </p>
          <div class="dz-save">
            <button class="dz-btn danger" name="action" value="end_game"
                    onclick="return confirm('End the game that is running?');">
              <i class="fa fa-stop"></i> End the game
            </button>
          </div>
        {% else %}
          <p class="dz-hint">
            Opens the join lobby in the channel you pick &mdash; players still join
            from Discord, and the game runs itself from there.
          </p>
          <div class="dz-row">
            <div style="flex:1 1 240px;">
              <div class="dz-label">Lobby channel</div>
              {{ picker('channel', text_channels, allow_none=true,
                        none_label='pick a channel') }}
            </div>
            <div style="flex:1 1 200px;">
              <div class="dz-label">Mode</div>
              {{ picker('mode', mode_options, allow_none=true,
                        none_label='the server default') }}
            </div>
          </div>
          <div class="dz-save">
            <button class="dz-btn primary" name="action" value="start_game"
                    {% if not can_manage %}disabled{% endif %}>
              <i class="fa fa-play"></i> Open the lobby
            </button>
          </div>
        {% endif %}
      </div>
    </form>
  {% endif %}

  {% if not is_staff %}
    <div class="dz-panel">
      <p style="margin:0; opacity:.7;">
        <i class="fa fa-info-circle"></i>
        You can see what is going on and who is winning. Changing the setup is
        for server administrators.
      </p>
    </div>
  {% endif %}

  {% if is_staff %}
    {% for group in groups %}
      <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <div class="dz-panel">
          <h5><i class="fa fa-sliders"></i> {{ group.title }}</h5>
          <p class="dz-hint">{{ group.blurb }}</p>

          {% for o in group.options %}
            {% if o.kind == 'bool' %}
              <label class="dz-toggle" style="margin-top:9px;">
                <input type="checkbox" name="{{ o.key }}" {% if o.value %}checked{% endif %} />
                <span>{{ o.help or o.label }}</span>
              </label>
            {% else %}
              <div style="margin-top:11px;">
                <div class="dz-label">{{ o.label }}</div>
                {% if o.kind == 'picker' %}
                  {{ picker(o.key, o.options, allow_none=true,
                            none_label=o.none_label or 'none') }}
                {% elif o.kind == 'many' %}
                  {{ picker(o.key, o.options, allow_none=false, size=6,
                            placeholder='Search...', multiple=true) }}
                {% elif o.kind == 'number' %}
                  <input class="dz-input" type="number" name="{{ o.key }}"
                         value="{{ o.value }}"
                         {% if o.min is not none %}min="{{ o.min }}"{% endif %}
                         {% if o.max is not none %}max="{{ o.max }}"{% endif %}
                         style="max-width:200px;" />
                {% else %}
                  <input class="dz-input" type="text" name="{{ o.key }}"
                         value="{{ o.value }}" />
                {% endif %}
                {% if o.help %}
                  <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
                    {{ o.help }}
                  </div>
                {% endif %}
              </div>
            {% endif %}
          {% endfor %}

          <div class="dz-save">
            <button class="dz-btn primary" name="action"
                    value="save_{{ group.title|lower|replace(' ', '_') }}">
              <i class="fa fa-save"></i> Save {{ group.title|lower }}
            </button>
          </div>
        </div>
      </form>
    {% endfor %}
  {% endif %}

  <div class="dz-panel">
    <h5><i class="fa fa-trophy"></i> Win record</h5>
    <p class="dz-hint">Members of this server who have finished at least one game.</p>
    {% if leaderboard %}
      <table class="dz-t">
        <thead>
          <tr><th>#</th><th>Player</th><th>Wins</th><th>Games</th>
              <th>Win rate</th><th>Achievements</th></tr>
        </thead>
        <tbody>
          {% for row in leaderboard %}
            <tr>
              <td style="opacity:.5;">{{ row.position }}</td>
              <td>{{ row.name }}</td>
              <td><b>{{ row.wins }}</b></td>
              <td style="opacity:.7;">{{ row.games }}</td>
              <td style="opacity:.7;">{{ row.rate }}%</td>
              <td style="opacity:.7;">{{ row.achievements }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="dz-empty">Nobody here has finished a game yet.</p>
    {% endif %}
  </div>

  {% if modes %}
    <div class="dz-panel">
      <h5><i class="fa fa-book"></i> Modes</h5>
      <p class="dz-hint">What each mode changes. Pick the default under Game rules.</p>
      <div class="dz-grid three">
        {% for m in modes %}
          <div class="mf-mode">
            <b>{{ m.emoji }} {{ m.name }}</b>
            <span style="font-size:.78rem; opacity:.6;">{{ m.description }}</span>
          </div>
        {% endfor %}
      </div>
    </div>
  {% endif %}
</div>
"""
)
