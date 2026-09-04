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

log = logging.getLogger("red.simplecasino.dashboard")

# (key, label, help, minimum, maximum)
LIMITS = (
    ("bjmin", "Blackjack minimum bet", "Smallest allowed blackjack bet.", 1, 10_000_000),
    ("bjmax", "Blackjack maximum bet", "Largest allowed blackjack bet.", 1, 10_000_000),
    ("bjtime", "Blackjack decision time (s)", "How long a player has to act.", 1, 300),
    ("pokermin", "Poker minimum bet", "Smallest allowed poker starting bet.", 1, 10_000_000),
    ("pokermax", "Poker maximum bet", "Largest allowed poker starting bet.", 1, 10_000_000),
)

# Slot payout multipliers - the house edge, in other words. `payout_seven3` is
# also the bar the jackpot banner is measured against, which is why it is first.
PAYOUTS = (
    ("payout_seven3", "Three sevens (jackpot)", "Also the threshold for the jackpot banner.", 1, 100_000),
    ("payout_clover3", "Three clovers", "", 1, 100_000),
    ("payout_cherries3", "Three cherries", "", 1, 100_000),
    ("payout_triple", "Any other triple", "Three matching symbols not listed above.", 1, 100_000),
    ("payout_seven2", "Two sevens", "", 1, 100_000),
    ("payout_clover2", "Two clovers", "", 1, 100_000),
    ("payout_cherries2", "Two cherries", "", 1, 100_000),
    ("payout_double", "Any other pair", "Two matching symbols not listed above.", 1, 100_000),
)

# Table capacity.
CAPS = (
    ("max_concurrent_slots", "Concurrent slot players", 
     "Prefix-command spins allowed at once; /slot is not limited.", 1, 100),
    ("poker_max_players", "Poker seats", "Players allowed per table.", 2, 20),
)

TOGGLES = (
    ("coinfreespin", "Free spin on matching coins", "Grants a free spin instead of a loss."),
    ("sloteasy", "Easier slot odds", "Raises the chance of a winning combination."),
)

# The slot machine reads its limits from the Economy cog rather than from this
# one - see `SimpleCasino.slot`, which pulls SLOT_MIN/SLOT_MAX/SLOT_TIME off
# `economy.config`. Economy still registers them ("kept registered so old saved
# values are not orphaned") but no longer owns the game, so neither cog's page
# was showing them and the 100-credit default looked hardcoded. They are edited
# here, beside the other game limits, and written back to Economy's config.
SLOT_LIMITS = (
    ("SLOT_MIN", "Slot minimum bet", "Smallest allowed slot bet.", 1, 10_000_000),
    ("SLOT_MAX", "Slot maximum bet", "Largest allowed slot bet.", 1, 10_000_000),
    ("SLOT_TIME", "Slot cooldown (s)",
     "Wait between spins. Values under 3 are treated as 3.", 0, 3600),
)

# Bet pairs that must not be inverted.
PAIRS = (("bjmin", "bjmax", "Blackjack"), ("pokermin", "pokermax", "Poker"))
SLOT_PAIR = ("SLOT_MIN", "SLOT_MAX", "Slots")

# Table markers. These are interpolated straight into the embed text - see
# `poker.get_suit_emojis` - so a plain string like "(D)" is as valid as an
# emoji, which is why they are length-checked rather than run through
# `emoji_problem`. They are registered global-only, so unlike the limits above
# they are the same in every server and only the bot owner may change them.
MARKERS = (
    ("emoji_dealer", "Dealer", "Marks the dealer seat."),
    ("emoji_smallblind", "Small blind", "Marks the small-blind seat."),
    ("emoji_bigblind", "Big blind", "Marks the big-blind seat."),
    ("emoji_spades", "Spades", "Drawn beside a spades card."),
    ("emoji_hearts", "Hearts", "Drawn beside a hearts card."),
    ("emoji_diamonds", "Diamonds", "Drawn beside a diamonds card."),
    ("emoji_clubs", "Clubs", "Drawn beside a clubs card."),
)
MARKER_MAX = 32


