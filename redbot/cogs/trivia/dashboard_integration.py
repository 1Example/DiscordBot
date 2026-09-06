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
    fake_context,
    form_reader,
    guild_member,
    is_staff,
)

log = logging.getLogger("red.trivia.dashboard")

# (key, label, help, kind, minimum, maximum)
FIELDS = (
    ("max_score", "Points to win", "Score that ends the session.", "int", 1, 1000),
    ("timeout", "Session timeout (s)", "Stop after this long with no answers.", "float", 0, 3600),
    ("delay", "Time per question (s)", "How long players get to answer.", "float", 4, 300),
    ("payout_multiplier", "Payout multiplier", "Credits per point. 0 disables payouts.", "float", 0, 100),
)

TOGGLES = (
    ("bot_plays", "Bot earns points", "The bot scores a point when nobody answers."),
    ("reveal_answer", "Reveal the answer", "Show the answer when time runs out."),
    ("use_spoilers", "Hide answers behind spoilers", "Answers post as spoiler text."),
    ("allow_override", "Allow per-list overrides", "Lists may override these settings."),
)


class DashboardIntegration:
    """Trivia sessions, settings, lists and the leaderboard.

    Starts and stops sessions (``[p]trivia``, ``[p]trivia stop``), lists the
    categories (``[p]trivia list``), covers every ``[p]triviaset`` option, and
    lets the owner remove an uploaded custom list.
    """

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Trivia as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Trivia settings, lists and scores.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_trivia_page(
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
                    "error_message": "Only server administrators can change trivia settings.",
                }
            requested = form_reader(kwargs)("action")
            if requested in ("start", "stop", "delete_list"):
                notifications = await self._tv_session_action(
                    requested, member, guild, form_reader(kwargs)
                )
            else:
                notifications = await self._tv_handle_post(guild, kwargs)

        settings = await self.config.guild(guild).all()

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": TRIVIA_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_staff": staff,
                "fields": [
                    {
                        "key": key,
                        "label": label,
                        "help": help_text,
                        "step": "1" if kind == "int" else "0.5",
                        "min": minimum,
                        "max": maximum,
                        "value": settings.get(key, 0),
                    }
                    for key, label, help_text, kind, minimum, maximum in FIELDS
                ],
                "toggles": [
                    {"key": key, "label": label, "help": help_text, "on": bool(settings.get(key))}
                    for key, label, help_text in TOGGLES
                ],
                "lists": self._tv_lists(),
                "list_options": [
                    {
                        "id": entry["name"],
                        "name": f"{entry['name']} ({entry['count']})",
                        "group": "Categories",
                        "selected": False,
                        "warn": False,
                    }
                    for entry in self._tv_lists()
                ],
                "channel_options": channel_options(guild, require_send=True),
                "is_owner": await self.bot.is_owner(user),
                "leaderboard": await self._tv_leaderboard(guild),
                "active_sessions": self._tv_sessions(guild),
            },
        }

    def _tv_lists(self) -> list[dict]:
        """Every trivia list the bot can see, with its question count."""
        out = []
        try:
            paths = sorted(self._all_lists(), key=lambda p: p.stem)
        except Exception:  # noqa: BLE001
            log.exception("Could not enumerate trivia lists")
            return []
        seen = set()
        for path in paths:
            if path.stem in seen:
                # A custom list shadows a core list of the same name.
                continue
            seen.add(path.stem)
            try:
                data = self.get_trivia_list(path.stem)
                # AUTHOR and CONFIG are metadata keys, not questions.
                count = len([k for k in data if k not in ("AUTHOR", "CONFIG")])
                author = (data.get("AUTHOR") or "") if isinstance(data, dict) else ""
            except Exception:  # noqa: BLE001 - a malformed list shouldn't hide the rest
                count, author = 0, "could not be parsed"
            out.append({"name": path.stem, "count": count, "author": author})
        return out

    def _tv_sessions(self, guild: discord.Guild) -> list[dict]:
        out = []
        for session in getattr(self, "trivia_sessions", []) or []:
            channel = getattr(session.ctx, "channel", None)
            if channel is None or channel.guild != guild:
                continue
            out.append(
                {
                    "channel": f"#{channel.name}",
                    "channel_id": str(channel.id),
                    "scores": len(getattr(session, "scores", []) or []),
                }
            )
        return out

    async def _tv_leaderboard(self, guild: discord.Guild, limit: int = 15) -> list[dict]:
        try:
            data = await self.config.all_members(guild)
        except Exception:  # noqa: BLE001
            log.exception("Could not read trivia scores")
            return []
        rows = []
        for member_id, stats in data.items():
            member = guild.get_member(member_id)
            rows.append(
                {
                    "name": member.display_name if member else f"Unknown ({member_id})",
                    "wins": stats.get("wins", 0),
                    "games": stats.get("games", 0),
                    "score": stats.get("total_score", 0),
                }
            )
        rows.sort(key=lambda r: (-r["wins"], -r["score"]))
        for position, row in enumerate(rows[:limit], start=1):
            row["position"] = position
        return rows[:limit]

    async def _tv_session_action(
        self, action: str, member: discord.Member, guild: discord.Guild, field
    ) -> list[dict]:
        from .trivia import InvalidListError, TriviaSession

        if action == "stop":
            channel = guild.get_channel(field.integer("channel_id", 0) or 0)
            session = self._get_trivia_session(channel) if channel else None
            if session is None:
                return [
                    {"message": "No session is running in that channel.",
                     "category": "info"}
                ]
            await session.end_game()
            session.force_stop()
            return [
                {"message": f"Trivia stopped in #{channel.name}.", "category": "success"}
            ]

        if action == "start":
            channel = guild.get_channel(field.integer("channel_id", 0) or 0)
            if channel is None:
                return [{"message": "Pick a channel.", "category": "warning"}]
            if not channel.permissions_for(guild.me).send_messages:
                return [
                    {"message": f"I cannot post in #{channel.name}.", "category": "warning"}
                ]
            if self._get_trivia_session(channel) is not None:
                return [
                    {"message": f"A session is already running in #{channel.name}.",
                     "category": "warning"}
                ]
            categories = [c.lower() for c in field.many("categories")]
            if not categories:
                return [{"message": "Pick at least one category.", "category": "warning"}]

            trivia_dict: dict = {}
            authors: list = []
            # Reversed so the first list chosen wins on conflicting config.
            for category in reversed(categories):
                try:
                    loaded = self.get_trivia_list(category)
                except FileNotFoundError:
                    return [
                        {"message": f"No such category: {category}.",
                         "category": "warning"}
                    ]
                except InvalidListError:
                    return [
                        {"message": f"The {category} list is formatted incorrectly.",
                         "category": "danger"}
                    ]
                trivia_dict.update(loaded)
                authors.append(trivia_dict.pop("AUTHOR", None))
                trivia_dict.pop("DESCRIPTION", None)
            trivia_dict.pop("$schema", None)
            config = trivia_dict.pop("CONFIG", None)
            if not trivia_dict:
                return [
                    {"message": "Those lists parsed fine but hold no questions.",
                     "category": "warning"}
                ]

            settings = await self.config.guild(guild).all()
            if config and settings["allow_override"]:
                settings.update(config)
            settings["lists"] = dict(zip(categories, reversed(authors)))

            # `TriviaSession.start` needs a real Context to send into.
            context = await fake_context(self.bot, member, "trivia", channel=channel)
            if context is None:
                return [
                    {"message": "I could not open a session in that channel.",
                     "category": "danger"}
                ]
            session = TriviaSession.start(context, trivia_dict, settings)
            self.trivia_sessions.append(session)
            return [
                {
                    "message": f"Trivia started in #{channel.name} with "
                    + ", ".join(categories)
                    + ".",
                    "category": "success",
                }
            ]

        if action == "delete_list":
            if not await self.bot.is_owner(member):
                return [
                    {"message": "Only the bot owner can remove a trivia list.",
                     "category": "danger"}
                ]
            name = (field("list_name") or "").strip()
            from redbot.core.data_manager import cog_data_path

            path = cog_data_path(self) / f"{name}.yaml"
            if not path.exists():
                return [
                    {"message": f"No custom list named {name}.", "category": "warning"}
                ]
            path.unlink()
            return [
                {"message": f"Custom list {name} deleted.", "category": "success"}
            ]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    async def _tv_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        if field("action") != "save":
            return [{"message": "Unknown action.", "category": "warning"}]

        conf = self.config.guild(guild)
        errors: list[dict] = []
        saved = 0

        for key, label, _help, kind, minimum, maximum in FIELDS:
            raw = (field(f"f_{key}") or "").strip()
            if raw == "":
                continue
            try:
                value = int(raw) if kind == "int" else float(raw)
            except ValueError:
                errors.append({"message": f"{label}: '{raw}' is not a number.", "category": "danger"})
                continue
            if not minimum <= value <= maximum:
                errors.append(
                    {
                        "message": f"{label}: must be between {minimum} and {maximum}.",
                        "category": "danger",
                    }
                )
                continue
            await conf.get_attr(key).set(value)
            saved += 1

        for key, _label, _help in TOGGLES:
            await conf.get_attr(key).set(field.checked(f"t_{key}"))

        return errors + [
            {"message": f"Saved {saved} value(s) and the toggles.", "category": "success"}
        ]


