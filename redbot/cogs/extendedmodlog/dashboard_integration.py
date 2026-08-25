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
    role_options,
)

log = logging.getLogger("red.extendedmodlog.dashboard")

# Config keys that hold lists rather than an event definition.
NON_EVENT_KEYS = {"ignored_channels", "ignored_users", "ignored_mods", "invite_links"}

# Grouping only affects presentation - 21 flat toggles is unusable.
EVENT_GROUPS = (
    ("Messages", ("message_edit", "message_delete", "commands_used")),
    ("Members", ("user_join", "user_left", "user_change")),
    ("Roles", ("role_change", "role_create", "role_delete")),
    ("Channels", ("channel_change", "channel_create", "channel_delete", "voice_change")),
    ("Threads", ("thread_change", "thread_create", "thread_delete")),
    ("Server", ("guild_change", "emoji_change", "stickers_change")),
    ("Invites", ("invite_created", "invite_deleted")),
)

# Per-event extras beyond the shared enabled/channel/embed/colour/emoji.
EXTRA_LABELS = {
    "bots": "Include bots",
    "ignore_commands": "Ignore command invocations",
    "bulk_enabled": "Log bulk deletions",
    "bulk_individual": "List each bulk-deleted message",
    "cached_only": "Only messages still in cache",
    "nicknames": "Nickname changes",
    "pending": "Members pending screening",
    "avatar": "Avatar changes",
    "timeout": "Timeouts",
    "roles": "Role changes",
    "flags": "Account flag changes",
    "premium_since": "Boost changes",
}


