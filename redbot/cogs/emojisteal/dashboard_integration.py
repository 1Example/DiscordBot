from __future__ import annotations

import io
import logging
import typing as t

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
)

log = logging.getLogger("red.emojisteal.dashboard")

# Discord's per-guild emoji allowance by boost tier.
TIER_LIMITS = {0: 50, 1: 100, 2: 150, 3: 250}
STICKER_LIMITS = {0: 5, 1: 15, 2: 30, 3: 60}
# Discord's own limit for a sticker file, in kilobytes.
STICKER_KB = 512

MAX_UPLOAD_BYTES = 256 * 1024


class DashboardIntegration:
    """Browse, add and remove server emoji and stickers."""

    bot: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering EmojiSteal as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Manage this server's emoji and stickers.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_emojisteal_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        # Emoji management is its own Discord permission; respect it rather
        # than falling back to plain admin.
        perms = member.guild_permissions
        # manage_expressions replaced manage_emojis in newer discord.py.
        can_edit_emoji = getattr(perms, "manage_expressions", None)
        if can_edit_emoji is None:
            can_edit_emoji = getattr(perms, "manage_emojis", False)
        allowed = bool(can_edit_emoji) or await is_staff(self.bot, user, member, guild)
        if not allowed:
            return {
                "status": 1,
                "error_title": "Forbidden",
                "error_message": "You need Manage Expressions to edit emoji.",
            }

        notifications: list[dict] = []
        lookup: dict = {}
        if kwargs.get("method") == "POST":
            notifications, lookup = await self._es_handle_post(guild, member, kwargs)

        tier = guild.premium_tier or 0
        limit = TIER_LIMITS.get(tier, 50)
        static = [e for e in guild.emojis if not e.animated]
        animated = [e for e in guild.emojis if e.animated]

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": EMOJISTEAL_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "stat_items": [
                    ("Static", f"{len(static)}/{limit}"),
                    ("Animated", f"{len(animated)}/{limit}"),
                    ("Stickers", f"{len(guild.stickers)}/{STICKER_LIMITS.get(tier, 5)}"),
                    ("Boost tier", tier),
                ],
                "static": self._es_rows(static),
                "animated": self._es_rows(animated),
                "stickers": [
                    {"id": str(s.id), "name": s.name, "url": str(s.url),
                     "description": (s.description or "")[:80]}
                    for s in guild.stickers
                ],
                "static_full": len(static) >= limit,
                "animated_full": len(animated) >= limit,
                "can_manage": bool(
                    guild.me and guild.me.guild_permissions.manage_emojis
                ),
                "max_kb": MAX_UPLOAD_BYTES // 1024,
                "lookup": lookup,
                "sticker_full": len(guild.stickers) >= STICKER_LIMITS.get(tier, 5),
                "sticker_kb": STICKER_KB,
            },
        }

    @staticmethod
    def _es_rows(emojis) -> list[dict]:
        return [
            {
                "id": str(e.id),
                "name": e.name,
                "url": str(e.url),
                "token": f"<{'a' if e.animated else ''}:{e.name}:{e.id}>",
                "available": bool(e.available),
            }
            for e in sorted(emojis, key=lambda e: e.name.lower())
        ]

    async def _es_handle_post(self, guild, actor, kwargs: dict) -> tuple[list[dict], dict]:
        """Run one action, returning (notifications, emoji lookup result)."""
        field = form_reader(kwargs)
        if field("action") == "lookup":
            return await self._es_lookup(field)
        return await self._es_run_action(guild, actor, kwargs), {}

    async def _es_lookup(self, field) -> tuple[list[dict], dict]:
        """Image links for a custom emoji or a bare emoji ID, like `[p]getemoji`."""
        raw = (field("emoji_text") or "").strip()
        if not raw:
            return [{"message": "Paste an emoji or an emoji ID.", "category": "warning"}], {}

        if raw.isnumeric():
            # An ID alone does not say whether it is animated, so offer both.
            candidates = [
                discord.PartialEmoji(name="e", animated=animated, id=int(raw))
                for animated in (False, True)
            ]
        else:
            candidates = self.get_emojis(raw)
            if not candidates:
                return [
                    {"message": "That does not contain a custom emoji.",
                     "category": "warning"}
                ], {}

        return [], {
            "query": raw,
            "links": [
                {"name": emoji.name, "animated": bool(emoji.animated), "url": str(emoji.url)}
                for emoji in candidates
            ],
        }

    async def _es_run_action(self, guild, actor, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

        if not (guild.me and guild.me.guild_permissions.manage_emojis):
            return [{"message": "I need Manage Expressions in this server.", "category": "danger"}]

        try:
            if action == "rename":
                emoji = self._es_find(guild, field("emoji"))
                if emoji is None:
                    return [{"message": "That emoji is gone.", "category": "warning"}]
                name = (field("name") or "").strip().replace(" ", "_")
                if not (2 <= len(name) <= 32) or not name.replace("_", "").isalnum():
                    return [
                        {"message": "Names must be 2-32 characters, letters, digits and "
                                    "underscores only.", "category": "warning"}
                    ]
                old = emoji.name
                await emoji.edit(name=name, reason=f"Renamed from the dashboard by {actor}")
                return [{"message": f"Renamed :{old}: to :{name}:.", "category": "success"}]

            if action == "delete":
                emoji = self._es_find(guild, field("emoji"))
                if emoji is None:
                    return [{"message": "That emoji is gone.", "category": "warning"}]
                name = emoji.name
                await emoji.delete(reason=f"Deleted from the dashboard by {actor}")
                return [{"message": f"Deleted :{name}:.", "category": "success"}]

            if action == "add_url":
                return await self._es_add_url(guild, actor, field)

            if action == "add_sticker":
                return await self._es_add_sticker(guild, actor, field)

            if action == "delete_sticker":
                sticker = discord.utils.get(
                    guild.stickers, id=field.integer("sticker_id", 0) or 0
                )
                if sticker is None:
                    return [{"message": "That sticker is gone.", "category": "warning"}]
                name = sticker.name
                await sticker.delete(reason=f"Deleted from the dashboard by {actor}")
                return [{"message": f"Deleted the sticker {name}.", "category": "success"}]
        except discord.Forbidden:
            return [{"message": "Discord refused that.", "category": "danger"}]
        except discord.HTTPException as exc:
            return [{"message": f"Discord rejected it: {exc.text or exc}", "category": "danger"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("EmojiSteal dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    @staticmethod
    def _es_find(guild, raw) -> discord.Emoji | None:
        try:
            emoji_id = int(raw)
        except (TypeError, ValueError):
            return None
        return discord.utils.get(guild.emojis, id=emoji_id)

    async def _es_add_url(self, guild, actor, field) -> list[dict]:
        url = (field("url") or "").strip()
        name = (field("new_name") or "").strip().replace(" ", "_")
        if not url or not name:
            return [{"message": "A name and an image URL are both required.", "category": "warning"}]
        if not url.startswith("https://"):
            return [{"message": "The URL must start with https://.", "category": "warning"}]
        if not (2 <= len(name) <= 32) or not name.replace("_", "").isalnum():
            return [{"message": "Names must be 2-32 characters, alphanumeric.", "category": "warning"}]

        session = getattr(self, "session", None)
        try:
            if session is not None:
                async with session.get(url) as response:
                    if response.status != 200:
                        return [
                            {"message": f"Could not fetch the image (HTTP {response.status}).",
                             "category": "danger"}
                        ]
                    data = await response.read()
            else:
                import aiohttp

                async with aiohttp.ClientSession() as temp:
                    async with temp.get(url) as response:
                        if response.status != 200:
                            return [
                                {"message": f"Could not fetch the image (HTTP {response.status}).",
                                 "category": "danger"}
                            ]
                        data = await response.read()
        except Exception as exc:  # noqa: BLE001
            return [{"message": f"Could not download that URL: {exc}", "category": "danger"}]

        # Discord rejects anything over 256 KB with an unhelpful error.
        if len(data) > MAX_UPLOAD_BYTES:
            return [
                {
                    "message": f"That image is {len(data) // 1024} KB; Discord's limit is "
                    f"{MAX_UPLOAD_BYTES // 1024} KB.",
                    "category": "danger",
                }
            ]

        emoji = await guild.create_custom_emoji(
            name=name, image=data, reason=f"Added from the dashboard by {actor}"
        )
        return [{"message": f"Added :{emoji.name}:.", "category": "success"}]


    async def _es_fetch(self, url: str) -> tuple[bytes | None, list[dict]]:
        """Download an image, returning (data, notifications-on-failure)."""
        if not url.startswith("https://"):
            return None, [
                {"message": "The URL must start with https://.", "category": "warning"}
            ]
        session = getattr(self, "session", None)
        try:
            if session is not None:
                async with session.get(url) as response:
                    if response.status != 200:
                        return None, [
                            {"message": f"Could not fetch the image "
                                        f"(HTTP {response.status}).",
                             "category": "danger"}
                        ]
                    return await response.read(), []
            import aiohttp

            async with aiohttp.ClientSession() as temp:
                async with temp.get(url) as response:
                    if response.status != 200:
                        return None, [
                            {"message": f"Could not fetch the image "
                                        f"(HTTP {response.status}).",
                             "category": "danger"}
                        ]
                    return await response.read(), []
        except Exception as exc:  # noqa: BLE001
            return None, [
                {"message": f"Could not download that URL: {exc}", "category": "danger"}
            ]

    async def _es_add_sticker(self, guild, actor, field) -> list[dict]:
        """Create a sticker from a PNG URL, the web equivalent of `[p]uploadsticker`."""
        url = (field("sticker_url") or "").strip()
        name = (field("sticker_name") or "").strip()
        emoji = (field("sticker_emoji") or "").strip() or "grinning"
        description = (field("sticker_description") or "").strip()
        if not url or not name:
            return [
                {"message": "A name and a PNG URL are both required.",
                 "category": "warning"}
            ]
        if not 2 <= len(name) <= 30:
            return [
                {"message": "Sticker names must be 2-30 characters.",
                 "category": "warning"}
            ]

        data, problems = await self._es_fetch(url)
        if data is None:
            return problems
        # Discord caps sticker files at 512 KB.
        if len(data) > STICKER_KB * 1024:
            return [
                {
                    "message": f"That image is {len(data) // 1024} KB; Discord's sticker "
                    f"limit is {STICKER_KB} KB.",
                    "category": "danger",
                }
            ]

        sticker = await guild.create_sticker(
            name=name,
            description=description or name,
            emoji=emoji,
            file=discord.File(io.BytesIO(data), filename=f"{name}.png"),
            reason=f"Added from the dashboard by {actor}",
        )
        return [{"message": f"Added the sticker {sticker.name}.", "category": "success"}]


EMOJISTEAL_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<style>
  .es-grid { display:grid; gap:9px;
             grid-template-columns:repeat(auto-fill, minmax(132px, 1fr)); }
  .es-card { padding:10px; border-radius:11px; text-align:center;
             background:rgba(0,0,0,.22); border:1px solid rgba(255,255,255,.08); }
  .es-card img { width:44px; height:44px; object-fit:contain; }
  .es-card .n { font-size:.74rem; opacity:.8; margin:5px 0 6px;
                overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .es-card .row { display:flex; gap:4px; justify-content:center; }
  .es-card input { width:100%; padding:5px 7px; font-size:.72rem; border-radius:7px;
                   background:rgba(0,0,0,.35); border:1px solid rgba(255,255,255,.12);
                   color:inherit; margin-bottom:5px; }
  .es-mini { width:30px; height:30px; padding:0; border-radius:8px; font-size:.7rem; }
</style>

<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-smile-o"></i> Emoji in {{ guild_name }}</h4>
    <p>Add from a URL, rename, or remove. Click a token to copy it.</p>
  </div>

  {% if not can_manage %}
    <div class="dz-panel" style="border-color:rgba(255,90,90,.35);">
      <p style="margin:0; color:#ff8b8b;">
        <i class="fa fa-exclamation-circle"></i>
        I do not have <b>Manage Expressions</b>, so nothing here can be changed.
      </p>
    </div>
  {% endif %}

  {{ stats(stat_items) }}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-plus"></i> Add an emoji</h5>
      <p class="dz-hint">
        Paste a direct image link. PNG, JPG or GIF, up to {{ max_kb }} KB.
        {% if static_full %}<span class="dz-tag bad">static slots full</span>{% endif %}
        {% if animated_full %}<span class="dz-tag bad">animated slots full</span>{% endif %}
      </p>
      <div class="dz-row">
        <input class="dz-input" style="flex:1 1 150px;" type="text" name="new_name"
               placeholder="emoji_name" />
        <input class="dz-input" style="flex:2 1 300px;" type="text" name="url"
               placeholder="https://cdn.discordapp.com/emojis/....png" />
        <button class="dz-btn primary" name="action" value="add_url">
          <i class="fa fa-upload"></i> Add
        </button>
      </div>
    </div>
  </form>

  {% for title, items in [("Static", static), ("Animated", animated)] %}
    <div class="dz-panel">
      <h5><i class="fa fa-image"></i> {{ title }} ({{ items|length }})</h5>
      {% if items %}
        <div class="es-grid">
          {% for e in items %}
            <div class="es-card">
              <img src="{{ e.url }}" alt="{{ e.name }}" />
              <div class="n" title="{{ e.token }}"
                   style="cursor:pointer;"
                   onclick="navigator.clipboard && navigator.clipboard.writeText('{{ e.token }}');">
                :{{ e.name }}:
              </div>
              {% if not e.available %}<span class="dz-tag warn">unavailable</span>{% endif %}
              <form method="POST">
                <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                <input type="hidden" name="emoji" value="{{ e.id }}" />
                <input type="text" name="name" value="{{ e.name }}" />
                <div class="row">
                  <button class="dz-btn es-mini" name="action" value="rename" title="Rename">
                    <i class="fa fa-check"></i></button>
                  <button class="dz-btn es-mini danger" name="action" value="delete"
                          title="Delete" onclick="return confirm('Delete :{{ e.name }}:?');">
                    <i class="fa fa-trash-o"></i></button>
                </div>
              </form>
            </div>
          {% endfor %}
        </div>
      {% else %}
        <p class="dz-empty">None yet.</p>
      {% endif %}
    </div>
  {% endfor %}

  <div class="dz-panel">
    <h5><i class="fa fa-sticky-note-o"></i> Stickers ({{ stickers|length }})</h5>
    {% if stickers %}
      <div class="es-grid">
        {% for s in stickers %}
          <div class="es-card">
            <img src="{{ s.url }}" alt="{{ s.name }}" style="width:64px; height:64px;" />
            <div class="n">{{ s.name }}</div>
            {% if s.description %}
              <div style="font-size:.68rem; opacity:.45;">{{ s.description }}</div>
            {% endif %}
            {% if can_manage %}
              <form method="POST">
                <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                <input type="hidden" name="sticker_id" value="{{ s.id }}" />
                {{ confirm('', 'delete_sticker',
                           'Delete the sticker ' ~ s.name ~ '?') }}
              </form>
            {% endif %}
          </div>
        {% endfor %}
      </div>
    {% else %}
      <p class="dz-empty">No stickers in this server.</p>
    {% endif %}

    {% if can_manage and not sticker_full %}
      <form method="POST" style="margin-top:12px;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <label class="dz-label">Add a sticker from a PNG URL</label>
        <p class="dz-hint">Up to 320x320 and {{ sticker_kb }} KB, the same limits
           Discord applies in the app.</p>
        <div class="dz-row">
          <input class="dz-input" type="text" name="sticker_name"
                 placeholder="name" style="max-width:180px;" />
          <input class="dz-input" type="text" name="sticker_url"
                 placeholder="https://.../sticker.png" style="flex:1 1 220px;" />
        </div>
        <div class="dz-row" style="margin-top:8px;">
          <input class="dz-input" type="text" name="sticker_emoji"
                 placeholder="related emoji name, e.g. joy" style="max-width:220px;" />
          <input class="dz-input" type="text" name="sticker_description"
                 placeholder="description (optional)" style="flex:1 1 200px;" />
          <button class="dz-btn primary" name="action" value="add_sticker">
            <i class="fa fa-plus"></i> Add sticker
          </button>
        </div>
      </form>
    {% elif sticker_full %}
      <p class="dz-hint">This server has used every sticker slot.</p>
    {% endif %}
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-link"></i> Get an emoji image link</h5>
      <p class="dz-hint">Paste a custom emoji, or just its ID, to get the image URL.</p>
      <div class="dz-row">
        <input class="dz-input" type="text" name="emoji_text"
               placeholder="&lt;:name:123456789&gt; or 123456789" style="flex:1 1 260px;" />
        <button class="dz-btn primary" name="action" value="lookup">
          <i class="fa fa-search"></i> Get links
        </button>
      </div>
      {% if lookup %}
        <table class="dz-t" style="margin-top:10px;">
          <tr><th>Kind</th><th>Preview</th><th>Link</th></tr>
          {% for l in lookup.links %}
            <tr>
              <td>{{ 'animated' if l.animated else 'static' }}</td>
              <td><img src="{{ l.url }}" alt="" style="width:36px; height:36px;" /></td>
              <td><a href="{{ l.url }}" target="_blank" rel="noopener">{{ l.url }}</a></td>
            </tr>
          {% endfor %}
        </table>
        <p class="dz-hint">
          For a bare ID both variants are shown; only one of them will load.
        </p>
      {% endif %}
    </div>
  </form>
</div>
"""
)
