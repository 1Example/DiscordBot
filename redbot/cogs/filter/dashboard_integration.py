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
)

log = logging.getLogger("red.filter.dashboard")

FILTER_CHANNEL_KINDS = ("text", "voice", "stage", "forum")


class DashboardIntegration:
    """Word filter management from the dashboard.

    Covers ``[p]filter`` and ``[p]filterset`` in full: the server word list, the
    per-channel lists, name filtering with its replacement name, and the
    strike-based auto-ban.
    """

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Filter as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Filtered words, per-channel lists and auto-ban settings.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_filter_page(
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
                    "error_message": "Only server moderators can change the filter.",
                }
            notifications = await self._filter_handle_post(guild, kwargs)

        settings = await self.config.guild(guild).all()
        words = sorted(settings.get("filter") or [])

        # Only channels that actually have their own list are worth showing.
        all_channels = await self.config.all_channels()
        channel_rows = []
        for channel_id, data in all_channels.items():
            channel = guild.get_channel(channel_id)
            if channel is None or not data.get("filter"):
                continue
            channel_rows.append(
                {
                    "id": str(channel.id),
                    "name": channel.name,
                    "words": sorted(data["filter"]),
                }
            )
        channel_rows.sort(key=lambda c: c["name"])

        strikes = await self.config.all_members(guild)
        offenders = sorted(
            (
                {
                    "name": getattr(guild.get_member(uid), "display_name", f"ID {uid}"),
                    "count": data.get("filter_count", 0),
                }
                for uid, data in strikes.items()
                if data.get("filter_count")
            ),
            key=lambda o: -o["count"],
        )[:25]

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": FILTER_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_staff": staff,
                "words": words,
                "words_text": "\n".join(words),
                "channels": channel_rows,
                "channel_options": channel_options(
                    guild, kinds=FILTER_CHANNEL_KINDS
                ),
                "filter_names": bool(settings.get("filter_names")),
                "default_name": settings.get("filter_default_name") or "John Doe",
                "ban_count": settings.get("filterban_count") or 0,
                "ban_time": settings.get("filterban_time") or 0,
                "channel_word_total": sum(len(c["words"]) for c in channel_rows),
                "offenders": offenders,
            },
        }

    @staticmethod
    def _filter_words(raw: str) -> list[str]:
        """Split a textarea into words, one per line, lowercased and de-duplicated."""
        seen: list[str] = []
        for line in (raw or "").splitlines():
            word = line.strip().lower()
            if word and word not in seen:
                seen.append(word)
        return seen

    async def _filter_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

        try:
            if action == "save_words":
                words = self._filter_words(field("words"))
                await self.config.guild(guild).filter.set(words)
                self.invalidate_cache(guild)
                return [
                    {"message": f"Server filter saved with {len(words)} word(s).",
                     "category": "success"}
                ]

            if action == "add_words":
                words = self._filter_words(field("words"))
                if not words:
                    return [{"message": "Nothing to add.", "category": "warning"}]
                added = await self.add_to_filter(guild, words)
                self.invalidate_cache(guild)
                if added:
                    return [{"message": "Words added to the server filter.",
                             "category": "success"}]
                return [{"message": "Those words were already filtered.", "category": "info"}]

            if action == "clear_words":
                await self.config.guild(guild).filter.clear()
                self.invalidate_cache(guild)
                return [{"message": "Server filter cleared.", "category": "success"}]

            if action == "channel_save":
                channel = guild.get_channel(field.integer("channel_id", 0) or 0)
                if channel is None:
                    return [{"message": "Pick a channel first.", "category": "warning"}]
                words = self._filter_words(field("words"))
                await self.config.channel(channel).filter.set(words)
                self.invalidate_cache(guild, channel)
                return [
                    {
                        "message": f"#{channel.name} filter saved with {len(words)} word(s).",
                        "category": "success",
                    }
                ]

            if action == "channel_clear":
                channel = guild.get_channel(field.integer("channel_id", 0) or 0)
                if channel is None:
                    return [{"message": "That channel no longer exists.",
                             "category": "warning"}]
                await self.config.channel(channel).filter.clear()
                self.invalidate_cache(guild, channel)
                return [
                    {"message": f"#{channel.name} filter cleared.", "category": "success"}
                ]

            if action == "save_names":
                enabled = field.checked("filter_names")
                name = (field("default_name") or "").strip() or "John Doe"
                if len(name) > 32:
                    return [
                        {"message": "A nickname cannot be longer than 32 characters.",
                         "category": "warning"}
                    ]
                await self.config.guild(guild).filter_names.set(enabled)
                await self.config.guild(guild).filter_default_name.set(name)
                state = "on" if enabled else "off"
                return [
                    {"message": f"Name filtering is now {state}; replacement name is "
                                f"\"{name}\".", "category": "success"}
                ]

            if action == "save_ban":
                count = field.integer("ban_count", 0) or 0
                seconds = field.integer("ban_time", 0) or 0
                if count < 0 or seconds < 0:
                    return [{"message": "Values cannot be negative.", "category": "warning"}]
                if (count == 0) != (seconds == 0):
                    return [
                        {
                            "message": "Set both the strike count and the window, or set "
                            "both to 0 to disable auto-ban.",
                            "category": "warning",
                        }
                    ]
                await self.config.guild(guild).filterban_count.set(count)
                await self.config.guild(guild).filterban_time.set(seconds)
                if count == 0:
                    return [{"message": "Filter auto-ban disabled.", "category": "success"}]
                return [
                    {
                        "message": f"Members are banned after {count} filtered message(s) "
                        f"within {seconds} second(s).",
                        "category": "success",
                    }
                ]

            if action == "reset_strikes":
                await self.config.clear_all_members(guild)
                return [{"message": "Filter strike counts reset.", "category": "success"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("Filter dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]


FILTER_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-filter"></i> Word filter in {{ guild_name }}</h4>
    <p>Messages containing a filtered word are deleted. Channel lists apply on
       top of the server list.</p>
  </div>

  {{ stats([('Server words', words|length),
            ('Channel overrides', channels|length),
            ('Channel words', channel_word_total),
            ('Auto-ban', (ban_count ~ ' / ' ~ ban_time ~ 's') if ban_count else 'off')]) }}

  {% if not is_staff %}
    <div class="dz-panel">
      <p class="dz-empty">You need moderator permissions to change the filter.</p>
    </div>
  {% else %}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-list"></i> Server word list</h5>
      <p class="dz-hint">One word or phrase per line. Matching is case-insensitive.</p>
      <textarea class="dz-area" name="words" style="min-height:190px;"
                placeholder="badword&#10;another phrase">{{ words_text }}</textarea>
      <div class="dz-row dz-save">
        <button class="dz-btn primary" name="action" value="save_words">
          <i class="fa fa-save"></i> Save list
        </button>
        <button class="dz-btn" name="action" value="add_words">
          <i class="fa fa-plus"></i> Add without replacing
        </button>
        {{ confirm('Clear list', 'clear_words',
                   'Remove every filtered word from the server list?') }}
      </div>
    </div>
  </form>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-hashtag"></i> Channel list</h5>
      <p class="dz-hint">Pick a channel, then save the words that apply only there.</p>
      <div class="dz-grid two">
        <div>
          <label class="dz-label">Channel</label>
          {{ picker('channel_id', channel_options, false, 8, 'Search channels...') }}
        </div>
        <div>
          <label class="dz-label">Words for that channel</label>
          <textarea class="dz-area" name="words"
                    placeholder="one word per line"></textarea>
        </div>
      </div>
      <div class="dz-row dz-save">
        <button class="dz-btn primary" name="action" value="channel_save">
          <i class="fa fa-save"></i> Save channel list
        </button>
      </div>
    </div>
  </form>

  {% if channels %}
    <div class="dz-panel">
      <h5><i class="fa fa-sitemap"></i> Channels with their own list</h5>
      <table class="dz-t">
        <tr><th>Channel</th><th>Words</th><th></th></tr>
        {% for c in channels %}
          <tr>
            <td>#{{ c.name }}</td>
            <td>{% for w in c.words %}<span class="dz-tag">{{ w }}</span> {% endfor %}</td>
            <td>
              <form method="POST">
                <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                <input type="hidden" name="channel_id" value="{{ c.id }}" />
                {{ confirm('', 'channel_clear',
                           'Clear the filter for #' ~ c.name ~ '?') }}
              </form>
            </td>
          </tr>
        {% endfor %}
      </table>
    </div>
  {% endif %}

  <div class="dz-grid two">
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-user"></i> Name filtering</h5>
        <p class="dz-hint">Rename members whose name or nickname hits the filter.</p>
        <label class="dz-toggle">
          <input type="checkbox" name="filter_names" {% if filter_names %}checked{% endif %} />
          <span>Filter names and nicknames</span>
        </label>
        <label class="dz-label">Replacement nickname</label>
        <input class="dz-input" type="text" name="default_name" maxlength="32"
               value="{{ default_name }}" />
        <div class="dz-save">
          <button class="dz-btn primary" name="action" value="save_names">
            <i class="fa fa-save"></i> Save
          </button>
        </div>
      </div>
    </form>

    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-gavel"></i> Auto-ban</h5>
        <p class="dz-hint">Ban a member after enough filtered messages in a window.
           Set both to 0 to turn it off.</p>
        <label class="dz-label">Filtered messages before a ban</label>
        <input class="dz-input" type="number" min="0" name="ban_count" value="{{ ban_count }}" />
        <label class="dz-label">Window in seconds</label>
        <input class="dz-input" type="number" min="0" name="ban_time" value="{{ ban_time }}" />
        <div class="dz-row dz-save">
          <button class="dz-btn primary" name="action" value="save_ban">
            <i class="fa fa-save"></i> Save
          </button>
          {{ confirm('Reset strikes', 'reset_strikes',
                     'Reset the filter strike count for every member?') }}
        </div>
      </div>
    </form>
  </div>

  {% if offenders %}
    <div class="dz-panel">
      <h5><i class="fa fa-exclamation-triangle"></i> Current strikes</h5>
      <table class="dz-t">
        <tr><th>Member</th><th>Filtered messages in the window</th></tr>
        {% for o in offenders %}
          <tr><td>{{ o.name }}</td><td>{{ o.count }}</td></tr>
        {% endfor %}
      </table>
    </div>
  {% endif %}

  {% endif %}
</div>
"""
)
