from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import bank, commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
    member_options,
    role_options,
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


# What the slot machine pays, mirroring the PAYOUTS table in the cog.
SLOT_PAYOUTS = (
    ("2 2 6", "x50", "Jackpot"),
    ("4LC 4LC 4LC", "x25", "Three four-leaf clovers"),
    ("Cherry Cherry Cherry", "x20", "Three cherries"),
    ("Any three matching symbols", "x10", ""),
    ("2 6", "x4", "Two then six"),
    ("Cherry Cherry", "x3", "Two cherries"),
    ("Any two consecutive symbols", "x2", ""),
)


class DashboardIntegration:
    """Economy management, tuning and a live leaderboard.

    Covers every ``[p]economyset`` option including per-role payday amounts, the
    bank operations (``[p]bank set``, ``[p]bank add``, ``[p]bank sub``,
    ``[p]bank transfer``), the leaderboard and the slot payout table.
    """

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
                "member_options": member_options(guild, humans_only=True),
                "role_options": role_options(guild),
                "role_paydays": await self._eco_role_paydays(guild, global_bank),
                "payouts": SLOT_PAYOUTS,
            },
        }

    async def _eco_role_paydays(
        self, guild: discord.Guild, global_bank: bool
    ) -> list[dict]:
        """Per-role payday overrides. These only apply to a per-server bank."""
        if global_bank:
            return []
        rows = []
        for role_id, data in (await self.config.all_roles()).items():
            role = guild.get_role(role_id)
            credits = (data or {}).get("PAYDAY_CREDITS")
            if role is None or not credits:
                continue
            rows.append({"id": str(role.id), "name": role.name, "credits": credits})
        rows.sort(key=lambda r: -r["credits"])
        return rows

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

    async def _eco_bank_action(
        self, action: str, guild: discord.Guild, field
    ) -> list[dict]:
        from redbot.core.errors import BalanceTooHigh

        currency = await bank.get_currency_name(guild)

        try:
            if action == "role_payday":
                if await bank.is_global():
                    return [
                        {
                            "message": "Per-role paydays need a per-server bank.",
                            "category": "warning",
                        }
                    ]
                role = guild.get_role(field.integer("role_id", 0) or 0)
                if role is None:
                    return [{"message": "Pick a role.", "category": "warning"}]
                credits = field.integer("role_credits", 0) or 0
                if credits <= 0:
                    await self.config.role(role).clear()
                    default = await self.config.guild(guild).PAYDAY_CREDITS()
                    return [
                        {
                            "message": f"{role.name} now uses the default payday of "
                            f"{default} {currency}.",
                            "category": "success",
                        }
                    ]
                max_balance = await bank.get_max_balance(guild)
                if credits >= max_balance:
                    return [
                        {
                            "message": f"A payday must be below the maximum balance "
                            f"of {max_balance}.",
                            "category": "warning",
                        }
                    ]
                await self.config.role(role).PAYDAY_CREDITS.set(credits)
                return [
                    {
                        "message": f"Members with {role.name} now earn {credits} "
                        f"{currency} per payday.",
                        "category": "success",
                    }
                ]

            target = guild.get_member(field.integer("member_id", 0) or 0)
            if target is None:
                return [{"message": "Pick a member.", "category": "warning"}]
            amount = field.integer("amount", 0) or 0

            if action == "balance":
                mode = field("balance_mode") or "set"
                if mode != "set" and amount <= 0:
                    return [
                        {"message": "Enter a positive amount.", "category": "warning"}
                    ]
                if mode == "set":
                    if amount < 0:
                        return [
                            {"message": "A balance cannot be negative.",
                             "category": "warning"}
                        ]
                    new = await bank.set_balance(target, amount)
                elif mode == "add":
                    new = await bank.deposit_credits(target, amount)
                else:
                    if not await bank.can_spend(target, amount):
                        return [
                            {
                                "message": f"{target.display_name} does not have that "
                                "many credits.",
                                "category": "warning",
                            }
                        ]
                    new = await bank.withdraw_credits(target, amount)
                return [
                    {
                        "message": f"{target.display_name} now has {new} {currency}.",
                        "category": "success",
                    }
                ]

            if action == "transfer":
                source = guild.get_member(field.integer("from_member_id", 0) or 0)
                if source is None:
                    return [
                        {"message": "Pick who the credits come from.",
                         "category": "warning"}
                    ]
                if source == target:
                    return [
                        {"message": "Pick two different members.", "category": "warning"}
                    ]
                if amount <= 0:
                    return [
                        {"message": "Enter a positive amount.", "category": "warning"}
                    ]
                await bank.transfer_credits(source, target, amount)
                return [
                    {
                        "message": f"Transferred {amount} {currency} from "
                        f"{source.display_name} to {target.display_name}.",
                        "category": "success",
                    }
                ]
        except BalanceTooHigh as exc:
            return [
                {"message": f"That would exceed the maximum balance ({exc.max_balance}).",
                 "category": "warning"}
            ]
        except ValueError as exc:
            return [{"message": str(exc), "category": "warning"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("Economy dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    async def _eco_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

        if action in ("balance", "transfer", "role_payday"):
            return await self._eco_bank_action(action, guild, field)

        if action != "save":
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
    + MACROS
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
        <h5><i class="fa fa-bank"></i> Balances</h5>
        <p class="dz-hint">Set, add or subtract credits, or move them between members.</p>
        <div class="dz-grid two">
          <div>
            <label class="dz-label">Member</label>
            {{ picker('member_id', member_options, false, 8, 'Search members...') }}
          </div>
          <div>
            <label class="dz-label">Amount</label>
            <input class="dz-input" type="number" min="0" name="amount" value="0" />
            <label class="dz-label" style="margin-top:10px;">What to do</label>
            <select class="dz-select" name="balance_mode">
              <option value="set">Set their balance to this</option>
              <option value="add">Add this many</option>
              <option value="sub">Take this many away</option>
            </select>
          </div>
        </div>
        <div class="dz-row dz-save">
          <button class="dz-btn primary" name="action" value="balance">
            <i class="fa fa-money"></i> Apply
          </button>
        </div>
        <label class="dz-label" style="margin-top:14px;">Transfer from another member</label>
        <div class="dz-grid two">
          <div>
            {{ picker('from_member_id', member_options, false, 6, 'Search members...') }}
          </div>
          <div>
            <p class="dz-hint">Uses the member and amount chosen above as the
               destination. Transfer fees still apply.</p>
            <button class="dz-btn" name="action" value="transfer">
              <i class="fa fa-exchange"></i> Transfer
            </button>
          </div>
        </div>
      </div>
    </form>

    {% if not global_bank %}
      <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <div class="dz-panel">
          <h5><i class="fa fa-users"></i> Per-role paydays</h5>
          <p class="dz-hint">Members with one of these roles earn that amount
             instead of the default. Set 0 to remove an override.</p>
          {% if role_paydays %}
            <table class="dz-t">
              <tr><th>Role</th><th>Payday</th></tr>
              {% for r in role_paydays %}
                <tr><td>{{ r.name }}</td>
                    <td>{{ "{:,}".format(r.credits) }} {{ currency }}</td></tr>
              {% endfor %}
            </table>
          {% else %}
            <p class="dz-empty">No role overrides set.</p>
          {% endif %}
          <div class="dz-row dz-save">
            {{ picker('role_id', role_options, false, 6, 'Search roles...') }}
            <input class="dz-input" type="number" min="0" name="role_credits"
                   placeholder="credits (0 removes)" style="max-width:200px;" />
            <button class="dz-btn primary" name="action" value="role_payday">
              <i class="fa fa-save"></i> Save
            </button>
          </div>
        </div>
      </form>
    {% endif %}

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

  <div class="dz-panel">
    <h5><i class="fa fa-diamond"></i> Slot payouts</h5>
    <p class="dz-hint">What each result multiplies the bid by.</p>
    <table class="dz-t">
      <tr><th>Result</th><th>Payout</th><th></th></tr>
      {% for result, payout, note in payouts %}
        <tr><td>{{ result }}</td><td>{{ payout }}</td><td>{{ note }}</td></tr>
      {% endfor %}
    </table>
  </div>
</div>
"""
)
