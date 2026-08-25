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
                "total": sum(len(g["events"]) for g in groups),
                "ignored_channels": channel_options(guild),
                "ignored_channel_ids": [str(i) for i in (settings.get("ignored_channels") or [])],
                "ignored_roles": role_options(guild),
                "ignored_mods": bool(settings.get("ignored_mods")),
            },
        }

    async def _eml_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")
        conf = self.config.guild(guild)

        try:
            if action == "save_group":
                return await self._eml_save_group(conf, guild, field)
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
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-file-text-o"></i> Event logging in {{ guild_name }}</h4>
    <p>{{ active }} of {{ total }} events are being logged.</p>
  </div>

  <div class="dz-panel">
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
                <select class="dz-select" name="{{ e.key }}__channel">
                  <option value="">&mdash; default modlog &mdash;</option>
                  {% for c in e.channels %}
                    <option value="{{ c.id }}" {% if c.selected %}selected{% endif %}>{{ c.name }}</option>
                  {% endfor %}
                </select>
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
      <select class="dz-select" name="ignored_channels" multiple size="8">
        {% for c in ignored_channels %}
          <option value="{{ c.id }}" {% if c.id in ignored_channel_ids %}selected{% endif %}>
            {{ c.name }}
          </option>
        {% endfor %}
      </select>
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