class DashboardIntegration:
    """Per-event logging configuration."""

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering ExtendedModLog as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Choose which events are logged and where.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_modlog_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        if not await is_staff(self.bot, user, member, guild):
            return {
                "status": 1,
                "error_title": "Forbidden",
                "error_message": "Only server administrators can change logging.",
            }

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._eml_handle_post(guild, kwargs)

        settings = await self.config.guild(guild).all()
        groups = []
        active = 0
        for title, keys in EVENT_GROUPS:
            events = []
            for key in keys:
                data = settings.get(key)
                if not isinstance(data, dict):
                    continue
                if data.get("enabled"):
                    active += 1
                events.append(
                    {
                        "key": key,
                        "label": key.replace("_", " ").capitalize(),
                        "enabled": bool(data.get("enabled")),
                        "embed": bool(data.get("embed", True)),
                        "emoji": data.get("emoji") or "",
                        "channels": channel_options(guild, selected=data.get("channel")),
                        "extras": [
                            {"key": k, "label": EXTRA_LABELS[k], "on": bool(data.get(k))}
                            for k in data
                            if k in EXTRA_LABELS
                        ],
                    }
                )
            if events:
                groups.append({"title": title, "events": events})

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": MODLOG_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "groups": groups,
                "active": active,
                "stat_items": [
                    ("Events on", active),
                    ("Events off", sum(len(g["events"]) for g in groups) - active),
                    ("Ignored channels", len(settings.get("ignored_channels") or [])),
                ],
                "all_channels": channel_options(guild, require_send=True),
                "preview": self._eml_preview(guild),
                "total": sum(len(g["events"]) for g in groups),
                "ignored_channels": channel_options(
                    guild, selected_many=settings.get("ignored_channels") or []
                ),
                "ignored_roles": role_options(guild),
                "ignored_mods": bool(settings.get("ignored_mods")),
            },
        }

    def _eml_preview(self, guild: discord.Guild) -> dict:
        """A representative log entry, so the styling is visible before an event fires."""
        me = guild.me
        return {
            "id": "preview",
            "author": me.display_name if me else "Bot",
            "avatar": str(me.display_avatar) if me else "",
            "bot": True,
            "content": "",
            "timestamp": "now",
            "attachments": [],
            "pinned": False,
            "old": False,
            "embeds": [
                {
                    "title": "Message deleted",
                    "description": "A message by @someone was deleted in #general.",
                    "colour": "#ed4245",
                    "footer": "Message ID: 000000000000000000",
                    "fields": [
                        {"name": "Channel", "value": "#general"},
                        {"name": "Content", "value": "the deleted text"},
                    ],
                }
            ],
        }

    async def _eml_set_all_channels(self, conf, guild, field) -> list[dict]:
        """Point every currently enabled event at one channel."""
        raw = field("bulk_channel") or ""
        if not raw.isdigit():
            return [{"message": "Pick a channel first.", "category": "warning"}]
        channel = guild.get_channel(int(raw))
        if channel is None:
            return [{"message": "That channel no longer exists.", "category": "danger"}]
        if not channel.permissions_for(guild.me).send_messages:
            return [
                {"message": f"I cannot send messages in #{channel.name}.", "category": "danger"}
            ]

        changed = 0
        settings = await conf.all()
        for key, data in settings.items():
            if key in NON_EVENT_KEYS or not isinstance(data, dict) or "enabled" not in data:
                continue
            if not data.get("enabled"):
                continue
            await conf.get_attr(key).channel.set(channel.id)
            changed += 1
        return [
            {"message": f"Pointed {changed} enabled event(s) at #{channel.name}.",
             "category": "success"}
        ]

    async def _eml_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")
        conf = self.config.guild(guild)

        try:
            if action == "save_group":
                return await self._eml_save_group(conf, guild, field)
            if action == "set_all_channels":
                return await self._eml_set_all_channels(conf, guild, field)
            if action == "save_ignores":
                ids = [int(x) for x in field.many("ignored_channels") if str(x).isdigit()]
                await conf.ignored_channels.set(ids)
                await conf.ignored_mods.set(field.checked("ignored_mods"))
                return [{"message": "Ignore list saved.", "category": "success"}]
            if action in ("enable_all", "disable_all"):
                target = action == "enable_all"
                changed = 0
                for _title, keys in EVENT_GROUPS:
                    for key in keys:
                        group = conf.get_attr(key)
                        current = await group.all()
                        if not isinstance(current, dict) or current.get("enabled") is target:
                            continue
                        await group.enabled.set(target)
                        changed += 1
                word = "Enabled" if target else "Disabled"
                return [{"message": f"{word} {changed} event(s).", "category": "success"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("ExtendedModLog dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    async def _eml_save_group(self, conf, guild: discord.Guild, field) -> list[dict]:
        keys = [k for k in (field("keys") or "").split(",") if k and k not in NON_EVENT_KEYS]
        if not keys:
            return [{"message": "Nothing to save.", "category": "warning"}]

        warnings: list[dict] = []
        for key in keys:
            group = conf.get_attr(key)
            current = await group.all()
            if not isinstance(current, dict):
                continue

            await group.enabled.set(field.checked(f"{key}__enabled"))
            await group.embed.set(field.checked(f"{key}__embed"))

            raw_channel = field(f"{key}__channel") or ""
            channel_id = int(raw_channel) if raw_channel.isdigit() else None
            if channel_id is not None:
                channel = guild.get_channel(channel_id)
                # A log channel the bot cannot post in fails silently at runtime.
                if channel is not None and not channel.permissions_for(guild.me).send_messages:
                    warnings.append(
                        {
                            "message": f"{key}: I cannot send messages in #{channel.name}.",
                            "category": "warning",
                        }
                    )
            await group.channel.set(channel_id)

            emoji = (field(f"{key}__emoji") or "").strip()
            await group.emoji.set(emoji or None)

            for extra in current:
                if extra in EXTRA_LABELS:
                    await group.get_attr(extra).set(field.checked(f"{key}__{extra}"))

        return warnings + [
            {"message": f"Saved {len(keys)} event(s).", "category": "success"}
        ]


MODLOG_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-file-text-o"></i> Event logging in {{ guild_name }}</h4>
    <p>{{ active }} of {{ total }} events are being logged.</p>
  </div>

  {{ stats(stat_items) }}

  {% if preview %}
    <div class="dz-panel">
      <h5><i class="fa fa-eye"></i> Sample log entry</h5>
      <p class="dz-hint">How an event looks when it is posted.</p>
      {{ msg(preview) }}
    </div>
  {% endif %}

  <div class="dz-panel">
    <h5><i class="fa fa-bolt"></i> Bulk actions</h5>
    <p class="dz-hint">Point every enabled event at one channel, or flip them all at once.</p>
    <form method="POST" class="dz-row" style="margin-bottom:10px;">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div style="flex:1 1 260px;">
        {{ picker('bulk_channel', all_channels, allow_none=true,
                  none_label='pick a channel', placeholder='Search channels...') }}
      </div>
      <button class="dz-btn" name="action" value="set_all_channels">
        <i class="fa fa-share"></i> Send everything here
      </button>
    </form>
    <div class="dz-row">
      <form method="POST" style="display:inline;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <button class="dz-btn" name="action" value="enable_all">
          <i class="fa fa-check-square-o"></i> Enable everything
        </button>
      </form>
      <form method="POST" style="display:inline;"
            onsubmit="return confirm('Turn off every event?');">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <button class="dz-btn danger" name="action" value="disable_all">
          <i class="fa fa-square-o"></i> Disable everything
        </button>
      </form>
      <span class="dz-hint" style="margin:0 0 0 6px;">
        Events with no channel set fall back to the server's modlog channel.
      </span>
    </div>
  </div>

  {% for g in groups %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <input type="hidden" name="keys"
             value="{% for e in g.events %}{{ e.key }}{% if not loop.last %},{% endif %}{% endfor %}" />
      <div class="dz-panel">
        <h5><i class="fa fa-folder-o"></i> {{ g.title }}</h5>
        <p class="dz-hint">{{ g.events|length }} event(s).</p>

        {% for e in g.events %}
          <div style="padding:12px 0; border-top:1px solid rgba(255,255,255,.06);">
            <div class="dz-row" style="justify-content:space-between;">
              <label class="dz-toggle" style="padding:0;">
                <input type="checkbox" name="{{ e.key }}__enabled"
                       {% if e.enabled %}checked{% endif %} />
                <b>{{ e.label }}</b>
              </label>
              <span class="dz-hint" style="margin:0;"><code>{{ e.key }}</code></span>
            </div>

            <div class="dz-grid two" style="margin-top:9px;">
              <div>
                <div class="dz-label">Log channel</div>
                {{ picker(e.key ~ '__channel', e.channels, allow_none=true,
                          none_label='default modlog', placeholder='Search channels...') }}
              </div>
              <div>
                <div class="dz-label">Emoji</div>
                <input class="dz-input" type="text" name="{{ e.key }}__emoji"
                       value="{{ e.emoji }}" />
              </div>
            </div>

            <div class="dz-row" style="margin-top:7px;">
              <label class="dz-toggle">
                <input type="checkbox" name="{{ e.key }}__embed" {% if e.embed %}checked{% endif %} />
                <span>Embed</span>
              </label>
              {% for x in e.extras %}
                <label class="dz-toggle">
                  <input type="checkbox" name="{{ e.key }}__{{ x.key }}"
                         {% if x.on %}checked{% endif %} />
                  <span>{{ x.label }}</span>
                </label>
              {% endfor %}
            </div>
          </div>
        {% endfor %}

        <div style="margin-top:12px;">
          <button class="dz-btn primary" name="action" value="save_group">
            <i class="fa fa-save"></i> Save {{ g.title|lower }}
          </button>
        </div>
      </div>
    </form>
  {% endfor %}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-ban"></i> Ignore list</h5>
      <p class="dz-hint">Nothing from these channels is logged. Ctrl-click to multi-select.</p>
      {{ picker('ignored_channels', ignored_channels, multiple=true, size=9,
                placeholder='Search channels...') }}
      <label class="dz-toggle" style="margin-top:9px;">
        <input type="checkbox" name="ignored_mods" {% if ignored_mods %}checked{% endif %} />
        <span>Don't log actions performed by moderators</span>
      </label>
      <div style="margin-top:11px;">
        <button class="dz-btn primary" name="action" value="save_ignores">
          <i class="fa fa-save"></i> Save ignore list
        </button>
      </div>
    </div>
  </form>
</div>
"""
)