class DashboardIntegration:
    """Betting limits, slot odds and per-member game statistics."""

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering SimpleCasino as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Casino limits, odds and statistics.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_casino_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        staff = await is_staff(self.bot, user, member, guild)
        # The markers are global, so an admin of one server would be editing
        # every other server's tables too. Only the owner gets that panel.
        owner = await self.bot.is_owner(user)

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            if not staff:
                return {
                    "status": 1,
                    "error_title": "Forbidden",
                    "error_message": "Only server administrators can change casino settings.",
                }
            notifications = await self._sc_handle_post(guild, kwargs, owner=owner)

        # A global bank means the guild-scoped limits are not what players hit.
        global_bank = await self._sc_global_bank()
        scope = self.config if global_bank else self.config.guild(guild)
        settings = await scope.all()
        marker_values = await self.config.all() if owner else {}

        eco = self._sc_economy_scope(guild, global_bank)
        slot_settings = await eco.all() if eco is not None else {}

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": CASINO_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_staff": staff,
                "is_owner": owner,
                "markers": [
                    {"key": k, "label": lbl, "help": h,
                     "value": marker_values.get(k, ""), "max": MARKER_MAX}
                    for k, lbl, h in MARKERS
                ] if owner else [],
                "global_bank": global_bank,
                "currency": await self._sc_currency(guild),
                "limits": [
                    {
                        "key": k,
                        "label": lbl,
                        "help": h,
                        "min": lo,
                        "max": hi,
                        "value": settings.get(k, lo),
                    }
                    for k, lbl, h, lo, hi in LIMITS
                ],
                "payouts": [
                    {"key": k, "label": lbl, "help": h, "min": lo, "max": hi,
                     "value": settings.get(k, lo)}
                    for k, lbl, h, lo, hi in PAYOUTS
                ],
                "caps": [
                    {"key": k, "label": lbl, "help": h, "min": lo, "max": hi,
                     "value": settings.get(k, lo)}
                    for k, lbl, h, lo, hi in CAPS
                ],
                "slot_limits": [
                    {
                        "key": k,
                        "label": lbl,
                        "help": h,
                        "min": lo,
                        "max": hi,
                        "value": slot_settings.get(k, lo),
                    }
                    for k, lbl, h, lo, hi in SLOT_LIMITS
                ] if eco is not None else [],
                "economy_missing": eco is None,
                "toggles": [
                    {"key": k, "label": lbl, "help": h, "on": bool(settings.get(k))}
                    for k, lbl, h in TOGGLES
                ],
                "stats": await self._sc_stats(guild),
                "you": await self._sc_member_stats(guild, member),
            },
        }

    def _sc_economy_scope(self, guild: discord.Guild, global_bank: bool):
        """Economy's config at the scope the slot machine actually reads.

        Returns None when the Economy cog is not loaded - the slot command is
        already unusable in that state, so the page hides the fields rather
        than offering edits that would go nowhere.
        """
        economy = self.bot.get_cog("Economy")
        if economy is None or getattr(economy, "config", None) is None:
            return None
        return economy.config if global_bank else economy.config.guild(guild)

    async def _sc_global_bank(self) -> bool:
        try:
            return await bank.is_global()
        except Exception:  # noqa: BLE001
            return False

    async def _sc_currency(self, guild: discord.Guild) -> str:
        try:
            return await bank.get_currency_name(guild)
        except Exception:  # noqa: BLE001
            return "credits"

    @staticmethod
    def _sc_row(name: str, stats: dict) -> dict:
        blackjack = stats.get("bjcount", 0)
        wins = stats.get("bjwincount", 0)
        return {
            "name": name,
            "slots": stats.get("slotcount", 0),
            "slot_profit": stats.get("slotprofit", 0),
            "blackjack": blackjack,
            "bj_wins": wins,
            "bj_profit": stats.get("bjprofit", 0),
            "win_rate": round(wins / blackjack * 100) if blackjack else 0,
            "profit": stats.get("slotprofit", 0) + stats.get("bjprofit", 0),
        }

    async def _sc_stats(self, guild: discord.Guild, limit: int = 15) -> list[dict]:
        try:
            data = await self.config.all_members(guild)
        except Exception:  # noqa: BLE001
            log.exception("Could not read casino statistics")
            return []
        rows = []
        for member_id, stats in data.items():
            member = guild.get_member(member_id)
            if member is None:
                continue
            row = self._sc_row(member.display_name, stats)
            if row["slots"] or row["blackjack"]:
                rows.append(row)
        rows.sort(key=lambda r: -r["profit"])
        for position, row in enumerate(rows[:limit], start=1):
            row["position"] = position
        return rows[:limit]

    async def _sc_member_stats(self, guild: discord.Guild, member: discord.Member) -> dict | None:
        try:
            stats = await self.config.member(member).all()
        except Exception:  # noqa: BLE001
            return None
        row = self._sc_row(member.display_name, stats)
        return row if (row["slots"] or row["blackjack"]) else None

    async def _sc_handle_post(
        self, guild: discord.Guild, kwargs: dict, *, owner: bool = False
    ) -> list[dict]:
        field = form_reader(kwargs)
        if field("action") != "save":
            return [{"message": "Unknown action.", "category": "warning"}]

        global_bank = await self._sc_global_bank()
        scope = self.config if global_bank else self.config.guild(guild)

        errors: list[dict] = []
        values: dict[str, int] = {}
        for key, label, _h, low, high in LIMITS + PAYOUTS + CAPS:
            raw = (field(f"f_{key}") or "").strip()
            if raw == "":
                continue
            try:
                value = int(raw)
            except ValueError:
                errors.append({"message": f"{label}: '{raw}' is not a number.", "category": "danger"})
                continue
            if not low <= value <= high:
                errors.append(
                    {"message": f"{label}: must be between {low} and {high}.", "category": "danger"}
                )
                continue
            values[key] = value

        current = await scope.all()
        for low_key, high_key, game in PAIRS:
            low = values.get(low_key, current.get(low_key))
            high = values.get(high_key, current.get(high_key))
            if low is not None and high is not None and low > high:
                errors.append(
                    {
                        "message": f"{game}: minimum bet cannot exceed the maximum.",
                        "category": "danger",
                    }
                )
                values.pop(low_key, None)
                values.pop(high_key, None)

        for key, value in values.items():
            await scope.get_attr(key).set(value)
        for key, _lbl, _h in TOGGLES:
            await scope.get_attr(key).set(field.checked(f"t_{key}"))

        # Slot limits live on the Economy cog; same validation, different home.
        slot_saved = 0
        eco = self._sc_economy_scope(guild, global_bank)
        if eco is not None:
            slot_values: dict[str, int] = {}
            for key, label, _h, low, high in SLOT_LIMITS:
                raw = (field(f"f_{key}") or "").strip()
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
                        {"message": f"{label}: must be between {low} and {high}.",
                         "category": "danger"}
                    )
                    continue
                slot_values[key] = value

            eco_current = await eco.all()
            low_key, high_key, game = SLOT_PAIR
            low = slot_values.get(low_key, eco_current.get(low_key))
            high = slot_values.get(high_key, eco_current.get(high_key))
            if low is not None and high is not None and low > high:
                errors.append(
                    {"message": f"{game}: minimum bet cannot exceed the maximum.",
                     "category": "danger"}
                )
                slot_values.pop(low_key, None)
                slot_values.pop(high_key, None)

            for key, value in slot_values.items():
                await eco.get_attr(key).set(value)
            slot_saved = len(slot_values)

        # Markers are global and owner-only. A non-owner posting `m_*` fields by
        # hand is ignored rather than refused, so a stale form cannot lock an
        # admin out of saving the limits they are allowed to change.
        saved_markers = 0
        if owner:
            for key, label, _h in MARKERS:
                raw = field(f"m_{key}")
                if raw is None:
                    continue
                raw = raw.strip()
                if not raw:
                    errors.append(
                        {"message": f"{label}: cannot be blank.", "category": "danger"}
                    )
                    continue
                if len(raw) > MARKER_MAX:
                    errors.append(
                        {
                            "message": f"{label}: {len(raw)} characters is too long "
                            f"(limit {MARKER_MAX}).",
                            "category": "danger",
                        }
                    )
                    continue
                if raw != await self.config.get_attr(key)():
                    await self.config.get_attr(key).set(raw)
                    saved_markers += 1

        saved = [f"{len(values) + slot_saved} limit(s)"]
        if saved_markers:
            saved.append(f"{saved_markers} marker(s)")
        return errors + [
            {
                "message": "Saved " + " and ".join(saved)
                + (" (global bank)." if global_bank else "."),
                "category": "success",
            }
        ]


