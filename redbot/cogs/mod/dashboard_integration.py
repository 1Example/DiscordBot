from __future__ import annotations

import logging
import typing as t
from datetime import datetime, timedelta, timezone

import discord
from redbot.core import commands, modlog
from redbot.core.utils.mod import get_audit_reason

from .dashboard_modlog import ModLogDashboardMixin
from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    channel_options,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
    member_options,
)

log = logging.getLogger("red.mod.dashboard")

TOGGLES = (
    ("respect_hierarchy", "Respect role hierarchy",
     "Moderators cannot act on members at or above their own top role."),
    ("dm_on_kickban", "DM the member on kick or ban", "Sends the reason before the action."),
    ("require_reason", "Require a reason", "Moderation commands fail without one."),
    ("reinvite_on_unban", "Re-invite on unban", "DMs an invite when a member is unbanned."),
    ("track_nicknames", "Track nickname history", "Keeps past nicknames for lookup."),
    ("ban_show_extra", "Attach a staff message to bans", "Adds the embed configured below."),
)

# -1 means disabled for both of these.
NUMERIC = (
    ("delete_repeats", "Repeated messages before deletion",
     "Delete a message repeated this many times. -1 disables.", -1, 100),
    ("delete_delay", "Delete command invocations after (s)",
     "Remove the invoking message after this delay. -1 keeps it.", -1, 300),
    ("default_days", "Default days of messages to purge on ban",
     "0 keeps all message history.", 0, 7),
    ("default_tempban_duration", "Default tempban length (seconds)",
     "Used when no duration is given.", 60, 31536000),
)

SPAM_ACTIONS = ("warn", "kick", "ban")


