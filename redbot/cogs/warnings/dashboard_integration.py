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
    """Warning reasons, automated actions and the server warning list."""

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Warnings as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Warning reasons, actions and settings.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_warnings_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        if not await is_staff(self.bot, user, member, guild):
            return {
                "status": 1,
                "error_title": "Forbidden",
                "error_message": "Only server administrators can manage warnings.",
            }

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._warn_handle_post(guild, kwargs)

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

    async def _warn_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")
        conf = self.config.guild(guild)

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
