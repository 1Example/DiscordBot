from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands

from .tag_type import INTERNAL_TAGS
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
    """Feed management: add, format, inspect and remove RSS feeds.

    The only place these can be changed - the ``[p]rss`` group is gone. So
    everything it did has to stay here: adding and removing feeds, forcing
    a post, the character limit, the tag allow list, the message template,
    the embed colour and image tags, listing what tags a feed offers,
    finding feeds on a website, and the owner's parsing overrides.
    """

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

        owner = await self.bot.is_owner(user)
        notifications: list[dict] = []
        tags: dict = {}
        found: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications, tags, found = await self._rss_handle_post(
                guild, owner, kwargs
            )

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
                "is_owner": owner,
                "tags": tags,
                "found": found,
                "parse_overrides": (await self.config.use_published()) if owner else [],
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
                        # The whole template, not a preview: this page is
                        # the only place it can be edited now.
                        "template": data.get("template") or "",
                        "embed": bool(data.get("embed", True)),
                        "embed_color": (data.get("embed_color") or "").replace(
                            "0x", "#"
                        ),
                        "embed_image": data.get("embed_image") or "",
                        "embed_thumbnail": data.get("embed_thumbnail") or "",
                        "tag_count": len(data.get("tags") or []),
                        "limit": data.get("limit") or 0,
                        "allowed_tags": sorted(data.get("allowed_tags") or []),
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

    async def _rss_handle_post(
        self, guild: discord.Guild, owner: bool, kwargs: dict
    ) -> tuple[list[dict], dict, list[dict]]:
        field = form_reader(kwargs)
        action = field("action")

        # These three are not about one feed in one channel.
        if action == "find":
            notes, found = await self._rss_find_feeds(field)
            return notes, {}, found
        if action in ("parse_add", "parse_remove"):
            if not owner:
                return [
                    {
                        "message": "Only the bot owner can change parsing overrides.",
                        "category": "danger",
                    }
                ], {}, []
            return await self._rss_parse_override(action, field), {}, []

        channel = guild.get_channel(int(field("channel") or 0) or 0)
        if channel is None or not isinstance(channel, discord.TextChannel):
            return [
                {"message": "Pick a valid text channel.", "category": "warning"}
            ], {}, []

        try:
            if action == "list_tags":
                notes, tags = await self._rss_list_tags(channel, field("name"))
                return notes, tags, []
            if action == "save_template":
                return await self._rss_save_template(channel, field("name"), field), {}, []
            if action == "save_embed":
                return await self._rss_save_embed(channel, field("name"), field), {}, []
            if action == "add":
                return await self._rss_add(channel, field("name"), field("url")), {}, []
            if action == "remove":
                return await self._rss_remove(channel, field("name")), {}, []
            if action == "toggle_embed":
                return await self._rss_toggle_embed(channel, field("name")), {}, []
            if action == "force":
                return await self._rss_force_post(channel, field("name")), {}, []
            if action == "limit":
                return await self._rss_set_limit(channel, field("name"), field), {}, []
            if action in ("tag_allow", "tag_deny"):
                return await self._rss_tag(action, channel, field("name"), field), {}, []
        except Exception as exc:  # noqa: BLE001
            log.exception("RSS dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}], {}, []

        return [
            {"message": f"Unknown action: {action}", "category": "warning"}
        ], {}, []

    async def _rss_feed_or_warning(self, channel, name: str | None):
        """(feed, None) or (None, the warning to show)."""
        name = (name or "").strip().lower()
        feed = await self.config.channel(channel).feeds.get_raw(name, default=None)
        if not feed:
            return None, [
                {
                    "message": f"'{name}' is not a feed in {channel.mention}.",
                    "category": "warning",
                }
            ]
        return feed, None

    async def _rss_save_template(self, channel, name: str | None, field) -> list[dict]:
        """The message template for a feed, as `[p]rss template` set it.

        The command took \\n and \\t as escapes because a prefix invocation
        cannot contain real newlines. A textarea can, so both are accepted.
        """
        feed, warning = await self._rss_feed_or_warning(channel, name)
        if warning:
            return warning
        name = (name or "").strip().lower()
        template = (field("template") or "").replace("\\t", "\t").replace("\\n", "\n")
        if not template.strip():
            return [{"message": "A template cannot be empty.", "category": "warning"}]
        async with self.config.channel(channel).feeds() as feeds:
            feeds[name]["template"] = template
        return [{"message": f"Template saved for '{name}'.", "category": "success"}]

    async def _rss_save_embed(self, channel, name: str | None, field) -> list[dict]:
        """Embed on/off, colour, image tag and thumbnail tag in one save."""
        feed, warning = await self._rss_feed_or_warning(channel, name)
        if warning:
            return warning
        name = (name or "").strip().lower()
        notes: list[dict] = []

        embed_on = field.checked("embed")
        raw_colour = (field("embed_color") or "").strip()
        hex_code = None
        if raw_colour:
            from .color import Color

            hex_code = await Color()._color_converter(raw_colour.replace(" ", "_"))
            if not hex_code:
                return [
                    {
                        "message": f"'{raw_colour}' is not a colour. Use a hex code like "
                        "#990000, a Discord colour name, or a CSS3 colour name.",
                        "category": "warning",
                    }
                ]
            # 0xFFFFFF does not render as white in an embed, so nudge it.
            if hex_code == "0xFFFFFF":
                hex_code = "0xFFFFFE"

        image = (field("embed_image") or "").strip().lstrip("$")
        thumbnail = (field("embed_thumbnail") or "").strip().lstrip("$")

        async with self.config.channel(channel).feeds() as feeds:
            feeds[name]["embed"] = embed_on
            feeds[name]["embed_color"] = hex_code
            feeds[name]["embed_image"] = image or None
            feeds[name]["embed_thumbnail"] = thumbnail or None

        if hex_code:
            from .color import Color

            colour_name = await Color()._hex_to_css3_name(hex_code)
            notes.append(
                {
                    "message": f"Colour read as {hex_code.replace('0x', '#')} ({colour_name}).",
                    "category": "info",
                }
            )
        if not embed_on and (hex_code or image or thumbnail):
            notes.append(
                {
                    "message": "Embeds are off for this feed, so the colour and image "
                    "settings will not show until you turn them on.",
                    "category": "info",
                }
            )
        return notes + [{"message": f"Embed settings saved for '{name}'.", "category": "success"}]

    async def _rss_list_tags(self, channel, name: str | None) -> tuple[list[dict], dict]:
        """The tags this feed offers, with a preview - `[p]rss listtags`.

        Needs a live fetch: the tag set comes from the feed's newest entry, or
        from its channel header when the feed has no entries yet.
        """

        feed, warning = await self._rss_feed_or_warning(channel, name)
        if warning:
            return warning, {}
        name = (name or "").strip().lower()

        parsed = await self._fetch_feedparser_object(feed["url"])
        if not parsed or parsed.entries is None:
            return [
                {
                    "message": f"Could not fetch '{name}'. "
                    f"{getattr(parsed, 'error', '') or ''}".strip(),
                    "category": "danger",
                }
            ], {}
        source = parsed.entries[0] if parsed.entries else parsed.feed
        obj = await self._add_to_feedparser_object(source, feed["url"])

        rows = []
        for tag_name, content in sorted(obj.items()):
            if tag_name in INTERNAL_TAGS:
                continue
            kind = await self._get_tag_content_type(content)
            preview = str(content)
            rows.append(
                {
                    "tag": tag_name,
                    "kind": kind.name.lower() if hasattr(kind, "name") else str(kind),
                    "preview": preview[:220] + ("..." if len(preview) > 220 else ""),
                }
            )
        return [], {"feed": name, "channel": channel.mention, "rows": rows}

    async def _rss_find_feeds(self, field) -> tuple[list[dict], list[dict]]:
        """Feeds advertised in a page's HTML - `[p]rss find`."""
        import aiohttp
        from bs4 import BeautifulSoup

        url = (field("website_url") or "").strip()
        if not url:
            return [{"message": "Enter a website address.", "category": "warning"}], []
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"

        timeout = aiohttp.ClientTimeout(total=20)
        try:
            async with aiohttp.ClientSession(headers=self._headers, timeout=timeout) as session:
                async with session.get(url) as response:
                    soup = BeautifulSoup(await response.text(errors="replace"), "html.parser")
        except aiohttp.ClientError:
            return [{"message": f"I cannot reach {url}.", "category": "danger"}], []
        except Exception as exc:  # noqa: BLE001
            return [{"message": f"{url} could not be read: {exc}", "category": "danger"}], []

        found = []
        for link in soup.find_all("link"):
            kind = (link.get("type") or "").lower()
            if kind not in ("application/rss+xml", "application/atom+xml", "text/xml"):
                continue
            href = link.get("href") or ""
            if href.startswith("/"):
                from urllib.parse import urljoin

                href = urljoin(url, href)
            if href:
                found.append({"title": link.get("title") or href, "url": href})
        if not found:
            return [
                {
                    "message": f"{url} does not advertise any feeds in its HTML. "
                    "Some sites have one anyway; try /feed or /rss on the site.",
                    "category": "info",
                }
            ], []
        return [
            {"message": f"Found {len(found)} feed(s) on {url}.", "category": "success"}
        ], found

    async def _rss_parse_override(self, action: str, field) -> list[dict]:
        """The site list from `[p]rss parse`, which changes how times are read."""
        raw = (field("website_url") or "").strip()
        website = self._find_website(raw)
        if not website:
            return [
                {
                    "message": f"I cannot find a website in '{raw}'. Use something like "
                    "https://www.website.com/ or www.website.com.",
                    "category": "warning",
                }
            ]
        overrides = await self.config.use_published()
        if action == "parse_add":
            if website in overrides:
                return [
                    {"message": f"{website} is already overridden.", "category": "info"}
                ]
            overrides.append(website)
            await self.config.use_published.set(overrides)
            return [
                {
                    "message": f"{website} will now use the published date rather than "
                    "the updated date.",
                    "category": "success",
                }
            ]
        if website not in overrides:
            return [{"message": f"{website} is not overridden.", "category": "info"}]
        overrides.remove(website)
        await self.config.use_published.set(overrides)
        return [{"message": f"{website} is no longer overridden.", "category": "success"}]

    async def _rss_force_post(self, channel, name: str | None) -> list[dict]:
        """Post the newest entry now, the way `[p]rss force` does."""
        name = (name or "").strip().lower()
        feed = await self.config.channel(channel).feeds.get_raw(name, default=None)
        if not feed:
            return [
                {"message": f"'{name}' is not a feed in {channel.mention}.",
                 "category": "warning"}
            ]
        await self.get_current_feed(channel, name, feed, force=True)
        return [
            {"message": f"Forced a post of '{name}' into {channel.mention}.",
             "category": "success"}
        ]

    async def _rss_set_limit(self, channel, name: str | None, field) -> list[dict]:
        """Character cap for a feed's posts, matching `[p]rss limit`."""
        name = (name or "").strip().lower()
        feed = await self.config.channel(channel).feeds.get_raw(name, default=None)
        if not feed:
            return [
                {"message": f"'{name}' is not a feed in {channel.mention}.",
                 "category": "warning"}
            ]
        limit = field.integer("limit", 0) or 0
        if limit < 0:
            return [
                {"message": "A character limit cannot be negative.", "category": "warning"}
            ]
        note = ""
        # The command treats anything over 20000 as unlimited and floors at 20.
        if limit > 20000:
            limit = 0
        elif 0 < limit < 20:
            limit = 20
            note = " The minimum is 20, so that is what was saved."
        async with self.config.channel(channel).feeds() as feeds:
            feeds[name]["limit"] = limit
        if limit == 0:
            return [
                {"message": f"'{name}' posts are no longer truncated.{note}",
                 "category": "success"}
            ]
        return [
            {"message": f"'{name}' posts are capped at {limit} characters.{note}",
             "category": "success"}
        ]

    async def _rss_tag(self, action: str, channel, name: str | None, field) -> list[dict]:
        """Allow-list a feed's tags, matching `[p]rss tag allow`.

        With an allow list set, only entries carrying one of those tags post.
        """
        name = (name or "").strip().lower()
        feed = await self.config.channel(channel).feeds.get_raw(name, default=None)
        if not feed:
            return [
                {"message": f"'{name}' is not a feed in {channel.mention}.",
                 "category": "warning"}
            ]
        tag = (field("tag") or "").strip().lower()
        if not tag:
            return [{"message": "Enter a tag.", "category": "warning"}]

        async with self.config.channel(channel).feeds() as feeds:
            allowed = feeds[name].get("allowed_tags") or []
            if action == "tag_allow":
                if tag in allowed:
                    return [
                        {"message": f"'{tag}' is already allowed on '{name}'.",
                         "category": "info"}
                    ]
                allowed.append(tag)
                message = f"'{tag}' added to the allowed tags for '{name}'."
            else:
                if tag not in allowed:
                    return [
                        {"message": f"'{tag}' is not in the allowed list for '{name}'.",
                         "category": "info"}
                    ]
                allowed.remove(tag)
                message = f"'{tag}' removed from the allowed tags for '{name}'."
            feeds[name]["allowed_tags"] = allowed
            remaining = len(allowed)

        if not remaining:
            message += " With no allowed tags, every entry posts again."
        return [{"message": message, "category": "success"}]

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
                {% if f.limit %}<span class="dz-tag">{{ f.limit }} chars</span>{% endif %}
                {% if f.allowed_tags %}
                  <span class="dz-tag warn">only: {{ f.allowed_tags|join(', ') }}</span>
                {% endif %}
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
                  <button class="dz-btn round" name="action" value="force"
                          title="Post the newest entry now">
                    <i class="fa fa-paper-plane"></i>
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
            <tr>
              <td colspan="5" style="padding-top:0;">
                <details>
                  <summary style="cursor:pointer; opacity:.8;">
                    Edit {{ f.name }} &mdash; template, embed, limit, tags
                  </summary>

                  <div class="dz-grid two" style="margin-top:10px;">
                    <form method="POST">
                      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                      <input type="hidden" name="channel" value="{{ ch.id }}" />
                      <input type="hidden" name="name" value="{{ f.name }}" />
                      <div class="dz-label">Message template</div>
                      <p class="dz-hint">
                        Each variable starts with <code>$</code>. Use the button below to
                        see which ones this feed offers.
                      </p>
                      <textarea class="dz-area" name="template" rows="5"
                                placeholder="$title\n$link">{{ f.template }}</textarea>
                      <div class="dz-row" style="margin-top:8px;">
                        <button class="dz-btn primary" name="action" value="save_template">
                          <i class="fa fa-save"></i> Save template
                        </button>
                        <button class="dz-btn" name="action" value="list_tags">
                          <i class="fa fa-tags"></i> Show available tags
                        </button>
                      </div>
                    </form>

                    <form method="POST">
                      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                      <input type="hidden" name="channel" value="{{ ch.id }}" />
                      <input type="hidden" name="name" value="{{ f.name }}" />
                      <div class="dz-label">Embed</div>
                      <label class="dz-toggle">
                        <input type="checkbox" name="embed" {% if f.embed %}checked{% endif %} />
                        <span>Post this feed as an embed</span>
                      </label>
                      <div class="dz-label" style="margin-top:8px;">Colour</div>
                      <input class="dz-input" type="text" name="embed_color"
                             value="{{ f.embed_color }}"
                             placeholder="#990000, blurple, or a CSS colour name" />
                      <div class="dz-grid two" style="margin-top:8px;">
                        <div>
                          <div class="dz-label">Large image tag</div>
                          <input class="dz-input" type="text" name="embed_image"
                                 value="{{ f.embed_image }}" placeholder="content_image01" />
                        </div>
                        <div>
                          <div class="dz-label">Thumbnail tag</div>
                          <input class="dz-input" type="text" name="embed_thumbnail"
                                 value="{{ f.embed_thumbnail }}" placeholder="media_thumbnail" />
                        </div>
                      </div>
                      <div class="dz-save">
                        <button class="dz-btn primary" name="action" value="save_embed">
                          <i class="fa fa-save"></i> Save embed settings
                        </button>
                      </div>
                    </form>
                  </div>

                  <form method="POST" class="dz-row" style="gap:6px; margin-top:10px;">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                    <input type="hidden" name="channel" value="{{ ch.id }}" />
                    <input type="hidden" name="name" value="{{ f.name }}" />
                    <input class="dz-input" type="number" min="0" name="limit"
                           value="{{ f.limit }}" placeholder="character limit (0 = none)"
                           style="max-width:210px;" />
                    <button class="dz-btn" name="action" value="limit" title="Save the limit">
                      <i class="fa fa-text-width"></i> Limit
                    </button>
                    <input class="dz-input" type="text" name="tag"
                           placeholder="tag to allow or remove" style="max-width:220px;" />
                    <button class="dz-btn" name="action" value="tag_allow"
                            title="Only post entries carrying this tag">
                      <i class="fa fa-filter"></i> Allow
                    </button>
                    <button class="dz-btn" name="action" value="tag_deny"
                            title="Stop requiring this tag">
                      <i class="fa fa-times"></i> Unallow
                    </button>
                  </form>
                </details>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  {% endfor %}

  {% if tags.rows %}
    <div class="dz-panel">
      <h5><i class="fa fa-tags"></i> Tags available from {{ tags.feed }}</h5>
      <p class="dz-hint">
        Use any of these in the template with a <code>$</code> in front. The
        preview is from the feed's newest entry, so it changes as the feed does.
      </p>
      <table class="dz-t">
        <thead><tr><th>Tag</th><th>Type</th><th>Preview</th></tr></thead>
        <tbody>
          {% for row in tags.rows %}
            <tr>
              <td><code>${{ row.tag }}</code></td>
              <td style="opacity:.7;">{{ row.kind }}</td>
              <td style="opacity:.8; word-break:break-word;">{{ row.preview }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  {% endif %}

  <div class="dz-panel">
    <h5><i class="fa fa-search"></i> Find a feed on a website</h5>
    <p class="dz-hint">
      Reads the page's HTML for a declared feed. Sites that have one without
      declaring it will not show up here.
    </p>
    <form method="POST" class="dz-row">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <input class="dz-input" style="flex:1 1 300px;" type="text" name="website_url"
             placeholder="https://www.example.com/" />
      <button class="dz-btn" name="action" value="find">
        <i class="fa fa-search"></i> Find feeds
      </button>
    </form>
    {% if found %}
      <table class="dz-t" style="margin-top:10px;">
        <thead><tr><th>Title</th><th>Feed URL</th></tr></thead>
        <tbody>
          {% for item in found %}
            <tr>
              <td>{{ item.title }}</td>
              <td style="word-break:break-all;">
                <a href="{{ item.url }}" target="_blank" rel="noopener">{{ item.url }}</a>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
      <p class="dz-hint">Copy one into the Add a feed box above.</p>
    {% endif %}
  </div>

  {% if is_owner %}
    <div class="dz-panel">
      <h5><i class="fa fa-clock-o"></i> Date parsing overrides</h5>
      <p class="dz-hint">
        Some sites set an updated date that moves, which makes old entries look
        new. For a site listed here the published date is used instead. This
        applies to every server the bot is in.
      </p>
      {% if parse_overrides %}
        <ul class="dz-hint" style="margin:0 0 10px 18px;">
          {% for site in parse_overrides %}<li><code>{{ site }}</code></li>{% endfor %}
        </ul>
      {% else %}
        <p class="dz-empty">No overrides.</p>
      {% endif %}
      <form method="POST" class="dz-row">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <input class="dz-input" style="flex:1 1 280px;" type="text" name="website_url"
               placeholder="www.example.com" />
        <button class="dz-btn" name="action" value="parse_add">
          <i class="fa fa-plus"></i> Add
        </button>
        <button class="dz-btn danger" name="action" value="parse_remove">
          <i class="fa fa-minus"></i> Remove
        </button>
      </form>
    </div>
  {% endif %}
</div>
"""
)
