from __future__ import annotations

import logging
import typing as t
from datetime import datetime, timedelta, timezone

import discord
from redbot.core import commands, modlog
from redbot.core.utils.mod import get_audit_reason

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    channel_options,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
    member_options,
    role_options,
)

log = logging.getLogger("red.mutes.dashboard")

# Discord caps a timeout at 28 days; a role mute has no such limit.
TIMEOUT_MAX_DAYS = 28


class DashboardIntegration:
    """Mute management from the dashboard.

    Covers ``[p]mute``, ``[p]unmute``, ``[p]mutechannel``, ``[p]unmutechannel``,
    ``[p]timeout``, ``[p]activemutes`` and every ``[p]muteset`` option: the mute
    role (including creating one), the default duration, DM notifications,
    whether the moderator is named, and the notification channel.
    """

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Mutes as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Mute members, review active mutes and configure muting.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_mutes_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        staff = await is_staff(self.bot, user, member, guild)
        is_mod = staff or await self.bot.is_mod(member)

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            if not is_mod:
                return {
                    "status": 1,
                    "error_title": "Forbidden",
                    "error_message": "Only server moderators can manage mutes.",
                }
            notifications = await self._mutes_handle_post(member, guild, staff, kwargs)

        settings = await self.config.guild(guild).all()
        mute_role = guild.get_role(settings.get("mute_role") or 0)
        notify_channel = guild.get_channel(settings.get("notification_channel") or 0)

        server_mutes = []
        for user_id, data in (self._server_mutes.get(guild.id) or {}).items():
            if not data:
                continue
            server_mutes.append(self._mutes_row(guild, user_id, data))

        timeouts = [
            {
                "id": str(m.id),
                "name": m.display_name,
                "until": m.timed_out_until.strftime("%d %b %Y, %H:%M")
                if m.timed_out_until
                else "",
                "author": "",
            }
            for m in guild.members
            if m.is_timed_out()
        ]

        channel_mutes = []
        for channel_id, data in self._channel_mutes.items():
            channel = guild.get_channel(channel_id)
            if channel is None or not data:
                continue
            for user_id, mute in data.items():
                if not mute:
                    continue
                row = self._mutes_row(guild, user_id, mute)
                row["channel"] = channel.name
                row["channel_id"] = str(channel.id)
                channel_mutes.append(row)

        default_time = settings.get("default_time") or 0

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": MUTES_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_mod": is_mod,
                "is_admin": staff,
                "mute_role": mute_role.name if mute_role else "",
                "mute_role_id": mute_role.id if mute_role else None,
                "role_options": role_options(
                    guild, selected=mute_role.id if mute_role else None, skip_managed=True
                ),
                "channel_options": channel_options(guild, require_send=True),
                "notify_options": channel_options(
                    guild,
                    selected=notify_channel.id if notify_channel else None,
                    require_send=True,
                ),
                "mute_channel_options": channel_options(
                    guild, kinds=("text", "voice", "stage", "forum")
                ),
                "member_options": member_options(guild, humans_only=True),
                "notification_channel": notify_channel.name if notify_channel else "",
                "default_time": default_time,
                "default_time_label": self._mutes_humanize(default_time),
                "send_dm": bool(settings.get("dm")),
                "show_mod": bool(settings.get("show_mod")),
                "server_mutes": server_mutes,
                "timeouts": timeouts,
                "channel_mutes": channel_mutes,
                "timeout_max_days": TIMEOUT_MAX_DAYS,
            },
        }

    @staticmethod
    def _mutes_humanize(seconds: int) -> str:
        if not seconds:
            return "indefinite"
        delta = timedelta(seconds=seconds)
        parts = []
        days, rest = divmod(int(delta.total_seconds()), 86400)
        hours, rest = divmod(rest, 3600)
        minutes, _sec = divmod(rest, 60)
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        return " ".join(parts) or f"{seconds}s"

    def _mutes_row(self, guild: discord.Guild, user_id: int, data: dict) -> dict:
        target = guild.get_member(user_id)
        author = guild.get_member(data.get("author") or 0)
        until = data.get("until")
        return {
            "id": str(user_id),
            "name": getattr(target, "display_name", None) or f"ID {user_id}",
            "author": getattr(author, "display_name", None) or "Unknown",
            "until": datetime.fromtimestamp(until, tz=timezone.utc).strftime("%d %b %Y, %H:%M")
            if until
            else "",
        }

    @staticmethod
    def _mutes_until(field) -> tuple[datetime | None, timedelta | None]:
        """Read the duration inputs into an absolute expiry."""
        days = field.integer("days", 0) or 0
        hours = field.integer("hours", 0) or 0
        minutes = field.integer("minutes", 0) or 0
        total = days * 86400 + hours * 3600 + minutes * 60
        if total <= 0:
            return None, None
        duration = timedelta(seconds=total)
        return datetime.now(tz=timezone.utc) + duration, duration

    async def _mutes_handle_post(
        self, member: discord.Member, guild: discord.Guild, staff: bool, kwargs: dict
    ) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")
        reason = (field("reason") or "").strip() or None
        audit_reason = get_audit_reason(member, reason, shorten=True)

        try:
            if action in ("mute", "timeout", "unmute", "channel_mute", "channel_unmute"):
                targets = [
                    m
                    for m in (
                        guild.get_member(int(i))
                        for i in field.many("member_ids")
                        if str(i).isdigit()
                    )
                    if m is not None
                ]
                if not targets:
                    return [{"message": "Pick at least one member.", "category": "warning"}]
                if guild.me in targets:
                    return [{"message": "You cannot mute me.", "category": "warning"}]
                if member in targets:
                    return [{"message": "You cannot mute yourself.", "category": "warning"}]
                return await self._mutes_apply(
                    action, member, guild, targets, field, reason, audit_reason
                )

            # Everything below is a setting.
            if not staff:
                return [
                    {"message": "Only server administrators can change mute settings.",
                     "category": "danger"}
                ]

            if action == "set_role":
                role_id = field.integer("role_id", 0) or 0
                role = guild.get_role(role_id) if role_id else None
                if role is not None:
                    if role >= guild.me.top_role:
                        return [
                            {
                                "message": "That role is above mine, so I cannot assign it.",
                                "category": "warning",
                            }
                        ]
                    if member != guild.owner and role >= member.top_role:
                        return [
                            {
                                "message": "That role is not below your highest role.",
                                "category": "warning",
                            }
                        ]
                await self.config.guild(guild).mute_role.set(role.id if role else None)
                # The cache is keyed only for guilds that have a role, matching
                # what `[p]muteset role` does.
                if role is None:
                    self.mute_role_cache.pop(guild.id, None)
                else:
                    self.mute_role_cache[guild.id] = role.id
                if role is None:
                    return [
                        {
                            "message": "Mute role cleared. Mutes now use Discord timeouts, "
                            "which always need a duration.",
                            "category": "success",
                        }
                    ]
                return [
                    {"message": f"Mute role set to {role.name}.", "category": "success"}
                ]

            if action == "make_role":
                name = (field("role_name") or "").strip() or "Muted"
                if not guild.me.guild_permissions.manage_roles:
                    return [
                        {"message": "I need the Manage Roles permission.",
                         "category": "warning"}
                    ]
                role = await guild.create_role(
                    name=name, reason="Mute role created from the dashboard"
                )
                await self.config.guild(guild).mute_role.set(role.id)
                self.mute_role_cache[guild.id] = role.id
                # Returns the channel mention when it could not set overwrites.
                failed = [
                    problem
                    for channel in guild.channels
                    if (problem := await self._set_mute_role_overwrites(role, channel))
                ]
                out = [
                    {"message": f"Created {role.name} and set it as the mute role.",
                     "category": "success"}
                ]
                if failed:
                    out.append(
                        {
                            "message": f"Could not set overwrites in {len(failed)} channel(s).",
                            "category": "warning",
                        }
                    )
                return out

            if action == "set_default_time":
                until, duration = self._mutes_until(field)
                seconds = int(duration.total_seconds()) if duration else 0
                await self.config.guild(guild).default_time.set(seconds)
                if not seconds:
                    return [
                        {"message": "Mutes without an explicit time are now indefinite.",
                         "category": "success"}
                    ]
                return [
                    {
                        "message": f"Mutes now default to {self._mutes_humanize(seconds)}.",
                        "category": "success",
                    }
                ]

            if action == "save_notifications":
                await self.config.guild(guild).dm.set(field.checked("send_dm"))
                await self.config.guild(guild).show_mod.set(field.checked("show_mod"))
                channel_id = field.integer("notification_channel", 0) or 0
                channel = guild.get_channel(channel_id) if channel_id else None
                await self.config.guild(guild).notification_channel.set(
                    channel.id if channel else None
                )
                return [{"message": "Notification settings saved.", "category": "success"}]
        except discord.Forbidden:
            return [
                {"message": "Discord refused that action; check my permissions.",
                 "category": "danger"}
            ]
        except Exception as exc:  # noqa: BLE001
            log.exception("Mutes dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    async def _mutes_apply(
        self,
        action: str,
        author: discord.Member,
        guild: discord.Guild,
        targets: list[discord.Member],
        field,
        reason: str | None,
        audit_reason: str,
    ) -> list[dict]:
        until, duration = self._mutes_until(field)
        now = datetime.now(tz=timezone.utc)

        if action in ("mute", "timeout") and until is None:
            default = await self.config.guild(guild).default_time()
            if default:
                duration = timedelta(seconds=default)
                until = now + duration

        channel = None
        if action in ("channel_mute", "channel_unmute"):
            channel = guild.get_channel(field.integer("mute_channel_id", 0) or 0)
            if channel is None:
                return [{"message": "Pick a channel.", "category": "warning"}]

        if action == "timeout":
            if until is None:
                return [
                    {"message": "A timeout needs a duration.", "category": "warning"}
                ]
            if (until - now) > timedelta(days=TIMEOUT_MAX_DAYS):
                return [
                    {
                        "message": f"A timeout cannot last longer than "
                        f"{TIMEOUT_MAX_DAYS} days.",
                        "category": "warning",
                    }
                ]

        done: list[str] = []
        failed: list[str] = []
        for target in targets:
            if action == "timeout":
                if not await self.is_allowed_by_hierarchy(guild, author, target):
                    failed.append(f"{target.display_name}: above you in the hierarchy")
                    continue
                try:
                    await target.edit(timed_out_until=until, reason=audit_reason)
                except discord.HTTPException as exc:
                    failed.append(f"{target.display_name}: {exc}")
                    continue
                await modlog.create_case(
                    self.bot, guild, now, "smute", target, author, reason,
                    until=until, channel=None,
                )
                await self._send_dm_notification(
                    target, author, guild, "Server mute", reason, duration
                )
                done.append(target.display_name)
                continue

            if action == "mute":
                response = await self.mute_user(guild, author, target, until, audit_reason)
                casetype, label = "smute", "Server mute"
            elif action == "unmute":
                response = await self.unmute_user(guild, author, target, audit_reason)
                casetype, label = "sunmute", "Server unmute"
            elif action == "channel_mute":
                response = await self.channel_mute_user(
                    guild, channel, author, target, until, audit_reason
                )
                casetype, label = "cmute", "Channel mute"
            else:
                response = await self.channel_unmute_user(
                    guild, channel, author, target, audit_reason
                )
                casetype, label = "cunmute", "Channel unmute"

            if not response.success:
                failed.append(f"{target.display_name}: {response.reason or 'unknown reason'}")
                continue

            await modlog.create_case(
                self.bot,
                guild,
                now,
                casetype,
                target,
                author,
                reason,
                until=until if casetype in ("smute", "cmute") else None,
                channel=channel,
            )
            await self._send_dm_notification(target, author, guild, label, reason, duration)
            done.append(target.display_name)

        out = []
        if done:
            verb = {
                "mute": "muted",
                "timeout": "timed out",
                "unmute": "unmuted",
                "channel_mute": f"muted in #{getattr(channel, 'name', '')}",
                "channel_unmute": f"unmuted in #{getattr(channel, 'name', '')}",
            }[action]
            suffix = f" for {self._mutes_humanize(int(duration.total_seconds()))}" if (
                duration and action in ("mute", "timeout", "channel_mute")
            ) else ""
            out.append(
                {"message": f"{', '.join(done)} {verb}{suffix}.", "category": "success"}
            )
        for problem in failed:
            out.append({"message": problem, "category": "warning"})
        return out or [{"message": "Nothing happened.", "category": "info"}]


