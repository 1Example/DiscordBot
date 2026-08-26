from __future__ import annotations

import logging
import random
import typing as t
from datetime import datetime, timezone

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    dashboard_page,
    form_reader,
    guild_member,
)

log = logging.getLogger("red.general.dashboard")

URBAN_URL = "https://api.urbandictionary.com/v0/define"

VERIFICATION_LABELS = {
    "none": "None",
    "low": "Low - verified email",
    "medium": "Medium - registered for 5 minutes",
    "high": "High - member for 10 minutes",
    "highest": "Highest - verified phone",
}


class DashboardIntegration:
    """Server information and the General cog's utilities, without Discord.

    Renders everything ``[p]serverinfo`` shows (including its detailed mode) and
    runs ``choose``, ``roll``, ``flip``, ``rps``, ``8ball``, ``lmgtfy`` and
    ``urban`` right on the page.
    """

    bot: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering General as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Server information and quick utilities.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_general_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error

        notifications: list[dict] = []
        result: dict = {}
        if kwargs.get("method") == "POST":
            notifications, result = await self._gen_handle_post(member, guild, kwargs)

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": GENERAL_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "info": self._gen_server_info(guild),
                "result": result,
            },
        }

    def _gen_server_info(self, guild: discord.Guild) -> dict:
        online = sum(
            1
            for m in guild.members
            if m.status is not discord.Status.offline and not m.bot
        )
        bots = sum(1 for m in guild.members if m.bot)
        created = guild.created_at or datetime.now(timezone.utc)
        age_days = (datetime.now(timezone.utc) - created).days

        by_status = {
            "online": 0,
            "idle": 0,
            "dnd": 0,
            "offline": 0,
        }
        for m in guild.members:
            by_status[str(m.status)] = by_status.get(str(m.status), 0) + 1

        return {
            "name": guild.name,
            "id": str(guild.id),
            "icon": str(guild.icon.url) if guild.icon else "",
            "banner": str(guild.banner.url) if guild.banner else "",
            "description": guild.description or "",
            "owner": str(guild.owner) if guild.owner else "Unknown",
            "created": created.strftime("%d %b %Y"),
            "age_days": age_days,
            "members": guild.member_count or len(guild.members),
            "humans": len(guild.members) - bots,
            "bots": bots,
            "online": online,
            "by_status": by_status,
            "text_channels": len(guild.text_channels),
            "voice_channels": len(guild.voice_channels),
            "stage_channels": len(getattr(guild, "stage_channels", [])),
            "forums": len(getattr(guild, "forums", [])),
            "categories": len(guild.categories),
            "roles": len(guild.roles) - 1,
            "emojis": len(guild.emojis),
            "emoji_limit": guild.emoji_limit,
            "stickers": len(getattr(guild, "stickers", [])),
            "boosts": guild.premium_subscription_count or 0,
            "boost_tier": guild.premium_tier,
            "boosters": len(guild.premium_subscribers),
            "verification": VERIFICATION_LABELS.get(
                str(guild.verification_level), str(guild.verification_level)
            ),
            "content_filter": str(guild.explicit_content_filter).replace("_", " "),
            "mfa": "Required" if guild.mfa_level else "Not required",
            "locale": str(guild.preferred_locale),
            "afk_channel": guild.afk_channel.name if guild.afk_channel else "None",
            "afk_timeout": (guild.afk_timeout or 0) // 60,
            "rules_channel": guild.rules_channel.name if guild.rules_channel else "None",
            "system_channel": guild.system_channel.name if guild.system_channel else "None",
            "features": sorted(f.replace("_", " ").title() for f in guild.features),
            "vanity": guild.vanity_url_code or "",
            "shard": guild.shard_id,
            "large": guild.large,
        }

    async def _gen_handle_post(
        self, member: discord.Member, guild: discord.Guild, kwargs: dict
    ) -> tuple[list[dict], dict]:
        field = form_reader(kwargs)
        action = field("action")

        try:
            if action == "choose":
                options = [
                    line.strip()
                    for line in (field("choices") or "").splitlines()
                    if line.strip()
                ]
                if len(options) < 2:
                    return (
                        [{"message": "Give at least two options.", "category": "warning"}],
                        {},
                    )
                return [], {"title": "Choice", "body": random.choice(options)}

            if action == "roll":
                sides = field.integer("sides", 100) or 100
                if sides < 1:
                    return [{"message": "Roll needs at least 1 side.",
                             "category": "warning"}], {}
                count = max(1, min(field.integer("count", 1) or 1, 25))
                rolls = [random.randint(1, sides) for _ in range(count)]
                body = ", ".join(str(r) for r in rolls)
                if count > 1:
                    body += f"  (total {sum(rolls)})"
                return [], {"title": f"Rolling d{sides} x{count}", "body": body}

            if action == "flip":
                return [], {
                    "title": "Coin flip",
                    "body": random.choice(["Heads!", "Tails!"]),
                }

            if action == "rps":
                choice = (field("choice") or "").lower()
                if choice not in ("rock", "paper", "scissors"):
                    return [{"message": "Pick rock, paper or scissors.",
                             "category": "warning"}], {}
                bot_choice = random.choice(["rock", "paper", "scissors"])
                beats = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
                if bot_choice == choice:
                    outcome = "It's a draw."
                elif beats[choice] == bot_choice:
                    outcome = "You win!"
                else:
                    outcome = "I win!"
                return [], {
                    "title": "Rock paper scissors",
                    "body": f"You picked {choice}, I picked {bot_choice}. {outcome}",
                }

            if action == "8ball":
                question = (field("question") or "").strip()
                if not question:
                    return [{"message": "Ask a question first.", "category": "warning"}], {}
                return [], {
                    "title": question,
                    "body": random.choice(list(type(self).ball)),
                }

            if action == "lmgtfy":
                terms = (field("terms") or "").strip()
                if not terms:
                    return [{"message": "Enter something to search for.",
                             "category": "warning"}], {}
                from urllib.parse import quote_plus

                return [], {
                    "title": "Let me google that for you",
                    "link": f"https://lmgtfy.com/?q={quote_plus(terms)}",
                    "body": terms,
                }

            if action == "urban":
                word = (field("word") or "").strip()
                if not word:
                    return [{"message": "Enter a word.", "category": "warning"}], {}
                entries = await self._gen_urban(word)
                if entries is None:
                    return (
                        [{"message": "Urban Dictionary could not be reached.",
                          "category": "danger"}],
                        {},
                    )
                if not entries:
                    return [{"message": "No entries found.", "category": "info"}], {}
                return [], {"title": f"Urban Dictionary: {word}", "urban": entries}
        except Exception as exc:  # noqa: BLE001
            log.exception("General dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}], {}

        return [{"message": f"Unknown action: {action}", "category": "warning"}], {}

    async def _gen_urban(self, word: str) -> list[dict] | None:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    URBAN_URL,
                    headers={"content-type": "application/json"},
                    params={"term": word.lower()},
                ) as response:
                    data = await response.json()
        except aiohttp.ClientError:
            return None

        out = []
        for entry in (data.get("list") or [])[:5]:
            out.append(
                {
                    "word": entry.get("word", word),
                    "author": entry.get("author", ""),
                    "definition": (entry.get("definition") or "")[:1200],
                    "example": (entry.get("example") or "")[:600],
                    "up": entry.get("thumbs_up", 0),
                    "down": entry.get("thumbs_down", 0),
                    "permalink": entry.get("permalink", ""),
                }
            )
        return out


