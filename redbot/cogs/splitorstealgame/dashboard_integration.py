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
    form_reader,
    guild_member,
    is_staff,
)

log = logging.getLogger("red.splitorstealgame.dashboard")


class _GameContext:
    """Stand-in for the ``Context`` the game view expects.

    The view only ever calls ``send`` and ``embed_color``, so this is enough to
    start a game from the dashboard.
    """

    def __init__(self, bot, channel: discord.abc.Messageable, author: discord.Member) -> None:
        self.bot = bot
        self.channel = channel
        self.guild = getattr(channel, "guild", None)
        self.author = author
        self.me = getattr(self.guild, "me", None)

    async def send(self, *args: t.Any, **kwargs: t.Any) -> discord.Message:
        return await self.channel.send(*args, **kwargs)

    async def embed_color(self) -> discord.Colour:
        return await self.bot.get_embed_color(self.channel)

    embed_colour = embed_color


class DashboardIntegration:
    """Start and watch Split Or Steal games from the dashboard.

    Replaces ``[p]splitorstealgame``: pick a channel and start a match, then
    follow who joined and what they picked while it runs.
    """

    bot: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering SplitOrStealGame as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Start a Split Or Steal match and watch it play out.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_sos_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        staff = await is_staff(self.bot, user, member, guild)

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._sos_handle_post(member, guild, kwargs)

        games = []
        for message, view in list(self.views.items()):
            if getattr(message, "guild", None) != guild:
                continue
            games.append(
                {
                    "message_id": str(message.id),
                    "channel": getattr(message.channel, "name", ""),
                    "link": message.jump_url,
                    "mode": view._mode or "starting",
                    "joined": [m.display_name for m in view.initial_players],
                    "players": [
                        {"name": m.display_name, "choice": choice or "thinking"}
                        for m, choice in view.players.items()
                    ],
                }
            )

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": SOS_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_staff": staff,
                "games": games,
                "channel_options": channel_options(guild, require_send=True),
            },
        }

    async def _sos_handle_post(
        self, member: discord.Member, guild: discord.Guild, kwargs: dict
    ) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

        try:
            if action == "start":
                channel = guild.get_channel(field.integer("channel_id", 0) or 0)
                if channel is None:
                    return [{"message": "Pick a channel.", "category": "warning"}]
                if not channel.permissions_for(guild.me).send_messages:
                    return [
                        {"message": f"I cannot post in #{channel.name}.",
                         "category": "warning"}
                    ]

                from .view import SplitOrStealGameView

                view = SplitOrStealGameView(cog=self)
                # `start` waits out the join and play windows, so it has to run
                # in the background rather than holding the request open.
                asyncio.create_task(
                    self._sos_run(view, _GameContext(self.bot, channel, member))
                )
                return [
                    {
                        "message": f"Game starting in #{channel.name}. Players have "
                        "60 seconds to join.",
                        "category": "success",
                    }
                ]

            if action == "cancel":
                message_id = field.integer("message_id", 0) or 0
                for message, view in list(self.views.items()):
                    if message.id == message_id:
                        await view.on_timeout()
                        view.stop()
                        self.views.pop(message, None)
                        return [{"message": "Game cancelled.", "category": "success"}]
                return [{"message": "That game is no longer running.", "category": "info"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("SplitOrStealGame dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    async def _sos_run(self, view, context) -> None:
        try:
            await view.start(context)
        except commands.UserFeedbackCheckFailure as exc:
            # The view raises this when too few players joined or someone left.
            with_message = str(exc) or "The game ended early."
            try:
                await context.send(with_message)
            except discord.HTTPException:
                pass
        except Exception:  # noqa: BLE001
            log.exception("A dashboard-started Split Or Steal game failed")


SOS_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-gamepad"></i> Split Or Steal in {{ guild_name }}</h4>
    <p>Two players each pick split or steal. Both split, both win; both steal,
       both lose; one of each, the stealer wins.</p>
  </div>

  {% if is_staff %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-play"></i> Start a match</h5>
        <p class="dz-hint">Players join from the message for 60 seconds, then two
           are picked at random and get 60 seconds to choose.</p>
        {{ picker('channel_id', channel_options, false, 8, 'Search channels...') }}
        <div class="dz-save">
          <button class="dz-btn primary" name="action" value="start">
            <i class="fa fa-play"></i> Start game
          </button>
        </div>
      </div>
    </form>
  {% endif %}

  <div class="dz-panel">
    <h5><i class="fa fa-users"></i> Running games</h5>
    {% if games %}
      {% for g in games %}
        <div style="padding:10px 0; border-bottom:1px solid rgba(255,255,255,.06);">
          <div class="dz-row">
            <b>#{{ g.channel }}</b>
            <span class="dz-tag">{{ g.mode }}</span>
            <a class="dz-hint" href="{{ g.link }}" target="_blank" rel="noopener">
              open in Discord</a>
            {% if is_staff %}
              <form method="POST" style="margin-left:auto;">
                <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                <input type="hidden" name="message_id" value="{{ g.message_id }}" />
                {{ confirm('Cancel', 'cancel', 'End this game now?') }}
              </form>
            {% endif %}
          </div>
          {% if g.players %}
            <p class="dz-hint" style="margin-top:6px;">
              {% for p in g.players %}
                {{ p.name }} <span class="dz-tag">{{ p.choice }}</span>
              {% endfor %}
            </p>
          {% elif g.joined %}
            <p class="dz-hint" style="margin-top:6px;">
              Joined: {{ g.joined|join(', ') }}
            </p>
          {% else %}
            <p class="dz-hint" style="margin-top:6px;">Nobody has joined yet.</p>
          {% endif %}
        </div>
      {% endfor %}
    {% else %}
      <p class="dz-empty">No games running in this server.</p>
    {% endif %}
  </div>
</div>
"""
)
