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
    emoji_options,
    form_reader,
    guild_member,
    is_staff,
    member_options,
)

log = logging.getLogger("red.privaterooms.dashboard")


class DashboardIntegration:
    """Set up the voice hubs, brand the panel, and manage live rooms."""

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering PrivateRooms as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    # ------------------------------------------------------------------ page

    @dashboard_page(
        name=None,
        description="Voice hubs, panel branding and live room control.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_privaterooms_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        if not await is_staff(self.bot, user, member, guild):
            return {
                "status": 1,
                "error_title": "Forbidden",
                "error_message": "Only server administrators can manage private rooms.",
            }

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._pr_handle_post(guild, member, kwargs)

        settings = await self.config.guild(guild).all()
        rooms = await self._pr_rooms(guild, settings)
        emojis = self._pr_emoji_rows(guild, settings)

        hub_public = guild.get_channel(settings.get("hub_public") or 0)
        hub_private = guild.get_channel(settings.get("hub_private") or 0)
        category = guild.get_channel(settings.get("category") or 0)
        panel_channel = guild.get_channel(settings.get("panel_channel") or 0)

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": PRIVATEROOMS_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "stat_items": [
                    ("Live rooms", len(rooms)),
                    ("Occupants", sum(r["members"] for r in rooms)),
                    ("Orphaned", sum(1 for r in rooms if r["orphaned"])),
                ],
                "rooms": rooms,
                "room_options": [
                    {
                        "id": r["id"],
                        "name": f"{r['name']} ({r['owner']})",
                        "group": "Orphaned" if r["orphaned"] else "Owned",
                        "selected": False,
                        "warn": r["orphaned"],
                    }
                    for r in rooms
                ],
                "voice_channels": channel_options(
                    guild, kinds=("voice",), selected=settings.get("hub_public")
                ),
                "voice_channels_private": channel_options(
                    guild, kinds=("voice",), selected=settings.get("hub_private")
                ),
                "categories": channel_options(
                    guild, kinds=("category",), selected=settings.get("category")
                ),
                "text_channels": channel_options(
                    guild, selected=settings.get("panel_channel"), require_send=True
                ),
                "members": member_options(guild, humans_only=True),
                "emoji_rows": emojis,
                "guild_emojis": emoji_options(guild),
                "setup_ok": bool(hub_public or hub_private),
                "hub_public": hub_public.name if hub_public else None,
                "hub_private": hub_private.name if hub_private else None,
                "category_name": category.name if category else None,
                "panel_channel": f"#{panel_channel.name}" if panel_channel else None,
                "panel_posted": bool(settings.get("panel_message")),
                "panel_title": settings.get("panel_title") or "",
                "panel_footer": settings.get("panel_footer") or "",
                "panel_colour": self._pr_colour_hex(settings.get("panel_colour")),
                "hub_public_name": settings.get("hub_public_name") or "CREATE PUBLIC",
                "hub_private_name": settings.get("hub_private_name") or "CREATE PRIVATE",
                "name_public": settings.get("name_format_public") or "{user}'s Room",
                "name_private": settings.get("name_format_private") or "{user}'s Room",
                "default_limit": settings.get("default_limit", 0),
                "preview": self._pr_preview(guild, settings, member),
                "can_manage": bool(guild.me and guild.me.guild_permissions.manage_channels),
            },
        }

    # ------------------------------------------------------------------ data

    @staticmethod
    def _pr_colour_hex(value) -> str:
        try:
            return f"#{int(value):06x}"
        except (TypeError, ValueError):
            return "#5865f2"

    def _pr_emoji_rows(self, guild: discord.Guild, settings: dict) -> list[dict]:
        from .privaterooms import ACTIONS, DEFAULT_EMOJIS, parse_emoji

        overrides = settings.get("emojis") or {}
        rows = []
        extra = (("hub", "panel title"), ("public", "public hub"), ("private", "private hub"))
        for key, default, label, _blurb in ACTIONS:
            rows.append(self._pr_emoji_row(key, label, default, overrides, parse_emoji))
        for key, label in extra:
            rows.append(
                self._pr_emoji_row(key, label, DEFAULT_EMOJIS[key], overrides, parse_emoji)
            )
        return rows

    @staticmethod
    def _pr_emoji_row(key, label, default, overrides, parse_emoji) -> dict:
        current = overrides.get(key) or ""
        return {
            "key": key,
            "label": label.capitalize(),
            "default": default,
            "current": current,
            "effective": current or default,
            # A stored value that no longer parses means the emoji was deleted
            # or the token was mistyped; the panel silently drops it.
            "broken": bool(current) and parse_emoji(current) is None,
        }

    async def _pr_rooms(self, guild: discord.Guild, settings: dict) -> list[dict]:
        rooms = []
        tracked = settings.get("rooms") or {}
        for channel_id, data in tracked.items():
            channel = guild.get_channel(int(channel_id))
            if channel is None:
                continue
            owner = guild.get_member((data or {}).get("owner") or 0)
            overwrite = channel.overwrites_for(guild.default_role)
            rooms.append(
                {
                    "id": str(channel.id),
                    "name": channel.name,
                    "owner": owner.display_name if owner else "nobody",
                    "owner_id": str(getattr(owner, "id", "")),
                    "public": bool((data or {}).get("public")),
                    "members": len(channel.members),
                    "limit": channel.user_limit,
                    "locked": overwrite.connect is False,
                    "hidden": overwrite.view_channel is False,
                    # The owner left but the room still has people in it.
                    "orphaned": owner is None or owner not in channel.members,
                    "occupants": [m.display_name for m in channel.members[:12]],
                }
            )
        return sorted(rooms, key=lambda r: (-r["members"], r["name"].lower()))

    def _pr_preview(self, guild: discord.Guild, settings: dict, member: discord.Member) -> dict:
        """Render the panel embed exactly as the hub message will look."""
        from .privaterooms import room_embed

        try:
            embed = room_embed(guild, settings)
        except Exception:  # noqa: BLE001 - a preview must never break the page
            log.exception("Could not build the panel preview")
            return {}
        return {
            "author": guild.me.display_name if guild.me else "Bot",
            "avatar": str(guild.me.display_avatar) if guild.me else "",
            "bot": True,
            "content": "",
            "timestamp": "now",
            "attachments": [],
            "pinned": False,
            "old": False,
            "embeds": [
                {
                    "title": embed.title or "",
                    "description": embed.description or "",
                    "colour": f"#{embed.colour.value:06x}" if embed.colour else "#5865f2",
                    "footer": embed.footer.text if embed.footer else "",
                    "fields": [],
                }
            ],
        }

    # ------------------------------------------------------------- post logic

    async def _pr_handle_post(self, guild, actor: discord.Member, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")
        conf = self.config.guild(guild)

        try:
            if action == "save_hubs":
                return await self._pr_save_hubs(guild, conf, field)
            if action == "save_branding":
                return await self._pr_save_branding(conf, field)
            if action == "save_emojis":
                return await self._pr_save_emojis(conf, field)
            if action == "create_hubs":
                return await self._pr_create_hubs(guild, conf, field)
            if action == "post_panel":
                return await self._pr_post_panel(guild, conf, field)
            if action in ("room_lock", "room_unlock", "room_hide", "room_unhide",
                          "room_delete", "room_transfer"):
                return await self._pr_room_action(guild, conf, field, action, actor)
            if action == "prune":
                return await self._pr_prune(guild, conf)
        except discord.Forbidden:
            return [
                {"message": "Discord refused that - check my channel permissions.",
                 "category": "danger"}
            ]
        except Exception as exc:  # noqa: BLE001
            log.exception("PrivateRooms dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    async def _pr_save_hubs(self, guild, conf, field) -> list[dict]:
        for key, form_key in (
            ("hub_public", "hub_public"),
            ("hub_private", "hub_private"),
            ("category", "category"),
        ):
            raw = field(form_key) or ""
            await conf.get_attr(key).set(int(raw) if raw.isdigit() else None)

        limit = field.integer("default_limit", 0) or 0
        await conf.default_limit.set(max(0, min(99, limit)))

        warnings = []
        for form_key, label in (("name_format_public", "Public"), ("name_format_private", "Private")):
            value = (field(form_key) or "").strip()
            if not value:
                continue
            if "{user}" not in value:
                warnings.append(
                    {
                        "message": f"{label} name format has no {{user}} placeholder, "
                        f"so every room would share one name.",
                        "category": "warning",
                    }
                )
                continue
            # Discord caps channel names at 100 characters.
            await conf.get_attr(form_key).set(value[:100])

        return warnings + [{"message": "Hub settings saved.", "category": "success"}]

    async def _pr_save_branding(self, conf, field) -> list[dict]:
        await conf.panel_title.set((field("panel_title") or "").strip() or None)
        await conf.panel_footer.set((field("panel_footer") or "").strip() or None)
        for key in ("hub_public_name", "hub_private_name"):
            value = (field(key) or "").strip()
            if value:
                await conf.get_attr(key).set(value[:100])

        raw = (field("panel_colour") or "").strip()
        if raw:
            try:
                await conf.panel_colour.set(int(raw.lstrip("#"), 16))
            except ValueError:
                return [{"message": f"'{raw}' is not a colour.", "category": "danger"}]
        return [
            {
                "message": "Branding saved. Re-post the panel to apply it to the live message.",
                "category": "success",
            }
        ]

    async def _pr_save_emojis(self, conf, field) -> list[dict]:
        from .privaterooms import DEFAULT_EMOJIS, parse_emoji

        bad = []
        async with conf.emojis() as emojis:
            for key in DEFAULT_EMOJIS:
                value = (field(f"e_{key}") or "").strip()
                if not value:
                    emojis.pop(key, None)
                    continue
                if parse_emoji(value) is None:
                    bad.append(key)
                    continue
                emojis[key] = value

        notes = [
            {"message": f"'{k}' was not a usable emoji and was skipped.", "category": "warning"}
            for k in bad
        ]
        return notes + [
            {
                "message": "Emoji saved. Re-post the panel to update the live buttons.",
                "category": "success",
            }
        ]

    async def _pr_create_hubs(self, guild, conf, field) -> list[dict]:
        if not (guild.me and guild.me.guild_permissions.manage_channels):
            return [{"message": "I need Manage Channels to create the hubs.", "category": "danger"}]

        raw = field("category") or ""
        category = guild.get_channel(int(raw)) if raw.isdigit() else None
        if raw and not isinstance(category, discord.CategoryChannel):
            category = None

        settings = await conf.all()
        public = await guild.create_voice_channel(
            name=settings.get("hub_public_name") or "CREATE PUBLIC",
            category=category,
            reason="Private rooms hub setup from the dashboard",
        )
        private = await guild.create_voice_channel(
            name=settings.get("hub_private_name") or "CREATE PRIVATE",
            category=category,
            reason="Private rooms hub setup from the dashboard",
        )
        await conf.hub_public.set(public.id)
        await conf.hub_private.set(private.id)
        if category is not None:
            await conf.category.set(category.id)
        return [
            {"message": f"Created {public.name} and {private.name}.", "category": "success"}
        ]

    async def _pr_post_panel(self, guild, conf, field) -> list[dict]:
        from .privaterooms import ControlPanelView, resolve_emojis, room_embed

        raw = field("panel_channel") or ""
        channel = guild.get_channel(int(raw)) if raw.isdigit() else None
        if channel is None:
            return [{"message": "Pick a text channel for the panel.", "category": "warning"}]
        if not channel.permissions_for(guild.me).send_messages:
            return [
                {"message": f"I cannot send messages in #{channel.name}.", "category": "danger"}
            ]

        settings = await conf.all()
        view = ControlPanelView(self, emojis=resolve_emojis(settings.get("emojis")))
        embed = room_embed(guild, settings)

        # Edit the existing panel where possible so the channel does not fill
        # up with stale copies.
        old_channel = guild.get_channel(settings.get("panel_channel") or 0)
        old_id = settings.get("panel_message")
        if old_channel is not None and old_id and old_channel.id == channel.id:
            try:
                message = await old_channel.fetch_message(old_id)
                await message.edit(embed=embed, view=view)
                return [{"message": "Existing panel updated in place.", "category": "success"}]
            except discord.NotFound:
                pass

        message = await channel.send(embed=embed, view=view)
        await conf.panel_channel.set(channel.id)
        await conf.panel_message.set(message.id)
        return [{"message": f"Panel posted in #{channel.name}.", "category": "success"}]

    async def _pr_room_action(self, guild, conf, field, action: str, actor) -> list[dict]:
        raw = field("room") or ""
        channel = guild.get_channel(int(raw)) if raw.isdigit() else None
        if channel is None or not isinstance(channel, discord.VoiceChannel):
            return [{"message": "That room no longer exists.", "category": "warning"}]

        if action == "room_delete":
            await channel.delete(reason=f"Deleted from the dashboard by {actor}")
            async with conf.rooms() as rooms:
                rooms.pop(str(channel.id), None)
            return [{"message": f"Deleted '{channel.name}'.", "category": "success"}]

        if action == "room_transfer":
            raw_member = field("new_owner") or ""
            new_owner = guild.get_member(int(raw_member)) if raw_member.isdigit() else None
            if new_owner is None:
                return [{"message": "Pick a member to hand the room to.", "category": "warning"}]
            async with conf.rooms() as rooms:
                entry = rooms.get(str(channel.id))
                if entry is None:
                    return [{"message": "That room is not tracked.", "category": "warning"}]
                entry["owner"] = new_owner.id
            return [
                {"message": f"'{channel.name}' now belongs to {new_owner.display_name}.",
                 "category": "success"}
            ]

        overwrite = channel.overwrites_for(guild.default_role)
        if action == "room_lock":
            overwrite.connect = False
        elif action == "room_unlock":
            overwrite.connect = None
        elif action == "room_hide":
            overwrite.view_channel = False
        elif action == "room_unhide":
            overwrite.view_channel = None
        await channel.set_permissions(
            guild.default_role, overwrite=overwrite, reason=f"Dashboard action by {actor}"
        )
        verb = action.split("_", 1)[1]
        return [{"message": f"'{channel.name}' {verb}ed.", "category": "success"}]

    async def _pr_prune(self, guild, conf) -> list[dict]:
        """Drop tracking entries whose channel is gone."""
        removed = 0
        async with conf.rooms() as rooms:
            for channel_id in list(rooms):
                if guild.get_channel(int(channel_id)) is None:
                    del rooms[channel_id]
                    removed += 1
        return [
            {
                "message": f"Cleared {removed} stale entr{'y' if removed == 1 else 'ies'}.",
                "category": "success" if removed else "info",
            }
        ]


PRIVATEROOMS_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-microphone"></i> Private rooms in {{ guild_name }}</h4>
    <p>
      {% if setup_ok %}
        Hubs: {{ hub_public or "not set" }} / {{ hub_private or "not set" }}
        {% if category_name %}&middot; created under <b>{{ category_name }}</b>{% endif %}
        {% if panel_channel %}&middot; panel in {{ panel_channel }}{% endif %}
      {% else %}
        No hubs configured yet &mdash; create them below to get started.
      {% endif %}
    </p>
  </div>

  {% if not can_manage %}
    <div class="dz-panel" style="border-color:rgba(255,90,90,.35);">
      <p style="margin:0; color:#ff8b8b;">
        <i class="fa fa-exclamation-circle"></i>
        I do not have <b>Manage Channels</b>, so rooms cannot be created or edited.
      </p>
    </div>
  {% endif %}

  {{ stats(stat_items) }}

  <div class="dz-panel">
    <h5><i class="fa fa-eye"></i> Panel preview</h5>
    <p class="dz-hint">Exactly what the hub message looks like with your current settings.</p>
    {% if preview %}{{ msg(preview) }}{% else %}<p class="dz-empty">Preview unavailable.</p>{% endif %}
    <form method="POST" class="dz-row" style="margin-top:11px;">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div style="flex:1 1 260px;">{{ picker('panel_channel', text_channels, allow_none=true, none_label='pick a channel') }}</div>
      <button class="dz-btn primary" name="action" value="post_panel">
        <i class="fa fa-paper-plane"></i>
        {% if panel_posted %}Update panel{% else %}Post panel{% endif %}
      </button>
    </form>
  </div>

  <div class="dz-grid two">
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-plug"></i> Hubs</h5>
        <p class="dz-hint">Joining one of these voice channels creates a room.</p>

        <div class="dz-label">Public hub</div>
        {{ picker('hub_public', voice_channels, allow_none=true) }}

        <div class="dz-label" style="margin-top:10px;">Private hub</div>
        {{ picker('hub_private', voice_channels_private, allow_none=true) }}

        <div class="dz-label" style="margin-top:10px;">Create rooms under</div>
        {{ picker('category', categories, allow_none=true, none_label="the hub's own category") }}

        <div class="dz-label" style="margin-top:10px;">Default user limit</div>
        <input class="dz-input" type="number" min="0" max="99" name="default_limit"
               value="{{ default_limit }}" />

        <div class="dz-label" style="margin-top:10px;">Public room name</div>
        <input class="dz-input" type="text" name="name_format_public" value="{{ name_public }}" />
        <div class="dz-label" style="margin-top:8px;">Private room name</div>
        <input class="dz-input" type="text" name="name_format_private" value="{{ name_private }}" />
        <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
          <code>{user}</code> is replaced with the owner's name and is required.
        </div>

        <div class="dz-row" style="margin-top:13px;">
          <button class="dz-btn primary" name="action" value="save_hubs">
            <i class="fa fa-save"></i> Save hubs
          </button>
          <button class="dz-btn" name="action" value="create_hubs"
                  onclick="return confirm('Create two new voice channels for the hubs?');">
            <i class="fa fa-magic"></i> Create hubs for me
          </button>
        </div>
      </div>
    </form>

    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-paint-brush"></i> Branding</h5>
        <p class="dz-hint">Defaults to your server name; nothing is hardcoded.</p>

        <div class="dz-label">Panel title</div>
        <input class="dz-input" type="text" name="panel_title" value="{{ panel_title }}"
               placeholder="{{ guild_name }} | Voice Hub" />

        <div class="dz-label" style="margin-top:10px;">Footer</div>
        <textarea class="dz-area" name="panel_footer" style="min-height:60px;">{{ panel_footer }}</textarea>

        <div class="dz-label" style="margin-top:10px;">Accent colour</div>
        <input class="dz-input" type="text" name="panel_colour" value="{{ panel_colour }}"
               placeholder="#5865f2" />

        <div class="dz-row" style="margin-top:10px;">
          <div style="flex:1 1 140px;">
            <div class="dz-label">Public hub label</div>
            <input class="dz-input" type="text" name="hub_public_name" value="{{ hub_public_name }}" />
          </div>
          <div style="flex:1 1 140px;">
            <div class="dz-label">Private hub label</div>
            <input class="dz-input" type="text" name="hub_private_name" value="{{ hub_private_name }}" />
          </div>
        </div>

        <div style="margin-top:13px;">
          <button class="dz-btn primary" name="action" value="save_branding">
            <i class="fa fa-save"></i> Save branding
          </button>
        </div>
      </div>
    </form>
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-smile-o"></i> Button emoji</h5>
      <p class="dz-hint">
        Leave blank for the unicode default. Paste a custom emoji as
        <code>&lt;:name:id&gt;</code>, or copy one from the list below.
        {% if guild_emojis %}This server has {{ guild_emojis|length }} custom emoji.{% endif %}
      </p>

      <div class="dz-grid three">
        {% for e in emoji_rows %}
          <div>
            <div class="dz-label">
              {{ e.label }}
              <span class="dz-tag">{{ e.effective }}</span>
              {% if e.broken %}<span class="dz-tag bad">unusable</span>{% endif %}
            </div>
            <input class="dz-input" type="text" name="e_{{ e.key }}" value="{{ e.current }}"
                   placeholder="default {{ e.default }}" />
          </div>
        {% endfor %}
      </div>

      {% if guild_emojis %}
        <div class="dz-label" style="margin-top:12px;">Available custom emoji</div>
        <div class="dz-row" style="max-height:120px; overflow-y:auto;">
          {% for g in guild_emojis %}
            <span class="dz-tag" title="{{ g.id }}"
                  style="cursor:pointer; display:inline-flex; align-items:center; gap:5px;"
                  onclick="navigator.clipboard && navigator.clipboard.writeText('{{ g.id }}');">
              <img class="dz-emoji" src="{{ g.url }}" alt="" /> {{ g.name }}
            </span>
          {% endfor %}
        </div>
        <div style="font-size:.72rem; opacity:.45; margin-top:5px;">
          Click one to copy its token, then paste it into a field above.
        </div>
      {% endif %}

      <div style="margin-top:13px;">
        <button class="dz-btn primary" name="action" value="save_emojis">
          <i class="fa fa-save"></i> Save emoji
        </button>
      </div>
    </div>
  </form>

  <div class="dz-panel">
    <h5><i class="fa fa-list"></i> Live rooms</h5>
    <p class="dz-hint">
      Every tracked room right now. Actions here apply immediately.
    </p>

    {% if rooms %}
      <table class="dz-t">
        <thead>
          <tr><th>Room</th><th>Owner</th><th>In room</th><th>State</th><th>Actions</th></tr>
        </thead>
        <tbody>
          {% for r in rooms %}
            <tr>
              <td>
                <b>{{ r.name }}</b>
                <div style="font-size:.72rem; opacity:.45;">
                  {% if r.occupants %}{{ r.occupants|join(', ') }}{% else %}empty{% endif %}
                </div>
              </td>
              <td>
                {{ r.owner }}
                {% if r.orphaned %}<span class="dz-tag warn">absent</span>{% endif %}
              </td>
              <td style="opacity:.7;">
                {{ r.members }}{% if r.limit %} / {{ r.limit }}{% endif %}
              </td>
              <td>
                {% if r.public %}<span class="dz-tag good">public</span>
                {% else %}<span class="dz-tag">private</span>{% endif %}
                {% if r.locked %}<span class="dz-tag warn">locked</span>{% endif %}
                {% if r.hidden %}<span class="dz-tag warn">hidden</span>{% endif %}
              </td>
              <td style="white-space:nowrap; width:1%;">
                <form method="POST" style="display:inline;">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="room" value="{{ r.id }}" />
                  {% if r.locked %}
                    <button class="dz-btn round" name="action" value="room_unlock" title="Unlock">
                      <i class="fa fa-unlock"></i></button>
                  {% else %}
                    <button class="dz-btn round" name="action" value="room_lock" title="Lock">
                      <i class="fa fa-lock"></i></button>
                  {% endif %}
                </form>
                <form method="POST" style="display:inline;">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="room" value="{{ r.id }}" />
                  {% if r.hidden %}
                    <button class="dz-btn round" name="action" value="room_unhide" title="Unhide">
                      <i class="fa fa-eye"></i></button>
                  {% else %}
                    <button class="dz-btn round" name="action" value="room_hide" title="Hide">
                      <i class="fa fa-eye-slash"></i></button>
                  {% endif %}
                </form>
                <form method="POST" style="display:inline;"
                      onsubmit="return confirm('Delete {{ r.name }}?');">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="room" value="{{ r.id }}" />
                  <button class="dz-btn round danger" name="action" value="room_delete"
                          title="Delete room"><i class="fa fa-trash-o"></i></button>
                </form>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>

      <form method="POST" style="margin-top:14px; padding-top:12px;
            border-top:1px solid rgba(255,255,255,.07);">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <div class="dz-label"><i class="fa fa-exchange"></i> Transfer ownership</div>
        <p class="dz-hint">Useful when an owner leaves but people are still in the room.</p>
        <div class="dz-row">
          <div style="flex:1 1 220px;">
            {{ picker('room', room_options, allow_none=true, none_label='pick a room') }}
          </div>
          <div style="flex:1 1 220px;">
            {{ picker('new_owner', members, allow_none=true, none_label='pick a member',
                      placeholder='Search members...') }}
          </div>
          <button class="dz-btn primary" name="action" value="room_transfer">
            <i class="fa fa-exchange"></i> Transfer
          </button>
        </div>
      </form>
    {% else %}
      <p class="dz-empty">No rooms are open right now.</p>
    {% endif %}

    <form method="POST" style="margin-top:11px;">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      {{ confirm('Clear stale entries', 'prune',
                 'Remove tracking for rooms whose channel no longer exists?',
                 'danger', 'fa-eraser') }}
    </form>
  </div>
</div>
"""
)