class DashboardIntegration(ModLogDashboardMixin):
    """Moderation actions and settings.

    The modlog and the event log are further pages of this module rather than
    modules of their own; see ``dashboard_modlog.py`` and ``eventlog/dashboard.py``.

    Runs the moderation actions themselves - kick, ban, tempban, softban,
    massban, unban, the voice actions, rename and slowmode - alongside the
    settings that used to be ``[p]modset``, the mention-spam thresholds, the
    tempban list, and a member lookup covering everything ``/userinfo`` shows.

    ``[p]modset`` no longer exists: this page is the only way to change these,
    which is why every one of its options has to stay covered here.
    """

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Mod as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Moderate members and configure moderation for this server.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_mod_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        staff = await is_staff(self.bot, user, member, guild)
        is_mod = staff or await self.bot.is_mod(member)
        if not is_mod:
            return {
                "status": 1,
                "error_title": "Forbidden",
                "error_message": "Only server moderators can use the moderation page.",
            }

        notifications: list[dict] = []
        lookup: dict = {}
        if kwargs.get("method") == "POST":
            notifications, lookup = await self._mod_handle_post(
                member, guild, staff, kwargs
            )

        settings = await self.config.guild(guild).all()
        spam = settings.get("mention_spam") or {}

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": MOD_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_admin": staff,
                "lookup": lookup,
                "member_options": member_options(guild),
                "channel_options": channel_options(
                    guild, kinds=("text", "voice", "stage", "forum")
                ),
                "toggles": [
                    {"key": k, "label": lbl, "help": h, "on": bool(settings.get(k))}
                    for k, lbl, h in TOGGLES
                ],
                "numeric": [
                    {
                        "key": k,
                        "label": lbl,
                        "help": h,
                        "min": lo,
                        "max": hi,
                        "value": settings.get(k, lo),
                    }
                    for k, lbl, h, lo, hi in NUMERIC
                ],
                "spam": [
                    {"key": a, "label": a.capitalize(), "value": spam.get(a) or ""}
                    for a in SPAM_ACTIONS
                ],
                "spam_strict": bool(spam.get("strict")),
                "ban_title": settings.get("ban_extra_embed_title") or "",
                "ban_body": settings.get("ban_extra_embed_contents") or "",
                "tempbans": await self._mod_tempbans(guild, settings),
                "ignored": bool(settings.get("ignored")),
                "is_owner": await self.bot.is_owner(user),
                "track_all_names": await self.config.track_all_names(),
            },
        }

    async def _mod_tempbans(self, guild: discord.Guild, settings: dict) -> list[dict]:
        rows = []
        for user_id in settings.get("current_tempbans") or []:
            who = self.bot.get_user(user_id)
            rows.append(
                {"id": str(user_id), "name": str(who) if who else f"Unknown ({user_id})"}
            )
        return rows

    async def _mod_handle_post(
        self, member: discord.Member, guild: discord.Guild, staff: bool, kwargs: dict
    ) -> tuple[list[dict], dict]:
        field = form_reader(kwargs)
        action = field("action")
        conf = self.config.guild(guild)

        try:
            if action == "lookup":
                return await self._mod_lookup(guild, field)

            if action.startswith("act_"):
                return await self._mod_action(
                    action.removeprefix("act_"), member, guild, field
                ), {}

            if action in ("save_name_tracking", "purge_names"):
                if not await self.bot.is_owner(member):
                    return [
                        {
                            "message": "Only the bot owner can change name tracking.",
                            "category": "danger",
                        }
                    ], {}
                if action == "save_name_tracking":
                    await self.config.track_all_names.set(
                        field.checked("track_all_names")
                    )
                    if field.checked("track_all_names"):
                        return [
                            {
                                "message": "I will record username and display name "
                                "changes across every server.",
                                "category": "success",
                            }
                        ], {}
                    return [
                        {
                            "message": "Username tracking is off. Existing history is "
                            "kept until you purge it.",
                            "category": "success",
                        }
                    ], {}
                # Same walk `[p]modset deletenames` used to do: drop the name lists
                # and then any record left empty by that.
                async with self.config._get_base_group(
                    self.config.MEMBER
                ).all() as member_data:
                    for guild_id in list(member_data):
                        guild_records = member_data[guild_id]
                        for member_id in list(guild_records):
                            guild_records[member_id].pop("past_nicks", None)
                            if not guild_records[member_id]:
                                del guild_records[member_id]
                        if not guild_records:
                            del member_data[guild_id]
                async with self.config._get_base_group(
                    self.config.USER
                ).all() as user_data:
                    for user_id in list(user_data):
                        user_data[user_id].pop("past_names", None)
                        user_data[user_id].pop("past_display_names", None)
                        if not user_data[user_id]:
                            del user_data[user_id]
                return [
                    {
                        "message": "Every stored username, display name and nickname "
                        "was deleted.",
                        "category": "success",
                    }
                ], {}

            if action == "save" and not staff:
                return [
                    {
                        "message": "Only server administrators can change moderation "
                        "settings.",
                        "category": "danger",
                    }
                ], {}

            if action == "save":
                errors: list[dict] = []

                for key, _lbl, _h in TOGGLES:
                    await conf.get_attr(key).set(field.checked(f"t_{key}"))
                await conf.ignored.set(field.checked("ignored"))

                for key, label, _h, low, high in NUMERIC:
                    raw = (field(f"n_{key}") or "").strip()
                    if raw == "":
                        continue
                    try:
                        value = int(raw)
                    except ValueError:
                        errors.append(
                            {"message": f"{label}: '{raw}' is not a number.", "category": "danger"}
                        )
                        continue
                    if not low <= value <= high:
                        errors.append(
                            {
                                "message": f"{label}: must be between {low} and {high}.",
                                "category": "danger",
                            }
                        )
                        continue
                    await conf.get_attr(key).set(value)

                # Mention-spam thresholds must ascend, or the lower action fires
                # first and the higher one is unreachable.
                spam: dict[str, t.Any] = {"strict": field.checked("spam_strict")}
                for key in SPAM_ACTIONS:
                    raw = (field(f"s_{key}") or "").strip()
                    if raw == "":
                        spam[key] = None
                        continue
                    try:
                        value = int(raw)
                    except ValueError:
                        errors.append(
                            {"message": f"Mention spam {key}: '{raw}' is not a number.",
                             "category": "danger"}
                        )
                        spam[key] = None
                        continue
                    spam[key] = value if value > 0 else None

                ordered = [(k, spam[k]) for k in SPAM_ACTIONS if spam.get(k)]
                for (first_key, first), (second_key, second) in zip(ordered, ordered[1:]):
                    if first >= second:
                        errors.append(
                            {
                                "message": f"Mention spam: {second_key} ({second}) must be higher "
                                f"than {first_key} ({first}), otherwise it never triggers.",
                                "category": "warning",
                            }
                        )
                await conf.mention_spam.set(spam)

                await conf.ban_extra_embed_title.set((field("ban_title") or "").strip())
                await conf.ban_extra_embed_contents.set((field("ban_body") or "").strip())

                return errors + [
                    {"message": "Moderation settings saved.", "category": "success"}
                ], {}
        except discord.Forbidden:
            return [
                {"message": "Discord refused that action; check my permissions.",
                 "category": "danger"}
            ], {}
        except Exception as exc:  # noqa: BLE001
            log.exception("Mod dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}], {}

        return [{"message": f"Unknown action: {action}", "category": "warning"}], {}

    async def _mod_targets(
        self, guild: discord.Guild, field
    ) -> tuple[list[discord.Member], list[int]]:
        """Return (members in the guild, raw IDs) chosen on the form.

        The ID box lets a ban or unban name someone who is not (or no longer) a
        member, the way the commands accept a bare ID.
        """
        members = []
        for raw in field.many("member_ids"):
            if str(raw).isdigit() and (found := guild.get_member(int(raw))):
                members.append(found)
        ids = []
        for token in (field("user_ids") or "").replace(",", " ").split():
            if token.isdigit():
                ids.append(int(token))
        return members, ids

    async def _mod_action(
        self, what: str, author: discord.Member, guild: discord.Guild, field
    ) -> list[dict]:
        reason = (field("reason") or "").strip() or None
        audit_reason = get_audit_reason(author, reason, shorten=True)
        now = datetime.now(tz=timezone.utc)
        members, raw_ids = await self._mod_targets(guild, field)

        if what == "slowmode":
            channel = guild.get_channel(field.integer("channel_id", 0) or 0)
            if channel is None:
                return [{"message": "Pick a channel.", "category": "warning"}]
            seconds = field.integer("slowmode_seconds", 0) or 0
            if not 0 <= seconds <= 21600:
                return [
                    {"message": "Slowmode must be between 0 and 21600 seconds (6 hours).",
                     "category": "warning"}
                ]
            await channel.edit(slowmode_delay=seconds, reason=audit_reason)
            if seconds:
                return [
                    {"message": f"Slowmode in #{channel.name} set to {seconds}s.",
                     "category": "success"}
                ]
            return [
                {"message": f"Slowmode disabled in #{channel.name}.", "category": "success"}
            ]

        if what == "rename":
            if not members:
                return [{"message": "Pick a member.", "category": "warning"}]
            nickname = (field("nickname") or "").strip()
            if len(nickname) > 32:
                return [
                    {"message": "A nickname cannot exceed 32 characters.",
                     "category": "warning"}
                ]
            done = []
            for target in members:
                await target.edit(nick=nickname or None, reason=audit_reason)
                done.append(target.display_name)
            if nickname:
                return [
                    {"message": f"Renamed {', '.join(done)} to {nickname}.",
                     "category": "success"}
                ]
            return [{"message": f"Cleared the nickname of {', '.join(done)}.",
                     "category": "success"}]

        if not members and not raw_ids:
            return [{"message": "Pick at least one member.", "category": "warning"}]

        require_reason = await self.config.guild(guild).require_reason()
        if require_reason and reason is None:
            return [
                {"message": "This server requires a reason for moderation actions.",
                 "category": "warning"}
            ]

        done: list[str] = []
        failed: list[str] = []

        # Bans and unbans can also act on a bare ID.
        if what in ("ban", "tempban", "massban"):
            days = field.integer("delete_days", 0) or 0
            if not 0 <= days <= 7:
                return [
                    {"message": "Days of messages to delete must be 0-7.",
                     "category": "warning"}
                ]
            until = None
            if what == "tempban":
                hours = field.integer("tempban_hours", 0) or 0
                if hours <= 0:
                    default = await self.config.guild(guild).default_tempban_duration()
                    hours = max(1, int(default // 3600))
                until = now + timedelta(hours=hours)

            targets: list = list(members) + [discord.Object(id=i) for i in raw_ids]
            for target in targets:
                label = getattr(target, "display_name", None) or f"ID {target.id}"
                if isinstance(target, discord.Member):
                    if target == author:
                        failed.append(f"{label}: you cannot ban yourself")
                        continue
                    if not await self._mod_allowed(guild, author, target):
                        failed.append(f"{label}: above you in the hierarchy")
                        continue
                    if guild.me.top_role <= target.top_role or target == guild.owner:
                        failed.append(f"{label}: above me in the hierarchy")
                        continue
                try:
                    await guild.ban(
                        target, reason=audit_reason, delete_message_seconds=days * 86400
                    )
                except discord.HTTPException as exc:
                    failed.append(f"{label}: {exc}")
                    continue
                await modlog.create_case(
                    self.bot, guild, now,
                    "tempban" if until else "ban",
                    target, author, reason, until=until,
                )
                if until:
                    async with self.config.guild(guild).current_tempbans() as tempbans:
                        if target.id not in tempbans:
                            tempbans.append(target.id)
                    await self.config.member_from_ids(
                        guild.id, target.id
                    ).banned_until.set(until.timestamp())
                done.append(label)

        elif what == "unban":
            for user_id in raw_ids + [m.id for m in members]:
                try:
                    ban_entry = await guild.fetch_ban(discord.Object(id=user_id))
                except discord.NotFound:
                    failed.append(f"ID {user_id}: not banned")
                    continue
                await guild.unban(ban_entry.user, reason=audit_reason)
                async with self.config.guild(guild).current_tempbans() as tempbans:
                    if user_id in tempbans:
                        tempbans.remove(user_id)
                await modlog.create_case(
                    self.bot, guild, now, "unban", ban_entry.user, author, reason
                )
                done.append(str(ban_entry.user))

        elif what in ("kick", "softban"):
            for target in members:
                if target == author:
                    failed.append(f"{target.display_name}: that is you")
                    continue
                if not await self._mod_allowed(guild, author, target):
                    failed.append(f"{target.display_name}: above you in the hierarchy")
                    continue
                try:
                    if what == "softban":
                        # Ban then immediately unban, which purges a day of messages.
                        await guild.ban(
                            target, reason=audit_reason, delete_message_seconds=86400
                        )
                        await guild.unban(target, reason=audit_reason)
                    else:
                        await guild.kick(target, reason=audit_reason)
                except discord.HTTPException as exc:
                    failed.append(f"{target.display_name}: {exc}")
                    continue
                await modlog.create_case(
                    self.bot, guild, now,
                    "softban" if what == "softban" else "kick",
                    target, author, reason,
                )
                done.append(target.display_name)

        elif what in ("voicekick", "voiceban", "voiceunban"):
            for target in members:
                state = target.voice
                if what == "voicekick":
                    if state is None or state.channel is None:
                        failed.append(f"{target.display_name}: not in a voice channel")
                        continue
                    if not await self._mod_allowed(guild, author, target):
                        failed.append(f"{target.display_name}: above you in the hierarchy")
                        continue
                    await target.move_to(None, reason=audit_reason)
                    case = "voicekick"
                elif what == "voiceban":
                    if state is None:
                        failed.append(f"{target.display_name}: not in a voice channel")
                        continue
                    await target.edit(mute=True, deafen=True, reason=audit_reason)
                    case = "voiceban"
                else:
                    if state is None:
                        failed.append(f"{target.display_name}: not in a voice channel")
                        continue
                    if not (state.mute or state.deaf):
                        failed.append(
                            f"{target.display_name}: not server muted or deafened"
                        )
                        continue
                    await target.edit(mute=False, deafen=False, reason=audit_reason)
                    case = "voiceunban"
                await modlog.create_case(
                    self.bot, guild, now, case, target, author, reason
                )
                done.append(target.display_name)
        else:
            return [{"message": f"Unknown action: {what}", "category": "warning"}]

        out = []
        if done:
            verb = {
                "ban": "banned",
                "massban": "banned",
                "tempban": "temporarily banned",
                "unban": "unbanned",
                "kick": "kicked",
                "softban": "softbanned",
                "voicekick": "disconnected from voice",
                "voiceban": "server muted and deafened",
                "voiceunban": "unmuted and undeafened",
            }[what]
            out.append({"message": f"{', '.join(done)} {verb}.", "category": "success"})
        for problem in failed:
            out.append({"message": problem, "category": "warning"})
        return out or [{"message": "Nothing happened.", "category": "info"}]

    async def _mod_allowed(
        self, guild: discord.Guild, author: discord.Member, target: discord.Member
    ) -> bool:
        from .utils import is_allowed_by_hierarchy

        return await is_allowed_by_hierarchy(self.bot, self.config, guild, author, target)

    async def _mod_lookup(self, guild: discord.Guild, field) -> tuple[list[dict], dict]:
        target = guild.get_member(field.integer("member_ids", 0) or 0)
        if target is None:
            return [{"message": "Pick a member.", "category": "warning"}], {}

        member_conf = await self.config.member(target).all()
        user_conf = await self.config.user(target).all()
        roles = [r.name for r in reversed(target.roles) if not r.is_default()]
        return [], {
            "name": target.display_name,
            "handle": str(target),
            "id": str(target.id),
            "avatar": str(target.display_avatar),
            "bot": target.bot,
            "created": target.created_at.strftime("%d %b %Y"),
            "joined": target.joined_at.strftime("%d %b %Y") if target.joined_at else "",
            "roles": roles,
            "top_role": target.top_role.name,
            "voice": getattr(getattr(target.voice, "channel", None), "name", ""),
            "timed_out": target.is_timed_out(),
            "past_nicks": member_conf.get("past_nicks") or [],
            "past_names": user_conf.get("past_names") or [],
            "past_display_names": user_conf.get("past_display_names") or [],
        }


MOD_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-gavel"></i> Moderation in {{ guild_name }}</h4>
    <p>
      {% if tempbans %}{{ tempbans|length }} active tempban(s).
      {% else %}No active tempbans.{% endif %}
      {% if ignored %} &middot; <b>this server is currently ignored</b>{% endif %}
    </p>
  </div>

  {{ subnav(name, [(none, 'Moderation', 'fa-gavel'),
                   ('modlog', 'Modlog', 'fa-book'),
                   ('eventlog', 'Event log', 'fa-file-text-o')], none, guild) }}

  {% if lookup %}
    <div class="dz-panel">
      <h5><i class="fa fa-user"></i> {{ lookup.name }}</h5>
      <div class="dz-row" style="align-items:flex-start;">
        <img class="dz-av" src="{{ lookup.avatar }}" alt=""
             style="width:84px; height:84px; border-radius:14px;" />
        <table class="dz-t" style="flex:1 1 260px;">
          <tr><th>Handle</th><td>{{ lookup.handle }}
              {% if lookup.bot %}<span class="dz-botpill">BOT</span>{% endif %}</td></tr>
          <tr><th>ID</th><td><code>{{ lookup.id }}</code></td></tr>
          <tr><th>Account created</th><td>{{ lookup.created }}</td></tr>
          <tr><th>Joined</th><td>{{ lookup.joined }}</td></tr>
          <tr><th>Top role</th><td>{{ lookup.top_role }}</td></tr>
          {% if lookup.voice %}<tr><th>Voice</th><td>{{ lookup.voice }}</td></tr>{% endif %}
          {% if lookup.timed_out %}
            <tr><th>Status</th><td><span class="dz-tag warn">timed out</span></td></tr>
          {% endif %}
        </table>
      </div>
      {% if lookup.roles %}
        <p class="dz-hint" style="margin-top:10px;">
          {% for r in lookup.roles %}<span class="dz-tag">{{ r }}</span> {% endfor %}
        </p>
      {% endif %}
      <div class="dz-grid three" style="margin-top:10px;">
        <div>
          <div class="dz-label">Past nicknames here</div>
          <p class="dz-hint">{{ lookup.past_nicks|join(', ') or 'none recorded' }}</p>
        </div>
        <div>
          <div class="dz-label">Past usernames</div>
          <p class="dz-hint">{{ lookup.past_names|join(', ') or 'none recorded' }}</p>
        </div>
        <div>
          <div class="dz-label">Past display names</div>
          <p class="dz-hint">{{ lookup.past_display_names|join(', ') or 'none recorded' }}</p>
        </div>
      </div>
    </div>
  {% endif %}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-bolt"></i> Take action</h5>
      <p class="dz-hint">
        Pick members from the list, or type raw IDs for people who already left
        or are already banned. Everything here files a modlog case.
      </p>
      <div class="dz-grid two">
        <div>
          <label class="dz-label">Members</label>
          {{ picker('member_ids', member_options, true, 10, 'Search members...') }}
          <label class="dz-label" style="margin-top:10px;">Or user IDs</label>
          <input class="dz-input" type="text" name="user_ids"
                 placeholder="123456789 987654321" />
        </div>
        <div>
          <label class="dz-label">Reason</label>
          <input class="dz-input" type="text" name="reason"
                 placeholder="shown in the modlog, the audit log and the DM" />
          <div class="dz-grid two" style="margin-top:10px;">
            <div>
              <label class="dz-label">Days of messages to delete</label>
              <input class="dz-input" type="number" min="0" max="7" name="delete_days"
                     placeholder="0" />
            </div>
            <div>
              <label class="dz-label">Tempban length (hours)</label>
              <input class="dz-input" type="number" min="0" name="tempban_hours"
                     placeholder="server default" />
            </div>
          </div>
          <label class="dz-label" style="margin-top:10px;">New nickname (for rename)</label>
          <input class="dz-input" type="text" name="nickname" maxlength="32"
                 placeholder="leave empty to clear" />
        </div>
      </div>
      <div class="dz-row dz-save">
        {{ confirm('Kick', 'act_kick', 'Kick the selected members?', '', 'fa-sign-out') }}
        {{ confirm('Ban', 'act_ban', 'Ban the selected members and IDs?') }}
        {{ confirm('Tempban', 'act_tempban', 'Temporarily ban the selected members?',
                   '', 'fa-clock-o') }}
        {{ confirm('Softban', 'act_softban',
                   'Ban and immediately unban, purging a day of their messages?',
                   '', 'fa-eraser') }}
        {{ confirm('Unban', 'act_unban', 'Unban the given IDs?', 'primary', 'fa-undo') }}
        <button class="dz-btn" name="action" value="act_rename">
          <i class="fa fa-pencil"></i> Rename
        </button>
      </div>
      <div class="dz-row">
        <button class="dz-btn" name="action" value="act_voicekick">
          <i class="fa fa-sign-out"></i> Disconnect from voice
        </button>
        {{ confirm('Voice ban', 'act_voiceban',
                   'Server mute and deafen the selected members?', '', 'fa-microphone-slash') }}
        <button class="dz-btn" name="action" value="act_voiceunban">
          <i class="fa fa-microphone"></i> Voice unban
        </button>
        <button class="dz-btn primary" name="action" value="lookup">
          <i class="fa fa-search"></i> Look up member
        </button>
      </div>
      <div class="dz-row" style="margin-top:12px;">
        <label class="dz-label" style="margin:0;">Slowmode</label>
        {{ picker('channel_id', channel_options, false, 6, 'Search channels...') }}
        <input class="dz-input" type="number" min="0" max="21600" name="slowmode_seconds"
               placeholder="seconds (0 = off)" style="max-width:190px;" />
        <button class="dz-btn" name="action" value="act_slowmode">
          <i class="fa fa-hourglass-half"></i> Apply slowmode
        </button>
      </div>
    </div>
  </form>

  {% if not is_admin %}
    <div class="dz-panel">
      <p class="dz-empty">Changing these settings needs administrator permissions.</p>
    </div>
  {% else %}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />

    <div class="dz-grid two">
      <div class="dz-panel">
        <h5><i class="fa fa-toggle-on"></i> Behaviour</h5>
        {% for t in toggles %}
          <div style="margin-bottom:9px;">
            <label class="dz-toggle" style="padding:0;">
              <input type="checkbox" name="t_{{ t.key }}" {% if t.on %}checked{% endif %} />
              <span>{{ t.label }}</span>
            </label>
            <div style="font-size:.72rem; opacity:.45; margin-left:26px;">{{ t.help }}</div>
          </div>
        {% endfor %}
        <div style="margin-top:11px; padding-top:11px;
                    border-top:1px solid rgba(255,255,255,.07);">
          <label class="dz-toggle" style="padding:0;">
            <input type="checkbox" name="ignored" {% if ignored %}checked{% endif %} />
            <span style="color:#f0aa3c;">Ignore this server entirely</span>
          </label>
          <div style="font-size:.72rem; opacity:.45; margin-left:26px;">
            The bot stops responding to moderation here.
          </div>
        </div>
      </div>

      <div class="dz-panel">
        <h5><i class="fa fa-sliders"></i> Thresholds</h5>
        {% for n in numeric %}
          <div style="margin-bottom:11px;">
            <div class="dz-label">{{ n.label }}</div>
            <input class="dz-input" type="number" min="{{ n.min }}" max="{{ n.max }}"
                   name="n_{{ n.key }}" value="{{ n.value }}" />
            <div style="font-size:.72rem; opacity:.45; margin-top:4px;">{{ n.help }}</div>
          </div>
        {% endfor %}
      </div>
    </div>

    <div class="dz-grid two" style="margin-top:14px;">
      <div class="dz-panel">
        <h5><i class="fa fa-at"></i> Mention spam</h5>
        <p class="dz-hint">
          Mentions in one message before acting. Blank disables that action.
          Values must ascend: warn &lt; kick &lt; ban.
        </p>
        {% for s in spam %}
          <div style="margin-bottom:9px;">
            <div class="dz-label">{{ s.label }} at</div>
            <input class="dz-input" type="number" min="0" name="s_{{ s.key }}"
                   value="{{ s.value }}" placeholder="disabled" />
          </div>
        {% endfor %}
        <label class="dz-toggle">
          <input type="checkbox" name="spam_strict" {% if spam_strict %}checked{% endif %} />
          <span>Count duplicate mentions of the same user</span>
        </label>
      </div>

      <div class="dz-panel">
        <h5><i class="fa fa-envelope-o"></i> Ban message</h5>
        <p class="dz-hint">Included when "attach a staff message" is on.</p>
        <div class="dz-label">Title</div>
        <input class="dz-input" type="text" name="ban_title" value="{{ ban_title }}" />
        <div class="dz-label" style="margin-top:10px;">Body</div>
        <textarea class="dz-area" name="ban_body">{{ ban_body }}</textarea>
      </div>
    </div>

    <div class="dz-save">
      <button class="dz-btn primary" name="action" value="save">
        <i class="fa fa-save"></i> Save settings
      </button>
    </div>
  </form>

  {% endif %}

  {% if tempbans %}
    <div class="dz-panel">
      <h5><i class="fa fa-clock-o"></i> Active tempbans</h5>
      <p class="dz-hint">Lift one early with the button, or wait for it to expire.</p>
      <table class="dz-t">
        <thead><tr><th>User</th><th>ID</th><th></th></tr></thead>
        <tbody>
          {% for b in tempbans %}
            <tr>
              <td>{{ b.name }}</td>
              <td style="opacity:.6;">{{ b.id }}</td>
              <td>
                <form method="POST">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="user_ids" value="{{ b.id }}" />
                  <input type="hidden" name="reason" value="Lifted from the dashboard" />
                  {{ confirm('Unban', 'act_unban',
                             'Lift the tempban on ' ~ b.name ~ '?', 'primary', 'fa-undo') }}
                </form>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  {% endif %}

  {% if is_owner %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-id-card-o"></i> Name history</h5>
        <p class="dz-hint">
          Bot-wide. Tracking records username and display name changes so the
          member lookup above can show them.
        </p>
        <label class="dz-toggle">
          <input type="checkbox" name="track_all_names"
                 {% if track_all_names %}checked{% endif %} />
          <span>Record username and display name changes</span>
        </label>
        <div class="dz-row dz-save">
          <button class="dz-btn primary" name="action" value="save_name_tracking">
            <i class="fa fa-save"></i> Save
          </button>
          {{ confirm('Delete all stored names', 'purge_names',
                     'Permanently delete every stored username, display name and nickname on the whole bot?') }}
        </div>
      </div>
    </form>
  {% endif %}
</div>
"""
)
