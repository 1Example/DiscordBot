from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import bank, commands
from redbot.core.utils.chat_formatting import humanize_list

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
    channel_options,
    member_options,
    role_options,
)

log = logging.getLogger("red.economy.dashboard")

# (config key, label, help, minimum)
FIELDS = (
    ("PAYDAY_TIME", "Payday cooldown (seconds)", "How long between paydays.", 0),
    ("PAYDAY_CREDITS", "Payday amount", "Credits granted per payday.", 0),
    ("REGISTER_CREDITS", "Starting balance", "Credits a new account begins with.", 0),
)


class DashboardIntegration:
    """Economy management, tuning and a live leaderboard.

    Covers every ``[p]economyset`` option including per-role payday amounts, the
    bank operations (``[p]bank set``, ``[p]bank add``, ``[p]bank sub``,
    ``[p]bank transfer``) and the leaderboard.
    """

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Economy as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Payday, balances and the credit leaderboard.",
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
            kwargs["_is_owner"] = await self.bot.is_owner(user)
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
                "is_owner": await self.bot.is_owner(user),
                "default_balance": await bank.get_default_balance(guild),
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
                **await self._eco_autopayday_context(guild),
            },
        }

    async def _eco_autopayday_context(self, guild: discord.Guild) -> dict:
        """The automatic-payday card.

        These always live on the guild even when the bank is global: the amount
        is shared across servers, but whether a server hands it out on a timer
        and where it says so are that server's business.
        """
        from .economy import (
            DEFAULT_PAYDAY_MESSAGE,
            DEFAULT_PAYDAY_TITLE,
            MAX_PAYSLIP_LEADERBOARD,
        )

        auto = await self.config.guild(guild).all()
        colour = auto.get("AUTO_PAYDAY_COLOUR") or 0
        return {
            "auto_enabled": bool(auto.get("AUTO_PAYDAY")),
            "auto_announce": bool(auto.get("AUTO_PAYDAY_ANNOUNCE", True)),
            "auto_channels": channel_options(
                guild, selected=auto.get("AUTO_PAYDAY_CHANNEL"), require_send=True
            ),
            "auto_roles": role_options(guild, selected=auto.get("AUTO_PAYDAY_ROLE")),
            "auto_title": auto.get("AUTO_PAYDAY_TITLE") or "",
            "auto_message": auto.get("AUTO_PAYDAY_MESSAGE") or "",
            "auto_image": auto.get("AUTO_PAYDAY_IMAGE") or "",
            "auto_board": bool(auto.get("AUTO_PAYDAY_LEADERBOARD", True)),
            "auto_board_size": auto.get("AUTO_PAYDAY_LEADERBOARD_SIZE", 5) or 5,
            "auto_board_max": MAX_PAYSLIP_LEADERBOARD,
            "auto_board_preview": await self._eco_leaderboard(
                guild,
                await bank.is_global(),
                limit=auto.get("AUTO_PAYDAY_LEADERBOARD_SIZE", 5) or 5,
            ),
            "auto_colour": f"#{colour:06x}" if colour else "#f1c40f",
            "auto_title_default": DEFAULT_PAYDAY_TITLE,
            "auto_message_default": DEFAULT_PAYDAY_MESSAGE,
            "eligible": sum(1 for m in guild.members if not m.bot),
            "auto_mode": auto.get("AUTO_PAYDAY_MODE") or "fixed",
            "auto_voice_rate": auto.get("AUTO_PAYDAY_VOICE_RATE", 100),
            "auto_voice_min": auto.get("AUTO_PAYDAY_VOICE_MIN", 5),
            "auto_voice_max": auto.get("AUTO_PAYDAY_VOICE_MAX", 0),
            "auto_voice_afk": bool(auto.get("AUTO_PAYDAY_VOICE_AFK")),
            "auto_voice_alone": bool(auto.get("AUTO_PAYDAY_VOICE_ALONE")),
            "auto_voice_deaf": bool(auto.get("AUTO_PAYDAY_VOICE_DEAF")),
            "auto_voice_preview": await self._eco_voice_preview(guild, auto),
            "auto_voice_live": sum(
                1
                for m in guild.members
                if not m.bot and m.voice is not None and m.voice.channel is not None
            ),
            "auto_last": auto.get("AUTO_PAYDAY_LAST") or 0,
        }

    async def _eco_voice_preview(self, guild: discord.Guild, auto: dict) -> list[dict]:
        """Who has banked the most voice time since the last payday.

        This reads the same numbers the payday will, so a server owner can see
        the tracking working before trusting it with anyone's balance.
        """
        try:
            rate = max(0, int(auto.get("AUTO_PAYDAY_VOICE_RATE") or 0))
            cap = max(0, int(auto.get("AUTO_PAYDAY_VOICE_MAX") or 0))
            minimum = max(0, int(auto.get("AUTO_PAYDAY_VOICE_MIN") or 0)) * 60
            rows = []
            for member_id, data in (await self.config.all_members(guild)).items():
                seconds = int((data or {}).get("voice_seconds", 0) or 0)
                if seconds <= 0:
                    continue
                member = guild.get_member(member_id)
                if member is None or member.bot:
                    continue
                amount = int(seconds * rate / 3600)
                if cap:
                    amount = min(amount, cap)
                rows.append(
                    {
                        "name": member.display_name,
                        "minutes": round(seconds / 60, 1),
                        "amount": amount,
                        "short": seconds < minimum,
                    }
                )
            rows.sort(key=lambda row: row["minutes"], reverse=True)
            return rows[:8]
        except Exception:  # noqa: BLE001 - a preview is never worth a 500
            log.exception("Could not build the voice payday preview for %s", guild.id)
            return []

    async def _eco_save_autopayday(self, guild: discord.Guild, field) -> list[dict]:
        conf = self.config.guild(guild)

        message = (field("auto_message") or "").strip()
        if message:
            try:
                message.format(
                    bot="", guild="", currency="", total="", members="",
                    average="", voice="",
                )
            except (KeyError, IndexError, ValueError) as exc:
                return [
                    {
                        "message": f"The message uses something I cannot fill in: {exc}. "
                        "Stick to {bot}, {guild}, {currency}, {total}, {members}, "
                        "{average} and {voice}.",
                        "category": "warning",
                    }
                ]

        from .economy import MAX_PAYSLIP_LEADERBOARD as board_max

        size = field.integer("auto_board_size", 5) or 5
        if not 1 <= size <= board_max:
            return [
                {"message": f"The leaderboard has to show between 1 and {board_max} members.",
                 "category": "warning"}
            ]

        image = (field("auto_image") or "").strip()
        if image and not image.startswith(("http://", "https://")):
            return [
                {
                    "message": "The image has to be a http(s) link to a gif or picture.",
                    "category": "warning",
                }
            ]

        mode = "voice" if (field("auto_mode") or "fixed") == "voice" else "fixed"
        rate = field.integer("auto_voice_rate", 100)
        minutes = field.integer("auto_voice_min", 0)
        ceiling = field.integer("auto_voice_max", 0)
        if mode == "voice":
            if rate <= 0:
                return [
                    {"message": "An hour in voice has to be worth something; set a rate above 0.",
                     "category": "warning"}
                ]
            if minutes < 0 or ceiling < 0:
                return [
                    {"message": "The minimum and the cap cannot be negative.",
                     "category": "warning"}
                ]

        enabled = field.checked("auto_enabled")
        channel = field.integer("auto_channel", 0) or 0
        announce = field.checked("auto_announce")
        if enabled and announce and not channel:
            return [
                {
                    "message": "Pick a channel for the payslip, or turn the "
                    "announcement off.",
                    "category": "warning",
                }
            ]

        await conf.AUTO_PAYDAY.set(enabled)
        await conf.AUTO_PAYDAY_ROLE.set(field.integer("auto_role", 0) or 0)
        await conf.AUTO_PAYDAY_ANNOUNCE.set(announce)
        await conf.AUTO_PAYDAY_CHANNEL.set(channel)
        await conf.AUTO_PAYDAY_TITLE.set((field("auto_title") or "").strip()[:250])
        await conf.AUTO_PAYDAY_MESSAGE.set(message[:2000])
        await conf.AUTO_PAYDAY_IMAGE.set(image[:500])
        await conf.AUTO_PAYDAY_COLOUR.set(self._eco_colour_int(field("auto_colour")))
        await conf.AUTO_PAYDAY_LEADERBOARD.set(field.checked("auto_board"))
        await conf.AUTO_PAYDAY_LEADERBOARD_SIZE.set(
            max(1, min(field.integer("auto_board_size", 5) or 5, board_max))
        )
        await conf.AUTO_PAYDAY_MODE.set(mode)
        await conf.AUTO_PAYDAY_VOICE_RATE.set(max(0, rate))
        await conf.AUTO_PAYDAY_VOICE_MIN.set(max(0, minutes))
        await conf.AUTO_PAYDAY_VOICE_MAX.set(max(0, ceiling))
        await conf.AUTO_PAYDAY_VOICE_AFK.set(field.checked("auto_voice_afk"))
        await conf.AUTO_PAYDAY_VOICE_ALONE.set(field.checked("auto_voice_alone"))
        await conf.AUTO_PAYDAY_VOICE_DEAF.set(field.checked("auto_voice_deaf"))
        # The clocks read these on every voice event, so drop what they have
        # cached, and start counting whoever is already talking.
        await self.resync_voice(guild)

        if not enabled:
            return [
                {"message": "Automatic payday is off; members claim it themselves.",
                 "category": "success"}
            ]
        every = await (
            self.config.PAYDAY_TIME() if await bank.is_global()
            else self.config.guild(guild).PAYDAY_TIME()
        )
        window = f"{every // 60 or 1} minute(s)"
        if mode == "voice":
            return [
                {
                    "message": f"Automatic payday is on. Every {window} the whole "
                    f"server is paid at once, {rate} per hour spent in voice.",
                    "category": "success",
                }
            ]
        return [
            {
                "message": f"Automatic payday is on. The whole server is paid at "
                f"once every {window}.",
                "category": "success",
            }
        ]

    @staticmethod
    def _eco_colour_int(value) -> int:
        raw = (value or "").strip().lstrip("#")
        try:
            return int(raw, 16) if raw else 0
        except ValueError:
            return 0

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

    async def _eco_bank_settings(
        self, guild: discord.Guild, field, is_owner: bool
    ) -> list[dict]:
        """The bank's own settings, which live in core `bank`, not in Economy's
        config: name, currency, and the balance limits.

        Everything here writes through the `bank` helpers rather than poking
        Config directly, so the same validation the commands get applies.
        """
        global_bank = await bank.is_global()
        # A global bank is bot-wide, so only the owner may retune it, and the
        # helpers take no guild in that mode.
        if global_bank and not is_owner:
            return [{"message": "The bank is global, so only the bot owner can change "
                                "these settings.", "category": "warning"}]
        scope_guild = None if global_bank else guild

        notes: list[dict] = []
        changed: list[str] = []

        name = (field("bank_name") or "").strip()
        if name:
            try:
                await bank.set_bank_name(name, scope_guild)
                changed.append("name")
            except Exception as exc:  # noqa: BLE001
                notes.append({"message": f"Could not rename the bank: {exc}", "category": "danger"})

        currency = (field("currency_name") or "").strip()
        if currency:
            try:
                await bank.set_currency_name(currency, scope_guild)
                changed.append("currency")
            except Exception as exc:  # noqa: BLE001
                notes.append({"message": f"Could not set the currency: {exc}", "category": "danger"})

        for key, setter, label in (
            ("max_balance", bank.set_max_balance, "maximum balance"),
            ("default_balance", bank.set_default_balance, "starting balance"),
        ):
            raw = (field(key) or "").strip()
            if not raw:
                continue
            try:
                amount = int(raw)
            except ValueError:
                notes.append({"message": f"The {label} must be a whole number.",
                              "category": "warning"})
                continue
            if amount < 0:
                notes.append({"message": f"The {label} cannot be negative.",
                              "category": "warning"})
                continue
            try:
                await setter(amount, scope_guild)
                changed.append(label)
            except Exception as exc:  # noqa: BLE001
                # set_default_balance refuses anything above the maximum, which
                # is a real answer rather than a failure - pass it straight on.
                notes.append({"message": f"Could not set the {label}: {exc}",
                              "category": "warning"})

        if changed:
            notes.append({"message": "Updated the bank's " + humanize_list(changed) + ".",
                          "category": "success"})
        elif not notes:
            notes.append({"message": "Nothing to change.", "category": "info"})
        return notes

    async def _eco_bank_danger(
        self, action: str, guild: discord.Guild, field, is_owner: bool
    ) -> list[dict]:
        """Pruning, wiping, and switching the bank between global and per-server.

        Each of these destroys balances, so each is gated and each is confirmed
        by typing the exact word the page asks for.
        """
        global_bank = await bank.is_global()
        confirm = (field("confirm") or "").strip().upper()

        if action == "bank_prune":
            if global_bank and not is_owner:
                return [{"message": "Pruning a global bank is owner-only.", "category": "warning"}]
            if confirm != "PRUNE":
                return [{"message": 'Type PRUNE in the box to confirm.', "category": "warning"}]
            try:
                await bank.bank_prune(self.bot, guild=None if global_bank else guild)
            except Exception as exc:  # noqa: BLE001
                return [{"message": f"Could not prune: {exc}", "category": "danger"}]
            return [{"message": "Pruned accounts belonging to people who have left.",
                     "category": "success"}]

        if action == "bank_wipe":
            if global_bank and not is_owner:
                return [{"message": "Wiping a global bank is owner-only.", "category": "warning"}]
            if confirm != "WIPE":
                return [{"message": 'Type WIPE in the box to confirm.', "category": "warning"}]
            try:
                await bank.wipe_bank(None if global_bank else guild)
            except Exception as exc:  # noqa: BLE001
                return [{"message": f"Could not wipe the bank: {exc}", "category": "danger"}]
            return [{"message": "Deleted every account in this bank.", "category": "success"}]

        if action == "bank_scope":
            if not is_owner:
                return [{"message": "Switching the bank between global and per-server is "
                                    "owner-only.", "category": "warning"}]
            if confirm != "SWITCH":
                return [{"message": 'Type SWITCH in the box to confirm.', "category": "warning"}]
            try:
                new_mode = await bank.set_global(not global_bank)
            except Exception as exc:  # noqa: BLE001
                return [{"message": f"Could not switch the bank: {exc}", "category": "danger"}]
            return [{
                "message": ("The bank is now global; every server shares one balance."
                            if new_mode else
                            "The bank is now per-server; each server has its own balances."),
                "category": "success",
            }]

        return [{"message": "Unknown action.", "category": "warning"}]

    async def _eco_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

        if action in ("balance", "transfer", "role_payday"):
            return await self._eco_bank_action(action, guild, field)

        if action == "save_autopayday":
            return await self._eco_save_autopayday(guild, field)

        if action == "bank_settings":
            return await self._eco_bank_settings(guild, field, kwargs.get("_is_owner", False))

        if action in ("bank_prune", "bank_wipe", "bank_scope"):
            return await self._eco_bank_danger(
                action, guild, field, kwargs.get("_is_owner", False)
            )

        if action == "run_payday":
            # A button press is the ask; it should not wait on the server clock.
            summary = await self.run_auto_payday(guild, force=True)
            if not summary["paid"]:
                if summary.get("voice"):
                    return [
                        {"message": "Nobody has spent enough time in voice to earn "
                         "anything yet.", "category": "info"}
                    ]
                return [
                    {"message": "Nobody was paid; check the amount and the required role.",
                     "category": "info"}
                ]
            currency = await bank.get_currency_name(guild)
            note = ""
            if summary.get("voice"):
                note = f" for {round(summary['voice_seconds'] / 3600, 1)} hours in voice"
            return [
                {
                    "message": f"Paid {summary['total']} {currency} to "
                    f"{summary['paid']} member(s){note}.",
                    "category": "success",
                }
            ]

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
<style>
  .eco-slip { margin-top:11px; padding:10px 13px; border-radius:8px;
              border-left:4px solid #f1c40f; background:rgba(255,255,255,.04); }
  .eco-slip-t { font-weight:600; font-size:.86rem; margin-bottom:7px; }
  .eco-slip-img { max-width:100%; max-height:190px; border-radius:6px; display:block; }
  .eco-slip-bad { font-size:.75rem; color:#ff8b8b; }
  .eco-rank { display:flex; align-items:center; gap:9px; padding:3px 0;
              font-size:.82rem; }
  .eco-pos { width:24px; text-align:center; opacity:.6; flex:none; }
  .eco-who { flex:1 1 auto; overflow:hidden; text-overflow:ellipsis;
             white-space:nowrap; }
  .eco-bal { font-weight:600; flex:none; }
</style>

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
        <h5><i class="fa fa-bank"></i> Bank</h5>
        <p class="dz-hint">
          {% if global_bank %}
            One bank shared by every server.
            {% if not is_owner %}Only the bot owner can change these.{% endif %}
          {% else %}
            This server's own bank. Leave a box empty to keep what it has now.
          {% endif %}
        </p>
        <div class="dz-grid two">
          <div>
            <div class="dz-label">Bank name</div>
            <input class="dz-input" type="text" name="bank_name" maxlength="100"
                   placeholder="{{ bank_name }}"
                   {% if global_bank and not is_owner %}disabled{% endif %} />
            <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
              Currently <b>{{ bank_name }}</b>.
            </div>
          </div>
          <div>
            <div class="dz-label">Currency name</div>
            <input class="dz-input" type="text" name="currency_name" maxlength="100"
                   placeholder="{{ currency }}"
                   {% if global_bank and not is_owner %}disabled{% endif %} />
            <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
              Currently <b>{{ currency }}</b>.
            </div>
          </div>
          <div>
            <div class="dz-label">Starting balance</div>
            <input class="dz-input" type="number" min="0" name="default_balance"
                   placeholder="{{ default_balance }}"
                   {% if global_bank and not is_owner %}disabled{% endif %} />
            <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
              What a brand new account opens with.
            </div>
          </div>
          <div>
            <div class="dz-label">Maximum balance</div>
            <input class="dz-input" type="number" min="0" name="max_balance"
                   placeholder="{{ max_balance }}"
                   {% if global_bank and not is_owner %}disabled{% endif %} />
            <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
              Nobody can hold more than this.
            </div>
          </div>
        </div>
        {% if not (global_bank and not is_owner) %}
          <div style="margin-top:14px;">
            <button class="dz-btn primary" name="action" value="bank_settings">
              <i class="fa fa-save"></i> Save bank settings
            </button>
          </div>
        {% endif %}
      </div>
    </form>

    {% if is_owner or not global_bank %}
      <div class="dz-panel">
        <h5><i class="fa fa-exclamation-triangle" style="color:#f0aa3c;"></i> Danger zone</h5>
        <p class="dz-hint">
          Each of these destroys balances and cannot be undone, so each needs the
          word typing out in full.
        </p>

        <div class="dz-grid two">
          <form method="POST">
            <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
            <div class="dz-label">Prune accounts</div>
            <p class="dz-hint">
              Deletes accounts belonging to people who are no longer
              {% if global_bank %}known to the bot{% else %}in this server{% endif %}.
            </p>
            <div class="dz-row">
              <input class="dz-input" type="text" name="confirm" placeholder="Type PRUNE" />
              <button class="dz-btn" name="action" value="bank_prune">
                <i class="fa fa-eraser"></i> Prune
              </button>
            </div>
          </form>

          <form method="POST">
            <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
            <div class="dz-label">Wipe the bank</div>
            <p class="dz-hint">Deletes every account and every balance in this bank.</p>
            <div class="dz-row">
              <input class="dz-input" type="text" name="confirm" placeholder="Type WIPE" />
              <button class="dz-btn danger" name="action" value="bank_wipe">
                <i class="fa fa-trash"></i> Wipe
              </button>
            </div>
          </form>
        </div>

        {% if is_owner %}
          <form method="POST" style="margin-top:16px;">
            <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
            <div class="dz-label">Bank scope</div>
            <p class="dz-hint">
              The bank is <b>{% if global_bank %}global{% else %}per-server{% endif %}</b>.
              Switching to {% if global_bank %}per-server{% else %}global{% endif %}
              <b>resets every account</b> — that is how the switch works, not a bug.
            </p>
            <div class="dz-row">
              <input class="dz-input" type="text" name="confirm" placeholder="Type SWITCH" />
              <button class="dz-btn danger" name="action" value="bank_scope">
                <i class="fa fa-exchange"></i>
                Switch to {% if global_bank %}per-server{% else %}global{% endif %}
              </button>
            </div>
          </form>
        {% endif %}
      </div>
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


  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-clock-o"></i> Automatic payday</h5>
      <p class="dz-hint">
        Hand out the payday on its own timer so nobody has to run the command.
        The whole server is paid together on one clock, so there is a single
        payslip per run no matter when somebody joined. While this is on, the
        <code>payday</code> command just tells members when the next one lands.
      </p>

      <label class="dz-toggle">
        <input type="checkbox" name="auto_enabled" {% if auto_enabled %}checked{% endif %} />
        <span>Pay everyone automatically
          <span class="dz-tag">{{ eligible }} member{{ '' if eligible == 1 else 's' }}</span>
        </span>
      </label>

      <div class="dz-label" style="margin-top:11px;">Only pay members with this role</div>
      {{ picker('auto_role', auto_roles, allow_none=true, none_label='everyone') }}

      <div style="margin-top:16px; padding-top:13px;
                  border-top:1px solid rgba(255,255,255,.07);">
        <div class="dz-label">How much everyone gets</div>
        <select class="dz-input" name="auto_mode" id="eco-mode"
                style="max-width:280px;"
                onchange="document.getElementById('eco-voice').style.display =
                          this.value === 'voice' ? 'block' : 'none';">
          <option value="fixed" {% if auto_mode != 'voice' %}selected{% endif %}>
            The same amount for everyone
          </option>
          <option value="voice" {% if auto_mode == 'voice' %}selected{% endif %}>
            Paid for time spent in voice
          </option>
        </select>
        <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
          The fixed amount is the payday amount above. Voice pay counts only the
          time since the last payslip, so it rewards the day just gone.
        </div>

        <div id="eco-voice"
             style="display:{% if auto_mode == 'voice' %}block{% else %}none{% endif %};
                    margin-top:13px;">
          <div class="dz-grid two">
            <div>
              <div class="dz-label">{{ currency }} per hour in voice</div>
              <input class="dz-input" type="number" min="1" name="auto_voice_rate"
                     value="{{ auto_voice_rate }}" />
            </div>
            <div>
              <div class="dz-label">Minutes needed to earn anything</div>
              <input class="dz-input" type="number" min="0" name="auto_voice_min"
                     value="{{ auto_voice_min }}" />
            </div>
          </div>

          <div class="dz-label" style="margin-top:11px;">Most anyone can earn per payday</div>
          <input class="dz-input" type="number" min="0" name="auto_voice_max"
                 value="{{ auto_voice_max }}" style="max-width:200px;" />
          <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
            0 for no cap. Per-role payday amounts act as per-hour rates in this
            mode, so a role that pays more still pays more.
          </div>

          <div style="margin-top:12px;">
            <label class="dz-toggle">
              <input type="checkbox" name="auto_voice_alone"
                     {% if auto_voice_alone %}checked{% endif %} />
              <span>Count time spent alone in a channel</span>
            </label>
            <label class="dz-toggle">
              <input type="checkbox" name="auto_voice_deaf"
                     {% if auto_voice_deaf %}checked{% endif %} />
              <span>Count time spent deafened</span>
            </label>
            <label class="dz-toggle">
              <input type="checkbox" name="auto_voice_afk"
                     {% if auto_voice_afk %}checked{% endif %} />
              <span>Count time in the AFK channel</span>
            </label>
          </div>

          <div class="eco-slip" style="border-left-color:{{ auto_colour }}; margin-top:13px;">
            <div class="eco-slip-t">
              Banked since the last payslip
              {% if auto_voice_live %}
                <span class="dz-tag">{{ auto_voice_live }} in voice now</span>
              {% endif %}
            </div>
            {% if auto_voice_preview %}
              {% for row in auto_voice_preview %}
                <div class="eco-rank">
                  <span class="eco-who">{{ row.name }}</span>
                  <span class="eco-bal" {% if row.short %}style="opacity:.4;"{% endif %}>
                    {{ row.minutes }} min &rarr; {{ "{:,}".format(row.amount) }}
                    {%- if row.short %} (under the minimum){% endif %}
                  </span>
                </div>
              {% endfor %}
            {% else %}
              <p class="dz-empty" style="margin:0;">
                Nothing banked yet. Time starts counting as soon as somebody talks.
              </p>
            {% endif %}
          </div>
        </div>
      </div>

      <div style="margin-top:16px; padding-top:13px;
                  border-top:1px solid rgba(255,255,255,.07);">
        <label class="dz-toggle">
          <input type="checkbox" name="auto_announce"
                 {% if auto_announce %}checked{% endif %} />
          <span>Post a payslip after each run</span>
        </label>

        <div class="dz-label" style="margin-top:11px;">Payslip channel</div>
        {{ picker('auto_channel', auto_channels, allow_none=true, none_label='pick a channel') }}

        <div class="dz-grid two" style="margin-top:11px;">
          <div>
            <div class="dz-label">Title</div>
            <input class="dz-input" type="text" name="auto_title" value="{{ auto_title }}"
                   placeholder="{{ auto_title_default }}" />
          </div>
          <div>
            <div class="dz-label">Accent colour</div>
            <input class="dz-input" type="text" name="auto_colour" value="{{ auto_colour }}"
                   placeholder="#f1c40f" />
          </div>
        </div>

        <div class="dz-label" style="margin-top:11px;">Message</div>
        <textarea class="dz-area" name="auto_message"
                  style="min-height:62px;">{{ auto_message }}</textarea>
        <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
          Defaults to <code>{{ auto_message_default }}</code>. You can use
          <code>{bot}</code>, <code>{guild}</code>, <code>{currency}</code>,
          <code>{total}</code>, <code>{members}</code>, <code>{average}</code>
          and <code>{voice}</code>.
        </div>

        <div style="margin-top:15px; padding-top:12px;
                    border-top:1px solid rgba(255,255,255,.07);">
          <label class="dz-toggle">
            <input type="checkbox" name="auto_board" {% if auto_board %}checked{% endif %} />
            <span>List the richest members on the payslip</span>
          </label>

          <div class="dz-label" style="margin-top:10px;">How many to show</div>
          <input class="dz-input" type="number" min="1" max="{{ auto_board_max }}"
                 name="auto_board_size" value="{{ auto_board_size }}"
                 style="max-width:160px;" />
          <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
            1 to {{ auto_board_max }}. Anyone on zero is left off.
          </div>

          {% if auto_board %}
            <div class="eco-slip" style="border-left-color:{{ auto_colour }}; margin-top:11px;">
              <div class="eco-slip-t">Richest in {{ guild_name }}</div>
              {% if auto_board_preview %}
                {% for row in auto_board_preview %}
                  <div class="eco-rank">
                    <span class="eco-pos">
                      {%- if row.position == 1 %}&#129351;
                      {%- elif row.position == 2 %}&#129352;
                      {%- elif row.position == 3 %}&#129353;
                      {%- else %}{{ row.position }}.{% endif -%}
                    </span>
                    <span class="eco-who">{{ row.name }}</span>
                    <span class="eco-bal">{{ "{:,}".format(row.balance) }}</span>
                  </div>
                {% endfor %}
              {% else %}
                <p class="dz-empty" style="margin:0;">Nobody holds any {{ currency }} yet.</p>
              {% endif %}
            </div>
          {% endif %}
        </div>

        <div class="dz-label" style="margin-top:11px;">Gif or image</div>
        <input class="dz-input" type="url" name="auto_image" value="{{ auto_image }}"
               placeholder="https://.../payday.gif" />
        <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
          A direct http(s) link, shown under everything else. Leave it empty to
          let the leaderboard stand on its own.
        </div>

        {% if auto_image %}
          <div class="eco-slip" style="border-left-color:{{ auto_colour }};">
            <div class="eco-slip-t">{{ auto_title or auto_title_default }}</div>
            <img src="{{ auto_image }}" alt="" class="eco-slip-img"
                 onerror="this.replaceWith(Object.assign(document.createElement('div'),
                          {className:'eco-slip-bad',
                           textContent:'That link did not load as an image.'}));" />
          </div>
        {% endif %}
      </div>

      <div class="dz-save">
        <button class="dz-btn primary" name="action" value="save_autopayday">
          <i class="fa fa-save"></i> Save
        </button>
        <button class="dz-btn" name="action" value="run_payday"
                onclick="return confirm('Pay the whole server right now?');">
          <i class="fa fa-bolt"></i> Run one now
        </button>
      </div>
    </div>
  </form>

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
