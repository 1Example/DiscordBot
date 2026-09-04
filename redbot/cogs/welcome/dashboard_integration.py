from __future__ import annotations

import logging
import random
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
    role_options,
)

log = logging.getLogger("red.welcome.dashboard")

TOGGLES = (
    ("ON", "Announce joins", "Post a message when someone joins."),
    ("LEAVE_ON", "Announce leaves", "Post a message when someone leaves."),
    ("WHISPER", "Also DM the new member", "Send the greeting to their DMs too."),
    ("GROUPED", "Group rapid joins", "Combine a burst of joins into one message."),
    ("EMBED", "Send as an embed", "Otherwise the greeting is plain text."),
    ("JOINED_TODAY", "Include today's join count", "Appends how many joined today."),
    ("PENDING", "Wait for membership screening", "Hold the greeting until they pass the gate."),
    ("DELETE_PREVIOUS_GREETING", "Delete the previous greeting", "Keeps the channel tidy."),
    ("DELETE_PREVIOUS_GOODBYE", "Delete the previous goodbye", "Keeps the channel tidy."),
)

MENTION_KEYS = ("users", "roles", "everyone")

PLACEHOLDERS = (
    ("{0}", "the member"),
    ("{0.name}", "their username"),
    ("{0.display_name}", "their nickname"),
    ("{0.mention}", "a ping"),
    ("{1}", "the server"),
    ("{1.name}", "the server name"),
    ("{count}", "member count"),
    ("{plural}", "an 's' unless count is 1"),
)


