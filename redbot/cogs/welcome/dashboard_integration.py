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
    role_options,
)

log = logging.getLogger("red.welcome.dashboard")

# Config keys that are plain on/off switches, with the label shown on the page.
TOGGLES = (
    ("ON", "Announce joins"),
    ("LEAVE_ON", "Announce leaves"),
    ("WHISPER", "Also DM the new member"),
    ("GROUPED", "Group rapid joins into one message"),
    ("EMBED", "Send as an embed"),
    ("JOINED_TODAY", "Include today's join count"),
    ("PENDING", "Wait until membership screening completes"),
    ("DELETE_PREVIOUS_GREETING", "Delete the previous greeting"),
    ("DELETE_PREVIOUS_GOODBYE", "Delete the previous goodbye"),
)

MENTION_KEYS = ("users", "roles", "everyone")


class DashboardIntegration:
    """Greeting and farewell configuration, with message editing."""

    bot: t.Any
    config: t.Any

    @discord.utils.cached_property
    def _welcome_placeholders(self) -> list[tuple[str, str]]:
        return [
            ("{0}", "the member who joined or left"),
            ("{0.name}", "their username"),
            ("{0.mention}", "a mention"),
            ("{1}", "the server"),
            ("{1.name}", "the server name"),
            ("{count}", "member count"),
            ("{plural}", "an 's' when count is not 1"),
        ]

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Welcome as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    # ------------------------------------------------------------------ page

    @dashboard_page(
        name=None,
        description="Configure join and leave announcements.",
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
            notifications = await self._welcome_handle_post(guild, kwargs)

        settings = await self.config.guild(guild).all()
        embed_data = settings.get("EMBED_DATA") or {}

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": WELCOME_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "toggles": [
                    {"key": key, "label": label, "on": bool(settings.get(key))}
                    for key, label in TOGGLES
                ],
                "greetings": settings.get("GREETING") or [],
                "goodbyes": settings.get("GOODBYE") or [],
                "channels": channel_options(guild, selected=settings.get("CHANNEL")),
                "leave_channels": channel_options(guild, selected=settings.get("LEAVE_CHANNEL")),
                "bot_roles": role_options(guild, selected=settings.get("BOTS_ROLE")),
                "bots_msg": settings.get("BOTS_MSG") or "",
                "bots_goodbye_msg": settings.get("BOTS_GOODBYE_MSG") or "",
                "minimum_days": settings.get("MINIMUM_DAYS") or 0,
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
                    "colour": embed_data.get("colour") or 0,
                    "colour_goodbye": embed_data.get("colour_goodbye") or 0,
                    "author": bool(embed_data.get("author")),
                    "timestamp": bool(embed_data.get("timestamp")),
                    "mention": bool(embed_data.get("mention")),
                },
                "placeholders": self._welcome_placeholders,
            },
        }

    # ------------------------------------------------------------- post logic

    async def _welcome_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")
        conf = self.config.guild(guild)

        try:
            if action == "add_greeting":
                return await self._welcome_add_message(conf, "GREETING", field("new_message"))
            if action == "add_goodbye":
                return await self._welcome_add_message(conf, "GOODBYE", field("new_message"))
            if action == "delete_greeting":
                return await self._welcome_delete_message(conf, "GREETING", field("index"))
            if action == "delete_goodbye":
                return await self._welcome_delete_message(conf, "GOODBYE", field("index"))
            if action == "save_messages":
                return await self._welcome_save_messages(conf, field)
            if action == "save_settings":
                return await self._welcome_save_settings(conf, field)
        except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
            log.exception("Welcome dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    @staticmethod
    async def _welcome_add_message(conf, key: str, text: str | None) -> list[dict]:
        text = (text or "").strip()
        if not text:
            return [{"message": "Enter a message first.", "category": "warning"}]
        async with getattr(conf, key)() as messages:
            messages.append(text)
        return [{"message": "Message added.", "category": "success"}]

    @staticmethod
    async def _welcome_delete_message(conf, key: str, index: str | None) -> list[dict]:
        try:
            position = int(index)
        except (TypeError, ValueError):
            return [{"message": "Bad message index.", "category": "danger"}]
        async with getattr(conf, key)() as messages:
            if not 0 <= position < len(messages):
                return [{"message": "That message no longer exists.", "category": "warning"}]
            # Keep at least one message: the cog picks randomly from this list
            # and an empty list would raise on the next join.
            if len(messages) == 1:
                return [
                    {
                        "message": "You need at least one message. Edit this one instead.",
                        "category": "warning",
                    }
                ]
            messages.pop(position)
        return [{"message": "Message removed.", "category": "success"}]

    @staticmethod
    async def _welcome_save_messages(conf, field) -> list[dict]:
        greetings = [m.strip() for m in field.many("greeting") if m.strip()]
        goodbyes = [m.strip() for m in field.many("goodbye") if m.strip()]
        if not greetings:
            return [{"message": "You need at least one greeting.", "category": "warning"}]
        await conf.GREETING.set(greetings)
        if goodbyes:
            await conf.GOODBYE.set(goodbyes)
        return [{"message": "Messages saved.", "category": "success"}]

    async def _welcome_save_settings(self, conf, field) -> list[dict]:
        errors: list[str] = []

        for key, _label in TOGGLES:
            await getattr(conf, key).set(field.checked(f"t_{key}"))

        for key, form_key in (("CHANNEL", "channel"), ("LEAVE_CHANNEL", "leave_channel")):
            raw = field(form_key) or ""
            await getattr(conf, key).set(int(raw) if raw.isdigit() else None)

        raw_role = field("bots_role") or ""
        await conf.BOTS_ROLE.set(int(raw_role) if raw_role.isdigit() else None)

        await conf.BOTS_MSG.set((field("bots_msg") or "").strip() or None)
        await conf.BOTS_GOODBYE_MSG.set((field("bots_goodbye_msg") or "").strip() or None)

        for key, form_key in (
            ("MINIMUM_DAYS", "minimum_days"),
            ("DELETE_AFTER_GREETING", "delete_after_greeting"),
            ("DELETE_AFTER_GOODBYE", "delete_after_goodbye"),
        ):
            raw = (field(form_key) or "").strip()
            if raw == "":
                # MINIMUM_DAYS is a count and must stay numeric; the delete
                # timers are "off" when unset.
                await getattr(conf, key).set(0 if key == "MINIMUM_DAYS" else None)
                continue
            try:
                await getattr(conf, key).set(int(raw))
            except ValueError:
                errors.append(f"{form_key.replace('_', ' ')}: '{raw}' is not a number")

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
                    # Accept both #rrggbb and a plain integer.
                    embed[key] = int(raw.lstrip("#"), 16) if raw.startswith("#") else int(raw)
                except ValueError:
                    errors.append(f"{key}: '{raw}' is not a colour")
            for key in ("author", "timestamp", "mention"):
                embed[key] = field.checked(f"e_{key}")

        notifications = [{"message": e, "category": "danger"} for e in errors]
        notifications.append(
            {
                "message": "Settings saved." if not errors else "Saved, with some fields skipped.",
                "category": "success" if not errors else "warning",
            }
        )
        return notifications


WELCOME_TEMPLATE = (
    BASE_CSS
    + """
<div class="dz">

  <div class="dz-head">
    <h4><i class="fa fa-handshake-o"></i> Greetings for {{ guild_name }}</h4>
    <p>Messages are picked at random each time someone joins or leaves.</p>
  </div>

  <div class="dz-panel">
    <h5><i class="fa fa-code"></i> Placeholders</h5>
    <p class="dz-hint">Use these inside any message below.</p>
    <div class="dz-row">
      {% for token, meaning in placeholders %}
        <span class="dz-tag" title="{{ meaning }}"><code>{{ token }}</code> &mdash; {{ meaning }}</span>
      {% endfor %}
    </div>
  </div>

  <div class="dz-grid two">

    <div class="dz-panel">
      <h5><i class="fa fa-sign-in"></i> Join messages</h5>
      <p class="dz-hint">{{ greetings|length }} in rotation.</p>
      <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        {% for text in greetings %}
          <div style="margin-bottom:9px;">
            <textarea class="dz-area" name="greeting" rows="2">{{ text }}</textarea>
          </div>
        {% endfor %}
        <button class="dz-btn primary" name="action" value="save_messages">
          <i class="fa fa-save"></i> Save join messages
        </button>
      </form>

      {% if greetings|length > 1 %}
        <div class="dz-row" style="margin-top:10px;">
          {% for text in greetings %}
            <form method="POST" style="display:inline;">
              <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
              <input type="hidden" name="index" value="{{ loop.index0 }}" />
              <button class="dz-btn danger" name="action" value="delete_greeting"
                      title="{{ text[:60] }}">
                <i class="fa fa-trash-o"></i> #{{ loop.index }}
              </button>
            </form>
          {% endfor %}
        </div>
      {% endif %}

      <form method="POST" style="margin-top:12px;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <textarea class="dz-area" name="new_message" rows="2"
                  placeholder="Add another join message..."></textarea>
        <button class="dz-btn" name="action" value="add_greeting" style="margin-top:8px;">
          <i class="fa fa-plus"></i> Add
        </button>
      </form>
    </div>

    <div class="dz-panel">
      <h5><i class="fa fa-sign-out"></i> Leave messages</h5>
      <p class="dz-hint">{{ goodbyes|length }} in rotation.</p>
      <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        {% for text in goodbyes %}
          <div style="margin-bottom:9px;">
            <textarea class="dz-area" name="goodbye" rows="2">{{ text }}</textarea>
          </div>
        {% endfor %}
        <button class="dz-btn primary" name="action" value="save_messages">
          <i class="fa fa-save"></i> Save leave messages
        </button>
      </form>

      <form method="POST" style="margin-top:12px;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <textarea class="dz-area" name="new_message" rows="2"
                  placeholder="Add another leave message..."></textarea>
        <button class="dz-btn" name="action" value="add_goodbye" style="margin-top:8px;">
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
          <label class="dz-toggle">
            <input type="checkbox" name="t_{{ t.key }}" {% if t.on %}checked{% endif %} />
            <span>{{ t.label }}</span>
          </label>
        {% endfor %}
      </div>

      <div class="dz-panel">
        <h5><i class="fa fa-hashtag"></i> Channels &amp; limits</h5>

        <div class="dz-label">Join channel</div>
        <select class="dz-select" name="channel">
          <option value="">&mdash; none &mdash;</option>
          {% for c in channels %}
            <option value="{{ c.id }}" {% if c.selected %}selected{% endif %}>{{ c.name }}</option>
          {% endfor %}
        </select>

        <div class="dz-label" style="margin-top:11px;">Leave channel</div>
        <select class="dz-select" name="leave_channel">
          <option value="">&mdash; same as join &mdash;</option>
          {% for c in leave_channels %}
            <option value="{{ c.id }}" {% if c.selected %}selected{% endif %}>{{ c.name }}</option>
          {% endfor %}
        </select>

        <div class="dz-label" style="margin-top:11px;">Minimum account age (days)</div>
        <input class="dz-input" type="number" min="0" name="minimum_days" value="{{ minimum_days }}" />

        <div class="dz-label" style="margin-top:11px;">Delete greeting after (seconds)</div>
        <input class="dz-input" type="number" min="0" name="delete_after_greeting"
               value="{{ delete_after_greeting }}" placeholder="blank = keep" />

        <div class="dz-label" style="margin-top:11px;">Delete goodbye after (seconds)</div>
        <input class="dz-input" type="number" min="0" name="delete_after_goodbye"
               value="{{ delete_after_goodbye }}" placeholder="blank = keep" />
      </div>
    </div>

    <div class="dz-grid two" style="margin-top:14px;">
      <div class="dz-panel">
        <h5><i class="fa fa-android"></i> Bots</h5>
        <p class="dz-hint">Used when the new member is a bot.</p>

        <div class="dz-label">Join message</div>
        <textarea class="dz-area" name="bots_msg" rows="2">{{ bots_msg }}</textarea>

        <div class="dz-label" style="margin-top:11px;">Leave message</div>
        <textarea class="dz-area" name="bots_goodbye_msg" rows="2">{{ bots_goodbye_msg }}</textarea>

        <div class="dz-label" style="margin-top:11px;">Auto-role for bots</div>
        <select class="dz-select" name="bots_role">
          <option value="">&mdash; none &mdash;</option>
          {% for r in bot_roles %}
            <option value="{{ r.id }}" {% if r.selected %}selected{% endif %}>{{ r.name }}</option>
          {% endfor %}
        </select>
      </div>

      <div class="dz-panel">
        <h5><i class="fa fa-at"></i> Allowed mentions</h5>
        <p class="dz-hint">What the greeting is permitted to ping.</p>
        <div class="dz-grid two">
          <div>
            <div class="dz-label">On join</div>
            {% for key, on in mentions.items() %}
              <label class="dz-toggle">
                <input type="checkbox" name="m_{{ key }}" {% if on %}checked{% endif %} />
                <span>{{ key }}</span>
              </label>
            {% endfor %}
          </div>
          <div>
            <div class="dz-label">On leave</div>
            {% for key, on in goodbye_mentions.items() %}
              <label class="dz-toggle">
                <input type="checkbox" name="gm_{{ key }}" {% if on %}checked{% endif %} />
                <span>{{ key }}</span>
              </label>
            {% endfor %}
          </div>
        </div>
      </div>
    </div>

    <div class="dz-panel" style="margin-top:14px;">
      <h5><i class="fa fa-window-maximize"></i> Embed appearance</h5>
      <p class="dz-hint">Only used when &ldquo;Send as an embed&rdquo; is on.</p>
      <div class="dz-grid two">
        <div>
          <div class="dz-label">Title</div>
          <input class="dz-input" type="text" name="e_title" value="{{ embed.title }}" />
          <div class="dz-label" style="margin-top:10px;">Footer</div>
          <input class="dz-input" type="text" name="e_footer" value="{{ embed.footer }}" />
          <div class="dz-label" style="margin-top:10px;">Thumbnail</div>
          <input class="dz-input" type="text" name="e_thumbnail" value="{{ embed.thumbnail }}"
                 placeholder="avatar, splash, icon, or a URL" />
        </div>
        <div>
          <div class="dz-label">Join image URL</div>
          <input class="dz-input" type="text" name="e_image" value="{{ embed.image }}" />
          <div class="dz-label" style="margin-top:10px;">Leave image URL</div>
          <input class="dz-input" type="text" name="e_image_goodbye" value="{{ embed.image_goodbye }}" />
          <div class="dz-row" style="margin-top:10px;">
            <div style="flex:1 1 120px;">
              <div class="dz-label">Join colour</div>
              <input class="dz-input" type="text" name="e_colour" value="{{ embed.colour }}"
                     placeholder="#5aa9ff" />
            </div>
            <div style="flex:1 1 120px;">
              <div class="dz-label">Leave colour</div>
              <input class="dz-input" type="text" name="e_colour_goodbye"
                     value="{{ embed.colour_goodbye }}" placeholder="#ed4245" />
            </div>
          </div>
        </div>
      </div>
      <div class="dz-row" style="margin-top:10px;">
        <label class="dz-toggle">
          <input type="checkbox" name="e_author" {% if embed.author %}checked{% endif %} />
          <span>Show author</span></label>
        <label class="dz-toggle">
          <input type="checkbox" name="e_timestamp" {% if embed.timestamp %}checked{% endif %} />
          <span>Show timestamp</span></label>
        <label class="dz-toggle">
          <input type="checkbox" name="e_mention" {% if embed.mention %}checked{% endif %} />
          <span>Mention above the embed</span></label>
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
