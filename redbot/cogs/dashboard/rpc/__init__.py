import asyncio
import base64
import logging
import pathlib
import random
import re
import time
import typing

import discord

from redbot.core import bank, commands, core_commands, i18n
from redbot.core.bot import Red
from redbot.core.i18n import Translator
from redbot.core.utils import AsyncIter
from redbot.core.utils.chat_formatting import humanize_list

from .cog_management import DashboardRPC_CogManagement
from .default_cogs import DashboardRPC_DefaultCogs
from .pagination import Pagination
from .third_parties import DashboardRPC_ThirdParties
from ..logs import LEVELS
from .utils import rpc_check
from .webhooks import DashboardRPC_Webhooks

log = logging.getLogger("red.dashboard.rpc")

# Credits:
# Thank you to NeuroAssassin for the original code.

_: Translator = Translator("Dashboard", __file__)


class DashboardRPC:
    """RPC server handlers for the dashboard to get special things from the bot."""

    def __init__(self, bot: Red, cog: commands.Cog) -> None:
        self.bot: Red = bot
        self.cog: commands.Cog = cog

        # To make sure that both RPC server and client are on the same "version".
        self.version: int = random.randint(1, 10000)

        # Initialize RPC handlers.
        self.bot.register_rpc_handler(self.check_version)
        self.bot.register_rpc_handler(self.get_user_profile)
        self.bot.register_rpc_handler(self.get_user_access)
        self.bot.register_rpc_handler(self.get_home_guilds)
        self.bot.register_rpc_handler(self.get_server_list)
        self.bot.register_rpc_handler(self.set_guild_bot_profile)
        self.bot.register_rpc_handler(self.get_data)
        self.bot.register_rpc_handler(self.get_variables)
        self.bot.register_rpc_handler(self.get_bot_variables)
        self.bot.register_rpc_handler(self.get_commands)
        self.bot.register_rpc_handler(self.get_user_guilds)
        self.bot.register_rpc_handler(self.get_guild)
        self.bot.register_rpc_handler(self.leave_guild)
        self.bot.register_rpc_handler(self.set_guild_settings)
        self.bot.register_rpc_handler(self.set_bot_profile)
        self.bot.register_rpc_handler(self.get_dashboard_settings)
        self.bot.register_rpc_handler(self.set_dashboard_settings)
        self.bot.register_rpc_handler(self.get_bot_settings)
        self.bot.register_rpc_handler(self.set_bot_settings)
        self.bot.register_rpc_handler(self.set_custom_pages)
        self.bot.register_rpc_handler(self.get_logs)
        self.bot.register_rpc_handler(self.bump_session_epoch)

        # Initialize handlers.
        self.handlers: dict[str, typing.Any] = {}
        self.handlers["cog_management"]: DashboardRPC_CogManagement = DashboardRPC_CogManagement(
            self.cog,
        )
        self.handlers["default_cogs"]: DashboardRPC_DefaultCogs = DashboardRPC_DefaultCogs(self.cog)
        self.handlers["webhooks"]: DashboardRPC_Webhooks = DashboardRPC_Webhooks(self.cog)
        self.third_parties_handler: DashboardRPC_ThirdParties = DashboardRPC_ThirdParties(self.cog)
        self.handlers["third_parties"]: DashboardRPC_ThirdParties = self.third_parties_handler

        # Caches: you can thank Trusty for the cogs infos.
        self.invite_url: str = None
        self.owner: str = None
        self.cogs_infos_cache: dict[str, dict[str, str]] = {}
        # Short-lived, keyed by user: a REST fetch for the banner, and the
        # nav's "can this person reach the Dashboard at all" answer.
        self.user_fetch_cache: dict[int, tuple[float, typing.Any]] = {}
        self.user_access_cache: dict[int, tuple[float, dict]] = {}
        self.home_guilds_cache: dict[int, tuple[float, dict]] = {}
        # Keyed by viewer (0 = anonymous), because the same list is
        # annotated differently depending on who is looking at it.
        self.server_list_cache: dict[int, tuple[float, dict]] = {}
        # Invites change rarely and cost an API call each, so they outlive
        # the directory's own 60-second cache.
        self.server_invite_cache: dict[int, tuple[float, str]] = {}
        self.guild_profile_cache: dict[int, tuple[float, dict]] = {}
        self.guilds_cache: dict[
            int,
            dict[
                typing.Literal["guilds", "time"],
                list[dict] | int,
            ],
        ] = {}

    def unload(self) -> None:
        if hasattr(self.bot, "dashboard_url"):
            delattr(self.bot, "dashboard_url")
        self.bot.unregister_rpc_handler(self.check_version)
        self.bot.unregister_rpc_handler(self.get_data)
        self.bot.unregister_rpc_handler(self.get_variables)
        self.bot.unregister_rpc_handler(self.get_bot_variables)
        self.bot.unregister_rpc_handler(self.get_commands)
        self.bot.unregister_rpc_handler(self.get_user_guilds)
        self.bot.unregister_rpc_handler(self.get_guild)
        self.bot.unregister_rpc_handler(self.leave_guild)
        self.bot.unregister_rpc_handler(self.set_guild_settings)
        self.bot.unregister_rpc_handler(self.set_bot_profile)
        self.bot.unregister_rpc_handler(self.get_dashboard_settings)
        self.bot.unregister_rpc_handler(self.set_dashboard_settings)
        self.bot.unregister_rpc_handler(self.get_bot_settings)
        self.bot.unregister_rpc_handler(self.set_bot_settings)
        self.bot.unregister_rpc_handler(self.get_user_profile)
        self.bot.unregister_rpc_handler(self.get_user_access)
        self.bot.unregister_rpc_handler(self.get_home_guilds)
        self.bot.unregister_rpc_handler(self.get_server_list)
        self.bot.unregister_rpc_handler(self.set_guild_bot_profile)
        self.bot.unregister_rpc_handler(self.set_custom_pages)
        self.bot.unregister_rpc_handler(self.get_logs)
        self.bot.unregister_rpc_handler(self.bump_session_epoch)
        for handler in self.handlers.values():
            handler.unload()

    # Discord's public-flag bitfield, as the badges a profile should show.
    # discord.py exposes these as `PublicUserFlags` attributes; the icons are
    # Font Awesome names so the page needs no extra assets.
    USER_BADGES = (
        ("staff", "Discord Staff", "fa-shield", "#5865f2"),
        ("partner", "Discord Partner", "fa-handshake-o", "#5865f2"),
        ("hypesquad", "HypeSquad Events", "fa-calendar", "#fbb848"),
        ("hypesquad_bravery", "HypeSquad Bravery", "fa-bolt", "#9c84ef"),
        ("hypesquad_brilliance", "HypeSquad Brilliance", "fa-diamond", "#f47b67"),
        ("hypesquad_balance", "HypeSquad Balance", "fa-balance-scale", "#45ddc0"),
        ("bug_hunter", "Bug Hunter", "fa-bug", "#3ba55d"),
        ("bug_hunter_level_2", "Bug Hunter Gold", "fa-bug", "#fbb848"),
        ("early_supporter", "Early Supporter", "fa-heart", "#ff73fa"),
        ("verified_bot_developer", "Verified Bot Developer", "fa-code", "#5865f2"),
        ("active_developer", "Active Developer", "fa-terminal", "#3ba55d"),
        ("discord_certified_moderator", "Certified Moderator", "fa-gavel", "#5865f2"),
    )

    async def _fetch_user_full(self, user_id: int):
        """A `User` with banner and accent colour on it.

        `get_user` only ever returns the gateway's cached object, which carries
        neither - they come back exclusively from a REST fetch. That is a real
        HTTP call, so the result is held for a while: a banner does not change
        between two page loads.
        """
        cached = self.user_fetch_cache.get(user_id)
        if cached is not None and (cached[0] + 900) > time.time():
            return cached[1]
        try:
            user = await self.bot.fetch_user(user_id)
        except Exception:  # noqa: BLE001 - fall back to the cached object
            user = None
        self.user_fetch_cache[user_id] = (time.time(), user)
        return user

    # Discord's status values, and the colour each one is drawn in.
    STATUS_COLOURS = {
        "online": ("Online", "#3ba55d"),
        "idle": ("Idle", "#faa81a"),
        "dnd": ("Do Not Disturb", "#ed4245"),
        "offline": ("Offline", "#80848e"),
    }

    def _presence(self, user_id: int) -> dict:
        """Status and what they are doing, from whichever server can see it.

        Presence rides on `Member`, not `User`, and only exists at all when the
        bot has the presence intent. A member object is looked at in every
        shared server because a single one can report `offline` while another
        has the real state.
        """
        out = {"status": "offline", "status_label": "Offline",
               "status_colour": "#80848e", "activity": None, "has_presence": False}
        best = None
        for guild in self.bot.guilds:
            member = guild.get_member(user_id)
            if member is None:
                continue
            try:
                status = str(member.status)
            except Exception:  # noqa: BLE001 - no presence intent
                continue
            out["has_presence"] = True
            if status != "offline":
                best = member
                out["status"] = status
                break
            best = best or member
        label, colour = self.STATUS_COLOURS.get(out["status"], ("Offline", "#80848e"))
        out["status_label"], out["status_colour"] = label, colour

        if best is None:
            return out
        try:
            for activity in best.activities:
                kind = getattr(getattr(activity, "type", None), "name", "") or ""
                name = getattr(activity, "name", "") or ""
                # A custom status is the one Discord shows as free text, and it
                # carries its own emoji rather than a name.
                if kind == "custom":
                    emoji = getattr(activity, "emoji", None)
                    out["activity"] = {
                        "kind": "custom",
                        "verb": "",
                        "name": getattr(activity, "state", "") or "",
                        "details": "",
                        "emoji": str(emoji) if emoji else "",
                        "image": "",
                    }
                    continue
                if not name:
                    continue
                verb = {
                    "playing": "Playing", "streaming": "Streaming",
                    "listening": "Listening to", "watching": "Watching",
                    "competing": "Competing in",
                }.get(kind, "")
                image = ""
                details = getattr(activity, "details", "") or ""
                # Spotify is its own activity type with nicer fields.
                if type(activity).__name__ == "Spotify":
                    verb, name = "Listening to", getattr(activity, "title", name)
                    details = getattr(activity, "artist", "") or ""
                    image = getattr(activity, "album_cover_url", "") or ""
                if not image:
                    image = getattr(activity, "large_image_url", "") or ""
                entry = {
                    "kind": kind, "verb": verb, "name": name,
                    "details": details, "emoji": "", "image": image,
                }
                # A real activity outranks a custom status line.
                if out["activity"] is None or out["activity"]["kind"] == "custom":
                    out["activity"] = entry
        except Exception:  # noqa: BLE001
            log.debug("Could not read activities for %s", user_id, exc_info=True)
        return out

    @staticmethod
    def _banner_url(user) -> str:
        """The banner, asked for at a size worth showing.

        discord.py hands back a 512px asset by default, which a page header
        several times that wide then upscales into mush. Banners go up to
        4096; 1024 is the point where it stops looking soft without making an
        animated one enormous.
        """
        banner = getattr(user, "banner", None)
        if banner is None:
            return ""
        try:
            return banner.with_size(1024).url
        except Exception:  # noqa: BLE001 - any refusal falls back to the default
            return banner.url

    def _levelup_profile(self, guild_id: int, user_id: int) -> dict | None:
        """Level, XP and activity for one member, if LevelUp is loaded.

        Reads the mapping directly rather than through `get_profile`, which is
        a `setdefault` and would write an empty profile for every member whose
        page happened to be looked at.
        """
        cog = self.bot.get_cog("LevelUp")
        if cog is None:
            return None
        try:
            conf = cog.db.configs.get(guild_id)
            if conf is None:
                return None
            profile = conf.users.get(user_id)
            if profile is None:
                return None
            return {
                "level": int(profile.level),
                "prestige": int(profile.prestige),
                "xp": int(profile.xp),
                "messages": int(profile.messages),
                "voice_hours": round(profile.voice / 3600, 1),
                "stars": int(profile.stars),
            }
        except Exception:  # noqa: BLE001 - a cog's internals are not a contract
            return None

    # What to pull out of each cog, and how to show it. Every entry is read
    # through the cog's own Config, scoped to this user, so nothing here
    # depends on a cog's internals beyond the keys it registers.
    #
    #   (cog name, scope, icon, colour)
    # `scope` is "member" for per-server data (summed across shared servers),
    # "user" for global data, and "both" where the cog picks at runtime.
    # SimpleCasino is the latter: it writes to the user scope on a global bank
    # and to the member scope otherwise, so reading only one of them came back
    # empty on any bot with per-server banks. Reading both is safe because only
    # one is ever written to.
    COG_STATS = (
        ("Warnings", "member", "fa-exclamation-triangle", "#ffb454"),
        ("Trivia", "member", "fa-question-circle", "#6c8cff"),
        ("Tickets", "member", "fa-ticket", "#38d39f"),
        ("SimpleCasino", "both", "fa-diamond", "#ff73fa"),
        ("Hunting", "user", "fa-crosshairs", "#a78bfa"),
        ("MafiaGame", "user", "fa-user-secret", "#ff6b6b"),
    )

    async def _read_config(self, cog, scope: str, user_id: int, guild_ids: list[int]) -> list[dict]:
        """Every stored blob for this person, from one cog's Config."""
        config = getattr(cog, "config", None)
        if config is None:
            return []
        out = []
        try:
            if scope in ("user", "both"):
                out.append(await config.user_from_id(user_id).all())
            if scope in ("member", "both"):
                for guild_id in guild_ids:
                    out.append(await config.member_from_ids(guild_id, user_id).all())
        except Exception:  # noqa: BLE001 - a cog's storage is not a contract
            log.debug("Could not read %s config", getattr(cog, "qualified_name", cog), exc_info=True)
            return []
        return [blob for blob in out if blob]

    @staticmethod
    def _total(blobs: list[dict], key: str) -> int:
        """Sum a numeric field, or a mapping whose values are counts.

        Some cogs key their counters by mode or difficulty, e.g. MafiaGame's
        ``wins = {"villager": 3}``.
        """
        total = 0
        for blob in blobs:
            value = blob.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                total += int(value)
            elif isinstance(value, dict):
                total += sum(
                    int(v) for v in value.values()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)
                )
        return total

    @staticmethod
    def _count(blobs: list[dict], key: str) -> int:
        """Count the entries in a collection field.

        The counterpart to `_total`: Warnings stores ``warnings`` as a mapping
        of id to record, so what matters is how many there are, not the sum of
        anything inside them - which is nothing, and is why summing produced a
        flat zero and dropped the row entirely.
        """
        total = 0
        for blob in blobs:
            value = blob.get(key)
            if isinstance(value, dict):
                # A dict of lists holds its entries one level down - MafiaGame
                # stores achievements as {location: [name, ...]}, so counting
                # the dict itself counted locations instead of achievements.
                if value and all(isinstance(v, (list, tuple, set)) for v in value.values()):
                    total += sum(len(v) for v in value.values())
                else:
                    total += len(value)
            elif isinstance(value, (list, tuple, set)):
                total += len(value)
        return total

    async def _cog_stats(self, user_id: int, guild_ids: list[int]) -> list[dict]:
        """Per-cog activity for this person, as cards the page can just render.

        Only cogs that are loaded *and* have something to say appear; a cog
        that stores nothing but zeroes is not worth a card.
        """
        cards = []
        skipped = []
        for name, scope, icon, colour in self.COG_STATS:
            cog = self.bot.get_cog(name)
            if cog is None:
                skipped.append(f"{name}: cog not loaded")
                continue
            blobs = await self._read_config(cog, scope, user_id, guild_ids)
            if not blobs:
                skipped.append(f"{name}: nothing stored for this user")
                continue

            rows: list[dict] = []
            if name == "Warnings":
                points = self._total(blobs, "total_points")
                count = self._count(blobs, "warnings")
                rows = [{"label": "Warnings", "value": count},
                        {"label": "Points", "value": points}]
            elif name == "Trivia":
                games = self._total(blobs, "games")
                wins = self._total(blobs, "wins")
                rows = [{"label": "Games", "value": games},
                        {"label": "Wins", "value": wins},
                        {"label": "Score", "value": self._total(blobs, "total_score")}]
            elif name == "Tickets":
                rows = [{"label": "Opened", "value": self._total(blobs, "tickets_number")},
                        {"label": "Closed", "value": self._total(blobs, "closed_tickets_number")}]
            elif name == "SimpleCasino":
                spins = self._total(blobs, "slotcount")
                hands = self._total(blobs, "bjcount")
                profit = self._total(blobs, "slotprofit")
                rows = [{"label": "Slot spins", "value": spins},
                        {"label": "Blackjack hands", "value": hands},
                        {"label": "Jackpots", "value": self._total(blobs, "slotjackpotcount")},
                        {"label": "Slot profit", "value": profit, "signed": True}]
            elif name == "Hunting":
                rows = [{"label": "Birds shot", "value": self._total(blobs, "total")},
                        {"label": "Kinds", "value": self._count(blobs, "score")}]
            elif name == "MafiaGame":
                rows = [{"label": "Games", "value": self._total(blobs, "games")},
                        {"label": "Wins", "value": self._total(blobs, "wins")},
                        {"label": "Achievements", "value": self._count(blobs, "achievements")}]

            if not rows:
                skipped.append(f"{name}: nothing to report")
                continue
            cards.append({
                "cog": name,
                "label": {"SimpleCasino": "Casino", "MafiaGame": "Mafia"}.get(name, name),
                "icon": icon,
                "colour": colour,
                "rows": rows,
            })
        # A card missing from the page is either a cog that is not loaded or a
        # cog with nothing to report, and those look identical from outside.
        if skipped:
            log.debug("Profile activity for %s skipped - %s", user_id, "; ".join(skipped))
        return cards

    @rpc_check()
    async def get_user_access(self, user_id: int) -> dict[str, typing.Any]:
        """How much of the Dashboard this person can actually reach.

        The nav uses this to decide whether to offer the Dashboard link at all,
        so it runs on ordinary page loads and has to stay cheap: membership and
        permission lookups only, no fetches, and cached briefly.
        """
        cached = self.user_access_cache.get(user_id)
        if cached is not None and (cached[0] + 120) > time.time():
            return cached[1]

        result = {
            "status": 0, "shared": 0, "manageable": 0,
            "avatar_url": "", "decoration_url": "",
            "presence": "offline", "presence_colour": "#80848e",
            # A server to land on when a page needs one and the URL has none -
            # the Audio link, which otherwise bounces through the guild picker.
            "default_guild": "",
        }
        shared_ids: list[str] = []
        manageable_ids: list[str] = []
        playing_ids: list[str] = []
        # Reached through the cog because pylav_auto_setup attaches the client
        # there, not to the bot.
        controller = self.bot.get_cog("PyLavController")
        pylav = getattr(controller, "pylav", None)
        user = self.bot.get_user(user_id)
        if user is not None:
            # The topbar draws the same avatar-and-frame as the profile does,
            # so the decoration comes back here too. It is behind the same
            # 15-minute fetch cache, not an extra call per page.
            result["avatar_url"] = user.display_avatar.url
            try:
                full = await self._fetch_user_full(user_id)
                decoration = getattr(full, "avatar_decoration", None) if full else None
                if decoration is not None:
                    result["decoration_url"] = decoration.url
            except Exception:  # noqa: BLE001
                pass
            presence = self._presence(user_id)
            result["presence"] = presence["status"]
            result["presence_colour"] = presence["status_colour"]
            is_owner = user_id in self.bot.owner_ids
            for guild in self.bot.guilds:
                member = guild.get_member(user_id)
                if member is None:
                    continue
                result["shared"] += 1
                shared_ids.append(str(guild.id))
                if (
                    is_owner
                    or guild.owner_id == user_id
                    or member.guild_permissions.administrator
                    or member.guild_permissions.manage_guild
                    or await self.bot.is_admin(member)
                    or await self.bot.is_mod(member)
                ):
                    result["manageable"] += 1
                    manageable_ids.append(str(guild.id))
                if pylav is not None:
                    try:
                        player = pylav.get_player(guild.id)
                        if player is not None and player.current is not None:
                            playing_ids.append(str(guild.id))
                    except Exception:  # noqa: BLE001 - audio is optional
                        pass

            # Somewhere music is already playing beats somewhere they merely
            # have rights, which beats the first server they happen to share.
            result["default_guild"] = next(
                iter(playing_ids or manageable_ids or shared_ids), ""
            )
        self.user_access_cache[user_id] = (time.time(), result)
        return result

    @rpc_check()
    async def get_home_guilds(self, user_id: int = None, limit: int = 24) -> dict[str, typing.Any]:
        """The servers to show on the landing page, with a few figures each.

        Deliberately scoped to servers this person is actually in: a bot's full
        server list is not the visitor's business, and an anonymous visitor
        gets nothing at all.

        Kept cheap - attribute reads and cached member lists only, no fetches -
        because it runs on the busiest page there is.
        """
        if not user_id:
            return {"status": 0, "guilds": [], "total": 0}

        user = self.bot.get_user(user_id)
        if user is None:
            return {"status": 0, "guilds": [], "total": 0}

        cached = self.home_guilds_cache.get(user_id)
        if cached is not None and (cached[0] + 60) > time.time():
            return cached[1]

        is_owner = user_id in self.bot.owner_ids
        rows = []
        for guild in self.bot.guilds:
            member = guild.get_member(user_id)
            if member is None:
                continue

            offline = discord.Status.offline
            online = None
            members = getattr(guild, "member_count", None) or len(guild.members)
            # Walking the member list is fine on a normal server and pointless
            # on a huge one, where the count is stale the moment it is read.
            if members <= 25000:
                try:
                    online = sum(1 for m in guild.members if not m.bot and m.status is not offline)
                except Exception:  # noqa: BLE001 - presences may be unavailable
                    online = None

            can_manage = bool(
                is_owner
                or guild.owner_id == user_id
                or member.guild_permissions.administrator
                or member.guild_permissions.manage_guild
                or await self.bot.is_admin(member)
                or await self.bot.is_mod(member)
            )

            rows.append(
                {
                    "id": str(guild.id),
                    "name": guild.name,
                    "icon": guild.icon.url if guild.icon else "",
                    "members": members,
                    "online": online,
                    "bots": sum(1 for m in guild.members if m.bot),
                    "text_channels": len(guild.text_channels),
                    "voice_channels": len(guild.voice_channels),
                    "roles": len(guild.roles),
                    "emojis": len(guild.emojis),
                    "boosts": guild.premium_subscription_count or 0,
                    "tier": guild.premium_tier or 0,
                    "owner": guild.owner.display_name if guild.owner else "",
                    "is_owner": guild.owner_id == user_id,
                    "can_manage": can_manage,
                    "created_at": guild.created_at.timestamp() if guild.created_at else None,
                }
            )

        # Biggest first: the servers somebody actually runs tend to be the ones
        # they came here for.
        rows.sort(key=lambda row: (-row["members"], row["name"].lower()))
        result = {"status": 0, "guilds": rows[:limit], "total": len(rows)}
        self.home_guilds_cache[user_id] = (time.time(), result)
        return result

    @rpc_check()
    async def get_server_list(
        self, user_id: int = None, limit: int = 60
    ) -> dict[str, typing.Any]:
        """Every server the bot is in, for the public directory on the landing page.

        Unlike `get_home_guilds` this is not scoped to the viewer: it is the
        bot's own server list, shown to anonymous visitors as well. Only what a
        server already publishes about itself goes out - name, icon, banner,
        description, member and presence counts, and the vanity invite if the
        server has one. No channel names, no member names, no owner identity,
        and no invite is ever created on the fly.

        When a viewer is known, their own servers are marked so the page can
        put them first and offer the ones they can manage.
        """
        cache_key = int(user_id or 0)
        cached = self.server_list_cache.get(cache_key)
        if cached is not None and (cached[0] + 60) > time.time():
            return cached[1]

        is_owner = bool(user_id) and user_id in self.bot.owner_ids
        offline = discord.Status.offline
        rows = []
        for guild in self.bot.guilds:
            member = guild.get_member(user_id) if user_id else None

            members = getattr(guild, "member_count", None) or len(guild.members)
            online = None
            # Same reasoning as `get_home_guilds`: walking a huge member list
            # costs more than the number is worth.
            if members <= 25000:
                try:
                    online = sum(
                        1 for m in guild.members if not m.bot and m.status is not offline
                    )
                except Exception:  # noqa: BLE001 - presences may be unavailable
                    online = None

            can_manage = False
            if member is not None:
                can_manage = bool(
                    is_owner
                    or guild.owner_id == user_id
                    or member.guild_permissions.administrator
                    or member.guild_permissions.manage_guild
                    or await self.bot.is_admin(member)
                    or await self.bot.is_mod(member)
                )

            rows.append(
                {
                    "id": str(guild.id),
                    "name": guild.name,
                    "icon": guild.icon.url if guild.icon else "",
                    "banner": guild.banner.url if guild.banner else "",
                    "description": guild.description or "",
                    "members": members,
                    "online": online,
                    "boosts": guild.premium_subscription_count or 0,
                    "tier": guild.premium_tier or 0,
                    "created_at": guild.created_at.timestamp() if guild.created_at else None,
                    "invite": await self._server_invite(guild),
                    "shared": member is not None,
                    "can_manage": can_manage,
                }
            )

        # Members first, so the directory leads with the servers most people
        # are looking for; a viewer's own servers get pulled to the front on
        # the page rather than here, so the ordering stays the same for
        # everyone who is not logged in.
        rows.sort(key=lambda row: (-row["members"], row["name"].lower()))
        result = {
            "status": 0,
            "guilds": rows[:limit],
            "total": len(rows),
            "total_members": sum(row["members"] for row in rows),
        }
        self.server_list_cache[cache_key] = (time.time(), result)
        return result

    async def _server_invite(self, guild: discord.Guild) -> str:
        """A link people can actually join with, or "" if there is not one.

        Preference order: the server's vanity URL, then an existing permanent
        invite it has already created. Nothing is ever created here - making an
        invite is a change to someone's server, and the directory only
        advertises what a server has already chosen to publish, which is why
        most entries have no button.

        Cached separately from the directory itself: listing invites is an API
        call per guild, and the answer changes far more slowly than member
        counts do.
        """
        if getattr(guild, "vanity_url_code", None):
            return f"https://discord.gg/{guild.vanity_url_code}"

        cached = self.server_invite_cache.get(guild.id)
        if cached is not None and (cached[0] + 900) > time.time():
            return cached[1]

        invite = ""
        # Listing invites needs Manage Server; without it this is simply a
        # server the directory cannot offer a link to.
        if guild.me is not None and guild.me.guild_permissions.manage_guild:
            try:
                for candidate in await guild.invites():
                    # Permanent and unlimited only: a link that expires or runs
                    # out of uses would be a dead button a week from now.
                    if not candidate.max_age and not candidate.max_uses:
                        invite = candidate.url
                        break
            except (discord.HTTPException, discord.Forbidden):
                log.debug("Could not list invites for %s", guild.id, exc_info=True)

        self.server_invite_cache[guild.id] = (time.time(), invite)
        return invite

    @rpc_check()
    async def get_user_profile(self, user_id: int) -> dict[str, typing.Any]:
        """Everything the profile page shows: identity, reach, and per-server standing."""
        user = self.bot.get_user(user_id)
        if user is None:
            return {"status": 1}

        full = await self._fetch_user_full(user_id) or user
        is_owner = user_id in self.bot.owner_ids

        badges = []
        try:
            flags = full.public_flags
            for attribute, label, icon, colour in self.USER_BADGES:
                if getattr(flags, attribute, False):
                    badges.append({"label": label, "icon": icon, "colour": colour})
        except Exception:  # noqa: BLE001
            pass

        # Nitro is not a public flag, but a member who has one of the things it
        # pays for is a safe enough signal to show it.
        try:
            if full.banner is not None or (full.avatar is not None and full.avatar.is_animated()):
                badges.append({"label": "Nitro", "icon": "fa-star", "colour": "#ff73fa"})
        except Exception:  # noqa: BLE001
            pass

        bank_is_global = False
        currency = "credits"
        try:
            bank_is_global = await bank.is_global()
            if bank_is_global:
                currency = await bank.get_currency_name()
        except Exception:  # noqa: BLE001 - economy is optional
            pass

        guilds = []
        owned = admin_of = mod_of = 0
        total_balance = 0
        for guild in sorted(self.bot.guilds, key=lambda g: g.name.lower()):
            member = guild.get_member(user_id)
            if member is None:
                continue

            if guild.owner_id == user_id:
                role, rank = "Owner", 3
                owned += 1
            elif member.guild_permissions.administrator or await self.bot.is_admin(member):
                role, rank = "Admin", 2
                admin_of += 1
            elif await self.bot.is_mod(member):
                role, rank = "Moderator", 1
                mod_of += 1
            else:
                role, rank = "Member", 0

            entry = {
                "id": str(guild.id),
                "name": guild.name,
                "icon": guild.icon.url if guild.icon else "",
                "role": role,
                "rank": rank,
                "members": guild.member_count or len(guild.members),
                "can_manage": bool(rank or is_owner),
                "joined_at": None,
                "top_role": "",
                "top_role_colour": "",
                "balance": None,
                "currency": currency,
                "level": self._levelup_profile(guild.id, user_id),
            }
            # Cosmetic extras, each optional. One server with an odd shape must
            # not take the whole profile down with it - which is exactly what
            # happened when this read `top_role.value` (the int lives on the
            # Colour, not on the Role) and every profile 404'd.
            try:
                if member.joined_at is not None:
                    entry["joined_at"] = member.joined_at.timestamp()
                top_role = member.top_role
                if top_role is not None:
                    entry["top_role"] = top_role.name
                    if top_role.colour.value:
                        entry["top_role_colour"] = str(top_role.colour)
            except Exception:  # noqa: BLE001
                log.debug("Could not read role details in %s", guild.id, exc_info=True)
            try:
                if not bank_is_global:
                    entry["balance"] = await bank.get_balance(member)
                    entry["currency"] = await bank.get_currency_name(guild)
                    total_balance += entry["balance"]
            except Exception:  # noqa: BLE001
                pass
            guilds.append(entry)

        # Owner first, then admin, then mod, then by name.
        guilds.sort(key=lambda g: (-g["rank"], g["name"].lower()))

        presence = self._presence(user_id)

        global_balance = None
        if bank_is_global:
            try:
                member = next(
                    (g.get_member(user_id) for g in self.bot.guilds if g.get_member(user_id)),
                    None,
                )
                if member is not None:
                    global_balance = await bank.get_balance(member)
            except Exception:  # noqa: BLE001
                pass

        return {
            "status": 0,
            "id": user.id,
            "name": user.display_name,
            "username": user.name,
            "global_name": getattr(user, "global_name", None),
            "avatar_url": user.display_avatar.url,
            "banner_url": self._banner_url(full),
            "accent_colour": (
                str(full.accent_colour) if getattr(full, "accent_colour", None) else ""
            ),
            "decoration_url": (
                full.avatar_decoration.url
                if getattr(full, "avatar_decoration", None)
                else ""
            ),
            "created_at": user.created_at.timestamp(),
            "is_owner": is_owner,
            "badges": badges,
            # Mapped key by key, not spread: `_presence` returns a "status" of
            # its own, and spreading it clobbered the payload's `status: 0`
            # success flag - which made the route treat every profile as an
            # error and serve a 404.
            "has_presence": presence["has_presence"],
            "presence": presence["status"],
            "presence_label": presence["status_label"],
            "presence_colour": presence["status_colour"],
            "activity": presence["activity"],
            "shared_guilds": len(guilds),
            "owned_guilds": owned,
            "admin_guilds": admin_of,
            "mod_guilds": mod_of,
            "manageable_guilds": sum(1 for g in guilds if g["can_manage"]),
            "bank_is_global": bank_is_global,
            "currency": currency,
            "global_balance": global_balance,
            "total_balance": total_balance,
            "guilds": guilds[:100],
            "cog_stats": await self._cog_stats(
                user_id, [int(g["id"]) for g in guilds]
            ),
        }

    @rpc_check()
    async def check_version(self) -> dict[str, int]:
        return {"version": self.bot.get_cog("Dashboard").rpc.version}

    @rpc_check()
    async def get_data(self) -> dict[str, typing.Any]:
        data = await self.cog.config.webserver()
        if data["ui"]["meta"]["title"] is None:
            data["ui"]["meta"]["title"] = _("{name} Dashboard").format(name=self.bot.user.name)
        else:
            data["ui"]["meta"]["title"] = data["ui"]["meta"]["title"].replace(
                "{name}",
                self.bot.user.name,
            )
        if data["ui"]["meta"]["icon"] is None:
            data["ui"]["meta"]["icon"] = self.bot.user.display_avatar.url
        if data["ui"]["meta"]["description"] is None:
            # About this bot, not about the software it happens to run on.
            # Set your own with `[p]setdashboard meta_description`.
            data["ui"]["meta"]["description"] = _(
                "Manage **{name}** from anywhere. Configure modules, moderate your "
                "servers and control the music player, all from one place.",
            ).format(name=self.bot.user.name)
        else:
            data["ui"]["meta"]["description"] = data["ui"]["meta"]["description"].replace(
                "{name}",
                self.bot.user.name,
            )
        if data["ui"]["meta"]["website_description"] is None:
            data["ui"]["meta"]["website_description"] = _(
                "Interactive Dashboard to control and interact with {name}.",
            ).format(name=self.bot.user.name)
        # if data["ui"]["meta"]["support_server"] is None:
        #     data["ui"]["meta"]["support_server"] = "https://discord.gg/red"
        return data

    @rpc_check()
    async def get_variables(
        self,
        only_bot_variables: bool = False,
        host_port: tuple[str, int] | None = None,
    ) -> dict[str, typing.Any]:
        variables = await self.get_bot_variables()
        variables.update(third_parties=await self.third_parties_handler.get_third_parties())
        variables.update(commands={} if only_bot_variables else await self.get_commands())
        if host_port is not None:
            redirect_uri = await self.cog.config.webserver.core.redirect_uri()
            host, port = host_port
            dashboard_url = (
                redirect_uri[:-9]
                if redirect_uri is not None
                else (
                    f"http://127.0.0.1:{port}"
                    if host in ("0.0.0.0", "127.0.0.1")
                    else f"http://{host}"
                )
            )
            is_private = redirect_uri is None and host in ("0.0.0.0", "127.0.0.1")
            setattr(self.bot, "dashboard_url", (dashboard_url, not is_private))
        return variables

    @rpc_check()
    async def get_bot_variables(self) -> dict[str, typing.Any]:
        bot_info = await self.bot._config.custom_info()
        prefixes = [
            p for p in await self.bot.get_valid_prefixes() if not re.match(r"<@!?([0-9]+)>", p)
        ]

        guilds_count = len(self.bot.guilds)
        users_count = len(self.bot.users)
        text_channels_count = 0
        voice_channels_count = 0
        categories_count = 0
        for guild in self.bot.guilds:
            text_channels_count += len(guild.text_channels)
            voice_channels_count += len(guild.voice_channels)
            categories_count += len(guild.categories)

        if self.invite_url is None:
            self.invite_url: str = await self.bot.get_invite_url()

        if self.owner is None:
            app_info = await self.bot.application_info()
            self.owner: str = (
                str(app_info.team.name) if app_info.team else app_info.owner.display_name
            )

        return {
            "bot": {
                "name": self.bot.user.name,
                "id": self.bot.user.id,
                "application_id": self.bot.application_id,
                "info": bot_info,
                "profile_description": (await self.bot.application_info()).description,
                "prefixes": prefixes,
                "owner_ids": list(self.bot.owner_ids),
                "owner": self.owner,
                "avatar": str(self.bot.user.display_avatar.url).split("?")[0],
                "default_avatar": str(self.bot.user.default_avatar.url).split("?")[0],
                "is_verified": self.bot.user.public_flags.verified_bot,
                "invite_url": self.invite_url,
                "invite_public": await self.bot._config.invite_public(),
                "blacklisted_users": list(await self.bot.get_blacklist()),
            },
            "stats": {
                "guilds": guilds_count,
                "text": text_channels_count,
                "voice": voice_channels_count,
                "categories": categories_count,
                "users": users_count,
                "uptime": int(self.bot.uptime.timestamp()),
            },
            "constants": {
                "MIN_PREFIX_LENGTH": getattr(
                    core_commands,
                    "MINIMUM_PREFIX_LENGTH",
                    1,
                ),  # Added by #6013 in Red 3.5.6.
                "MAX_PREFIX_LENGTH": core_commands.MAX_PREFIX_LENGTH,
                "MAX_DISCORD_PERMISSIONS_VALUE": discord.Permissions.all().value,
            },
        }

    async def build_cmd_list(
        self,
        commands_list: list[commands.Command],
        details: bool = True,
        is_owner: bool = False,
    ) -> list[dict[str, str | list]]:
        final = []
        async for command in AsyncIter(sorted(commands_list, key=lambda c: c.name)):
            if details:
                if command.hidden:
                    continue
                is_owner = (
                    is_owner
                    or command.requires.privilege_level == commands.PrivilegeLevel.BOT_OWNER
                )
                try:
                    details = {
                        "name": command.qualified_name,
                        "signature": command.signature,
                        "short_description": command.short_doc.strip() or "",
                        "description": command.help.strip() or "",
                        "aliases": list(command.aliases),
                        # "is_owner": is_owner,
                        "privilege_level": (
                            command.requires.privilege_level.name
                            if command.requires.privilege_level is not None
                            else None
                        ),
                        "user_permissions": (
                            "\n".join(
                                [
                                    permission.replace("_", " ").capitalize()
                                    for permission, value in dict(
                                        command.requires.user_perms,
                                    ).items()
                                    if value
                                ],
                            )
                            if command.requires.user_perms is not None
                            else None
                        ),
                        "subs": [],
                    }
                except ValueError:
                    continue
                if isinstance(command, commands.Group):
                    details["subs"] = await self.build_cmd_list(command.commands, is_owner=is_owner)
                final.append(details)
            else:
                if (
                    command.hidden
                    or command.requires.privilege_level == commands.PrivilegeLevel.BOT_OWNER
                ):
                    continue
                final.append(command.qualified_name)
                if isinstance(command, commands.Group):
                    final += await self.build_cmd_list(command.commands, details=False)
        return final

    @rpc_check()
    async def get_commands(
        self,
    ) -> dict[
        str,
        dict[
            str,
            str | list[dict[str, str | list]],
        ],
    ]:
        returning = {}
        downloader_cog = self.bot.get_cog("Downloader")
        installed_cogs = await downloader_cog.installed_cogs() if downloader_cog is not None else []
        for cog in self.bot.cogs.copy().values():
            name = cog.qualified_name
            stripped = [c for c in cog.__cog_commands__ if c.parent is None]
            cmds = await self.build_cmd_list(stripped)
            if not cmds:
                continue

            author = "Unknown"
            repo = "Unknown"
            # Taken from Trusty's downloader fuckery (https://gist.github.com/TrustyJAID/784c8c32dd45b1cc8155ed42c0c56591).
            if name in self.cogs_infos_cache:
                author = self.cogs_infos_cache[name]["author"]
                repo = self.cogs_infos_cache[name]["repo"]
            elif downloader_cog is not None:
                module = cog.__module__.split(".")[0]  # downloader_cog.cog_name_from_instance(cog)
                cog_info = next(
                    (
                        installed_cog
                        for installed_cog in installed_cogs
                        if installed_cog.name == module
                    ),
                    None,
                )
                if cog_info is not None:
                    author = humanize_list(cog_info.author) if cog_info.author else "Unknown"
                    try:
                        repo = cog_info.repo.clean_url or "Unknown"
                    except AttributeError:
                        repo = "Unknown (Removed from Downloader)"
                elif cog.__module__.startswith("redbot."):
                    author = "Cog Creators"
                    repo = "https://github.com/Cog-Creators/Red-DiscordBot"
                elif (
                    pathlib.Path(__import__(cog.__module__).__path__[0]).parent.name == "AAA3A-cogs"
                ):  # Handle my repo's clones... :P
                    author = "AAA3A"
                    repo = "https://github.com/AAA3A-AAA3A/AAA3A-cogs"
            author = getattr(cog, "__authors__", []) or getattr(cog, "__author__", []) or author
            if isinstance(author, (list, tuple)):
                author = humanize_list(author)
            self.cogs_infos_cache[name] = {"author": author, "repo": repo}
            returning[name] = {
                "name": name,
                "description": (cog.__doc__ or "").strip(),
                "author": author or "",
                "repo": repo,
                "commands": cmds,
            }
        return {name: returning[name] for name in sorted(returning.keys())}

    async def notify_owners_of_blacklist(self, ip: str):
        async with self.cog.config.webserver.core.blacklisted_ips() as blacklisted_ips:
            blacklisted_ips.append(ip)
        await self.bot.send_to_owners(
            f"[Dashboard] Detected suspicious activity from IP `{ip}`. They have been blacklisted.",
        )

    @rpc_check()
    async def get_user_guilds(
        self,
        user_id: int,
        per_page: int | str | None = None,
        page: int | str | None = None,
        query: str | None = None,
        filter: typing.Literal["owner", "admin", "mod", "member"] | None = None,
    ) -> dict[str, typing.Any]:
        user = self.bot.get_user(user_id)
        if user is None:
            # Bot doesn't even find user using bot.get_user, might as well spare all the data processing and return.
            return {"guilds": [], "total": 0, "per_page": 10, "pages": 0, "page": 1}
        is_owner = user.id in self.bot.owner_ids
        guilds = []
        if filter is None and user_id in self.guilds_cache:
            cached = self.guilds_cache[user_id]
            if (cached["time"] + 60) > time.time():
                guilds = cached["guilds"]
            else:
                del self.guilds_cache[user_id]

        if not guilds:
            # This could take a while.
            async for guild in AsyncIter(
                sorted(
                    self.bot.guilds,
                    key=lambda guild: (guild.owner.id != user_id, guild.name.lower()),
                ),
                steps=1300,
            ):
                guild_infos = {
                    "id": guild.id,
                    "name": guild.name,
                    "owner": guild.owner.display_name,
                    "owner_id": guild.owner.id,
                    "icon_url": (
                        guild.icon.url.split("?")[0]
                        if guild.icon is not None
                        else "https://cdn.discordapp.com/embed/avatars/1.png"
                    ),
                    "icon_animated": guild.icon.is_animated() if guild.icon is not None else False,
                    "user_role": None,
                }
                if filter is None and is_owner:
                    guilds.append(guild_infos)
                    continue
                member = guild.get_member(user_id)
                if member is None:
                    continue
                if (filter is None or filter == "owner") and member == guild.owner:
                    guild_infos["user_role"] = "OWNER"
                    guilds.append(guild_infos)
                elif (filter is None or filter == "admin") and (
                    await self.bot.is_admin(member) or member.guild_permissions.manage_guild
                ):
                    guild_infos["user_role"] = "ADMIN"
                    guilds.append(guild_infos)
                elif (filter is None or filter == "mod") and await self.bot.is_mod(member):
                    guild_infos["user_role"] = "MOD"
                    guilds.append(guild_infos)
                elif filter is None or filter == "member":
                    # Plain members previously matched no branch at all, so the guild
                    # list came back empty for them and they could never reach the
                    # per-guild module pages (e.g. the audio player). get_guild()
                    # already allows them through when for_third_parties is set.
                    guild_infos["user_role"] = "MEMBER"
                    guilds.append(guild_infos)
            if filter is None:
                self.guilds_cache[user_id] = {"guilds": guilds, "time": time.time()}

        if query is not None:
            query = query.strip().lower()
            guilds = [
                guild
                for guild in guilds
                if query in guild["name"].lower() or query == str(guild["id"])
            ]
        return Pagination.from_list(guilds, per_page=per_page, page=page).to_dict()

    @rpc_check()
    async def get_guild(
        self,
        user_id: int,
        guild_id: int,
        for_third_parties: bool = False,
    ) -> dict[str, typing.Any]:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return {"status": 1}
        member = guild.get_member(user_id)
        is_owner = user_id in self.bot.owner_ids
        if not is_owner and (
            member is None
            or (
                not await self.bot.is_mod(member)
                and not member.guild_permissions.manage_guild
                and not for_third_parties
            )
        ):
            return {"status": 1}

        # joined_at = member.joined_at if member is not None else None
        if is_owner:
            humanized = "Everything (Bot Owner)"
        elif member == guild.owner:
            humanized = "Everything (Guild Owner)"
        else:
            humanized = "Admin" if await self.bot.is_admin(member) else "Mod"

        status_stats = {"online": 0, "dnd": 0, "idle": 0, "offline": 0}
        for m in guild.members:
            status_stats[m.raw_status if m.raw_status in status_stats else "offline"] += 1

        if guild.verification_level is discord.VerificationLevel.none:
            verification_level = "None"
        elif guild.verification_level is discord.VerificationLevel.low:
            verification_level = "1 - Low"
        elif guild.verification_level is discord.VerificationLevel.medium:
            verification_level = "2 - Medium"
        elif guild.verification_level is discord.VerificationLevel.high:
            verification_level = "3 - High"
        elif guild.verification_level is discord.VerificationLevel.highest:
            verification_level = "4 - Extreme"
        else:
            verification_level = "Unknown"

        all_roles = list(reversed([{"id": role.id, "name": role.name} for role in guild.roles]))
        config_group = self.bot._config.guild(guild)
        admin_roles = [
            {"id": role.id, "name": role.name}
            for role_id in await config_group.admin_role()
            if (role := guild.get_role(role_id)) is not None
        ]
        mod_roles = [
            {"id": role.id, "name": role.name}
            for role_id in await config_group.mod_role()
            if (role := guild.get_role(role_id)) is not None
        ]

        return {
            "status": 0,
            "id": guild.id,
            "name": guild.name,
            "owner": guild.owner.display_name,
            "owner_id": guild.owner.id,
            "icon_url": (
                guild.icon.url
                if guild.icon is not None
                else "https://cdn.discordapp.com/embed/avatars/1.png"
            ),
            "icon_animated": guild.icon.is_animated() if guild.icon is not None else False,
            # Extra presentation data used by the guild Overview tab.
            "banner_url": guild.banner.url if guild.banner is not None else None,
            "splash_url": guild.splash.url if guild.splash is not None else None,
            "description": guild.description,
            "vanity_url_code": guild.vanity_url_code,
            "boosters_number": guild.premium_subscription_count or 0,
            "boost_tier": guild.premium_tier or 0,
            "emojis_number": len(guild.emojis),
            "stickers_number": len(guild.stickers),
            "categories_number": len(guild.categories),
            "verification_level": verification_level,
            "created_at": guild.created_at.timestamp(),
            "joined_at": guild.me.joined_at.timestamp(),
            # Guild stats.
            "members_number": len(guild.members),
            "online_number": status_stats["online"],
            "dnd_number": status_stats["dnd"],
            "idle_number": status_stats["idle"],
            "offline_number": status_stats["offline"],
            "bots_number": len([user for user in guild.members if user.bot]),
            "humans_number": len([user for user in guild.members if not user.bot]),
            "channels_number": len(guild.channels),
            "text_channels_number": len(guild.text_channels),
            "voice_channels_number": len(guild.voice_channels),
            "roles_number": len(guild.roles),
            "roles": all_roles,
            # Bot wide settings.
            "prefixes": sorted(await self.bot.get_valid_prefixes(guild)),
            "settings": {
                "edit_permission": user_id in self.bot.owner_ids
                or await self.bot.is_admin(member)
                or member.guild_permissions.manage_guild,
                # Base.
                "bot_nickname": guild.me.nick,
                "bot_profile": await self._guild_profile(guild),
                "prefixes": await config_group.prefix(),
                "admin_roles": admin_roles,
                "mod_roles": mod_roles,
                "whitelist": await config_group.whitelist(),
                "blacklist": await config_group.blacklist(),
                # Commands.
                "ignored": await self.bot._ignored_cache.get_ignored_guild(guild),
                "disabled_commands": await config_group.disabled_commands(),
                # Look.
                "embeds": await config_group.embeds(),
                "use_bot_color": await config_group.use_bot_color(),
                "fuzzy": await config_group.fuzzy(),
                "delete_delay": await config_group.delete_delay(),
                # Locale.
                "locale": await i18n.get_locale_from_guild(self.bot, guild),
                "regional_format": await i18n.get_regional_format_from_guild(self.bot, guild),
            },
            "perms": humanize_list(humanized),
        }

    @rpc_check()
    async def leave_guild(self, user_id: int, guild_id: int) -> dict[str, int]:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return {"status": 1}
        if user_id not in self.bot.owner_ids:
            return {"status": 1}
        await guild.leave()
        return {"status": 0}

    @rpc_check()
    async def set_guild_settings(
        self,
        user_id: int,
        guild_id: int,
        settings: dict[str, typing.Any],
    ) -> dict[str, int | str]:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return {"status": 1}
        member = guild.get_member(user_id)
        if user_id not in self.bot.owner_ids and (
            member is None
            or not (await self.bot.is_admin(member) or member.guild_permissions.manage_guild)
        ):
            return {"status": 1}
        change_nickname_error = False
        if settings["bot_nickname"] != guild.me.nick:
            try:
                await guild.me.edit(nick=settings["bot_nickname"])
            except discord.HTTPException as e:
                change_nickname_error = str(e)
        await self.bot.set_prefixes(settings["prefixes"], guild=guild)
        config_group = self.bot._config.guild(guild)
        await config_group.admin_role.set([int(role_id) for role_id in settings["admin_roles"]])
        await config_group.mod_role.set([int(role_id) for role_id in settings["mod_roles"]])
        await config_group.ignore.set(settings["ignored"])
        already_disabled_commands = await config_group.disabled_commands()
        for command_name in settings["disabled_commands"].copy():
            if command_name in already_disabled_commands:
                continue
            if (
                (command := self.bot.get_command(command_name)) is None
                or isinstance(command, commands.commands._RuleDropper)
                or (
                    command.requires.privilege_level is not None
                    and command.requires.privilege_level
                    > await commands.PrivilegeLevel.from_ctx(
                        type("Context", (), {"bot": self.bot, "author": member, "guild": guild}),
                    )
                )
            ):
                settings["disabled_commands"].remove(command_name)
            else:
                command.disable_in(guild)
        for command_name in already_disabled_commands:
            if command_name not in settings["disabled_commands"]:
                if (command := self.bot.get_command(command_name)) is not None and (
                    command.requires.privilege_level is None
                    or not command.requires.privilege_level
                    > await commands.PrivilegeLevel.from_ctx(
                        type("Context", (), {"bot": self.bot, "author": member, "guild": guild}),
                    )
                ):
                    command.enable_in(guild)
        await config_group.disabled_commands.set(settings["disabled_commands"])
        await config_group.embeds.set(settings["embeds"])
        await config_group.use_bot_color.set(settings["use_bot_color"])
        await config_group.fuzzy.set(settings["fuzzy"])
        await config_group.delete_delay.set(settings["delete_delay"])
        if settings["locale"] is None:
            settings["locale"] = await self.bot._config.locale()
        i18n.set_contextual_locale(settings["locale"])
        await self.bot._i18n_cache.set_locale(guild, settings["locale"])
        i18n.set_contextual_regional_format(settings["regional_format"])
        await self.bot._i18n_cache.set_regional_format(guild, settings["regional_format"])
        return {"status": 0, "change_nickname_error": change_nickname_error}

    # Discord's "modify current member" endpoint. It takes avatar, banner and
    # bio as well as nick, which is how a bot gets a per-server look - the same
    # route the `perserverbotprofile` commands use.
    _GUILD_ME_ROUTE = "/guilds/{}/members/@me"
    _BIO_LIMIT = 190

    async def _guild_me(self, guild: discord.Guild) -> dict[str, typing.Any]:
        """The bot's own member object in this guild, straight from the API.

        The gateway's cached member carries no per-guild banner or bio, so the
        page would have nothing to show without asking for it.
        """
        cached = self.guild_profile_cache.get(guild.id)
        if cached is not None and (cached[0] + 300) > time.time():
            return cached[1]
        data = {}
        try:
            from discord.http import Route

            data = await self.bot.http.request(
                Route("GET", self._GUILD_ME_ROUTE.format(guild.id))
            ) or {}
        except Exception:  # noqa: BLE001 - the card degrades to the global look
            log.debug("Could not read the bot's member profile in %s", guild.id, exc_info=True)
            data = {}
        self.guild_profile_cache[guild.id] = (time.time(), data)
        return data

    async def _guild_profile(self, guild: discord.Guild) -> dict[str, typing.Any]:
        """What the bot currently looks like in one server."""
        data = await self._guild_me(guild)
        base = "https://cdn.discordapp.com"
        avatar = data.get("avatar")
        banner = data.get("banner")
        return {
            "nickname": guild.me.nick or "",
            "bio": data.get("bio") or "",
            # Per-guild assets live under a different CDN path to global ones.
            "avatar_url": (
                f"{base}/guilds/{guild.id}/users/{self.bot.user.id}/avatars/{avatar}.png?size=256"
                if avatar else ""
            ),
            "banner_url": (
                f"{base}/guilds/{guild.id}/users/{self.bot.user.id}/banners/{banner}.png?size=512"
                if banner else ""
            ),
            "global_avatar_url": self.bot.user.display_avatar.url,
            "bio_limit": self._BIO_LIMIT,
        }

    @rpc_check()
    async def set_guild_bot_profile(
        self, user_id: int, guild_id: int, settings: dict[str, typing.Any]
    ) -> dict[str, typing.Any]:
        """Set the bot's avatar, banner and bio for one server.

        This only changes how the bot looks in that server, so it is open to
        the people who administer it rather than to the bot owner alone.

        Each field is optional: absent means leave it alone, the string
        ``"reset"`` means fall back to the bot's global asset, and an empty
        string clears it outright.
        """
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return {"status": 1, "message": "Unknown server."}
        member = guild.get_member(user_id)
        if member is None:
            return {"status": 1, "message": "You are not in that server."}
        if not (
            user_id in self.bot.owner_ids
            or guild.owner_id == user_id
            or member.guild_permissions.administrator
            or await self.bot.is_admin(member)
        ):
            return {"status": 1, "message": "You need to administer this server to change this."}

        from discord.http import Route

        route = Route("PATCH", self._GUILD_ME_ROUTE.format(guild_id))
        payload: dict[str, typing.Any] = {}
        changed: list[str] = []

        for key, asset_name in (("avatar", "avatar"), ("banner", "banner")):
            value = settings.get(key)
            if value is None:
                continue
            if value == "reset":
                # Falling back means re-uploading the global asset, because the
                # endpoint has no "inherit" - only a value or nothing.
                asset = getattr(self.bot.user, asset_name, None)
                if asset is None:
                    payload[key] = ""
                else:
                    try:
                        raw = await asset.read()
                        payload[key] = "data:image/png;base64," + base64.b64encode(raw).decode()
                    except Exception:  # noqa: BLE001
                        payload[key] = ""
            else:
                payload[key] = value
            changed.append(key)

        if "bio" in settings and settings["bio"] is not None:
            bio = str(settings["bio"])
            if len(bio) > self._BIO_LIMIT:
                return {
                    "status": 1,
                    "message": f"The bio can be at most {self._BIO_LIMIT} characters.",
                }
            payload["bio"] = bio
            changed.append("bio")

        if not payload:
            return {"status": 0, "message": "Nothing to change.", "changed": []}

        try:
            await self.bot.http.request(route, json=payload)
        except discord.HTTPException as exc:
            return {
                "status": 1,
                "message": f"Discord refused the change ({exc.status}): {exc.text}",
            }
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not set the per-server bot profile in %s", guild_id)
            return {"status": 1, "message": f"Could not apply the change: {exc}"}

        # The look just changed, so anything cached about it is now wrong.
        self.guild_profile_cache.pop(guild_id, None)
        return {"status": 0, "changed": changed}

    @rpc_check()
    async def set_bot_profile(
        self,
        user_id: int,
        settings: dict[str, typing.Any],
    ) -> dict[str, int | str]:
        if user_id not in self.bot.owner_ids:
            return {"status": 1}
        try:
            if settings["avatar"] == "default":
                await self.bot.user.edit(avatar=None)
            elif settings["avatar"] != "keep":
                avatar = base64.b64decode(settings["avatar"])
                await self.bot.user.edit(avatar=avatar)
            if settings["name"] != self.bot.user.name:
                try:
                    await asyncio.wait_for(
                        self.bot.get_cog("Core")._name(name=settings["name"]),
                        timeout=30,
                    )
                except TimeoutError:
                    return {
                        "status": 1,
                        "error": "Changing the name timed out. Remember that you can only change it twice per hour.",
                    }
            if settings["profile_description"] is not None:
                from discord.http import Route

                await self.bot.http.request(
                    Route("PATCH", "/applications/@me"),
                    json={"description": settings["profile_description"]},
                )
        except discord.HTTPException as e:
            return {"status": 1, "error": str(e)}

    @rpc_check()
    async def get_dashboard_settings(self, user_id: int) -> dict[str, typing.Any]:
        if user_id not in self.bot.owner_ids:
            return {"status": 1}
        config_group = self.cog.config.webserver.ui.meta
        return {
            "status": 0,
            "title": await config_group.title(),
            "icon": await config_group.icon(),
            "website_description": await config_group.website_description(),
            "description": await config_group.description(),
            "support_server": await config_group.support_server(),
            "default_color": await config_group.default_color(),
            "default_background_theme": await config_group.default_background_theme(),
            "default_sidenav_theme": await config_group.default_sidenav_theme(),
            "disabled_third_parties": await self.cog.config.webserver.disabled_third_parties(),
        }

    @rpc_check()
    async def set_dashboard_settings(
        self,
        user_id: int,
        settings: dict[str, typing.Any],
    ) -> dict[str, int]:
        if user_id not in self.bot.owner_ids:
            return {"status": 1}
        config_group = self.cog.config.webserver.ui.meta
        await config_group.title.set(settings["title"])
        await config_group.icon.set(settings["icon"])
        await config_group.website_description.set(settings["website_description"])
        await config_group.description.set(settings["description"])
        await config_group.support_server.set(settings["support_server"])
        await config_group.default_color.set(settings["default_color"])
        await config_group.default_background_theme.set(settings["default_background_theme"])
        await config_group.default_sidenav_theme.set(settings["default_sidenav_theme"])
        await self.cog.config.webserver.disabled_third_parties.set(
            settings["disabled_third_parties"],
        )
        return {"status": 0}

    @rpc_check()
    async def get_bot_settings(self, user_id: int) -> dict[str, typing.Any]:
        if user_id not in self.bot.owner_ids:
            return {"status": 1}
        config_group = self.bot._config
        color = discord.Color(await config_group.color())
        return {
            "status": 0,
            # Base.
            "prefixes": await config_group.prefix(),
            "invoke_error_msg": await config_group.invoke_error_msg(),
            "whitelist": await config_group.whitelist(),
            "blacklist": await config_group.blacklist(),
            # Commands.
            "disabled_commands": await config_group.disabled_commands(),
            "disabled_command_msg": await config_group.disabled_command_msg(),
            # Descriptions.
            "description": await config_group.description(),
            "custom_info": await config_group.custom_info(),
            # Look.
            "embeds": await config_group.embeds(),
            "color": f"#{color.value:06X}",
            "fuzzy": await config_group.fuzzy(),
            "use_buttons": await config_group.use_buttons(),
            # Invite.
            "invite_public": await config_group.invite_public(),
            "invite_commands_scope": await config_group.invite_commands_scope(),
            "invite_perms": await config_group.invite_perm(),
            # Locale.
            "locale": await config_group.locale(),
            "regional_format": await config_group.regional_format(),
        }

    @rpc_check()
    async def set_bot_settings(
        self,
        user_id: int,
        settings: dict[str, typing.Any],
    ) -> dict[str, int]:
        if user_id not in self.bot.owner_ids:
            return {"status": 1}
        config_group = self.bot._config
        await config_group.prefix.set(settings["prefixes"])
        await config_group.invoke_error_msg.set(settings["invoke_error_msg"])
        already_disabled_commands = await config_group.disabled_commands()
        for command_name in settings["disabled_commands"].copy():
            if command_name in already_disabled_commands:
                continue
            if (command := self.bot.get_command(command_name)) is None or isinstance(
                command,
                commands.commands._RuleDropper,
            ):
                settings["disabled_commands"].remove(command_name)
            else:
                command.enabled = False
        for command_name in already_disabled_commands:
            if command_name not in settings["disabled_commands"]:
                if (command := self.bot.get_command(command_name)) is not None:
                    command.enabled = True
        await config_group.disabled_commands.set(settings["disabled_commands"])
        if settings["disabled_command_msg"] is not None:
            await config_group.disabled_command_msg.set(settings["disabled_command_msg"])
        else:
            await config_group.disabled_command_msg.clear()
        if settings["description"] is not None:
            await config_group.description.set(settings["description"])
            self.bot.description = settings["description"]
        else:
            await config_group.description.clear()
            self.bot.description = "Red V3"
        if settings["custom_info"] is not None:
            await config_group.custom_info.set(settings["custom_info"])
        else:
            await config_group.custom_info.clear()
        await config_group.embeds.set(settings["embeds"])
        if settings["color"] is not None:
            hex_color = settings["color"].lstrip("#")
            r = int(hex_color[:2], 16)
            g = int(hex_color[2:4], 16)
            b = int(hex_color[4:6], 16)
            color = discord.Color.from_rgb(r, g, b)
            await config_group.color.set(color.value)
            self.bot._color = color
        else:
            await config_group.color.clear()
            self.bot._color = discord.Color.red()
        await config_group.fuzzy.set(settings["fuzzy"])
        await config_group.use_buttons.set(settings["use_buttons"])
        await config_group.invite_public.set(settings["invite_public"])
        await config_group.invite_commands_scope.set(settings["invite_commands_scope"])
        await config_group.invite_perm.set(settings["invite_perms"])
        i18n.set_contextual_locale(settings["locale"])
        await self.bot._i18n_cache.set_locale(None, settings["locale"])
        i18n.set_contextual_regional_format(settings["regional_format"])
        await self.bot._i18n_cache.set_regional_format(None, settings["regional_format"])
        return {"status": 0}

    @rpc_check()
    async def set_custom_pages(
        self,
        user_id: int,
        custom_pages: list[dict[str, str]],
    ) -> dict[str, int]:
        if user_id not in self.bot.owner_ids:
            return {"status": 1}
        await self.cog.config.webserver.custom_pages.set(custom_pages)
        return {"status": 0}

    @rpc_check()
    async def bump_session_epoch(
        self, user_id: int, scope: str = "user", target_id: int = None
    ) -> dict[str, typing.Any]:
        """Revoke dashboard logins by moving the cut-off they are checked against.

        `scope` is "user" (that person's own sessions, which is what logging
        out does) or "global" (everyone, which is the Admin page's "Refresh
        sessions"). Only an owner may revoke globally or on someone else's
        behalf; anyone may revoke their own.

        Stored rather than kept in memory, so revoking outlives a restart -
        and, just as importantly, so a restart on its own no longer revokes
        every session as a side effect.
        """
        is_owner = user_id in self.bot.owner_ids
        if scope == "global":
            if not is_owner:
                return {"status": 1}
            key = "global"
        else:
            key = str(target_id if target_id is not None else user_id)
            if key != str(user_id) and not is_owner:
                return {"status": 1}

        now = int(time.time())
        async with self.cog.config.webserver.core.session_epochs() as epochs:
            epochs[key] = now
        return {"status": 0, "epoch": now, "key": key}

    @rpc_check()
    async def get_logs(
        self,
        user_id: int,
        after: int = 0,
        limit: int = 500,
        levels: list[str] | None = None,
        query: str | None = None,
    ) -> dict[str, typing.Any]:
        """The bot's recent log output, for the owner-only log viewer.

        Owner-only without exception: log lines carry channel names, member
        names, command arguments and tracebacks from every server the bot is
        in, so this is strictly more sensitive than anything else the dashboard
        exposes. There is no guild-scoped version of it on purpose.

        `after` is the sequence number the caller last saw; the page polls with
        it to receive only what is new.
        """
        if user_id not in self.bot.owner_ids:
            return {"status": 1}
        handler = getattr(self.cog, "log_handler", None)
        if handler is None:
            return {"status": 1, "error": "Log capture is not running."}
        # Bound what a caller can ask for: this crosses the RPC boundary as
        # JSON, and the buffer is not large enough to be worth paging beyond.
        limit = max(1, min(int(limit or 500), handler.capacity))
        result = handler.read(after=int(after or 0), limit=limit, levels=levels, query=query)
        result["status"] = 0
        result["levels"] = list(LEVELS)
        return result