TRIVIA_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-question-circle"></i> Trivia in {{ guild_name }}</h4>
    <p>
      {{ lists|length }} list(s) available
      {% if active_sessions %}
        &middot; <b>{{ active_sessions|length }} session(s) running</b>
        ({% for s in active_sessions %}{{ s.channel }}{% if not loop.last %}, {% endif %}{% endfor %})
      {% endif %}
    </p>
  </div>

  {% if is_staff %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-play"></i> Run a session</h5>
        <p class="dz-hint">Pick a channel and one or more categories. Combining
           lists mixes their questions.</p>
        <div class="dz-grid two">
          <div>
            <label class="dz-label">Channel</label>
            {{ picker('channel_id', channel_options, false, 6, 'Search channels...') }}
          </div>
          <div>
            <label class="dz-label">Categories</label>
            {{ picker('categories', list_options, true, 8, 'Search categories...') }}
          </div>
        </div>
        <div class="dz-save">
          <button class="dz-btn primary" name="action" value="start">
            <i class="fa fa-play"></i> Start trivia
          </button>
        </div>
      </div>
    </form>

    {% if active_sessions %}
      <div class="dz-panel">
        <h5><i class="fa fa-stop"></i> Running sessions</h5>
        <table class="dz-t">
          <tr><th>Channel</th><th>Players</th><th></th></tr>
          {% for s in active_sessions %}
            <tr>
              <td>{{ s.channel }}</td>
              <td>{{ s.scores }}</td>
              <td>
                <form method="POST">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="channel_id" value="{{ s.channel_id }}" />
                  {{ confirm('Stop', 'stop',
                             'Stop the trivia session in ' ~ s.channel ~ '?') }}
                </form>
              </td>
            </tr>
          {% endfor %}
        </table>
      </div>
    {% endif %}

    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-grid two">
        <div class="dz-panel">
          <h5><i class="fa fa-sliders"></i> Session</h5>
          {% for f in fields %}
            <div style="margin-bottom:11px;">
              <div class="dz-label">{{ f.label }}</div>
              <input class="dz-input" type="number" step="{{ f.step }}"
                     min="{{ f.min }}" max="{{ f.max }}"
                     name="f_{{ f.key }}" value="{{ f.value }}" />
              <div style="font-size:.72rem; opacity:.45; margin-top:4px;">{{ f.help }}</div>
            </div>
          {% endfor %}
        </div>

        <div class="dz-panel">
          <h5><i class="fa fa-toggle-on"></i> Behaviour</h5>
          {% for t in toggles %}
            <div style="margin-bottom:9px;">
              <label class="dz-toggle" style="padding:0;">
                <input type="checkbox" name="t_{{ t.key }}" {% if t.on %}checked{% endif %} />
                <span>{{ t.label }}</span>
              </label>
              <div style="font-size:.72rem; opacity:.45; margin-left:26px;">{{ t.help }}</div>
            </div>
          {% endfor %}
          <div style="margin-top:14px;">
            <button class="dz-btn primary" name="action" value="save">
              <i class="fa fa-save"></i> Save settings
            </button>
          </div>
        </div>
      </div>
    </form>
  {% endif %}

  <div class="dz-grid two">
    <div class="dz-panel">
      <h5><i class="fa fa-list"></i> Available lists</h5>
      <p class="dz-hint">
        Pick any of these when starting a session above. New lists are uploaded
        in Discord by uploading the file there, since that needs
        a file attachment.
      </p>
      {% if lists %}
        <table class="dz-t">
          <thead><tr><th>List</th><th>Questions</th><th>Author</th>
            {% if is_owner %}<th></th>{% endif %}</tr></thead>
          <tbody>
            {% for l in lists %}
              <tr>
                <td><b>{{ l.name }}</b></td>
                <td style="opacity:.7;">{{ l.count }}</td>
                <td style="opacity:.6; font-size:.8rem;">{{ l.author }}</td>
                {% if is_owner %}
                  <td>
                    <form method="POST">
                      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                      <input type="hidden" name="list_name" value="{{ l.name }}" />
                      {{ confirm('', 'delete_list',
                                 'Delete the custom list ' ~ l.name ~ '? Built-in lists cannot be removed.') }}
                    </form>
                  </td>
                {% endif %}
              </tr>
            {% endfor %}
          </tbody>
        </table>
      {% else %}
        <p class="dz-empty">No trivia lists found.</p>
      {% endif %}
    </div>

    <div class="dz-panel">
      <h5><i class="fa fa-trophy"></i> Top players</h5>
      {% if leaderboard %}
        <table class="dz-t">
          <thead><tr><th>#</th><th>Member</th><th>Wins</th><th>Games</th><th>Points</th></tr></thead>
          <tbody>
            {% for row in leaderboard %}
              <tr>
                <td style="opacity:.5; width:34px;">{{ row.position }}</td>
                <td>{{ row.name }}</td>
                <td><b>{{ row.wins }}</b></td>
                <td style="opacity:.7;">{{ row.games }}</td>
                <td style="opacity:.7;">{{ row.score }}</td>
              </tr>
            {% endfor %}
          </tbody>
        </table>
      {% else %}
        <p class="dz-empty">Nobody has played yet.</p>
      {% endif %}
    </div>
  </div>
</div>
"""
)