class DashboardIntegration:
    """Greeting editor with a live preview of the rendered message."""

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Welcome as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Join and leave messages, with a live preview.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_welcome_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        if not await is_staff(self.bot, user, member, guild):
            return {
                "status": 1,
                "error_title": "Forbidden",
                "error_message": "Only server administrators can change greetings.",
            }

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._wc_handle_post(guild, member, kwargs)

        settings = await self.config.guild(guild).all()
        embed_data = settings.get("EMBED_DATA") or {}
        greetings = settings.get("GREETING") or []
        goodbyes = settings.get("GOODBYE") or []

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": WELCOME_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "stat_items": [
                    ("Join messages", len(greetings)),
                    ("Leave messages", len(goodbyes)),
                    ("Members", guild.member_count or 0),
                ],
                "toggles": [
                    {"key": k, "label": lbl, "help": h, "on": bool(settings.get(k))}
                    for k, lbl, h in TOGGLES
                ],
                "greetings": greetings,
                "goodbyes": goodbyes,
                "channels": channel_options(
                    guild, selected=settings.get("CHANNEL"), require_send=True
                ),
                "leave_channels": channel_options(
                    guild, selected=settings.get("LEAVE_CHANNEL"), require_send=True
                ),
                "bot_roles": role_options(guild, selected=settings.get("BOTS_ROLE")),
                "bots_msg": settings.get("BOTS_MSG") or "",
                "bots_goodbye_msg": settings.get("BOTS_GOODBYE_MSG") or "",
                "minimum_days": settings.get("MINIMUM_DAYS") or 0,
                "filter_setting": settings.get("FILTER_SETTING") or "",
                "delete_after_greeting": settings.get("DELETE_AFTER_GREETING") or "",
                "delete_after_goodbye": settings.get("DELETE_AFTER_GOODBYE") or "",
                "mentions": {k: bool((settings.get("MENTIONS") or {}).get(k)) for k in MENTION_KEYS},
                "goodbye_mentions": {
                    k: bool((settings.get("GOODBYE_MENTIONS") or {}).get(k)) for k in MENTION_KEYS
                },
                "embed": {
                    "title": embed_data.get("title") or "",
                    "footer": embed_data.get("footer") or "",
                    "thumbnail": embed_data.get("thumbnail") or "",
                    "image": embed_data.get("image") or "",
                    "image_goodbye": embed_data.get("image_goodbye") or "",
                    "colour": self._wc_hex(embed_data.get("colour")),
                    "colour_goodbye": self._wc_hex(embed_data.get("colour_goodbye")),
                    "author": bool(embed_data.get("author")),
                    "timestamp": bool(embed_data.get("timestamp")),
                    "mention": bool(embed_data.get("mention")),
                },
                "placeholders": PLACEHOLDERS,
                # Rendered exactly as it will appear, using a real member.
                "preview_join": self._wc_preview(guild, member, settings, greetings, join=True),
                "preview_leave": self._wc_preview(guild, member, settings, goodbyes, join=False),
                "preview_member": member.display_name,
            },
        }

    # ---------------------------------------------------------------- preview

    @staticmethod
    def _wc_hex(value) -> str:
        try:
            return f"#{int(value):06x}"
        except (TypeError, ValueError):
            return "#5865f2"

    def _wc_render(self, template: str, guild: discord.Guild, member: discord.Member) -> str:
        """Apply the same substitutions the cog uses when it posts."""
        try:
            text = template.format(member, guild, count=guild.member_count or 0,
                                   plural="" if (guild.member_count or 0) == 1 else "s")
        except (IndexError, KeyError, AttributeError) as exc:
            return f"[placeholder error: {exc}]"
        return text

    def _wc_preview(self, guild, member, settings: dict, messages: list, *, join: bool) -> dict:
        if not messages:
            return {}
        template = random.choice(messages)
        body = self._wc_render(template, guild, member)
        embed_data = settings.get("EMBED_DATA") or {}
        as_embed = bool(settings.get("EMBED"))

        preview = {
            "id": "preview",
            "author": guild.me.display_name if guild.me else "Bot",
            "avatar": str(guild.me.display_avatar) if guild.me else "",
            "bot": True,
            "timestamp": "now",
            "attachments": [],
            "pinned": False,
            "old": False,
            "content": "" if as_embed else body,
            "embeds": [],
        }
        if not as_embed:
            return preview

        colour_key = "colour" if join else "colour_goodbye"
        preview["embeds"] = [
            {
                "title": self._wc_render(embed_data.get("title") or "", guild, member),
                "description": body,
                "colour": self._wc_hex(embed_data.get(colour_key)),
                "footer": self._wc_render(embed_data.get("footer") or "", guild, member),
                "fields": [],
            }
        ]
        if embed_data.get("mention"):
            preview["content"] = member.mention
        return preview

    # ------------------------------------------------------------- post logic

    async def _wc_handle_post(self, guild, actor, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")
        conf = self.config.guild(guild)

        try:
            if action in ("add_greeting", "add_goodbye"):
                key = "GREETING" if action == "add_greeting" else "GOODBYE"
                text = (field("new_message") or "").strip()
                if not text:
                    return [{"message": "Enter a message first.", "category": "warning"}]
                bad = self._wc_validate(text, guild, actor)
                if bad:
                    return [{"message": bad, "category": "danger"}]
                async with conf.get_attr(key)() as messages:
                    messages.append(text)
                return [{"message": "Message added.", "category": "success"}]

            if action in ("delete_greeting", "delete_goodbye"):
                key = "GREETING" if action == "delete_greeting" else "GOODBYE"
                index = field.integer("index")
                if index is None:
                    return [{"message": "Bad index.", "category": "danger"}]
                async with conf.get_attr(key)() as messages:
                    if not 0 <= index < len(messages):
                        return [{"message": "That message is gone.", "category": "warning"}]
                    # The cog picks at random; an empty list raises on the next join.
                    if len(messages) == 1:
                        return [
                            {"message": "Keep at least one message - edit it instead.",
                             "category": "warning"}
                        ]
                    messages.pop(index)
                return [{"message": "Message removed.", "category": "success"}]

            if action == "save_messages":
                errors = []
                for key, form_key in (("GREETING", "greeting"), ("GOODBYE", "goodbye")):
                    values = [m.strip() for m in field.many(form_key) if m.strip()]
                    if key == "GREETING" and not values:
                        errors.append("You need at least one join message.")
                        continue
                    for value in values:
                        bad = self._wc_validate(value, guild, actor)
                        if bad:
                            errors.append(bad)
                            break
                    else:
                        if values:
                            await conf.get_attr(key).set(values)
                if errors:
                    return [{"message": e, "category": "danger"} for e in errors]
                return [{"message": "Messages saved.", "category": "success"}]

            if action == "save_settings":
                return await self._wc_save_settings(conf, guild, field)

            if action == "test":
                return await self._wc_test(guild, actor, field)
        except Exception as exc:  # noqa: BLE001
            log.exception("Welcome dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    def _wc_validate(self, template: str, guild, member) -> str | None:
        """Reject a template that would raise when the cog formats it."""
        try:
            template.format(member, guild, count=0, plural="s")
        except (IndexError, KeyError) as exc:
            return f"Unknown placeholder {exc} - only {{0}}, {{1}}, {{count}} and {{plural}} exist."
        except AttributeError as exc:
            return f"Invalid attribute in the template: {exc}"
        return None

    async def _wc_save_settings(self, conf, guild, field) -> list[dict]:
        errors: list[dict] = []
        for key, _lbl, _h in TOGGLES:
            await conf.get_attr(key).set(field.checked(f"t_{key}"))

        for key, form_key in (("CHANNEL", "channel"), ("LEAVE_CHANNEL", "leave_channel")):
            raw = field(form_key) or ""
            channel_id = int(raw) if raw.isdigit() else None
            if channel_id is not None:
                channel = guild.get_channel(channel_id)
                if channel and not channel.permissions_for(guild.me).send_messages:
                    errors.append(
                        {"message": f"I cannot post in #{channel.name}.", "category": "warning"}
                    )
            await conf.get_attr(key).set(channel_id)

        raw_role = field("bots_role") or ""
        role_id = int(raw_role) if raw_role.isdigit() else None
        if role_id:
            role = guild.get_role(role_id)
            if role and guild.me and role.position >= guild.me.top_role.position:
                errors.append(
                    {"message": f"'{role.name}' is above my top role; I cannot assign it.",
                     "category": "warning"}
                )
        await conf.BOTS_ROLE.set(role_id)

        # What a filtered username is replaced with. Blank falls back to the
        # cog's own "[Redacted]" rather than storing that literal.
        await conf.FILTER_SETTING.set((field("filter_setting") or "").strip() or None)

        await conf.BOTS_MSG.set((field("bots_msg") or "").strip() or None)
        await conf.BOTS_GOODBYE_MSG.set((field("bots_goodbye_msg") or "").strip() or None)

        for key, form_key, floor in (
            ("MINIMUM_DAYS", "minimum_days", 0),
            ("DELETE_AFTER_GREETING", "delete_after_greeting", None),
            ("DELETE_AFTER_GOODBYE", "delete_after_goodbye", None),
        ):
            raw = (field(form_key) or "").strip()
            if raw == "":
                await conf.get_attr(key).set(0 if floor == 0 else None)
                continue
            value = field.integer(form_key)
            if value is None:
                errors.append({"message": f"'{raw}' is not a number.", "category": "danger"})
                continue
            await conf.get_attr(key).set(max(0, value))

        await conf.MENTIONS.set({k: field.checked(f"m_{k}") for k in MENTION_KEYS})
        await conf.GOODBYE_MENTIONS.set({k: field.checked(f"gm_{k}") for k in MENTION_KEYS})

        async with conf.EMBED_DATA() as embed:
            for key in ("title", "footer", "thumbnail", "image", "image_goodbye"):
                embed[key] = (field(f"e_{key}") or "").strip() or None
            for key in ("colour", "colour_goodbye"):
                raw = (field(f"e_{key}") or "").strip()
                if not raw:
                    embed[key] = 0
                    continue
                try:
                    embed[key] = int(raw.lstrip("#"), 16)
                except ValueError:
                    errors.append({"message": f"'{raw}' is not a colour.", "category": "danger"})
            for key in ("author", "timestamp", "mention"):
                embed[key] = field.checked(f"e_{key}")

        return errors + [{"message": "Settings saved.", "category": "success"}]

    async def _wc_test(self, guild, actor, field) -> list[dict]:
        """Send the greeting to the configured channel, using the actor."""
        settings = await self.config.guild(guild).all()
        which = field("which") or "join"
        messages = settings.get("GREETING" if which == "join" else "GOODBYE") or []
        if not messages:
            return [{"message": "No messages to test.", "category": "warning"}]

        channel_id = settings.get("CHANNEL") if which == "join" else (
            settings.get("LEAVE_CHANNEL") or settings.get("CHANNEL")
        )
        channel = guild.get_channel(channel_id or 0)
        if channel is None:
            return [{"message": "No channel configured.", "category": "warning"}]
        if not channel.permissions_for(guild.me).send_messages:
            return [{"message": f"I cannot post in #{channel.name}.", "category": "danger"}]

        body = self._wc_render(random.choice(messages), guild, actor)
        try:
            if settings.get("EMBED"):
                data = settings.get("EMBED_DATA") or {}
                colour = data.get("colour" if which == "join" else "colour_goodbye") or 0
                embed = discord.Embed(
                    title=(data.get("title") or "") or None,
                    description=body,
                    colour=discord.Colour(colour) if colour else discord.Colour.blurple(),
                )
                if data.get("footer"):
                    embed.set_footer(text=data["footer"])
                await channel.send(embed=embed)
            else:
                await channel.send(body)
        except discord.HTTPException as exc:
            return [{"message": f"Discord rejected the test: {exc}", "category": "danger"}]
        return [
            {"message": f"Test {which} message sent to #{channel.name}.", "category": "success"}
        ]


WELCOME_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-handshake-o"></i> Greetings in {{ guild_name }}</h4>
    <p>One message is picked at random each time. Previews use <b>{{ preview_member }}</b>.</p>
  </div>

  {{ stats(stat_items) }}

  <div class="dz-grid two">
    <div class="dz-panel">
      <h5><i class="fa fa-eye"></i> Join preview</h5>
      {% if preview_join %}{{ msg(preview_join) }}
      {% else %}<p class="dz-empty">No join messages yet.</p>{% endif %}
      <form method="POST" style="margin-top:9px;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <input type="hidden" name="which" value="join" />
        <button class="dz-btn" name="action" value="test">
          <i class="fa fa-paper-plane"></i> Send a real test
        </button>
      </form>
    </div>
    <div class="dz-panel">
      <h5><i class="fa fa-eye"></i> Leave preview</h5>
      {% if preview_leave %}{{ msg(preview_leave) }}
      {% else %}<p class="dz-empty">No leave messages yet.</p>{% endif %}
      <form method="POST" style="margin-top:9px;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <input type="hidden" name="which" value="leave" />
        <button class="dz-btn" name="action" value="test">
          <i class="fa fa-paper-plane"></i> Send a real test
        </button>
      </form>
    </div>
  </div>

  <div class="dz-panel">
    <h5><i class="fa fa-code"></i> Placeholders</h5>
    <div class="dz-row">
      {% for token, meaning in placeholders %}
        <span class="dz-tag"><code>{{ token }}</code> {{ meaning }}</span>
      {% endfor %}
    </div>
    <p class="dz-hint" style="margin:9px 0 0;">
      Anything else is rejected when you save, rather than failing on the next join.
    </p>
  </div>

  <div class="dz-grid two">
    <div class="dz-panel">
      <h5><i class="fa fa-sign-in"></i> Join messages</h5>
      <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        {% for text in greetings %}
          <textarea class="dz-area" name="greeting" rows="2"
                    style="margin-bottom:8px;">{{ text }}</textarea>
        {% endfor %}
        <button class="dz-btn primary" name="action" value="save_messages">
          <i class="fa fa-save"></i> Save
        </button>
      </form>
      {% if greetings|length > 1 %}
        <div class="dz-row" style="margin-top:9px;">
          {% for text in greetings %}
            <form method="POST" style="display:inline;">
              <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
              <input type="hidden" name="index" value="{{ loop.index0 }}" />
              <button class="dz-btn danger" name="action" value="delete_greeting">
                <i class="fa fa-trash-o"></i> #{{ loop.index }}
              </button>
            </form>
          {% endfor %}
        </div>
      {% endif %}
      <form method="POST" style="margin-top:10px;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <textarea class="dz-area" name="new_message" rows="2"
                  placeholder="Add another join message..."></textarea>
        <button class="dz-btn" name="action" value="add_greeting" style="margin-top:7px;">
          <i class="fa fa-plus"></i> Add
        </button>
      </form>
    </div>

    <div class="dz-panel">
      <h5><i class="fa fa-sign-out"></i> Leave messages</h5>
      <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        {% for text in goodbyes %}
          <textarea class="dz-area" name="goodbye" rows="2"
                    style="margin-bottom:8px;">{{ text }}</textarea>
        {% endfor %}
        <button class="dz-btn primary" name="action" value="save_messages">
          <i class="fa fa-save"></i> Save
        </button>
      </form>
      <form method="POST" style="margin-top:10px;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <textarea class="dz-area" name="new_message" rows="2"
                  placeholder="Add another leave message..."></textarea>
        <button class="dz-btn" name="action" value="add_goodbye" style="margin-top:7px;">
          <i class="fa fa-plus"></i> Add
        </button>
      </form>
    </div>
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />

    <div class="dz-grid two">
      <div class="dz-panel">
        <h5><i class="fa fa-toggle-on"></i> Behaviour</h5>
        {% for t in toggles %}
          <div style="margin-bottom:8px;">
            <label class="dz-toggle" style="padding:0;">
              <input type="checkbox" name="t_{{ t.key }}" {% if t.on %}checked{% endif %} />
              <span>{{ t.label }}</span>
            </label>
            <div style="font-size:.72rem; opacity:.45; margin-left:26px;">{{ t.help }}</div>
          </div>
        {% endfor %}
      </div>

      <div class="dz-panel">
        <h5><i class="fa fa-hashtag"></i> Channels &amp; limits</h5>
        <div class="dz-label">Join channel</div>
        {{ picker('channel', channels, allow_none=true, placeholder='Search channels...') }}
        <div class="dz-label" style="margin-top:10px;">Leave channel</div>
        {{ picker('leave_channel', leave_channels, allow_none=true,
                  none_label='same as join', placeholder='Search channels...') }}
        <div class="dz-label" style="margin-top:10px;">Minimum account age (days)</div>
        <input class="dz-input" type="number" min="0" name="minimum_days" value="{{ minimum_days }}" />
        <div class="dz-label" style="margin-top:10px;">Replace filtered names with</div>
        <input class="dz-input" name="filter_setting" value="{{ filter_setting }}"
               placeholder="[Redacted]" />
        <div class="dz-hint">Used when a new member's name matches the server filter.</div>
        <div class="dz-row" style="margin-top:10px;">
          <div style="flex:1 1 130px;">
            <div class="dz-label">Delete greeting after (s)</div>
            <input class="dz-input" type="number" min="0" name="delete_after_greeting"
                   value="{{ delete_after_greeting }}" placeholder="keep" />
          </div>
          <div style="flex:1 1 130px;">
            <div class="dz-label">Delete goodbye after (s)</div>
            <input class="dz-input" type="number" min="0" name="delete_after_goodbye"
                   value="{{ delete_after_goodbye }}" placeholder="keep" />
          </div>
        </div>
      </div>
    </div>

    <div class="dz-grid two" style="margin-top:14px;">
      <div class="dz-panel">
        <h5><i class="fa fa-android"></i> Bots</h5>
        <div class="dz-label">Join message</div>
        <textarea class="dz-area" name="bots_msg" rows="2">{{ bots_msg }}</textarea>
        <div class="dz-label" style="margin-top:10px;">Leave message</div>
        <textarea class="dz-area" name="bots_goodbye_msg" rows="2">{{ bots_goodbye_msg }}</textarea>
        <div class="dz-label" style="margin-top:10px;">Auto-role for bots</div>
        {{ picker('bots_role', bot_roles, allow_none=true, placeholder='Search roles...') }}
      </div>

      <div class="dz-panel">
        <h5><i class="fa fa-at"></i> Allowed mentions</h5>
        <div class="dz-grid two">
          <div>
            <div class="dz-label">On join</div>
            {% for key, on in mentions.items() %}
              <label class="dz-toggle">
                <input type="checkbox" name="m_{{ key }}" {% if on %}checked{% endif %} />
                <span>{{ key }}</span></label>
            {% endfor %}
          </div>
          <div>
            <div class="dz-label">On leave</div>
            {% for key, on in goodbye_mentions.items() %}
              <label class="dz-toggle">
                <input type="checkbox" name="gm_{{ key }}" {% if on %}checked{% endif %} />
                <span>{{ key }}</span></label>
            {% endfor %}
          </div>
        </div>
      </div>
    </div>

    <div class="dz-panel" style="margin-top:14px;">
      <h5><i class="fa fa-window-maximize"></i> Embed appearance</h5>
      <p class="dz-hint">Used when "send as an embed" is on. The preview above updates on save.</p>
      <div class="dz-grid two">
        <div>
          <div class="dz-label">Title</div>
          <input class="dz-input" type="text" name="e_title" value="{{ embed.title }}" />
          <div class="dz-label" style="margin-top:9px;">Footer</div>
          <input class="dz-input" type="text" name="e_footer" value="{{ embed.footer }}" />
          <div class="dz-label" style="margin-top:9px;">Thumbnail</div>
          <input class="dz-input" type="text" name="e_thumbnail" value="{{ embed.thumbnail }}"
                 placeholder="avatar, splash, icon, or a URL" />
        </div>
        <div>
          <div class="dz-label">Join image URL</div>
          <input class="dz-input" type="text" name="e_image" value="{{ embed.image }}" />
          <div class="dz-label" style="margin-top:9px;">Leave image URL</div>
          <input class="dz-input" type="text" name="e_image_goodbye" value="{{ embed.image_goodbye }}" />
          <div class="dz-row" style="margin-top:9px;">
            <div style="flex:1 1 120px;">
              <div class="dz-label">Join colour</div>
              <input class="dz-input" type="color" name="e_colour" value="{{ embed.colour }}" />
            </div>
            <div style="flex:1 1 120px;">
              <div class="dz-label">Leave colour</div>
              <input class="dz-input" type="color" name="e_colour_goodbye"
                     value="{{ embed.colour_goodbye }}" />
            </div>
          </div>
        </div>
      </div>
      <div class="dz-row" style="margin-top:10px;">
        <label class="dz-toggle"><input type="checkbox" name="e_author"
          {% if embed.author %}checked{% endif %} /><span>Show author</span></label>
        <label class="dz-toggle"><input type="checkbox" name="e_timestamp"
          {% if embed.timestamp %}checked{% endif %} /><span>Show timestamp</span></label>
        <label class="dz-toggle"><input type="checkbox" name="e_mention"
          {% if embed.mention %}checked{% endif %} /><span>Ping above the embed</span></label>
      </div>
    </div>

    <div class="dz-save">
      <button class="dz-btn primary" name="action" value="save_settings">
        <i class="fa fa-save"></i> Save settings
      </button>
    </div>
  </form>
</div>
"""
)