GENERAL_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-info-circle"></i> {{ guild_name }}</h4>
    <p>Everything <code>[p]serverinfo</code> reports, plus the General cog's
       utilities without leaving the dashboard.</p>
  </div>

  {% if result %}
    <div class="dz-panel">
      <h5><i class="fa fa-bolt"></i> {{ result.title }}</h5>
      {% if result.body %}<div class="dz-text">{{ result.body }}</div>{% endif %}
      {% if result.link %}
        <p><a class="dz-btn" href="{{ result.link }}" target="_blank" rel="noopener">
          <i class="fa fa-external-link"></i> Open link</a></p>
      {% endif %}
      {% for u in result.urban or [] %}
        <div class="dz-embed" style="max-width:none; margin-top:10px;">
          <div class="et">{{ u.word }} &mdash; by {{ u.author }}</div>
          <div class="ed">{{ u.definition }}</div>
          {% if u.example %}<div class="efield"><b>Example</b>{{ u.example }}</div>{% endif %}
          <div class="ef">{{ u.up }} up / {{ u.down }} down
            {% if u.permalink %} &middot;
              <a href="{{ u.permalink }}" target="_blank" rel="noopener">permalink</a>
            {% endif %}
          </div>
        </div>
      {% endfor %}
    </div>
  {% endif %}

  {{ stats([('Members', info.members),
            ('Humans', info.humans),
            ('Bots', info.bots),
            ('Roles', info.roles),
            ('Boosts', info.boosts)]) }}

  <div class="dz-panel">
    <h5><i class="fa fa-server"></i> Overview</h5>
    <div class="dz-grid two">
      <div>
        <table class="dz-t">
          <tr><th>Owner</th><td>{{ info.owner }}</td></tr>
          <tr><th>Server ID</th><td><code>{{ info.id }}</code></td></tr>
          <tr><th>Created</th><td>{{ info.created }} ({{ info.age_days }} days ago)</td></tr>
          <tr><th>Shard</th><td>{{ info.shard }}</td></tr>
          <tr><th>Locale</th><td>{{ info.locale }}</td></tr>
          {% if info.vanity %}
            <tr><th>Vanity URL</th><td><code>{{ info.vanity }}</code></td></tr>
          {% endif %}
          {% if info.description %}
            <tr><th>Description</th><td>{{ info.description }}</td></tr>
          {% endif %}
        </table>
      </div>
      <div>
        <table class="dz-t">
          <tr><th>Text channels</th><td>{{ info.text_channels }}</td></tr>
          <tr><th>Voice channels</th><td>{{ info.voice_channels }}</td></tr>
          <tr><th>Stage channels</th><td>{{ info.stage_channels }}</td></tr>
          <tr><th>Forums</th><td>{{ info.forums }}</td></tr>
          <tr><th>Categories</th><td>{{ info.categories }}</td></tr>
          <tr><th>Emojis</th><td>{{ info.emojis }} / {{ info.emoji_limit }}</td></tr>
          <tr><th>Stickers</th><td>{{ info.stickers }}</td></tr>
        </table>
      </div>
    </div>
  </div>

  <div class="dz-panel">
    <h5><i class="fa fa-shield"></i> Moderation and presence</h5>
    <div class="dz-grid two">
      <div>
        <table class="dz-t">
          <tr><th>Verification</th><td>{{ info.verification }}</td></tr>
          <tr><th>Content filter</th><td>{{ info.content_filter }}</td></tr>
          <tr><th>2FA for mods</th><td>{{ info.mfa }}</td></tr>
          <tr><th>Rules channel</th><td>{{ info.rules_channel }}</td></tr>
          <tr><th>System channel</th><td>{{ info.system_channel }}</td></tr>
          <tr><th>AFK</th>
              <td>{{ info.afk_channel }} after {{ info.afk_timeout }} min</td></tr>
        </table>
      </div>
      <div>
        <table class="dz-t">
          <tr><th>Online</th><td>{{ info.by_status.online }}</td></tr>
          <tr><th>Idle</th><td>{{ info.by_status.idle }}</td></tr>
          <tr><th>Do not disturb</th><td>{{ info.by_status.dnd }}</td></tr>
          <tr><th>Offline</th><td>{{ info.by_status.offline }}</td></tr>
          <tr><th>Boost tier</th>
              <td>{{ info.boost_tier }} ({{ info.boosters }} boosters)</td></tr>
        </table>
      </div>
    </div>
    {% if info.features %}
      <p class="dz-hint" style="margin-top:10px;">
        {% for f in info.features %}<span class="dz-tag">{{ f }}</span> {% endfor %}
      </p>
    {% endif %}
  </div>

  <div class="dz-grid two">
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-random"></i> Pick one</h5>
        <p class="dz-hint">One option per line.</p>
        <textarea class="dz-area" name="choices" placeholder="pizza&#10;sushi&#10;tacos"></textarea>
        <div class="dz-save">
          <button class="dz-btn primary" name="action" value="choose">
            <i class="fa fa-random"></i> Choose
          </button>
        </div>
      </div>
    </form>

    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-cube"></i> Dice and coins</h5>
        <div class="dz-row">
          <input class="dz-input" type="number" min="1" name="count" value="1"
                 style="max-width:100px;" title="How many dice" />
          <input class="dz-input" type="number" min="1" name="sides" value="100"
                 style="max-width:130px;" title="Sides per die" />
          <button class="dz-btn primary" name="action" value="roll">
            <i class="fa fa-cube"></i> Roll
          </button>
          <button class="dz-btn" name="action" value="flip">
            <i class="fa fa-circle-o"></i> Flip a coin
          </button>
        </div>
        <label class="dz-label" style="margin-top:14px;">Rock paper scissors</label>
        <div class="dz-row">
          <select class="dz-select" name="choice" style="max-width:180px;">
            <option value="rock">Rock</option>
            <option value="paper">Paper</option>
            <option value="scissors">Scissors</option>
          </select>
          <button class="dz-btn" name="action" value="rps">
            <i class="fa fa-hand-rock-o"></i> Play
          </button>
        </div>
      </div>
    </form>
  </div>

  <div class="dz-grid two">
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-magic"></i> Magic 8-ball</h5>
        <div class="dz-row">
          <input class="dz-input" type="text" name="question"
                 placeholder="Will it rain?" style="flex:1 1 200px;" />
          <button class="dz-btn primary" name="action" value="8ball">
            <i class="fa fa-magic"></i> Ask
          </button>
        </div>
      </div>
    </form>

    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-search"></i> Lookups</h5>
        <div class="dz-row">
          <input class="dz-input" type="text" name="word"
                 placeholder="urban dictionary term" style="flex:1 1 160px;" />
          <button class="dz-btn" name="action" value="urban">
            <i class="fa fa-book"></i> Define
          </button>
        </div>
        <div class="dz-row" style="margin-top:10px;">
          <input class="dz-input" type="text" name="terms"
                 placeholder="let me google that for you" style="flex:1 1 160px;" />
          <button class="dz-btn" name="action" value="lmgtfy">
            <i class="fa fa-google"></i> Build link
          </button>
        </div>
      </div>
    </form>
  </div>
</div>
"""
)
