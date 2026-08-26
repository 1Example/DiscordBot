from __future__ import annotations

import logging
import typing as t
from datetime import datetime, timezone

import discord
from redbot.core import commands, modlog

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    channel_options,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
    query_reader,
)

log = logging.getLogger("red.modlog.dashboard")

PAGE_SIZE = 50


class DashboardIntegration:
    """Case browsing and modlog settings from the dashboard.

    Covers ``[p]case``, ``[p]casesfor``, ``[p]listcases`` and ``[p]reason``, plus
    the ``[p]modlogset`` settings: the modlog channel, which case types are
    logged, and resetting the case history.
    """

    bot: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering ModLog as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Browse moderation cases and configure the modlog.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_modlog_page(
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
                    "error_message": "Only server moderators can use the modlog.",
                }
            notifications = await self._modlog_handle_post(member, guild, staff, kwargs)

        # The search form is a GET, so its values arrive as query arguments.
        query_arg = query_reader(kwargs)
        query = (query_arg("q") or "").strip()
        wanted_type = query_arg("case_type") or ""

        cases = await modlog.get_all_cases(guild, self.bot)
        cases.sort(key=lambda c: c.case_number, reverse=True)
        rows = [self._modlog_case_row(c) for c in cases]

        if wanted_type:
            rows = [r for r in rows if r["action_type"] == wanted_type]
        if query:
            needle = query.lower()
            rows = [
                r
                for r in rows
                if needle in r["user"].lower()
                or needle in (r["reason"] or "").lower()
                or needle in r["moderator"].lower()
                or needle == str(r["number"])
                or needle == r["user_id"]
            ]

        total = len(rows)
        page = max(1, query_arg.integer("page", 1) or 1)
        start = (page - 1) * PAGE_SIZE
        visible = rows[start : start + PAGE_SIZE]

        casetypes = await modlog.get_all_casetypes(guild)
        casetype_rows = []
        for casetype in sorted(casetypes, key=lambda c: c.name):
            casetype_rows.append(
                {
                    "name": casetype.name,
                    "label": casetype.case_str,
                    "image": casetype.image,
                    "enabled": await casetype.is_enabled(),
                    "count": sum(1 for c in cases if c.action_type == casetype.name),
                }
            )

        try:
            channel = await modlog.get_modlog_channel(guild)
        except RuntimeError:
            channel = None

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": MODLOG_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_mod": is_mod,
                "is_admin": staff,
                "cases": visible,
                "total": total,
                "all_total": len(cases),
                "page": page,
                "pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
                "query": query,
                "wanted_type": wanted_type,
                "casetypes": casetype_rows,
                "modlog_channel": channel.name if channel else "",
                "channel_options": channel_options(
                    guild,
                    selected=channel.id if channel else None,
                    require_send=True,
                ),
            },
        }

    def _modlog_case_row(self, case) -> dict:
        def display(entity) -> tuple[str, str]:
            if entity is None:
                return "Unknown", ""
            if isinstance(entity, int):
                return f"ID {entity}", str(entity)
            return str(entity), str(getattr(entity, "id", ""))

        user_name, user_id = display(case.user)
        if user_name.startswith("ID ") and case.last_known_username:
            user_name = f"{case.last_known_username} ({user_id})"
        mod_name, _mod_id = display(case.moderator)

        created = datetime.fromtimestamp(case.created_at, tz=timezone.utc)
        until = (
            datetime.fromtimestamp(case.until, tz=timezone.utc).strftime("%d %b %Y, %H:%M")
            if case.until
            else ""
        )
        channel = case.channel
        return {
            "number": case.case_number,
            "action_type": case.action_type,
            "action": case.action_type.replace("_", " ").title(),
            "user": user_name,
            "user_id": user_id,
            "moderator": mod_name,
            "reason": case.reason or "",
            "created": created.strftime("%d %b %Y, %H:%M"),
            "until": until,
            "channel": getattr(channel, "name", str(channel) if channel else ""),
            "amended_by": str(case.amended_by) if case.amended_by else "",
        }

    async def _modlog_handle_post(
        self, member: discord.Member, guild: discord.Guild, staff: bool, kwargs: dict
    ) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

        try:
            if action == "reason":
                number = field.integer("case_number", 0) or 0
                reason = (field("reason") or "").strip()
                if not reason:
                    return [{"message": "Enter a reason.", "category": "warning"}]
                try:
                    case = await modlog.get_case(number, guild, self.bot)
                except RuntimeError:
                    return [{"message": f"Case #{number} does not exist.",
                             "category": "warning"}]
                # Same rule as `[p]reason`: mods, the server owner, or the
                # moderator who opened the case.
                allowed = (
                    staff
                    or member == guild.owner
                    or member == case.moderator
                    or await self.bot.is_mod(member)
                )
                if not allowed:
                    return [
                        {"message": "You are not authorised to edit that case.",
                         "category": "danger"}
                    ]
                to_modify: dict[str, t.Any] = {"reason": reason}
                if case.moderator != member:
                    to_modify["amended_by"] = member
                to_modify["modified_at"] = datetime.now(timezone.utc).timestamp()
                await case.edit(to_modify)
                return [{"message": f"Reason for case #{number} updated.",
                         "category": "success"}]

            # Everything below changes server-wide settings.
            if not staff:
                return [
                    {"message": "Only server administrators can change modlog settings.",
                     "category": "danger"}
                ]

            if action == "set_channel":
                channel_id = field.integer("channel_id", 0) or 0
                channel = guild.get_channel(channel_id) if channel_id else None
                await modlog.set_modlog_channel(guild, channel)
                if channel is None:
                    return [{"message": "Modlog channel cleared; cases are still recorded.",
                             "category": "success"}]
                return [
                    {"message": f"Cases will be posted in #{channel.name}.",
                     "category": "success"}
                ]

            if action == "save_casetypes":
                enabled = set(field.many("casetypes"))
                changed = 0
                for casetype in await modlog.get_all_casetypes(guild):
                    want = casetype.name in enabled
                    if await casetype.is_enabled() != want:
                        await casetype.set_enabled(want)
                        changed += 1
                return [
                    {"message": f"{changed} case type(s) updated.", "category": "success"}
                ]

            if action == "reset_cases":
                await modlog.reset_cases(guild)
                return [{"message": "Every modlog case was deleted.", "category": "success"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("ModLog dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]


MODLOG_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-book"></i> Modlog for {{ guild_name }}</h4>
    <p>Browse every case, correct a reason, and choose what gets logged where.</p>
  </div>

  {{ stats([('Cases', all_total),
            ('Matching filter', total),
            ('Logged types', casetypes|selectattr('enabled')|list|length),
            ('Channel', modlog_channel or 'not set')]) }}

  {% if not is_mod %}
    <div class="dz-panel">
      <p class="dz-empty">You need moderator permissions to view the modlog.</p>
    </div>
  {% else %}

  <form method="GET">
    <div class="dz-panel">
      <h5><i class="fa fa-search"></i> Find cases</h5>
      <p class="dz-hint">Search by member, moderator, reason or case number.</p>
      <div class="dz-row">
        <input class="dz-input" type="text" name="q" value="{{ query }}"
               placeholder="e.g. spam, 42, or a user ID" style="flex:1 1 220px;" />
        <select class="dz-select" name="case_type" style="max-width:220px;">
          <option value="">All case types</option>
          {% for ct in casetypes %}
            <option value="{{ ct.name }}" {% if ct.name == wanted_type %}selected{% endif %}>
              {{ ct.label }} ({{ ct.count }})
            </option>
          {% endfor %}
        </select>
        <button class="dz-btn primary" type="submit"><i class="fa fa-search"></i> Search</button>
      </div>
    </div>
  </form>

  <div class="dz-panel">
    <h5><i class="fa fa-list"></i> Cases</h5>
    {% if cases %}
      <table class="dz-t">
        <tr><th>#</th><th>Action</th><th>Member</th><th>Moderator</th>
            <th>When</th><th>Reason</th></tr>
        {% for c in cases %}
          <tr>
            <td><b>{{ c.number }}</b></td>
            <td>{{ c.action }}
                {% if c.until %}<span class="dz-tag">until {{ c.until }}</span>{% endif %}</td>
            <td>{{ c.user }}</td>
            <td>{{ c.moderator }}</td>
            <td>{{ c.created }}</td>
            <td>
              <form method="POST" class="dz-row" style="gap:6px;">
                <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                <input type="hidden" name="case_number" value="{{ c.number }}" />
                <input class="dz-input" type="text" name="reason" value="{{ c.reason }}"
                       placeholder="no reason given" style="flex:1 1 200px;" />
                <button class="dz-btn" name="action" value="reason" title="Update reason">
                  <i class="fa fa-pencil"></i>
                </button>
              </form>
              {% if c.amended_by %}
                <span class="dz-hint">amended by {{ c.amended_by }}</span>
              {% endif %}
            </td>
          </tr>
        {% endfor %}
      </table>
      {% if pages > 1 %}
        <div class="dz-row dz-save">
          {% if page > 1 %}
            <a class="dz-btn" href="?q={{ query }}&case_type={{ wanted_type }}&page={{ page - 1 }}">
              <i class="fa fa-chevron-left"></i> Newer
            </a>
          {% endif %}
          <span class="dz-hint">Page {{ page }} of {{ pages }}</span>
          {% if page < pages %}
            <a class="dz-btn" href="?q={{ query }}&case_type={{ wanted_type }}&page={{ page + 1 }}">
              Older <i class="fa fa-chevron-right"></i>
            </a>
          {% endif %}
        </div>
      {% endif %}
    {% else %}
      <p class="dz-empty">No cases match.</p>
    {% endif %}
  </div>

  {% if is_admin %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-hashtag"></i> Modlog channel</h5>
        <p class="dz-hint">Where new cases are posted. Leaving this unset still
           records cases; they just are not announced.</p>
        {{ picker('channel_id', channel_options, false, 8, 'Search channels...',
                  true, 'do not post cases') }}
        <div class="dz-save">
          <button class="dz-btn primary" name="action" value="set_channel">
            <i class="fa fa-save"></i> Save channel
          </button>
        </div>
      </div>
    </form>

    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-toggle-on"></i> Logged case types</h5>
        <p class="dz-hint">Untick a type to stop creating cases for it.</p>
        <div class="dz-grid two">
          {% for ct in casetypes %}
            <label class="dz-toggle">
              <input type="checkbox" name="casetypes" value="{{ ct.name }}"
                     {% if ct.enabled %}checked{% endif %} />
              <span>{{ ct.image }} {{ ct.label }}
                <span class="dz-tag">{{ ct.count }}</span></span>
            </label>
          {% endfor %}
        </div>
        <div class="dz-row dz-save">
          <button class="dz-btn primary" name="action" value="save_casetypes">
            <i class="fa fa-save"></i> Save case types
          </button>
          {{ confirm('Delete all cases', 'reset_cases',
                     'Permanently delete every modlog case in this server?') }}
        </div>
      </div>
    </form>
  {% endif %}

  {% endif %}
</div>
"""
)