MUTES_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-microphone-slash"></i> Mutes in {{ guild_name }}</h4>
    <p>
      {% if mute_role %}Using the <b>{{ mute_role }}</b> role.
      {% else %}No mute role set, so mutes use Discord timeouts &mdash; which always
        need a duration and cap at {{ timeout_max_days }} days.{% endif %}
      Default duration: {{ default_time_label }}.
    </p>
  </div>

  {{ stats([('Server mutes', server_mutes|length),
            ('Timeouts', timeouts|length),
            ('Channel mutes', channel_mutes|length),
            ('DM on mute', 'on' if send_dm else 'off')]) }}

  {% if not is_mod %}
    <div class="dz-panel">
      <p class="dz-empty">You need moderator permissions to manage mutes.</p>
    </div>
  {% else %}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-gavel"></i> Mute members</h5>
      <p class="dz-hint">Leave the duration empty to use the server default.</p>
      <div class="dz-grid two">
        <div>
          <label class="dz-label">Members</label>
          {{ picker('member_ids', member_options, true, 10, 'Search members...') }}
        </div>
        <div>
          <label class="dz-label">Duration</label>
          <div class="dz-row">
            <input class="dz-input" type="number" min="0" name="days"
                   placeholder="days" style="max-width:110px;" />
            <input class="dz-input" type="number" min="0" name="hours"
                   placeholder="hours" style="max-width:110px;" />
            <input class="dz-input" type="number" min="0" name="minutes"
                   placeholder="minutes" style="max-width:120px;" />
          </div>
          <label class="dz-label" style="margin-top:12px;">Reason</label>
          <input class="dz-input" type="text" name="reason"
                 placeholder="shown in the modlog and the audit log" />
          <label class="dz-label" style="margin-top:12px;">Channel (for channel mutes)</label>
          {{ picker('mute_channel_id', mute_channel_options, false, 6,
                    'Search channels...', true, 'not a channel mute') }}
        </div>
      </div>
      <div class="dz-row dz-save">
        <button class="dz-btn primary" name="action" value="mute">
          <i class="fa fa-microphone-slash"></i> Mute
        </button>
        <button class="dz-btn" name="action" value="timeout">
          <i class="fa fa-clock-o"></i> Timeout
        </button>
        <button class="dz-btn" name="action" value="unmute">
          <i class="fa fa-microphone"></i> Unmute
        </button>
        <button class="dz-btn" name="action" value="channel_mute">
          <i class="fa fa-hashtag"></i> Mute in channel
        </button>
        <button class="dz-btn" name="action" value="channel_unmute">
          <i class="fa fa-hashtag"></i> Unmute in channel
        </button>
      </div>
    </div>
  </form>

  <div class="dz-panel">
    <h5><i class="fa fa-list"></i> Active mutes</h5>
    {% if server_mutes or timeouts or channel_mutes %}
      <table class="dz-t">
        <tr><th>Member</th><th>Kind</th><th>Where</th><th>Until</th><th>By</th></tr>
        {% for m in server_mutes %}
          <tr><td>{{ m.name }}</td><td>Server mute</td><td>&mdash;</td>
              <td>{{ m.until or 'indefinite' }}</td><td>{{ m.author }}</td></tr>
        {% endfor %}
        {% for m in timeouts %}
          <tr><td>{{ m.name }}</td><td>Timeout</td><td>&mdash;</td>
              <td>{{ m.until }}</td><td>&mdash;</td></tr>
        {% endfor %}
        {% for m in channel_mutes %}
          <tr><td>{{ m.name }}</td><td>Channel mute</td><td>#{{ m.channel }}</td>
              <td>{{ m.until or 'indefinite' }}</td><td>{{ m.author }}</td></tr>
        {% endfor %}
      </table>
    {% else %}
      <p class="dz-empty">Nobody is muted right now.</p>
    {% endif %}
  </div>

  {% if is_admin %}
    <div class="dz-grid two">
      <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <div class="dz-panel">
          <h5><i class="fa fa-user-secret"></i> Mute role</h5>
          <p class="dz-hint">With a role, mutes can be indefinite. Without one,
             Discord timeouts are used instead.</p>
          {{ picker('role_id', role_options, false, 8, 'Search roles...',
                    true, 'use timeouts') }}
          <div class="dz-save">
            <button class="dz-btn primary" name="action" value="set_role">
              <i class="fa fa-save"></i> Save role
            </button>
          </div>
          <label class="dz-label" style="margin-top:14px;">Or create one</label>
          <div class="dz-row">
            <input class="dz-input" type="text" name="role_name" value="Muted"
                   style="max-width:200px;" />
            {{ confirm('Create mute role', 'make_role',
                       'Create the role and deny sending messages in every channel?',
                       '', 'fa-plus') }}
          </div>
        </div>
      </form>

      <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <div class="dz-panel">
          <h5><i class="fa fa-clock-o"></i> Default duration</h5>
          <p class="dz-hint">Used when a mute is issued without a time.
             All zero means indefinite.</p>
          <div class="dz-row">
            <input class="dz-input" type="number" min="0" name="days"
                   placeholder="days" style="max-width:110px;" />
            <input class="dz-input" type="number" min="0" name="hours"
                   placeholder="hours" style="max-width:110px;" />
            <input class="dz-input" type="number" min="0" name="minutes"
                   placeholder="minutes" style="max-width:120px;" />
          </div>
          <p class="dz-hint" style="margin-top:8px;">Currently {{ default_time_label }}.</p>
          <div class="dz-save">
            <button class="dz-btn primary" name="action" value="set_default_time">
              <i class="fa fa-save"></i> Save default
            </button>
          </div>
        </div>
      </form>
    </div>

    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-bell"></i> Notifications</h5>
        <label class="dz-toggle">
          <input type="checkbox" name="send_dm" {% if send_dm %}checked{% endif %} />
          <span>DM members when they are muted or unmuted</span>
        </label>
        <label class="dz-toggle">
          <input type="checkbox" name="show_mod" {% if show_mod %}checked{% endif %} />
          <span>Name the moderator in that DM</span>
        </label>
        <label class="dz-label" style="margin-top:10px;">
          Channel for mute failures and unmute reports
        </label>
        {{ picker('notification_channel', notify_options, false, 8,
                  'Search channels...', true, 'no notifications') }}
        <div class="dz-save">
          <button class="dz-btn primary" name="action" value="save_notifications">
            <i class="fa fa-save"></i> Save
          </button>
        </div>
      </div>
    </form>
  {% endif %}

  {% endif %}
</div>
"""
)
