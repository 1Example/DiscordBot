from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
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


class DashboardIntegration:
    """Moderation behaviour, mention-spam thresholds and active tempbans."""

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Mod as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Moderation settings for this server.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_mod_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        if not await is_staff(self.bot, user, member, guild):
            return {
                "status": 1,
                "error_title": "Forbidden",
                "error_message": "Only server administrators can change moderation settings.",
            }

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._mod_handle_post(guild, kwargs)

        settings = await self.config.guild(guild).all()
        spam = settings.get("mention_spam") or {}

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": MOD_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
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

    async def _mod_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")
        conf = self.config.guild(guild)

        try:
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

                return errors + [{"message": "Moderation settings saved.", "category": "success"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("Mod dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]


MOD_TEMPLATE = (
    BASE_CSS
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

  {% if tempbans %}
    <div class="dz-panel">
      <h5><i class="fa fa-clock-o"></i> Active tempbans</h5>
      <p class="dz-hint">Lift these with the unban command in Discord.</p>
      <table class="dz-t">
        <thead><tr><th>User</th><th>ID</th></tr></thead>
        <tbody>
          {% for b in tempbans %}
            <tr><td>{{ b.name }}</td><td style="opacity:.6;">{{ b.id }}</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  {% endif %}
</div>
"""
)
