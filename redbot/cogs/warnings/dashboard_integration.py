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
    fake_context,
    form_reader,
    guild_member,
    is_staff,
    member_options,
)

log = logging.getLogger("red.warnings.dashboard")

TOGGLES = (
    ("toggle_dm", "DM the warned member", "Sends the warning to their DMs."),
    ("show_mod", "Show the moderator's name", "Includes who issued the warning."),
    ("toggle_channel", "Post warnings to a channel", "Uses the channel chosen below."),
    ("allow_custom_reasons", "Allow custom reasons",
     "Moderators may warn with a free-text reason instead of a registered one."),
    ("mywarnings_in_dms", "Send self-requested warnings by DM",
     "Applies to the mywarnings command."),
)


class DashboardIntegration:
    """Warnings, end to end.

    Issues and removes warnings (``[p]warn``, ``[p]unwarn``), shows a member's
    warning history (``[p]warnings``), lists reasons and actions
    (``[p]reasonlist``, ``[p]actionlist``) and covers every ``[p]warningset``,
    ``[p]warnreason`` and ``[p]warnaction`` option.
    """

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Warnings as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Issue warnings and configure reasons, actions and settings.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_warnings_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        staff = await is_staff(self.bot, user, member, guild)
        # `[p]warn` needs Ban Members; the settings need administrator.
        can_warn = staff or member.guild_permissions.ban_members
        if not can_warn:
            return {
                "status": 1,
                "error_title": "Forbidden",
                "error_message": "Only server moderators can manage warnings.",
            }

        notifications: list[dict] = []
        history: dict = {}
        if kwargs.get("method") == "POST":
            requested = form_reader(kwargs)("action")
            if requested in ("warn", "unwarn", "history"):
                notifications, history = await self._warn_action(
                    requested, member, guild, form_reader(kwargs)
                )
            else:
                notifications = await self._warn_handle_post(guild, staff, kwargs)

        settings = await self.config.guild(guild).all()
        reasons = settings.get("reasons") or {}
        actions = settings.get("actions") or []

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": WARNINGS_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_admin": staff,
                "history": history,
                "member_options": member_options(guild, humans_only=True),
                "toggles": [
                    {"key": k, "label": lbl, "help": h, "on": bool(settings.get(k))}
                    for k, lbl, h in TOGGLES
                ],
                "channels": channel_options(guild, selected=settings.get("warn_channel")),
                "reasons": [
                    {
                        "name": name,
                        "points": (data or {}).get("points", 0),
                        "description": (data or {}).get("description", ""),
                    }
                    for name, data in sorted(reasons.items())
                ],
                # Stored descending by points; the highest matching action wins.
                "actions": [
                    {
                        "name": a.get("action_name", ""),
                        "points": a.get("points", 0),
                        "exceed": a.get("exceed_command", ""),
                        "drop": a.get("drop_command", ""),
                    }
                    for a in actions
                ],
                "members": await self._warn_members(guild),
            },
        }

    async def _warn_members(self, guild: discord.Guild, limit: int = 20) -> list[dict]:
        try:
            data = await self.config.all_members(guild)
        except Exception:  # noqa: BLE001
            log.exception("Could not read member warnings")
            return []
        rows = []
        for member_id, stats in data.items():
            warnings = stats.get("warnings") or {}
            if not warnings:
                continue
            member = guild.get_member(member_id)
            rows.append(
                {
                    "name": member.display_name if member else f"Unknown ({member_id})",
                    "count": len(warnings),
                    "points": stats.get("total_points", 0),
                }
            )
        rows.sort(key=lambda r: -r["points"])
        return rows[:limit]

    async def _warn_action(
        self, action: str, author: discord.Member, guild: discord.Guild, field
    ) -> tuple[list[dict], dict]:
        from .helpers import warning_points_add_check, warning_points_remove_check

        target = guild.get_member(field.integer("member_id", 0) or 0)
        if target is None:
            return [{"message": "Pick a member.", "category": "warning"}], {}

        if action == "history":
            return [], await self._warn_history(guild, target)

        settings = await self.config.guild(guild).all()
        member_settings = self.config.member(target)

        try:
            if action == "warn":
                if target == author:
                    return [
                        {"message": "You cannot warn yourself.", "category": "warning"}
                    ], {}
                if target.bot:
                    return [
                        {"message": "You cannot warn a bot.", "category": "warning"}
                    ], {}
                if target == guild.owner:
                    return [
                        {"message": "You cannot warn the server owner.",
                         "category": "warning"}
                    ], {}
                if target.top_role >= author.top_role and author != guild.owner:
                    return [
                        {
                            "message": "That member is equal to or above you in the "
                            "hierarchy.",
                            "category": "warning",
                        }
                    ], {}

                registered = settings.get("reasons") or {}
                chosen = (field("reason_name") or "").strip().lower()
                if chosen and chosen in registered:
                    reason_type = registered[chosen]
                else:
                    if not settings.get("allow_custom_reasons"):
                        return [
                            {
                                "message": "That is not a registered reason, and custom "
                                "reasons are turned off for this server.",
                                "category": "warning",
                            }
                        ], {}
                    description = (field("custom_reason") or "").strip()
                    if not description:
                        return [
                            {"message": "Enter a reason.", "category": "warning"}
                        ], {}
                    points = field.integer("points", 1) or 1
                    if points < 0:
                        return [
                            {"message": "Points cannot be negative.", "category": "warning"}
                        ], {}
                    reason_type = {"description": description, "points": points}

                now = datetime.now(tz=timezone.utc)
                # The warning key is the invoking message ID in Discord; a
                # timestamp snowflake keeps the same shape and stays unique.
                warn_id = str(discord.utils.time_snowflake(now))
                async with member_settings.warnings() as user_warnings:
                    user_warnings[warn_id] = {
                        "points": reason_type["points"],
                        "description": reason_type["description"],
                        "mod": author.id,
                    }
                total = (await member_settings.total_points()) + reason_type["points"]
                await member_settings.total_points.set(total)

                notes = []
                if settings.get("toggle_dm"):
                    title = (
                        f"Warning from {author}"
                        if settings.get("show_mod")
                        else "Warning"
                    )
                    embed = discord.Embed(
                        title=title,
                        description=reason_type["description"],
                        color=await self.bot.get_embed_color(target),
                    )
                    embed.add_field(name="Points", value=str(reason_type["points"]))
                    try:
                        await target.send(
                            f"You have received a warning in {guild.name}.", embed=embed
                        )
                    except discord.HTTPException:
                        notes.append(
                            {"message": "I could not DM them the warning.",
                             "category": "warning"}
                        )

                if settings.get("toggle_channel"):
                    channel = guild.get_channel(settings.get("warn_channel") or 0)
                    if channel is not None and channel.permissions_for(
                        guild.me
                    ).send_messages:
                        embed = discord.Embed(
                            title="Warning",
                            description=reason_type["description"],
                            color=await self.bot.get_embed_color(channel),
                        )
                        embed.add_field(name="Points", value=str(reason_type["points"]))
                        await channel.send(
                            f"{target.mention} has been warned.", embed=embed
                        )

                await modlog.create_case(
                    self.bot, guild, now, "warning", target, author,
                    f"{reason_type['description']}\nPoints: {reason_type['points']}",
                )

                # Automated actions are configured as command strings, so they
                # need a Context to run in.
                context = await fake_context(self.bot, author, "warn")
                if context is not None:
                    await warning_points_add_check(self.config, context, target, total)
                else:
                    notes.append(
                        {
                            "message": "The warning was recorded, but I could not run "
                            "the automated action; no channel I can talk in.",
                            "category": "warning",
                        }
                    )

                return notes + [
                    {
                        "message": f"{target.display_name} warned; they now have "
                        f"{total} point(s).",
                        "category": "success",
                    }
                ], await self._warn_history(guild, target)

            if action == "unwarn":
                warn_id = (field("warn_id") or "").strip()
                if target == author:
                    return [
                        {"message": "You cannot remove your own warnings.",
                         "category": "warning"}
                    ], {}
                total = await member_settings.total_points()
                async with member_settings.warnings() as user_warnings:
                    if warn_id not in user_warnings:
                        return [
                            {"message": "That warning no longer exists.",
                             "category": "warning"}
                        ], {}
                    total -= user_warnings[warn_id]["points"]
                    user_warnings.pop(warn_id)
                await member_settings.total_points.set(total)

                context = await fake_context(self.bot, author, "unwarn")
                if context is not None:
                    await warning_points_remove_check(self.config, context, target, total)

                await modlog.create_case(
                    self.bot, guild, datetime.now(tz=timezone.utc), "unwarned",
                    target, author, (field("reason") or "").strip() or None,
                )
                return [
                    {
                        "message": f"Warning removed; {target.display_name} now has "
                        f"{total} point(s).",
                        "category": "success",
                    }
                ], await self._warn_history(guild, target)
        except discord.Forbidden:
            return [
                {"message": "Discord refused that action; check my permissions.",
                 "category": "danger"}
            ], {}
        except Exception as exc:  # noqa: BLE001
            log.exception("Warnings dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}], {}

        return [{"message": f"Unknown action: {action}", "category": "warning"}], {}

    async def _warn_history(self, guild: discord.Guild, target: discord.Member) -> dict:
        data = await self.config.member(target).all()
        entries = []
        for warn_id, warning in (data.get("warnings") or {}).items():
            moderator = guild.get_member(warning.get("mod") or 0)
            entries.append(
                {
                    "id": warn_id,
                    "points": warning.get("points", 0),
                    "description": warning.get("description", ""),
                    "mod": getattr(moderator, "display_name", None) or "Unknown",
                    "when": discord.utils.snowflake_time(int(warn_id)).strftime(
                        "%d %b %Y, %H:%M"
                    )
                    if warn_id.isdigit()
                    else "",
                }
            )
        entries.sort(key=lambda e: e["id"], reverse=True)
        return {
            "member": target.display_name,
            "member_id": str(target.id),
            "total": data.get("total_points", 0),
            "entries": entries,
        }

    async def _warn_handle_post(
        self, guild: discord.Guild, staff: bool, kwargs: dict
    ) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")
        conf = self.config.guild(guild)

        if not staff:
            return [
                {
                    "message": "Only server administrators can change warning settings.",
                    "category": "danger",
                }
            ]

        try:
            if action == "save_settings":
                for key, _lbl, _h in TOGGLES:
                    await conf.get_attr(key).set(field.checked(f"t_{key}"))
                raw = field("warn_channel") or ""
                channel_id = int(raw) if raw.isdigit() else None
                warnings = []
                if channel_id is not None:
                    channel = guild.get_channel(channel_id)
                    if channel is not None and not channel.permissions_for(guild.me).send_messages:
                        warnings.append(
                            {
                                "message": f"I cannot send messages in #{channel.name}.",
                                "category": "warning",
                            }
                        )
                await conf.warn_channel.set(channel_id)
                return warnings + [{"message": "Settings saved.", "category": "success"}]

            if action == "add_reason":
                name = (field("name") or "").strip().lower()
                description = (field("description") or "").strip()
                raw_points = (field("points") or "").strip()
                if not name or not description:
                    return [{"message": "Name and description are required.", "category": "warning"}]
                # The cog treats "custom" as a sentinel, so it cannot be a reason.
                if name == "custom":
                    return [
                        {"message": "'custom' is reserved and cannot be a reason name.",
                         "category": "warning"}
                    ]
                try:
                    points = int(raw_points)
                except ValueError:
                    return [{"message": f"'{raw_points}' is not a number.", "category": "danger"}]
                if points < 0:
                    return [{"message": "Points cannot be negative.", "category": "warning"}]
                async with conf.reasons() as reasons:
                    reasons[name] = {"points": points, "description": description}
                return [{"message": f"Reason '{name}' saved.", "category": "success"}]

            if action == "delete_reason":
                name = (field("name") or "").strip().lower()
                async with conf.reasons() as reasons:
                    if reasons.pop(name, None) is None:
                        return [{"message": f"No reason named '{name}'.", "category": "warning"}]
                return [{"message": f"Reason '{name}' deleted.", "category": "success"}]

            if action == "add_action":
                name = (field("name") or "").strip()
                raw_points = (field("points") or "").strip()
                exceed = (field("exceed") or "").strip()
                drop = (field("drop") or "").strip()
                if not name:
                    return [{"message": "An action name is required.", "category": "warning"}]
                try:
                    points = int(raw_points)
                except ValueError:
                    return [{"message": f"'{raw_points}' is not a number.", "category": "danger"}]
                if not exceed and not drop:
                    return [
                        {"message": "Set at least one command to run.", "category": "warning"}
                    ]
                async with conf.actions() as actions:
                    if any(a.get("action_name") == name for a in actions):
                        return [{"message": "That action name already exists.", "category": "warning"}]
                    actions.append(
                        {
                            "action_name": name,
                            "points": points,
                            "exceed_command": exceed,
                            "drop_command": drop,
                        }
                    )
                    # The cog scans this list in order and takes the first match,
                    # so it must stay sorted by points descending.
                    actions.sort(key=lambda a: a.get("points", 0), reverse=True)
                return [{"message": f"Action '{name}' added.", "category": "success"}]

            if action == "delete_action":
                name = (field("name") or "").strip()
                async with conf.actions() as actions:
                    remaining = [a for a in actions if a.get("action_name") != name]
                    if len(remaining) == len(actions):
                        return [{"message": f"No action named '{name}'.", "category": "warning"}]
                    actions.clear()
                    actions.extend(remaining)
                return [{"message": f"Action '{name}' deleted.", "category": "success"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("Warnings dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]


WARNINGS_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-exclamation-triangle"></i> Warnings in {{ guild_name }}</h4>
    <p>
      {{ reasons|length }} reason(s) &middot; {{ actions|length }} automated action(s)
      &middot; {{ members|length }} member(s) with warnings
    </p>
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-gavel"></i> Warn a member</h5>
      <p class="dz-hint">Pick a registered reason, or write a custom one if this
         server allows them. Points drive the automated actions below.</p>
      <div class="dz-grid two">
        <div>
          <label class="dz-label">Member</label>
          {{ picker('member_id', member_options, false, 8, 'Search members...') }}
          <label class="dz-label" style="margin-top:10px;">Registered reason</label>
          <select class="dz-select" name="reason_name">
            <option value="">&mdash; custom reason &mdash;</option>
            {% for r in reasons %}
              <option value="{{ r.name }}">{{ r.name }} ({{ r.points }} pts)</option>
            {% endfor %}
          </select>
        </div>
        <div>
          <label class="dz-label">Custom reason</label>
          <input class="dz-input" type="text" name="custom_reason"
                 placeholder="used when no registered reason is chosen" />
          <label class="dz-label" style="margin-top:10px;">Points for a custom reason</label>
          <input class="dz-input" type="number" min="0" name="points" value="1" />
        </div>
      </div>
      <div class="dz-row dz-save">
        {{ confirm('Warn', 'warn', 'Issue this warning?', 'primary',
                   'fa-exclamation-triangle') }}
        <button class="dz-btn" name="action" value="history">
          <i class="fa fa-history"></i> Show their warnings
        </button>
      </div>
    </div>
  </form>

  {% if history %}
    <div class="dz-panel">
      <h5><i class="fa fa-history"></i> {{ history.member }} &mdash;
          {{ history.total }} point(s)</h5>
      {% if history.entries %}
        <table class="dz-t">
          <tr><th>When</th><th>Points</th><th>Reason</th><th>By</th><th></th></tr>
          {% for w in history.entries %}
            <tr>
              <td>{{ w.when }}</td>
              <td>{{ w.points }}</td>
              <td>{{ w.description }}</td>
              <td>{{ w.mod }}</td>
              <td>
                <form method="POST">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="member_id" value="{{ history.member_id }}" />
                  <input type="hidden" name="warn_id" value="{{ w.id }}" />
                  {{ confirm('', 'unwarn', 'Remove this warning?') }}
                </form>
              </td>
            </tr>
          {% endfor %}
        </table>
      {% else %}
        <p class="dz-empty">No warnings on record.</p>
      {% endif %}
    </div>
  {% endif %}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-cog"></i> Settings</h5>
      <div class="dz-grid two">
        <div>
          {% for t in toggles %}
            <div style="margin-bottom:9px;">
              <label class="dz-toggle" style="padding:0;">
                <input type="checkbox" name="t_{{ t.key }}" {% if t.on %}checked{% endif %} />
                <span>{{ t.label }}</span>
              </label>
              <div style="font-size:.72rem; opacity:.45; margin-left:26px;">{{ t.help }}</div>
            </div>
          {% endfor %}
        </div>
        <div>
          <div class="dz-label">Warning channel</div>
          <select class="dz-select" name="warn_channel">
            <option value="">&mdash; where the command was used &mdash;</option>
            {% for c in channels %}
              <option value="{{ c.id }}" {% if c.selected %}selected{% endif %}>{{ c.name }}</option>
            {% endfor %}
          </select>
          <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
            Only used when "post warnings to a channel" is on.
          </div>
        </div>
      </div>
      <div style="margin-top:13px;">
        <button class="dz-btn primary" name="action" value="save_settings">
          <i class="fa fa-save"></i> Save settings
        </button>
      </div>
    </div>
  </form>

  <div class="dz-panel">
    <h5><i class="fa fa-tags"></i> Reasons</h5>
    <p class="dz-hint">Named reasons carry a fixed point value.</p>
    {% if reasons %}
      <table class="dz-t">
        <thead><tr><th>Name</th><th>Points</th><th>Description</th><th></th></tr></thead>
        <tbody>
          {% for r in reasons %}
            <tr>
              <td><b>{{ r.name }}</b></td>
              <td style="opacity:.7;">{{ r.points }}</td>
              <td style="opacity:.75;">{{ r.description }}</td>
              <td style="width:1%;">
                <form method="POST">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="name" value="{{ r.name }}" />
                  <button class="dz-btn round danger" name="action" value="delete_reason"
                          title="Delete"><i class="fa fa-trash-o"></i></button>
                </form>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="dz-empty">No reasons registered.</p>
    {% endif %}

    <form method="POST" class="dz-row" style="margin-top:11px;">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <input class="dz-input" style="flex:1 1 140px;" type="text" name="name" placeholder="name" />
      <input class="dz-input" style="flex:0 1 90px;" type="number" min="0" name="points"
             placeholder="points" />
      <input class="dz-input" style="flex:2 1 240px;" type="text" name="description"
             placeholder="description" />
      <button class="dz-btn primary" name="action" value="add_reason">
        <i class="fa fa-plus"></i> Save reason
      </button>
    </form>
  </div>

  <div class="dz-panel">
    <h5><i class="fa fa-bolt"></i> Automated actions</h5>
    <p class="dz-hint">
      Run a command when a member's points cross a threshold. Listed highest
      first &mdash; the first match is the one that runs.
    </p>
    {% if actions %}
      <table class="dz-t">
        <thead><tr><th>Name</th><th>At points</th><th>On exceed</th><th>On drop</th><th></th></tr></thead>
        <tbody>
          {% for a in actions %}
            <tr>
              <td><b>{{ a.name }}</b></td>
              <td style="opacity:.7;">{{ a.points }}</td>
              <td><code style="font-size:.76rem;">{{ a.exceed or "-" }}</code></td>
              <td><code style="font-size:.76rem;">{{ a.drop or "-" }}</code></td>
              <td style="width:1%;">
                <form method="POST">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="name" value="{{ a.name }}" />
                  <button class="dz-btn round danger" name="action" value="delete_action"
                          title="Delete"><i class="fa fa-trash-o"></i></button>
                </form>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="dz-empty">No automated actions.</p>
    {% endif %}

    <form method="POST" style="margin-top:11px;">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-row">
        <input class="dz-input" style="flex:1 1 150px;" type="text" name="name" placeholder="action name" />
        <input class="dz-input" style="flex:0 1 100px;" type="number" name="points" placeholder="points" />
      </div>
      <div class="dz-row" style="margin-top:8px;">
        <input class="dz-input" style="flex:1 1 220px;" type="text" name="exceed"
               placeholder="command on exceed, e.g. mute {member}" />
        <input class="dz-input" style="flex:1 1 220px;" type="text" name="drop"
               placeholder="command on drop, e.g. unmute {member}" />
        <button class="dz-btn primary" name="action" value="add_action">
          <i class="fa fa-plus"></i> Add action
        </button>
      </div>
    </form>
  </div>

  <div class="dz-panel">
    <h5><i class="fa fa-users"></i> Members with warnings</h5>
    {% if members %}
      <table class="dz-t">
        <thead><tr><th>Member</th><th>Warnings</th><th>Points</th></tr></thead>
        <tbody>
          {% for m in members %}
            <tr>
              <td>{{ m.name }}</td>
              <td style="opacity:.7;">{{ m.count }}</td>
              <td><b>{{ m.points }}</b></td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
      <p class="dz-hint" style="margin-top:9px;">
        Issue and remove warnings with the warn and unwarn commands.
      </p>
    {% else %}
      <p class="dz-empty">Nobody has been warned.</p>
    {% endif %}
  </div>
</div>
"""
)
