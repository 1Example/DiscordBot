from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    channel_options,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
)

log = logging.getLogger("red.rss.dashboard")


class DashboardIntegration:
    """Feed management: browse, add, remove and inspect RSS feeds."""

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering RSS as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    # ------------------------------------------------------------------ page

    @dashboard_page(
        name=None,
        description="Manage RSS feeds for this server.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_rss_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        if not await is_staff(self.bot, user, member, guild):
            return {
                "status": 1,
                "error_title": "Forbidden",
                "error_message": "Only server administrators can manage feeds.",
            }

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._rss_handle_post(guild, kwargs)

        channels = await self._rss_collect(guild)
        total = sum(len(c["feeds"]) for c in channels)

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": RSS_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "channels": channels,
                "total": total,
                "channel_options": channel_options(guild),
            },
        }

    # ------------------------------------------------------------------ data

    async def _rss_collect(self, guild: discord.Guild) -> list[dict]:
        """Every channel in this guild that has at least one feed."""
        out = []
        for channel in guild.text_channels:
            try:
                feeds = await self.config.channel(channel).feeds()
            except Exception:  # noqa: BLE001 - a bad channel shouldn't kill the page
                log.exception("Could not read feeds for #%s", channel.name)
                continue
            if not feeds:
                continue
            entries = []
            for name, data in sorted(feeds.items()):
                data = data or {}
                entries.append(
                    {
                        "name": name,
                        "url": data.get("url") or "",
                        "title": data.get("title") or "",
                        "last_post": str(data.get("last_title") or "")[:90],
                        "template": (data.get("template") or "")[:120],
                        "embed": bool(data.get("embed", True)),
                        "tag_count": len(data.get("tags") or []),
                    }
                )
            out.append(
                {
                    "id": str(channel.id),
                    "name": f"#{channel.name}",
                    "feeds": entries,
                }
            )
        return out

    # ------------------------------------------------------------- post logic

    async def _rss_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

        channel = guild.get_channel(int(field("channel") or 0) or 0)
        if channel is None or not isinstance(channel, discord.TextChannel):
            return [{"message": "Pick a valid text channel.", "category": "warning"}]

        try:
            if action == "add":
                return await self._rss_add(channel, field("name"), field("url"))
            if action == "remove":
                return await self._rss_remove(channel, field("name"))
            if action == "toggle_embed":
                return await self._rss_toggle_embed(channel, field("name"))
        except Exception as exc:  # noqa: BLE001
            log.exception("RSS dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    async def _rss_add(self, channel, name: str | None, url: str | None) -> list[dict]:
        name = (name or "").strip().lower()
        url = (url or "").strip()
        if not name or not url:
            return [{"message": "Both a feed name and a URL are required.", "category": "warning"}]
        if " " in name:
            return [{"message": "Feed names cannot contain spaces.", "category": "warning"}]

        existing = await self.config.channel(channel).feeds()
        if name in (existing or {}):
            return [
                {"message": f"'{name}' already exists in {channel.mention}.", "category": "warning"}
            ]

        # Mirrors RSS._add_feed, minus the ctx-bound messaging so it can run
        # from the web with no invoking Discord message.
        feedparser_obj = await self._fetch_feedparser_object(url)
        if not feedparser_obj:
            return [
                {"message": "Could not fetch that feed - no feed objects found.", "category": "danger"}
            ]

        if feedparser_obj.entries:
            sorted_entries = await self._sort_by_post_time(feedparser_obj.entries)
        else:
            # A valid feed with no posts yet still has a usable header.
            sorted_entries = [feedparser_obj.feed]

        enriched = await self._add_to_feedparser_object(sorted_entries[0], url)
        rss_object = await self._convert_feedparser_to_rssfeed(name, enriched, url)

        async with self.config.channel(channel).feeds() as feeds:
            feeds[name] = rss_object.to_json()

        return [
            {
                "message": f"Feed '{name}' added to {channel.mention}. "
                f"Adjust the template with the rss template command.",
                "category": "success",
            }
        ]

    async def _rss_remove(self, channel, name: str | None) -> list[dict]:
        name = (name or "").strip().lower()
        async with self.config.channel(channel).feeds() as feeds:
            if name not in feeds:
                return [{"message": f"No feed named '{name}' here.", "category": "warning"}]
            del feeds[name]
        return [{"message": f"Removed '{name}' from {channel.mention}.", "category": "success"}]

    async def _rss_toggle_embed(self, channel, name: str | None) -> list[dict]:
        name = (name or "").strip().lower()
        async with self.config.channel(channel).feeds() as feeds:
            if name not in feeds:
                return [{"message": f"No feed named '{name}' here.", "category": "warning"}]
            current = feeds[name].get("embed", True)
            feeds[name]["embed"] = not current
        state = "embed" if not current else "plain text"
        return [{"message": f"'{name}' will now post as {state}.", "category": "success"}]


RSS_TEMPLATE = (
    BASE_CSS
    + """
<div class="dz">

  <div class="dz-head">
    <h4><i class="fa fa-rss"></i> RSS feeds in {{ guild_name }}</h4>
    <p>{{ total }} feed(s) across {{ channels|length }} channel(s).</p>
  </div>

  <div class="dz-panel">
    <h5><i class="fa fa-plus"></i> Add a feed</h5>
    <p class="dz-hint">The URL is fetched immediately, so a bad link fails right away.</p>
    <form method="POST" class="dz-row">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <input class="dz-input" style="flex:1 1 150px;" type="text" name="name"
             placeholder="short-name" />
      <input class="dz-input" style="flex:2 1 280px;" type="text" name="url"
             placeholder="https://example.com/feed.xml" />
      <select class="dz-select" style="flex:1 1 170px;" name="channel">
        {% for c in channel_options %}
          <option value="{{ c.id }}">{{ c.name }}</option>
        {% endfor %}
      </select>
      <button class="dz-btn primary" name="action" value="add">
        <i class="fa fa-plus"></i> Add feed
      </button>
    </form>
  </div>

  {% if not channels %}
    <p class="dz-empty">No feeds configured yet.</p>
  {% endif %}

  {% for ch in channels %}
    <div class="dz-panel">
      <h5><i class="fa fa-hashtag"></i> {{ ch.name }}</h5>
      <p class="dz-hint">{{ ch.feeds|length }} feed(s).</p>
      <table class="dz-t">
        <thead>
          <tr><th>Name</th><th>Source</th><th>Latest post</th><th>Format</th><th></th></tr>
        </thead>
        <tbody>
          {% for f in ch.feeds %}
            <tr>
              <td><b>{{ f.name }}</b></td>
              <td style="max-width:230px; overflow:hidden; text-overflow:ellipsis;">
                {% if f.url %}<a href="{{ f.url }}" target="_blank" rel="noopener">{{ f.title or f.url }}</a>
                {% else %}<span style="opacity:.5;">unknown</span>{% endif %}
              </td>
              <td style="opacity:.7;">{{ f.last_post or "-" }}</td>
              <td>
                <span class="dz-tag">{% if f.embed %}embed{% else %}text{% endif %}</span>
                {% if f.tag_count %}<span class="dz-tag">{{ f.tag_count }} tags</span>{% endif %}
              </td>
              <td style="white-space:nowrap; width:1%;">
                <form method="POST" style="display:inline;">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="channel" value="{{ ch.id }}" />
                  <input type="hidden" name="name" value="{{ f.name }}" />
                  <button class="dz-btn round" name="action" value="toggle_embed"
                          title="Switch between embed and plain text">
                    <i class="fa fa-exchange"></i>
                  </button>
                </form>
                <form method="POST" style="display:inline;"
                      onsubmit="return confirm('Remove {{ f.name }}?');">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="channel" value="{{ ch.id }}" />
                  <input type="hidden" name="name" value="{{ f.name }}" />
                  <button class="dz-btn round danger" name="action" value="remove" title="Remove">
                    <i class="fa fa-trash-o"></i>
                  </button>
                </form>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  {% endfor %}
</div>
"""
)
