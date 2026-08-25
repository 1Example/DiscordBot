from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import bank, commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
)

log = logging.getLogger("red.economy.dashboard")

# (config key, label, help, minimum)
FIELDS = (
    ("PAYDAY_TIME", "Payday cooldown (seconds)", "How long between paydays.", 0),
    ("PAYDAY_CREDITS", "Payday amount", "Credits granted per payday.", 0),
    ("REGISTER_CREDITS", "Starting balance", "Credits a new account begins with.", 0),
    ("SLOT_MIN", "Minimum slot bid", "Smallest allowed slot machine bid.", 1),
    ("SLOT_MAX", "Maximum slot bid", "Largest allowed slot machine bid.", 1),
    ("SLOT_TIME", "Slot cooldown (seconds)", "How long between slot spins.", 0),
)


class DashboardIntegration:
    """Economy tuning plus a live leaderboard."""

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Economy as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Payday, slots and the credit leaderboard.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_economy_page(
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
                    "error_message": "Only server administrators can change economy settings.",
                }
            notifications = await self._eco_handle_post(guild, kwargs)

        # With a global bank, per-guild settings are ignored entirely - showing
        # editable guild fields would be actively misleading.
        global_bank = await bank.is_global()
        scope = self.config if global_bank else self.config.guild(guild)
        settings = await scope.all()

        currency = await bank.get_currency_name(guild)
        try:
            max_balance = await bank.get_max_balance(guild)
        except Exception:  # noqa: BLE001
            max_balance = None

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": ECONOMY_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "global_bank": global_bank,
                "is_staff": staff,
                "currency": currency,
                "max_balance": max_balance,
                "bank_name": await bank.get_bank_name(guild),
                "fields": [
                    {
                        "key": key,
                        "label": label,
                        "help": help_text,
                        "min": minimum,
                        "value": settings.get(key, 0),
                    }
                    for key, label, help_text, minimum in FIELDS
                ],
                "leaderboard": await self._eco_leaderboard(guild, global_bank),
                "balance": await self._eco_balance(member),
            },
        }

    async def _eco_balance(self, member: discord.Member):
        try:
            return await bank.get_balance(member)
        except Exception:  # noqa: BLE001
            return None

    async def _eco_leaderboard(self, guild: discord.Guild, global_bank: bool, limit: int = 15):
        try:
            raw = await bank.get_leaderboard(positions=limit, guild=None if global_bank else guild)
        except Exception:  # noqa: BLE001
            log.exception("Could not build the economy leaderboard")
            return []
        rows = []
        for position, (user_id, data) in enumerate(raw, start=1):
            who = guild.get_member(user_id) or self.bot.get_user(user_id)
            rows.append(
                {
                    "position": position,
                    "name": getattr(who, "display_name", None) or f"Unknown ({user_id})",
                    "balance": (data or {}).get("balance", 0),
                }
            )
        return rows

    async def _eco_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        if field("action") != "save":
            return [{"message": "Unknown action.", "category": "warning"}]

        global_bank = await bank.is_global()
        scope = self.config if global_bank else self.config.guild(guild)

        values: dict[str, int] = {}
        errors: list[dict] = []
        for key, label, _help, minimum in FIELDS:
            raw = (field(f"f_{key}") or "").strip()
            if raw == "":
                continue
            try:
                value = int(raw)
            except ValueError:
                errors.append({"message": f"{label}: '{raw}' is not a number.", "category": "danger"})
                continue
            if value < minimum:
                errors.append(
                    {"message": f"{label}: must be at least {minimum}.", "category": "danger"}
                )
                continue
            values[key] = value

        # A minimum bid above the maximum makes slots unplayable with no error
        # message anywhere, so reject the pair rather than storing it.
        slot_min = values.get("SLOT_MIN")
        slot_max = values.get("SLOT_MAX")
        if slot_min is not None and slot_max is not None and slot_min > slot_max:
            errors.append(
                {"message": "Minimum slot bid cannot exceed the maximum.", "category": "danger"}
            )
            values.pop("SLOT_MIN", None)
            values.pop("SLOT_MAX", None)

        for key, value in values.items():
            await scope.get_attr(key).set(value)

        return errors + [
            {
                "message": f"Saved {len(values)} setting(s)"
                + (" (global bank)." if global_bank else f" for {guild.name}."),
                "category": "success" if values else "info",
            }
        ]


ECONOMY_TEMPLATE = (
    BASE_CSS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-money"></i> {{ bank_name }}</h4>
    <p>
      Currency: <b>{{ currency }}</b>
      {% if global_bank %}&middot; <b>global bank</b> - these settings apply to every server
      {% else %}&middot; per-server bank for {{ guild_name }}{% endif %}
      {% if max_balance %}&middot; max balance {{ "{:,}".format(max_balance) }}{% endif %}
      {% if balance is not none %}&middot; your balance: <b>{{ "{:,}".format(balance) }}</b>{% endif %}
    </p>
  </div>

  {% if is_staff %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-sliders"></i> Settings</h5>
        <p class="dz-hint">
          {% if global_bank %}Editing the bot-wide values because the bank is global.
          {% else %}These apply only to this server.{% endif %}
        </p>
        <div class="dz-grid two">
          {% for f in fields %}
            <div>
              <div class="dz-label">{{ f.label }}</div>
              <input class="dz-input" type="number" min="{{ f.min }}"
                     name="f_{{ f.key }}" value="{{ f.value }}" />
              <div style="font-size:.72rem; opacity:.45; margin-top:4px;">{{ f.help }}</div>
            </div>
          {% endfor %}
        </div>
        <div style="margin-top:14px;">
          <button class="dz-btn primary" name="action" value="save">
            <i class="fa fa-save"></i> Save settings
          </button>
        </div>
      </div>
    </form>
  {% endif %}

  <div class="dz-panel">
    <h5><i class="fa fa-trophy"></i> Leaderboard</h5>
    <p class="dz-hint">
      Top {{ leaderboard|length }}{% if not global_bank %} in this server{% endif %}.
    </p>
    {% if leaderboard %}
      <table class="dz-t">
        <thead><tr><th>#</th><th>Member</th><th style="text-align:right;">Balance</th></tr></thead>
        <tbody>
          {% for row in leaderboard %}
            <tr>
              <td style="opacity:.5; width:40px;">{{ row.position }}</td>
              <td>{{ row.name }}</td>
              <td style="text-align:right; font-variant-numeric:tabular-nums;">
                {{ "{:,}".format(row.balance) }} {{ currency }}
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="dz-empty">No accounts yet.</p>
    {% endif %}
  </div>
</div>
"""
)