CASINO_TEMPLATE = (
    BASE_CSS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-diamond"></i> Casino in {{ guild_name }}</h4>
    <p>
      Bets in <b>{{ currency }}</b>
      {% if global_bank %}&middot; <b>global bank</b> - editing the bot-wide limits
      {% else %}&middot; per-server limits{% endif %}
      {% if you %}&middot; your net: <b>{{ "{:,}".format(you.profit) }}</b>{% endif %}
    </p>
  </div>

  {% if is_staff %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-grid two">
        <div class="dz-panel">
          <h5><i class="fa fa-sliders"></i> Limits</h5>
          {% for f in limits %}
            <div style="margin-bottom:11px;">
              <div class="dz-label">{{ f.label }}</div>
              <input class="dz-input" type="number" min="{{ f.min }}" max="{{ f.max }}"
                     name="f_{{ f.key }}" value="{{ f.value }}" />
              <div style="font-size:.72rem; opacity:.45; margin-top:4px;">{{ f.help }}</div>
            </div>
          {% endfor %}

          {% if slot_limits %}
            <div style="margin:16px 0 11px; padding-top:13px;
                        border-top:1px solid var(--cx-panel-3-bd, rgba(255,255,255,.08));">
              <div class="dz-label" style="opacity:.8;">Slots</div>
              <div style="font-size:.72rem; opacity:.45; margin-top:2px;">
                Stored by the Economy cog, which the slot machine reads at spin time.
              </div>
            </div>
            {% for f in slot_limits %}
              <div style="margin-bottom:11px;">
                <div class="dz-label">{{ f.label }}</div>
                <input class="dz-input" type="number" min="{{ f.min }}" max="{{ f.max }}"
                       name="f_{{ f.key }}" value="{{ f.value }}" />
                <div style="font-size:.72rem; opacity:.45; margin-top:4px;">{{ f.help }}</div>
              </div>
            {% endfor %}
          {% elif economy_missing %}
            <p class="dz-hint" style="margin-top:14px;">
              Slot limits are stored by the Economy cog, which is not loaded, so
              the slot machine is unavailable and its limits cannot be edited.
            </p>
          {% endif %}
        </div>

        <div class="dz-panel">
          <h5><i class="fa fa-trophy"></i> Slot payouts</h5>
          <p class="dz-hint">
            What each combination pays, as a multiple of the bet. Lower numbers
            mean a bigger house edge.
          </p>
          {% for f in payouts %}
            <div style="display:flex; align-items:center; gap:10px; margin-bottom:8px;">
              <div style="flex:1 1 auto; min-width:0;">
                <div class="dz-label" style="margin:0;">{{ f.label }}</div>
                {% if f.help %}
                  <div style="font-size:.72rem; opacity:.45;">{{ f.help }}</div>
                {% endif %}
              </div>
              <div style="display:flex; align-items:center; gap:5px; flex:none;">
                <span style="opacity:.5; font-size:.8rem;">&times;</span>
                <input class="dz-input" type="number" style="width:96px;"
                       min="{{ f.min }}" max="{{ f.max }}"
                       name="f_{{ f.key }}" value="{{ f.value }}" />
              </div>
            </div>
          {% endfor %}
        </div>

        <div class="dz-panel">
          <h5><i class="fa fa-users"></i> Table capacity</h5>
          {% for f in caps %}
            <div style="margin-bottom:11px;">
              <div class="dz-label">{{ f.label }}</div>
              <input class="dz-input" type="number" min="{{ f.min }}" max="{{ f.max }}"
                     name="f_{{ f.key }}" value="{{ f.value }}" />
              <div style="font-size:.72rem; opacity:.45; margin-top:4px;">{{ f.help }}</div>
            </div>
          {% endfor %}
        </div>

        <div class="dz-panel">
          <h5><i class="fa fa-random"></i> Odds</h5>
          {% for t in toggles %}
            <div style="margin-bottom:9px;">
              <label class="dz-toggle" style="padding:0;">
                <input type="checkbox" name="t_{{ t.key }}" {% if t.on %}checked{% endif %} />
                <span>{{ t.label }}</span>
              </label>
              <div style="font-size:.72rem; opacity:.45; margin-left:26px;">{{ t.help }}</div>
            </div>
          {% endfor %}
          {% if not is_owner %}
          <div style="margin-top:14px;">
            <button class="dz-btn primary" name="action" value="save">
              <i class="fa fa-save"></i> Save
            </button>
          </div>
          {% endif %}
        </div>
      </div>

      {% if is_owner %}
      <div class="dz-panel">
        <h5><i class="fa fa-id-badge"></i> Table markers</h5>
        <p class="dz-hint">
          Drawn beside seats and cards. Text such as <code>(D)</code> works as
          well as an emoji. These are bot-wide - every server sees the same
          markers - which is why only you can edit them.
        </p>
        <div class="dz-grid two">
          {% for m in markers %}
            <div style="margin-bottom:11px;">
              <div class="dz-label">{{ m.label }}</div>
              <input class="dz-input" type="text" maxlength="{{ m.max }}"
                     name="m_{{ m.key }}" value="{{ m.value }}" />
              <div style="font-size:.72rem; opacity:.45; margin-top:4px;">{{ m.help }}</div>
            </div>
          {% endfor %}
        </div>
      </div>

      <div style="margin-top:14px;">
        <button class="dz-btn primary" name="action" value="save">
          <i class="fa fa-save"></i> Save
        </button>
      </div>
      {% endif %}
    </form>
  {% endif %}

  <div class="dz-panel">
    <h5><i class="fa fa-bar-chart"></i> Player statistics</h5>
    <p class="dz-hint">Ranked by net profit across slots and blackjack.</p>
    {% if stats %}
      <table class="dz-t">
        <thead>
          <tr><th>#</th><th>Member</th><th>Slots</th><th>Blackjack</th>
              <th>BJ win rate</th><th style="text-align:right;">Net</th></tr>
        </thead>
        <tbody>
          {% for row in stats %}
            <tr>
              <td style="opacity:.5; width:34px;">{{ row.position }}</td>
              <td>{{ row.name }}</td>
              <td style="opacity:.7;">{{ row.slots }}</td>
              <td style="opacity:.7;">{{ row.blackjack }}</td>
              <td style="opacity:.7;">{{ row.win_rate }}%</td>
              <td style="text-align:right; font-variant-numeric:tabular-nums;
                         color:{% if row.profit > 0 %}#3ba55d{% elif row.profit < 0 %}#ff8b8b{% else %}inherit{% endif %};">
                {{ "{:,}".format(row.profit) }}
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="dz-empty">Nobody has played yet.</p>
    {% endif %}
  </div>
</div>
"""
)
