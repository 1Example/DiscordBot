from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import bank, commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    channel_options,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
)

log = logging.getLogger("red.hunting.dashboard")

MIN_INTERVAL = 10
MAX_INTERVAL = 86400


class DashboardIntegration:
    """Where birds spawn, how often, and the server scoreboard."""

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Hunting as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Hunting channels, timing and scores.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_hunting_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        staff = await is_staff(self.bot, user, member, guild)

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            if not staff:
                return {
                    "status": 1,
                    "error_title": "Forbidden",
                    "error_message": "Only server administrators can change hunting.",
                }
            notifications = await self._hunt_handle_post(guild, kwargs)

        settings = await self.config.guild(guild).all()
        active_ids = [str(i) for i in (settings.get("channels") or [])]
        reward = settings.get("reward_range") or []

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": HUNTING_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_staff": staff,
                "channels": channel_options(guild),
                "active_ids": active_ids,
                "interval_min": settings.get("hunt_interval_minimum", 900),
                "interval_max": settings.get("hunt_interval_maximum", 3600),
                "timeout": settings.get("wait_for_bang_timeout", 20),
                "bang_time": bool(settings.get("bang_time")),
                "bang_words": bool(settings.get("bang_words")),
                "eagle": bool(settings.get("eagle")),
                "reward_low": reward[0] if len(reward) > 1 else "",
                "reward_high": reward[1] if len(reward) > 1 else "",
                "currency": await self._hunt_currency(guild),
                "scores": await self._hunt_scores(guild),
            },
        }

    async def _hunt_currency(self, guild: discord.Guild) -> str:
        try:
            return await bank.get_currency_name(guild)
        except Exception:  # noqa: BLE001
            return "credits"

    async def _hunt_scores(self, guild: discord.Guild, limit: int = 15) -> list[dict]:
        try:
            data = await self.config.all_users()
        except Exception:  # noqa: BLE001
            log.exception("Could not read hunting scores")
            return []
        rows = []
        for user_id, stats in data.items():
            member = guild.get_member(user_id)
            if member is None:
                # all_users is bot-wide; only show people actually in this guild.
                continue
            score = stats.get("score") or {}
            rows.append(
                {
                    "name": member.display_name,
                    "total": stats.get("total", 0),
                    "birds": ", ".join(f"{k}: {v}" for k, v in sorted(score.items())) or "-",
                }
            )
        rows.sort(key=lambda r: -r["total"])
        for position, row in enumerate(rows[:limit], start=1):
            row["position"] = position
        return rows[:limit]

    async def _hunt_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        if field("action") != "save":
            return [{"message": "Unknown action.", "category": "warning"}]

        conf = self.config.guild(guild)
        errors: list[dict] = []

        channel_ids = [int(x) for x in field.many("channels") if str(x).isdigit()]
        await conf.channels.set(channel_ids)

        def number(form_key, label, low, high):
            raw = (field(form_key) or "").strip()
            if raw == "":
                return None
            try:
                value = int(raw)
            except ValueError:
                errors.append({"message": f"{label}: '{raw}' is not a number.", "category": "danger"})
                return None
            if not low <= value <= high:
                errors.append(
                    {"message": f"{label}: must be between {low} and {high}.", "category": "danger"}
                )
                return None
            return value

        low = number("interval_min", "Minimum interval", MIN_INTERVAL, MAX_INTERVAL)
        high = number("interval_max", "Maximum interval", MIN_INTERVAL, MAX_INTERVAL)
        # An inverted range makes the spawn timer misbehave rather than error.
        if low is not None and high is not None and low > high:
            errors.append(
                {"message": "Minimum interval cannot exceed the maximum.", "category": "danger"}
            )
        else:
            if low is not None:
                await conf.hunt_interval_minimum.set(low)
            if high is not None:
                await conf.hunt_interval_maximum.set(high)

        timeout = number("timeout", "Bang timeout", 1, 600)
        if timeout is not None:
            await conf.wait_for_bang_timeout.set(timeout)

        reward_low = number("reward_low", "Minimum reward", 0, 1000000)
        reward_high = number("reward_high", "Maximum reward", 0, 1000000)
        if reward_low is not None and reward_high is not None:
            if reward_low > reward_high:
                errors.append(
                    {"message": "Minimum reward cannot exceed the maximum.", "category": "danger"}
                )
            else:
                await conf.reward_range.set([reward_low, reward_high])
        elif (field("reward_low") or "").strip() == "" and (field("reward_high") or "").strip() == "":
            await conf.reward_range.set([])

        for key in ("bang_time", "bang_words", "eagle"):
            await conf.get_attr(key).set(field.checked(f"t_{key}"))

        return errors + [{"message": "Hunting settings saved.", "category": "success"}]


HUNTING_TEMPLATE = (
    BASE_CSS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-crosshairs"></i> Hunting in {{ guild_name }}</h4>
    <p>
      {% if active_ids %}Active in {{ active_ids|length }} channel(s).
      {% else %}No channels selected, so nothing will spawn.{% endif %}
    </p>
  </div>

  {% if is_staff %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />

      <div class="dz-grid two">
        <div class="dz-panel">
          <h5><i class="fa fa-hashtag"></i> Channels</h5>
          <p class="dz-hint">Ctrl-click to select several.</p>
          <select class="dz-select" name="channels" multiple size="9">
            {% for c in channels %}
              <option value="{{ c.id }}" {% if c.id in active_ids %}selected{% endif %}>
                {{ c.name }}
              </option>
            {% endfor %}
          </select>
        </div>

        <div class="dz-panel">
          <h5><i class="fa fa-clock-o"></i> Timing &amp; rewards</h5>
          <div class="dz-row">
            <div style="flex:1 1 130px;">
              <div class="dz-label">Min interval (s)</div>
              <input class="dz-input" type="number" name="interval_min" value="{{ interval_min }}" />
            </div>
            <div style="flex:1 1 130px;">
              <div class="dz-label">Max interval (s)</div>
              <input class="dz-input" type="number" name="interval_max" value="{{ interval_max }}" />
            </div>
          </div>

          <div class="dz-label" style="margin-top:10px;">Time to shoot (s)</div>
          <input class="dz-input" type="number" name="timeout" value="{{ timeout }}" />

          <div class="dz-row" style="margin-top:10px;">
            <div style="flex:1 1 130px;">
              <div class="dz-label">Min reward</div>
              <input class="dz-input" type="number" name="reward_low" value="{{ reward_low }}"
                     placeholder="none" />
            </div>
            <div style="flex:1 1 130px;">
              <div class="dz-label">Max reward</div>
              <input class="dz-input" type="number" name="reward_high" value="{{ reward_high }}"
                     placeholder="none" />
            </div>
          </div>
          <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
            Paid in {{ currency }}. Leave both blank to disable rewards.
          </div>

          <div style="margin-top:11px;">
            <label class="dz-toggle">
              <input type="checkbox" name="t_bang_time" {% if bang_time %}checked{% endif %} />
              <span>Show reaction time after a hit</span>
            </label>
            <label class="dz-toggle">
              <input type="checkbox" name="t_bang_words" {% if bang_words %}checked{% endif %} />
              <span>Accept typed words, not just reactions</span>
            </label>
            <label class="dz-toggle">
              <input type="checkbox" name="t_eagle" {% if eagle %}checked{% endif %} />
              <span>Shooting the eagle costs credits</span>
            </label>
          </div>
        </div>
      </div>

      <div class="dz-save">
        <button class="dz-btn primary" name="action" value="save">
          <i class="fa fa-save"></i> Save settings
        </button>
      </div>
    </form>
  {% endif %}

  <div class="dz-panel">
    <h5><i class="fa fa-trophy"></i> Scoreboard</h5>
    {% if scores %}
      <table class="dz-t">
        <thead><tr><th>#</th><th>Member</th><th>Total</th><th>Breakdown</th></tr></thead>
        <tbody>
          {% for row in scores %}
            <tr>
              <td style="opacity:.5; width:34px;">{{ row.position }}</td>
              <td>{{ row.name }}</td>
              <td><b>{{ row.total }}</b></td>
              <td style="opacity:.6; font-size:.78rem;">{{ row.birds }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="dz-empty">Nobody has scored yet.</p>
    {% endif %}
  </div>
</div>
"""
)
