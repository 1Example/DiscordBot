from __future__ import annotations

import contextlib
import inspect
import json
import logging
import time
import typing as t

import discord
from redbot.core import bank, commands
from redbot.core.errors import BalanceTooHigh  # noqa: F401

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    NOTIFICATIONS,
    emoji_options,
    emoji_rejection,
    form_reader,
)

from pylav.players.query.obj import Query

log = logging.getLogger("red.plcontroller.dashboard")


def dashboard_page(*args: t.Any, **kwargs: t.Any) -> t.Callable[[t.Any], t.Any]:
    def decorator(func: t.Callable) -> t.Callable[[t.Any], t.Any]:
        func.__dashboard_decorator_params__ = (args, kwargs)
        return func

    return decorator


def _dash_json(payload: dict) -> str:
    """Serialise for embedding inside a ``<script>`` tag.

    ``json.dumps`` leaves ``<`` alone, so a track called ``</script>`` would
    otherwise close the tag and put its own markup on the page. The three
    escapes below are still valid JSON and decode back to the same string.
    """
    return (
        json.dumps(payload, default=str)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _fmt_ms(milliseconds: float | int | None) -> str:
    """Format a millisecond duration as H:MM:SS / M:SS."""
    if not milliseconds or milliseconds < 0:
        return "0:00"
    total_seconds = int(milliseconds // 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


class DashboardIntegration:
    bot: t.Any

    # Volume before a mute, per guild, so unmuting restores the level instead
    # of guessing one. In memory only - a restart just means the fallback.
    _dash_premute: dict[int, int] = {}

    # The web player polls, so anything it reads runs several times a second
    # per open tab. These two keep that off the hot path: decoded favourites
    # keyed by the playlist's own contents, and staff status for a few seconds
    # at a time. Both are in-memory and per-process; losing them costs one
    # rebuild, never correctness.
    _dash_fav_cache: dict[int, tuple] = {}
    _dash_staff_cache: dict[tuple[int, int], tuple[float, bool]] = {}
    _dash_online_cache: dict[int, tuple[float, int | None]] = {}
    _DASH_STAFF_TTL = 20.0
    _DASH_ONLINE_TTL = 15.0

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog: commands.Cog) -> None:
        log.info("Dashboard cog found, registering PLController as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    # ---------- helpers ----------

    # Actions any guild member may perform. Everything else is staff-only.
    LISTENER_ACTIONS = frozenset(
        {
            "pause", "resume", "skip", "previous", "shuffle",
            "seek", "volume_up", "volume_down", "volume_set", "mute",
            "search", "play", "play_now",
            "fav_add", "fav_play", "fav_queue",
        }
    )

    # Actions that only read - the client polls these constantly and they must
    # never hit the permission gate, the charge gate, or the audit log.
    READONLY_ACTIONS = frozenset({"state"})

    async def _dash_is_staff(self, user: discord.User, member: discord.Member, guild: discord.Guild) -> bool:
        # Short-lived, because a demotion should take effect without a restart,
        # but long enough that a polling page is not re-asking the config four
        # times a second.
        key = (guild.id, user.id)
        cached = self._dash_staff_cache.get(key)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self._DASH_STAFF_TTL:
            return cached[1]
        result = (
            await self.bot.is_owner(user)
            or member.id == guild.owner_id
            or member.guild_permissions.administrator
            or await self.bot.is_admin(member)
            or await self.bot.is_mod(member)
        )
        self._dash_staff_cache[key] = (now, result)
        return result

    async def _dash_check_perms(
        self, user: discord.User, guild: discord.Guild
    ) -> tuple[discord.Member | None, dict | None]:
        """Any member of the guild may open the page; per-action gating happens later."""
        member = guild.get_member(user.id)
        if member is None:
            return None, {
                "status": 1,
                "error_title": "Member not found",
                "error_message": "You are not a member of this guild.",
            }
        return member, None

    def _dash_player(self, guild: discord.Guild):
        return self.pylav.get_player(guild)

    def _dash_guild_card(self, guild: discord.Guild) -> dict[str, t.Any]:
        """The handful of server facts the player page's own header shows.

        This exists so the page can stand on its own: the dashboard's guild
        banner is hidden on this page, and these are the parts of it that were
        actually worth keeping.
        """
        members = getattr(guild, "member_count", None) or len(guild.members)

        # Counting presences means walking the member list, which the page asks
        # for on every poll. Cheap on a normal server, not on a huge one, so it
        # is cached briefly and skipped entirely past the point where it would
        # cost more than it tells anyone.
        online = None
        cached = self._dash_online_cache.get(guild.id)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self._DASH_ONLINE_TTL:
            online = cached[1]
        elif members <= 25000:
            try:
                offline = discord.Status.offline
                online = sum(1 for m in guild.members if not m.bot and m.status is not offline)
            except Exception:  # noqa: BLE001 - presences may not be available
                online = None
            self._dash_online_cache[guild.id] = (now, online)

        return {
            "id": str(guild.id),
            "name": guild.name,
            "icon": guild.icon.url if guild.icon else "",
            "members": members,
            "online": online,
            "channels": len(guild.channels),
            "roles": len(guild.roles),
        }

    def _dash_guild_options(self, user: discord.User) -> list[dict[str, str]]:
        """Every server this person and the bot are both in.

        The switcher is what replaces the dashboard's own guild banner, so it
        has to be able to reach anywhere that banner could.
        """
        out = []
        for guild in self.bot.guilds:
            try:
                if guild.get_member(user.id) is None:
                    continue
            except Exception:  # noqa: BLE001
                continue
            out.append(
                {
                    "id": str(guild.id),
                    "name": guild.name,
                    "icon": guild.icon.url if guild.icon else "",
                }
            )
        out.sort(key=lambda row: row["name"].lower())
        return out[:250]

    # ---------- pages ----------

    @dashboard_page(
        name=None,
        description="Control the music player from the web.",
        methods=("GET", "POST"),
    )
    async def dashboard_player_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = await self._dash_check_perms(user, guild)
        if error is not None:
            return error

        player = self._dash_player(guild)

        # --- handle an action, if one was submitted ---
        # The webserver builds this with `request.form.to_dict(flat=False)`, so every
        # value arrives as a list (e.g. {"action": ["pause"]}). Unwrap before comparing.
        raw_form = (kwargs.get("data") or {}).get("form") or {}
        raw_args = kwargs.get("extra_kwargs") or {}

        def _unwrap(value, default=None):
            if isinstance(value, (list, tuple)):
                return value[0] if value else default
            return value

        def field(key: str, default=None):
            return _unwrap(raw_form.get(key, default), default)

        def query(key: str, default=None):
            return _unwrap(raw_args.get(key, default), default)

        is_post = kwargs.get("method") == "POST"
        action = field("action") if is_post else None
        # The page talks to itself over fetch(): same route, same session, same
        # CSRF token, but the answer comes back as a JSON blob in a marker
        # <script> rather than a whole re-rendered page. Every form on the page
        # still works with JavaScript off, which is why the classic
        # redirect-and-reload replies below are all still here.
        api = bool(field("plc_api") or query("plc_api"))
        is_staff = await self._dash_is_staff(user, member, guild)

        async def frame(**extra) -> dict[str, t.Any]:
            """Everything the client redraws from, in one dict."""
            return {
                # This page is meant to be left open for hours, so every frame
                # carries the current token. If the dashboard ever rotates or
                # expires them, the client rolls forward instead of quietly
                # failing every action from then on.
                "csrf": (kwargs.get("csrf_token") or ("", ""))[1],
                "state": await self._dash_build_state(self._dash_player(guild)),
                "guild": self._dash_guild_card(guild),
                "is_staff": is_staff,
                "favourites": await self._dash_fav_list(guild),
                "wallet": await self._dash_wallet(member, guild),
                "economy": await self._dash_economy_state(member, guild, is_staff),
                "search_sources": [list(row) for row in self.SEARCH_SOURCES],
                **extra,
            }

        async def api_reply(notifications=None, **extra) -> dict[str, t.Any]:
            """One frame of truth for the client, as embedded JSON."""
            return {
                "status": 0,
                "web_content": {
                    "source": API_TEMPLATE,
                    "payload": _dash_json(
                        await frame(ok=True, notifications=notifications or [], **extra)
                    ),
                },
            }

        async def reply(message: str, category: str = "success", **extra) -> dict[str, t.Any]:
            """Answer an action either way: JSON for fetch(), redirect for a
            plain form submit."""
            notifications = [{"message": message, "category": category}] if message else []
            if api:
                return await api_reply(notifications, **extra)
            return {
                "status": 0,
                "notifications": notifications,
                "redirect_url": kwargs.get("request_url"),
            }

        # --- polling: read-only, no permission gate, no charge ---
        if api and (action in (None, "", "state") or not is_post):
            return await api_reply()

        # Permission + billing gates must run before ANY action branch below,
        # including the search/favourites/play early returns - otherwise those
        # actions bypass both checks entirely.
        if action:
            if action not in self.LISTENER_ACTIONS and not is_staff:
                return await reply(
                    "That one is for moderators. You can still play, pause, "
                    "skip, shuffle and queue music.",
                    "warning",
                )
            _ok, _charge_msg = await self._dash_charge(member, guild, action, is_staff)
            if not _ok:
                return await reply(_charge_msg, "warning")

        # --- search: doesn't need an existing player ---
        if action == "search":
            search_term = (field("query") or "").strip()
            source = (field("source") or "ytsearch").strip()
            if not search_term:
                return await reply("Enter something to search for.", "warning")
            results, error = await self._dash_search(search_term, source=source)
            if error:
                return await reply(error, "danger")
            if api:
                return await api_reply(
                    [] if results else [{"message": "Nothing found.", "category": "warning"}],
                    search_results=results,
                    search_term=search_term,
                    search_source=source,
                )
            # The switcher list only changes when the bot joins or leaves a
            # server, so it rides along with the first render rather than with
            # every poll.
            data = await frame(
                search_results=results, search_term=search_term, search_source=source,
                guilds=self._dash_guild_options(user),
            )
            return {
                "status": 0,
                "web_content": {
                    "source": PLAYER_TEMPLATE,
                    # No guild hero and no breadcrumb: this page carries its
                    # own server bar, with the switcher and the stats in it.
                    "bare": True,
                    # The client boots from the same blob it later polls for,
                    # so the first paint and every later one share one code path.
                    "boot": _dash_json(data),
                    "player_state": data["state"],
                    "search_results": results,
                    "search_term": search_term,
                    "is_staff": is_staff,
                    "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                },
            }

        # --- guild favourites playlist ---
        if action in ("fav_add", "fav_remove", "fav_play", "fav_queue", "fav_clear"):
            message, category = await self._dash_favourites(action, member, guild, player, field)
            return await reply(message, category)

        # --- play / enqueue: connects if needed ---
        if action in ("play", "play_now"):
            identifier = (field("identifier") or field("query") or "").strip()
            if not identifier:
                return await reply("Nothing to play.", "warning")
            message, category = await self._dash_play(
                member, guild, player, identifier, play_now=(action == "play_now")
            )
            return await reply(message, category)

        if action:
            if player is None:
                return await reply("I am not connected to a voice channel.", "warning")
            try:
                if action == "pause":
                    await player.set_pause(True, member)
                elif action == "resume":
                    await player.set_pause(False, member)
                elif action == "skip":
                    await player.skip(member)
                elif action == "previous":
                    await player.previous(member)
                elif action == "stop":
                    await player.stop(member)
                elif action == "shuffle":
                    await player.shuffle_queue(member.id)
                elif action == "disconnect":
                    await player.disconnect(requester=member)
                elif action == "volume_up":
                    await player.set_volume(min(player.volume + 5, 1000), member)
                elif action == "volume_down":
                    await player.set_volume(max(player.volume - 5, 0), member)
                elif action == "volume_set":
                    raw = field("volume")
                    await player.set_volume(max(0, min(int(raw), 1000)), member)
                elif action == "seek":
                    # form sends seconds; PyLav wants milliseconds
                    await player.seek(float(field("position") or 0) * 1000, member)
                elif action == "repeat_track":
                    # Repeat state lives in async config, not a plain attribute.
                    current = await player.config.fetch_repeat_current()
                    await player.set_repeat("current", not current, member)
                elif action == "repeat_queue":
                    current = await player.config.fetch_repeat_queue()
                    await player.set_repeat("queue", not current, member)
                elif action == "repeat_off":
                    await player.set_repeat("disable", False, member)
                elif action == "clear_queue":
                    player.queue.clear()
                elif action == "remove_track":
                    # popindex() is PlayerQueue's supported positional removal.
                    player.queue.popindex(int(field("index")))
                elif action == "move_top":
                    # "Play this next" in one click instead of skip-and-hope.
                    #
                    # PlayerQueue has no insert(): the deque behind it is not
                    # meant to be written to directly, and doing so skips the
                    # bookkeeping the player relies on. move_track is PyLav's
                    # own reorder and is what the audio cog uses. The fallback
                    # is for builds predating it - add(index=0) is the other
                    # supported way in.
                    index = int(field("index"))
                    mover = getattr(player, "move_track", None)
                    if mover is not None:
                        await mover(index, member, 0)
                    else:
                        track = player.queue.popindex(index)
                        await player.add(requester=member.id, track=track, index=0)
                elif action == "repeat_cycle":
                    # One control that walks off -> track -> queue -> off, which
                    # is what the three separate buttons were really doing.
                    if await player.config.fetch_repeat_current():
                        await player.set_repeat("queue", True, member)
                    elif await player.config.fetch_repeat_queue():
                        await player.set_repeat("disable", False, member)
                    else:
                        await player.set_repeat("current", True, member)
                elif action == "autoplay":
                    current = bool(
                        await self._maybe_await(getattr(player, "autoplay_enabled", None), False)
                    )
                    await player.set_autoplay(not current)
                elif action == "mute":
                    # Remember the level so unmuting returns to it rather than
                    # to some arbitrary default.
                    if player.volume > 0:
                        self._dash_premute[guild.id] = player.volume
                        await player.set_volume(0, member)
                    else:
                        await player.set_volume(
                            self._dash_premute.pop(guild.id, None) or 50, member
                        )
                else:
                    return await reply(f"Unknown action: {action}", "warning")
            except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
                log.exception("Dashboard player action %r failed", action)
                return await reply(f"Action failed: {exc}", "danger")
            # The client redraws from the state in this reply, so a successful
            # action needs no message of its own - the UI moving is the receipt.
            return await reply("" if api else "Done.", "success")

        # --- build the view ---
        data = await frame(
            search_results=[], search_term="", search_source="ytsearch",
            guilds=self._dash_guild_options(user),
        )
        return {
            "status": 0,
            "web_content": {
                "source": PLAYER_TEMPLATE,
                "bare": True,
                "boot": _dash_json(data),
                "player_state": data["state"],
                # kwargs["csrf_token"] is (raw, signed); the signed value goes in the form.
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "search_results": [],
                "search_term": "",
                "is_staff": is_staff,
            },
        }

    async def _dash_track_dict(self, track, position: int | None = None) -> dict[str, t.Any]:
        """PyLav track fields are async methods, so each one must be awaited."""

        async def safe(coro_method, default):
            if coro_method is None:
                return default
            try:
                value = await coro_method()
            except Exception:  # noqa: BLE001 - a single bad field shouldn't kill the page
                return default
            return default if value is None else value

        data = {
            "title": await safe(getattr(track, "title", None), "Unknown title"),
            "author": await safe(getattr(track, "author", None), ""),
            "uri": await safe(getattr(track, "uri", None), ""),
            "duration": _fmt_ms(await safe(getattr(track, "duration", None), 0)),
        }
        if position is not None:
            data["position"] = position
        return data

    @staticmethod
    async def _maybe_await(value, default=None):
        """Read a PyLav attribute that is a coroutine on some versions and a
        plain value on others (``autoplay_enabled`` is both, depending on the
        build), without letting either shape raise."""
        try:
            if callable(value):
                value = value()
            if inspect.isawaitable(value):
                value = await value
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    async def _dash_build_state(self, player) -> dict[str, t.Any]:
        """Everything the page needs for one frame, as plain JSON-able data.

        The client polls this a few times a second's worth of interval and
        redraws from it, so anything the UI shows has to be in here - including
        the things that only change from Discord, like a skip pressed on the
        controller embed.
        """
        # `stamp` lets the client work out how stale a frame is and extrapolate
        # the playhead between polls instead of freezing on the last value.
        stamp = int(time.time() * 1000)
        if player is None:
            return {"connected": False, "stamp": stamp, "queue": [], "queue_length": 0}

        current = player.current
        try:
            raw_queue = list(player.queue.raw_queue)
        except Exception:  # noqa: BLE001 - queue internals vary by version
            raw_queue = []

        queue_items = []
        queue_ms = 0
        # Deep enough to be useful, shallow enough that a 500-track queue does
        # not turn every poll into thousands of field reads.
        for index, track in enumerate(raw_queue[:50], start=1):
            item = await self._dash_track_dict(track, position=index)
            try:
                item["duration_ms"] = int(await track.duration() or 0)
            except Exception:  # noqa: BLE001
                item["duration_ms"] = 0
            try:
                item["identifier"] = track.encoded or item.get("uri") or ""
            except Exception:  # noqa: BLE001
                item["identifier"] = item.get("uri") or ""
            queue_ms += item["duration_ms"]
            queue_items.append(item)

        try:
            position_ms = await player.position()
        except Exception:  # noqa: BLE001
            position_ms = 0

        try:
            repeat_track = bool(await player.config.fetch_repeat_current())
            repeat_queue = bool(await player.config.fetch_repeat_queue())
        except Exception:  # noqa: BLE001
            repeat_track = repeat_queue = False

        voice = getattr(player, "channel", None)
        listeners = [
            {"id": str(m.id), "name": m.display_name, "avatar": (m.display_avatar.url if m.display_avatar else "")}
            for m in getattr(voice, "members", ()) or ()
            if not m.bot
        ]

        try:
            history_length = len(list(player.history.raw_queue))
        except Exception:  # noqa: BLE001
            history_length = 0

        state: dict[str, t.Any] = {
            "connected": True,
            "stamp": stamp,
            "position_ms": int(position_ms or 0),
            "position": _fmt_ms(position_ms),
            "paused": bool(player.paused),
            "playing": bool(player.is_playing),
            "volume": int(player.volume),
            "channel": getattr(voice, "name", ""),
            "channel_id": str(getattr(voice, "id", "") or ""),
            "queue_length": len(raw_queue),
            "queue_duration_ms": queue_ms,
            "queue_duration": _fmt_ms(queue_ms),
            "queue": queue_items,
            "queue_truncated": max(0, len(raw_queue) - len(queue_items)),
            "repeat": "track" if repeat_track else ("queue" if repeat_queue else "off"),
            "autoplay": bool(await self._maybe_await(getattr(player, "autoplay_enabled", None), False)),
            "listeners": listeners,
            "history_length": history_length,
            "current": None,
        }

        if current is not None:
            current_data = await self._dash_track_dict(current)
            try:
                current_data["duration_ms"] = int(await current.duration() or 0)
            except Exception:  # noqa: BLE001
                current_data["duration_ms"] = 0
            try:
                current_data["artwork"] = await current.artworkUrl() or ""
            except Exception:  # noqa: BLE001
                current_data["artwork"] = ""
            try:
                current_data["stream"] = bool(await current.stream())
            except Exception:  # noqa: BLE001
                current_data["stream"] = False
            try:
                current_data["identifier"] = current.encoded or current_data.get("uri") or ""
            except Exception:  # noqa: BLE001
                current_data["identifier"] = current_data.get("uri") or ""
            try:
                current_data["source"] = await current.source() or ""
            except Exception:  # noqa: BLE001
                current_data["source"] = ""
            requester = self.bot.get_user(getattr(current, "requester_id", 0) or 0)
            current_data["requester"] = requester.display_name if requester else ""
            current_data["requester_avatar"] = (
                requester.display_avatar.url if requester and requester.display_avatar else ""
            )
            # A track key that changes whenever the track does, so the client
            # can tell "same song, later position" from "somebody hit skip".
            current_data["key"] = f"{current_data.get('identifier','')}|{current_data.get('title','')}"
            state["current"] = current_data
        return state


    # ---------- search / play helpers ----------

    # Where a search can be pointed. The value is the Lavaplayer prefix; a
    # source the nodes have no plugin for simply returns nothing, which is why
    # the page shows the picker rather than silently rewriting the query.
    SEARCH_SOURCES = (
        ("ytsearch", "YouTube", "fa-youtube-play"),
        ("ytmsearch", "YouTube Music", "fa-music"),
        ("spsearch", "Spotify", "fa-spotify"),
        ("dzsearch", "Deezer", "fa-headphones"),
        ("amsearch", "Apple Music", "fa-apple"),
        ("scsearch", "SoundCloud", "fa-soundcloud"),
        ("ymsearch", "Yandex Music", "fa-music"),
    )
    _SEARCH_PREFIXES = frozenset(key for key, _label, _icon in SEARCH_SOURCES)

    async def _dash_search(self, search_term: str, limit: int = 24, source: str = "ytsearch"):
        """Returns (results, error_message). Results are plain dicts for the template."""
        if source not in self._SEARCH_PREFIXES:
            source = "ytsearch"
        try:
            # A bare string can resolve to a single track; prefixing forces the
            # node to return a search result set instead of one match. A term
            # that already carries its own prefix is left alone, so power users
            # can still type `scsearch:...` regardless of the picker.
            looks_like_url = search_term.startswith(("http://", "https://", "spotify:"))
            already_prefixed = any(
                search_term.lower().startswith(f"{prefix}:") for prefix in self._SEARCH_PREFIXES
            )
            query = await Query.from_string(
                search_term
                if (looks_like_url or already_prefixed)
                else f"{source}:{search_term}"
            )
            # fullsearch=True is required, otherwise PyLav returns only the first match.
            response = await self.pylav.search_query(query, fullsearch=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("Dashboard search failed for %r", search_term)
            return [], f"Search failed: {exc}"

        if response is None:
            return [], "No response from the audio node."

        load_type = getattr(response, "loadType", None)
        data = getattr(response, "data", None)

        if load_type == "error":
            return [], f"Search error: {getattr(data, 'message', 'unknown error')}"
        if load_type == "empty" or data is None:
            return [], None

        if load_type == "search":
            tracks = list(data)
        elif load_type == "playlist":
            tracks = list(getattr(data, "tracks", []))
        elif load_type == "track":
            tracks = [data]
        else:
            tracks = []

        results = []
        for track in tracks[:limit]:
            info = getattr(track, "info", None)
            if info is None:
                continue
            # The uri is what the play path has always been given, so it stays
            # the identifier. The encoded blob is carried alongside it because
            # it is the only handle that survives a source with no public URL,
            # and it is what the favourites playlist stores.
            uri = getattr(info, "uri", None) or ""
            encoded = getattr(track, "encoded", None) or ""
            results.append(
                {
                    "title": getattr(info, "title", None) or "Unknown title",
                    "author": getattr(info, "author", None) or "",
                    "duration": _fmt_ms(getattr(info, "length", 0)),
                    "duration_ms": int(getattr(info, "length", 0) or 0),
                    "uri": uri,
                    "encoded": encoded,
                    "identifier": uri or encoded,
                    "artwork": getattr(info, "artworkUrl", None) or "",
                    "stream": bool(getattr(info, "isStream", False)),
                    "source": getattr(info, "sourceName", None) or "",
                }
            )
        return results, None

    async def _dash_play(self, member, guild, player, identifier: str, play_now: bool = False):
        """Enqueue (or immediately play) a query/URL. Returns (message, category)."""
        # Connect if we have no player yet - the requester must be in a voice channel.
        if player is None:
            voice_state = getattr(member, "voice", None)
            channel = getattr(voice_state, "channel", None)
            if channel is None:
                return ("Join a voice channel first, then try again.", "warning")
            try:
                player = await self.pylav.player_manager.create(channel=channel, requester=member)
            except Exception as exc:  # noqa: BLE001
                log.exception("Dashboard could not create a player")
                return (f"Could not connect: {exc}", "danger")

        try:
            query = await Query.from_string(identifier)
            response = await self.pylav.get_tracks(query, player=player)
        except Exception as exc:  # noqa: BLE001
            log.exception("Dashboard could not resolve %r", identifier)
            return (f"Could not resolve that: {exc}", "danger")

        load_type = getattr(response, "loadType", None)
        data = getattr(response, "data", None)

        if load_type == "error":
            return (f"Load error: {getattr(data, 'message', 'unknown error')}", "danger")
        if load_type == "empty" or data is None:
            return ("Nothing found for that query.", "warning")

        if load_type == "playlist":
            tracks = list(getattr(data, "tracks", []))
        elif load_type == "search":
            tracks = list(data)[:1]
        else:
            tracks = [data]

        if not tracks:
            return ("Nothing found for that query.", "warning")

        try:
            if play_now or not player.current:
                await player.play(tracks[0], query, member)
                extra = tracks[1:]
            else:
                extra = tracks
            for track in extra:
                await player.add(requester=member.id, track=track, query=query)
        except Exception as exc:  # noqa: BLE001
            log.exception("Dashboard playback failed")
            return (f"Playback failed: {exc}", "danger")

        if len(tracks) > 1:
            return (f"Added {len(tracks)} tracks to the queue.", "success")
        return ("Added to the queue." if not play_now else "Now playing.", "success")



    # ---------- economy ----------
    #
    # Optional per-action charge. Costs live in the cog's own Config under
    # `dashboard_action_costs`; an action missing from that mapping is free.
    # Staff are never charged.

    async def _dash_economy_state(self, member: discord.Member, guild: discord.Guild, is_staff: bool):
        """Costs + balance for rendering the price list on the page."""
        try:
            enabled = await self._config.guild(guild).dashboard_economy_enabled()
            costs = await self._config.guild(guild).dashboard_action_costs() or {}
        except Exception:  # noqa: BLE001
            return None
        if not enabled or is_staff or not costs:
            return None
        try:
            balance = await bank.get_balance(member)
            currency = await bank.get_currency_name(guild)
        except Exception:  # noqa: BLE001
            return None
        return {
            "balance": balance,
            "currency": currency,
            "costs": dict(sorted(costs.items(), key=lambda kv: -int(kv[1] or 0))),
        }

    async def _dash_charge(self, member: discord.Member, guild: discord.Guild, action: str, is_staff: bool):
        """Returns (ok, message). Charges the member if a cost is configured."""
        if is_staff:
            return True, None
        try:
            if not await self._config.guild(guild).dashboard_economy_enabled():
                return True, None
            costs = await self._config.guild(guild).dashboard_action_costs()
        except Exception:  # noqa: BLE001
            return True, None
        cost = int((costs or {}).get(action, 0) or 0)
        if cost <= 0:
            return True, None
        try:
            if not await bank.can_spend(member, cost):
                currency = await bank.get_currency_name(guild)
                balance = await bank.get_balance(member)
                return False, (
                    f"That costs {cost} {currency}, but you only have {balance}."
                )
            await bank.withdraw_credits(member, cost)
            currency = await bank.get_currency_name(guild)
            # Let anything else that cares (the notifier, for one) report the
            # charge without having to reach into this cog.
            self.bot.dispatch("plcontroller_charged", member, action, cost, currency)
            return True, f"Charged {cost} {currency}."
        except Exception as exc:  # noqa: BLE001
            log.exception("Economy charge failed for %r", action)
            # Never block playback because the economy backend misbehaved.
            return True, None


    async def _dash_wallet(self, member: discord.Member, guild: discord.Guild) -> dict:
        """Balance + configured costs, for display on the player page."""
        try:
            costs = await self._config.guild(guild).dashboard_action_costs() or {}
        except Exception:  # noqa: BLE001
            costs = {}
        if not costs:
            return {"enabled": False}
        try:
            return {
                "enabled": True,
                "balance": await bank.get_balance(member),
                "currency": await bank.get_currency_name(guild),
                "costs": dict(sorted(costs.items())),
            }
        except Exception:  # noqa: BLE001
            return {"enabled": False}

    # ---------- settings page ----------

    @dashboard_page(
        name="settings",
        description="Buttons, icons and what each action costs.",
        methods=("GET", "POST"),
    )
    async def dashboard_settings_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = await self._dash_check_perms(user, guild)
        if error is not None:
            return error
        if not await self._dash_is_staff(user, member, guild):
            return {
                "status": 1,
                "error_title": "Forbidden",
                "error_message": "Only moderators can change the controller settings.",
            }

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._dash_settings_post(guild, kwargs)

        settings = await self._config.guild(guild).all()
        currency = await self._dash_currency(guild)
        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": SETTINGS_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "button_rows": self._dash_button_rows(settings),
                "upload_target": settings.get("upload_target") or "guild",
                "style_options": ("secondary", "primary", "success", "danger"),
                "guild_emojis": emoji_options(guild),
                "economy_enabled": bool(settings.get("dashboard_economy_enabled")),
                "currency": currency,
                "cost_rows": self._dash_cost_rows(settings),
                "posted": bool(settings.get("persistent_view_message_id")),
                "live": self._dash_live_buttons(guild),
            },
        }

    # ---------- settings helpers ----------

    @staticmethod
    async def _dash_currency(guild: discord.Guild) -> str:
        try:
            return await bank.get_currency_name(guild)
        except Exception:  # noqa: BLE001 - the page must render without a bank
            return "credits"

    def _dash_button_rows(self, settings: dict) -> list[dict]:
        from .view import BUTTONS, DEFAULT_STYLES, parse_emoji

        overrides = settings.get("button_emojis") or {}
        labels = settings.get("button_labels") or {}
        styles = settings.get("button_styles") or {}
        owned = settings.get("owned_emojis") or {}
        rejected = settings.get("rejected_emojis") or {}

        rows = []
        for key, default_label, default_emoji, blurb in BUTTONS:
            current = overrides.get(key) or ""
            rows.append(
                {
                    "key": key,
                    "blurb": blurb,
                    "default_label": default_label,
                    "label_override": labels.get(key, ""),
                    "default": default_emoji,
                    "current": current,
                    "effective": current or default_emoji,
                    # A stored value that no longer parses means the emoji was
                    # deleted or mistyped; the button silently drops it.
                    "broken": bool(current) and parse_emoji(current) is None,
                    # The page must show the colour the button actually has,
                    # which is the guild's override or the button's own
                    # default - not a blanket "secondary".
                    "style": styles.get(key) or DEFAULT_STYLES.get(key, "secondary"),
                    "image": self._dash_emoji_image(owned.get(key), current),
                    "rejected": rejected.get(key, ""),
                }
            )
        return rows

    @staticmethod
    def _dash_emoji_image(owned_token, current) -> str:
        """The CDN url for a custom emoji, so the page can show the picture."""
        import re as _re

        token = (owned_token or current or "").strip()
        match = _re.match(r"^<(a?):[A-Za-z0-9_]+:(\d{15,25})>$", token)
        if not match:
            return ""
        animated, emoji_id = match.groups()
        return f"https://cdn.discordapp.com/emojis/{emoji_id}.{'gif' if animated else 'png'}"

    def _dash_cost_rows(self, settings: dict) -> list[dict]:
        costs = settings.get("dashboard_action_costs") or {}
        return [
            {"key": key, "label": key.replace("_", " ").title(), "value": int(value or 0)}
            for key, value in sorted(costs.items())
        ]

    async def _dash_settings_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")
        conf = self._config.guild(guild)
        try:
            if action == "save_buttons":
                return await self._dash_save_buttons(guild, conf, field)
            if action == "save_costs":
                return await self._dash_save_costs(conf, field)
            if action == "reset_buttons":
                await conf.button_emojis.set({})
                await conf.button_labels.set({})
                await conf.button_styles.set({})
                await conf.rejected_emojis.set({})
                pushed = await self._dash_refresh_controller(guild)
                return [
                    {"message": "Buttons are back to the defaults." + pushed,
                     "category": "success"}
                ]
        except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
            log.exception("Controller settings action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]
        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    # Discord caps an emoji image at 256 KB. The browser sends base64, which is
    # about a third larger, so the raw form value runs a little over that.
    _MAX_IMAGE_BYTES = 256 * 1024
    _IMAGE_TYPES = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/gif": "gif",
        "image/webp": "webp",
    }

    @classmethod
    def _dash_decode_image(cls, value: str) -> tuple[bytes | None, str]:
        """Turn a `data:image/png;base64,...` field into bytes.

        Returns (None, reason) rather than raising: a bad paste should report
        itself on the page instead of failing the whole save.
        """
        import base64

        if not value or not value.startswith("data:"):
            return None, "that was not an image"
        try:
            header, payload = value.split(",", 1)
            mime = header[5:].split(";", 1)[0].strip().lower()
        except ValueError:
            return None, "the image data was malformed"
        if mime not in cls._IMAGE_TYPES:
            return None, f"{mime or 'that file type'} is not supported"
        try:
            raw = base64.b64decode(payload, validate=True)
        except Exception:  # noqa: BLE001
            return None, "the image data was malformed"
        if not raw:
            return None, "the image was empty"
        if len(raw) > cls._MAX_IMAGE_BYTES:
            return None, f"the image is {len(raw) // 1024} KB, over Discord's 256 KB limit"
        return raw, ""

    async def _dash_make_bot_emoji(self, name: str, raw: bytes):
        """Upload to the bot's own application emoji. No server slot is used.

        These are shared by every server the bot is in, so the name carries no
        guild id and a second server uploading the same button reuses whatever
        is already there rather than colliding.
        """
        create = getattr(self.bot, "create_application_emoji", None)
        if create is None:
            return None, "this discord.py build has no application emoji support."

        # A duplicate name is rejected outright, so clear the old one first.
        listing = getattr(self.bot, "fetch_application_emojis", None)
        if listing is not None:
            with contextlib.suppress(Exception):
                for existing in await listing():
                    if existing.name == name:
                        await existing.delete()
        try:
            emoji = await create(name=name, image=raw)
        except discord.HTTPException as exc:
            return None, f"Discord refused the picture: {exc.text or exc}"
        return f"<{'a' if emoji.animated else ''}:{emoji.name}:{emoji.id}>", ""

    async def _dash_make_emoji(self, guild: discord.Guild, key: str, raw: bytes,
                               target: str = "guild"):
        """Register an image as a guild emoji and return its `<:name:id>` token.

        It has to be a *guild* emoji: Discord rejects an application emoji on a
        message component, even though the application owns it and the CDN
        serves it happily. The name carries no guild id because a guild emoji
        only needs to be unique within its own guild, which also keeps it under
        the 32-character cap without truncating.
        """
        name = f"plc_{key}"[:32]

        if target == "bot":
            token, reason = await self._dash_make_bot_emoji(name, raw)
            if token is not None:
                return token, reason
            # Fall through and use a server emoji rather than refusing outright.
            log.warning("Bot emoji upload failed for %s, using a guild one: %s", key, reason)

        me = guild.me
        if me is None or not me.guild_permissions.manage_expressions:
            return None, (
                "I need the Manage Expressions permission to upload a picture. "
                "Grant it, or paste an existing emoji as <:name:id> instead."
            )

        # Clear a previous upload for this button first; creating a duplicate
        # name is rejected outright.
        for existing in guild.emojis:
            if existing.name == name:
                with contextlib.suppress(discord.HTTPException):
                    await existing.delete(reason="PyLavController button image replaced")

        try:
            emoji = await guild.create_custom_emoji(
                name=name, image=raw, reason="PyLavController button image"
            )
        except discord.HTTPException as exc:
            if exc.code == 30008:
                return None, "this server has no free emoji slots."
            return None, f"Discord refused the picture: {exc.text or exc}"
        return f"<{'a' if emoji.animated else ''}:{emoji.name}:{emoji.id}>", ""

    async def _dash_drop_emoji(self, guild: discord.Guild, token: str) -> None:
        """Delete an emoji this cog created, so a replaced picture is not a leak.

        Still checks the application's own emoji: earlier versions uploaded
        there, and those need cleaning up even though they were never usable on
        a button.
        """
        import re as _re

        match = _re.search(r":(\\d{15,25})>$", token or "")
        if not match:
            return
        emoji_id = int(match.group(1))
        emoji = guild.get_emoji(emoji_id)
        if emoji is not None:
            with contextlib.suppress(discord.HTTPException):
                await emoji.delete(reason="PyLavController button image replaced")
            return
        fetch = getattr(self.bot, "fetch_application_emoji", None)
        if fetch is not None:
            with contextlib.suppress(Exception):
                await (await fetch(emoji_id)).delete()

    async def _dash_save_images(self, guild, conf, field) -> list[str]:
        """Apply any uploaded or cleared button pictures. Returns problems."""
        from .view import BUTTONS

        problems: list[str] = []
        # Read the choice from this submit so a change takes effect on the same
        # save rather than only on the next one.
        target = (field("upload_target") or "").strip()
        if target not in ("guild", "bot"):
            target = await conf.upload_target()
        else:
            await conf.upload_target.set(target)
        async with conf.button_emojis() as emojis, conf.owned_emojis() as owned:
            for key, _label, _emoji, _blurb in BUTTONS:
                if field.checked(f"clear_img_{key}"):
                    if key in owned:
                        await self._dash_drop_emoji(guild, owned.pop(key))
                        emojis.pop(key, None)
                    continue
                value = field(f"img_{key}") or ""
                if not value:
                    continue
                raw, reason = self._dash_decode_image(value)
                if raw is None:
                    problems.append(f"'{key}': {reason}.")
                    continue
                token, reason = await self._dash_make_emoji(guild, key, raw, target)
                if token is None:
                    problems.append(f"'{key}': {reason}")
                    continue
                if key in owned:
                    await self._dash_drop_emoji(guild, owned[key])
                owned[key] = token
                emojis[key] = token
        async with conf.rejected_emojis() as rejected:
            for key, _label, _emoji, _blurb in BUTTONS:
                if field(f"img_{key}"):
                    rejected.pop(key, None)
        return problems

    def _dash_live_buttons(self, guild: discord.Guild) -> dict:
        """What the running controller currently holds, button by button.

        Compares three things that should agree and often do not:
        what is saved, what the in-memory button carries, and whether this
        build of the cog even has the code that copies one to the other.
        """
        info = {
            "has_styling_code": False,
            "channel": None,
            "posted": False,
            "rows": [],
            "note": "",
        }
        try:
            from .view import BUTTONS, PersistentControllerView

            info["has_styling_code"] = hasattr(
                PersistentControllerView, "_apply_button_styling"
            )

            channel_id = (getattr(self, "_channel_cache", None) or {}).get(guild.id)
            if not channel_id:
                info["note"] = "No controller channel is set for this server."
                return info
            channel = guild.get_channel(channel_id)
            info["channel"] = f"#{channel.name}" if channel is not None else str(channel_id)

            view = (getattr(self, "_view_cache", None) or {}).get(channel_id)
            if view is None:
                info["note"] = "No controller view is loaded; post the panel first."
                return info
            info["posted"] = getattr(view, "message", None) is not None
            if not info["posted"]:
                info["note"] = "The view has no message attached, so nothing can be edited."

            for attribute, default_label, _default_emoji, _blurb in BUTTONS:
                button = getattr(view, attribute, None)
                if button is None:
                    continue
                emoji = getattr(button, "emoji", None)
                info["rows"].append(
                    {
                        "key": attribute,
                        "label": getattr(button, "label", None) or default_label,
                        # A custom emoji has an id; a unicode one does not.
                        "custom": bool(getattr(emoji, "id", None)),
                        "emoji": str(emoji) if emoji is not None else "none",
                        "attached": any(
                            child is button for child in getattr(view, "children", [])
                        ),
                    }
                )
        except Exception as exc:  # noqa: BLE001 - diagnostics must not break the page
            log.exception("Could not inspect the live controller")
            info["note"] = f"Could not inspect the controller: {exc}"
        return info

    async def _dash_refresh_controller(self, guild: discord.Guild) -> str:
        """Redraw the live controller so a save is visible immediately.

        Deliberately does not trust `_view_cache`: it is populated at cog load
        and can easily not match the message on screen. The channel and the
        message id both live in config, so the panel is rebuilt from those, and
        the fresh view replaces whatever was cached.

        Returns a sentence to append to the notification, so the page says what
        actually happened rather than leaving you to guess.
        """
        try:
            from .view import PersistentControllerView

            settings = await self._config.guild(guild).all()
            channel = guild.get_channel(settings.get("channel") or 0)
            if channel is None:
                return " No controller channel is set, so there is nothing to redraw."

            message_id = settings.get("persistent_view_message_id")
            if not message_id:
                return " No controller has been posted yet, so there is nothing to redraw."

            try:
                message = await channel.fetch_message(message_id)
            except discord.NotFound:
                return " The controller message is gone; post it again."
            except discord.Forbidden:
                return f" I cannot read messages in #{channel.name}."

            view = PersistentControllerView(cog=self, channel=channel, message=message)
            await view.prepare()
            with contextlib.suppress(Exception):
                await view.set_permissions()

            kwargs = await view.get_now_playing_embed()
            attachments = []
            if "file" in kwargs:
                attachments = [kwargs.pop("file")]
            elif "files" in kwargs:
                attachments = kwargs.pop("files")
            if attachments:
                kwargs["attachments"] = attachments

            # Discord reports every bad emoji at once but only lets the view
            # drop the first, so one retry is not enough when two buttons are
            # bad. Keep dropping until it goes through, bounded by the number
            # of buttons there are to drop.
            for _attempt in range(len(view.children) + 1):
                try:
                    await message.edit(view=view, **kwargs)
                    break
                except discord.HTTPException as exc:
                    if not await view._drop_rejected_emoji(exc):
                        raise
            else:
                return " Discord refused every emoji on the controller."

            # Keep clicks working: the old cached view is now out of date, and
            # the persistent registration has to point at the new one.
            self._view_cache[channel.id] = view
            with contextlib.suppress(Exception):
                self.bot.add_view(view, message_id=message.id)
            return " The controller has been redrawn."
        except Exception as exc:  # noqa: BLE001 - the save itself already worked
            log.exception("Could not redraw the controller after a save")
            return f" The controller could not be redrawn: {exc}"

    async def _dash_save_buttons(self, guild, conf, field) -> list[dict]:
        from .view import BUTTONS, BUTTON_STYLES, DEFAULT_STYLES, parse_emoji

        problems = await self._dash_save_images(guild, conf, field)
        uploaded = set(await conf.owned_emojis())
        bad = []

        async with conf.button_emojis() as emojis, conf.button_labels() as labels:
            async with conf.button_styles() as styles:
                for key, _label, _emoji, _blurb in BUTTONS:
                    value = (field(f"e_{key}") or "").strip()
                    if not value:
                        # A blank box must not wipe a picture uploaded in the
                        # same submit.
                        if key not in uploaded:
                            emojis.pop(key, None)
                    elif parse_emoji(value) is None:
                        bad.append((key, value))
                    else:
                        emojis[key] = value

                    text = (field(f"l_{key}") or "").strip()
                    if text:
                        labels[key] = text[:80]
                    else:
                        labels.pop(key, None)

                    # Only a colour that differs from the button's default is
                    # worth storing. Comparing against the default rather than
                    # against "secondary" is what lets somebody turn Stop back
                    # to transparent and have it stick.
                    style = (field(f"s_{key}") or "").strip()
                    if style in BUTTON_STYLES and style != DEFAULT_STYLES.get(key, "secondary"):
                        styles[key] = style
                    else:
                        styles.pop(key, None)

        notes = [emoji_rejection(k, v) for k, v in bad] + [
            {"message": text, "category": "warning"} for text in problems
        ]
        pushed = await self._dash_refresh_controller(guild)
        return notes + [
            {"message": "Buttons saved." + pushed, "category": "success"}
        ]

    async def _dash_save_costs(self, conf, field) -> list[dict]:
        costs = await conf.dashboard_action_costs()
        bad = []
        updated = {}
        for key in costs:
            raw = field.integer(f"c_{key}", None)
            if raw is None:
                updated[key] = int(costs[key] or 0)
                continue
            if raw < 0:
                bad.append(key)
                updated[key] = int(costs[key] or 0)
                continue
            updated[key] = raw
        await conf.dashboard_action_costs.set(updated)
        await conf.dashboard_economy_enabled.set(field.checked("economy_enabled"))
        notes = [
            {"message": f"'{k}' cannot be negative and was left alone.", "category": "warning"}
            for k in bad
        ]
        return notes + [{"message": "Prices saved.", "category": "success"}]

    # ---------- guild favourites ----------

    FAV_PLAYLIST_NAME = "Dashboard Favourites"

    async def _dash_get_fav_playlist(self, guild: discord.Guild, author_id: int):
        """Fetch the guild favourites playlist, creating it only if missing.

        IMPORTANT: create_or_update_guild_playlist() runs `tracks=tracks or []`,
        so calling it on an existing playlist WIPES every track. It must only be
        used for first-time creation - reads go through get_playlist().
        """
        playlist = self.pylav.playlist_db_manager.get_playlist(identifier=guild.id)
        if not await playlist.exists():
            playlist = await self.pylav.playlist_db_manager.create_or_update_guild_playlist(
                guild=guild, author=author_id, name=self.FAV_PLAYLIST_NAME, tracks=[]
            )
        return playlist

    async def _dash_favourites(self, action, member, guild, player, field):
        try:
            playlist = await self._dash_get_fav_playlist(guild, member.id)
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not open the guild favourites playlist")
            return (f"Could not open the playlist: {exc}", "danger")

        try:
            if action == "fav_add":
                identifier = (field("identifier") or "").strip()
                if not identifier and player is not None and player.current is not None:
                    identifier = player.current.encoded or await player.current.uri()
                if not identifier:
                    return ("Nothing to save.", "warning")
                existing = await playlist.fetch_tracks() or []
                if identifier in existing:
                    return ("That track is already in the favourites.", "warning")
                await playlist.add_track([identifier])
                return ("Saved to the guild favourites.", "success")

            if action == "fav_remove":
                identifier = (field("identifier") or "").strip()
                if not identifier:
                    return ("Nothing to remove.", "warning")
                await playlist.remove_track(identifier)
                return ("Removed from the guild favourites.", "success")

            if action == "fav_clear":
                await playlist.remove_all_tracks()
                return ("Cleared the guild favourites.", "success")

            tracks = await playlist.fetch_tracks() or []
            if not tracks:
                return ("The guild favourites playlist is empty.", "warning")
            play_now = action == "fav_play"
            added = 0
            for entry in tracks:
                identifier = entry if isinstance(entry, str) else (entry or {}).get("encoded")
                if not identifier:
                    continue
                message, category = await self._dash_play(
                    member, guild, player, identifier, play_now=(play_now and added == 0)
                )
                if category == "danger":
                    return (message, category)
                player = self._dash_player(guild) or player
                added += 1
            return (f"Queued {added} track(s) from the guild favourites.", "success")
        except Exception as exc:  # noqa: BLE001
            log.exception("Favourites action %r failed", action)
            return (f"Favourites action failed: {exc}", "danger")

    async def _dash_fav_list(self, guild: discord.Guild):
        """Read-only listing. Must never call create_or_update (it wipes tracks)."""
        try:
            playlist = self.pylav.playlist_db_manager.get_playlist(identifier=guild.id)
            if not await playlist.exists():
                return []
            raw = await playlist.fetch_tracks() or []
        except Exception:  # noqa: BLE001
            log.exception("Could not read the guild favourites playlist")
            return []

        # Decoding is the expensive half of this, and the web player asks for
        # the list on every poll. The stored identifiers are the whole input,
        # so caching on them means a decode only happens when the playlist
        # actually changes - not several times a second, per viewer.
        signature = tuple(
            entry if isinstance(entry, str) else (entry or {}).get("encoded") for entry in raw[:50]
        )
        cached = self._dash_fav_cache.get(guild.id)
        if cached is not None and cached[0] == signature:
            return cached[1]

        out = []
        for entry in raw[:50]:
            identifier = entry if isinstance(entry, str) else (entry or {}).get("encoded")
            if not identifier:
                continue
            title = identifier
            try:
                decoded = await self.pylav.decode_track(identifier, raise_on_failure=False)
                info = getattr(decoded, "info", None)
                if info is not None and getattr(info, "title", None):
                    title = f"{info.title}" + (f" - {info.author}" if getattr(info, "author", None) else "")
            except Exception:  # noqa: BLE001
                pass
            out.append({"identifier": identifier, "title": title})
        self._dash_fav_cache[guild.id] = (signature, out)
        return out


# The reply to every fetch() the page makes. The dashboard always renders a
# full HTML document around whatever a page returns, so the payload travels as
# a marker <script> the client digs back out with DOMParser. `payload` is
# already JSON and already has its angle brackets escaped, so |safe here cannot
# let a track title close the tag early.
API_TEMPLATE = """<script type="application/json" id="plc-api-payload">{{ payload|safe }}</script>"""


PLAYER_TEMPLATE = NOTIFICATIONS + r"""
<style>
/* ============================================================
   PyLav web player
   Self-contained: no dependency on the dashboard's own classes,
   so it renders the same wherever it is embedded.
   ============================================================ */
#plcRoot{
  --plc-bg:rgba(12,16,28,.55);
  --plc-line:rgba(255,255,255,.10);
  --plc-line-2:rgba(255,255,255,.16);
  --plc-txt:#eef2ff;
  --plc-dim:rgba(238,242,255,.62);
  --plc-dimmer:rgba(238,242,255,.40);
  --plc-accent:#6c8cff;
  --plc-accent-2:#a06cff;
  --plc-good:#38d39f;
  --plc-warn:#ffb454;
  --plc-bad:#ff6b6b;
  --plc-r:16px;
  color:var(--plc-txt);
  display:flex; flex-direction:column; gap:16px;
  font-variant-numeric:tabular-nums;
}
#plcRoot *{ box-sizing:border-box; }
/* Several elements below set an explicit display, which outranks the UA rule
   for [hidden] and leaves them on screen. This puts hidden back in charge. */
#plcRoot [hidden]{ display:none !important; }

/* ---------- hero ---------- */
.plc-hero{
  position:relative; overflow:hidden; border-radius:22px;
  border:1px solid var(--plc-line); background:var(--plc-bg);
  padding:22px; display:grid; gap:20px;
  grid-template-columns:auto 1fr; align-items:center;
}
.plc-hero-bg{
  position:absolute; inset:-20%; z-index:0;
  background-size:cover; background-position:center;
  filter:blur(46px) saturate(180%); opacity:.34; transform:scale(1.1);
  transition:background-image .45s ease, opacity .45s ease;
}
.plc-hero-veil{
  position:absolute; inset:0; z-index:0;
  background:linear-gradient(115deg, rgba(8,10,20,.86) 0%, rgba(8,10,20,.55) 55%, rgba(8,10,20,.80) 100%);
}
.plc-hero > *{ position:relative; z-index:1; }

.plc-art-wrap{ position:relative; flex:0 0 auto; }
.plc-art{
  width:148px; height:148px; border-radius:18px; object-fit:cover; display:block;
  box-shadow:0 18px 44px rgba(0,0,0,.6); background:rgba(255,255,255,.05);
}
.plc-art.ph{ display:grid; place-items:center; font-size:2.6rem; color:rgba(255,255,255,.18); }
.plc-art-wrap.spin .plc-art{ animation:plcFloat 6s ease-in-out infinite; }
@keyframes plcFloat{ 0%,100%{ transform:translateY(0); } 50%{ transform:translateY(-6px); } }

.plc-info{ min-width:0; }
.plc-eyebrow{
  display:flex; align-items:center; gap:9px; flex-wrap:wrap;
  font-size:.68rem; letter-spacing:.12em; text-transform:uppercase;
  font-weight:800; color:var(--plc-dimmer); margin:0 0 8px;
}
.plc-live{ display:inline-flex; align-items:center; gap:6px; }
.plc-dot{
  width:8px; height:8px; border-radius:50%; background:var(--plc-good);
  box-shadow:0 0 0 0 rgba(56,211,159,.6); animation:plcPulse 2.2s infinite;
}
.plc-dot.stale{ background:var(--plc-warn); animation:none; }
.plc-dot.dead{ background:var(--plc-bad); animation:none; }
@keyframes plcPulse{
  0%{ box-shadow:0 0 0 0 rgba(56,211,159,.55); }
  70%{ box-shadow:0 0 0 7px rgba(56,211,159,0); }
  100%{ box-shadow:0 0 0 0 rgba(56,211,159,0); }
}
.plc-track-title{
  font-size:1.5rem; font-weight:800; line-height:1.2; margin:0 0 4px;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
}
.plc-track-title a{ color:inherit; text-decoration:none; }
.plc-track-title a:hover{ text-decoration:underline; }
.plc-track-author{
  margin:0; color:var(--plc-dim); font-size:.95rem;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
}
.plc-chips{ display:flex; gap:6px; flex-wrap:wrap; margin-top:12px; }
.plc-chip{
  display:inline-flex; align-items:center; gap:6px;
  font-size:.7rem; font-weight:700; letter-spacing:.02em;
  padding:4px 10px; border-radius:999px;
  background:rgba(255,255,255,.07); border:1px solid var(--plc-line);
  color:var(--plc-dim);
}
.plc-chip i{ opacity:.8; }
.plc-chip.live{ background:rgba(255,107,107,.18); border-color:rgba(255,107,107,.45); color:#ffb3b3; }
.plc-chip.on{ background:rgba(108,140,255,.20); border-color:rgba(108,140,255,.50); color:#c3d0ff; }

/* ---------- seek ---------- */
.plc-seek{ margin-top:16px; }
.plc-bar{
  position:relative; height:22px; cursor:pointer; touch-action:none;
  display:flex; align-items:center;
}
.plc-bar-track{
  position:relative; width:100%; height:6px; border-radius:999px;
  background:rgba(255,255,255,.13); overflow:hidden;
}
.plc-bar-fill{
  position:absolute; inset:0 auto 0 0; width:0%;
  background:linear-gradient(90deg,var(--plc-accent),var(--plc-accent-2));
  border-radius:999px;
}
.plc-bar-buffer{ position:absolute; inset:0 auto 0 0; width:0%; background:rgba(255,255,255,.08); }
.plc-bar-knob{
  position:absolute; top:50%; left:0%; width:14px; height:14px; border-radius:50%;
  background:#fff; transform:translate(-50%,-50%) scale(.6); opacity:0;
  box-shadow:0 2px 10px rgba(0,0,0,.6); transition:opacity .15s, transform .15s;
  pointer-events:none;
}
.plc-bar:hover .plc-bar-knob, .plc-bar.dragging .plc-bar-knob{ opacity:1; transform:translate(-50%,-50%) scale(1); }
.plc-bar.disabled{ cursor:default; opacity:.55; }
.plc-times{ display:flex; justify-content:space-between; font-size:.76rem; color:var(--plc-dimmer); margin-top:2px; }
.plc-scrub{
  position:absolute; bottom:100%; transform:translateX(-50%); margin-bottom:6px;
  padding:2px 7px; border-radius:7px; font-size:.72rem; font-weight:700;
  background:#11142a; border:1px solid var(--plc-line-2); white-space:nowrap;
  opacity:0; transition:opacity .12s; pointer-events:none;
}
.plc-bar:hover .plc-scrub, .plc-bar.dragging .plc-scrub{ opacity:1; }

/* ---------- buttons ---------- */
.plc-btn{
  display:inline-flex; align-items:center; justify-content:center; gap:8px;
  position:relative; height:42px; min-width:42px; padding:0 14px;
  border-radius:12px; cursor:pointer; font:inherit; font-size:.85rem; font-weight:650;
  color:var(--plc-txt); background:rgba(255,255,255,.06);
  border:1px solid var(--plc-line); text-decoration:none;
  transition:background .14s, border-color .14s, transform .08s, color .14s;
  -webkit-appearance:none; appearance:none;
}
.plc-btn:hover:not(:disabled){ background:rgba(255,255,255,.13); border-color:var(--plc-line-2); }
.plc-btn:active:not(:disabled){ transform:translateY(1px); }
.plc-btn:disabled{ opacity:.35; cursor:not-allowed; }
.plc-btn:focus-visible{ outline:2px solid var(--plc-accent); outline-offset:2px; }
.plc-btn.icon{ padding:0; width:42px; border-radius:50%; }
.plc-btn.sm{ height:34px; min-width:34px; font-size:.78rem; padding:0 10px; }
.plc-btn.sm.icon{ width:34px; padding:0; }
.plc-btn.primary{
  background:linear-gradient(135deg,var(--plc-accent),var(--plc-accent-2));
  border-color:transparent; color:#fff;
}
.plc-btn.primary:hover:not(:disabled){ filter:brightness(1.12); background:linear-gradient(135deg,var(--plc-accent),var(--plc-accent-2)); }
.plc-btn.play{ width:58px; height:58px; border-radius:50%; padding:0; font-size:1.2rem; }
.plc-btn.on{ background:rgba(108,140,255,.22); border-color:rgba(108,140,255,.55); color:#cdd8ff; }
.plc-btn.danger{ color:#ff9d9d; border-color:rgba(255,107,107,.35); }
.plc-btn.danger:hover:not(:disabled){ background:rgba(255,107,107,.16); border-color:rgba(255,107,107,.55); }
.plc-btn .plc-cost{
  position:absolute; top:-6px; right:-6px; min-width:18px; height:18px; padding:0 5px;
  border-radius:999px; font-size:.6rem; font-weight:800; line-height:18px;
  background:var(--plc-accent); color:#fff; border:1px solid rgba(255,255,255,.3);
}
.plc-btn.wide .plc-cost{ position:static; margin-left:2px; border:none; }

.plc-transport{ display:flex; align-items:center; gap:9px; flex-wrap:wrap; }
.plc-transport .plc-sep{ width:1px; height:26px; background:var(--plc-line); margin:0 3px; }
.plc-transport-grp{ display:flex; align-items:center; gap:9px; }

/* ---------- volume ---------- */
.plc-vol{ display:flex; align-items:center; gap:10px; min-width:190px; }
.plc-vol input[type=range]{
  -webkit-appearance:none; appearance:none; height:6px; border-radius:999px;
  background:rgba(255,255,255,.13); flex:1 1 auto; min-width:80px; cursor:pointer; outline:none;
}
.plc-vol input[type=range]::-webkit-slider-thumb{
  -webkit-appearance:none; width:14px; height:14px; border-radius:50%;
  background:#fff; box-shadow:0 2px 8px rgba(0,0,0,.5); cursor:pointer;
}
.plc-vol input[type=range]::-moz-range-thumb{
  width:14px; height:14px; border:none; border-radius:50%;
  background:#fff; box-shadow:0 2px 8px rgba(0,0,0,.5); cursor:pointer;
}
.plc-vol-num{ font-size:.78rem; color:var(--plc-dim); min-width:38px; text-align:right; }

/* ---------- layout ---------- */
.plc-bar-row{
  display:flex; gap:14px; align-items:center; flex-wrap:wrap;
  padding:14px 18px; border-radius:var(--plc-r);
  background:var(--plc-bg); border:1px solid var(--plc-line);
}
/* minmax(0,…) rather than a bare 1fr: a grid track sized `1fr` still refuses
   to go below its content's min-content width, and one un-wrappable track
   title is enough to push the whole column past the viewport. Same reason for
   the min-width:0 on the nested column and the cards. */
.plc-grid{ display:grid; gap:16px; grid-template-columns:minmax(0,1fr); }
@media (min-width:1080px){
  .plc-grid{ grid-template-columns:minmax(0,1.25fr) minmax(0,.95fr); align-items:start; }
}
.plc-grid > *{ min-width:0; }
.plc-card{
  border-radius:var(--plc-r); border:1px solid var(--plc-line);
  background:var(--plc-bg); overflow:hidden; min-width:0;
}
.plc-card-head{
  display:flex; align-items:center; gap:10px; flex-wrap:wrap;
  padding:14px 18px; border-bottom:1px solid var(--plc-line);
}
.plc-card-head h5{ margin:0; font-size:.92rem; font-weight:750; display:flex;
                   align-items:center; gap:8px; min-width:0; }
.plc-card-head .plc-spacer{ margin-left:auto; }
.plc-card-body{ padding:14px 18px; }
.plc-card-body.flush{ padding:0; }
.plc-count{
  font-size:.68rem; font-weight:800; padding:2px 8px; border-radius:999px;
  background:rgba(255,255,255,.08); border:1px solid var(--plc-line); color:var(--plc-dim);
}
.plc-sub{ font-size:.78rem; color:var(--plc-dimmer); margin:0; }

/* ---------- search ---------- */
.plc-searchbar{ display:flex; gap:8px; flex-wrap:wrap; }
.plc-field{ position:relative; flex:1 1 240px; min-width:0; }
.plc-field > i{ position:absolute; left:13px; top:50%; transform:translateY(-50%); color:var(--plc-dimmer); pointer-events:none; }
.plc-input{
  width:100%; height:44px; padding:0 40px 0 36px; border-radius:12px;
  background:rgba(0,0,0,.32); border:1px solid var(--plc-line);
  color:var(--plc-txt); font:inherit; font-size:.9rem; outline:none;
  transition:border-color .14s, background .14s;
}
.plc-input::placeholder{ color:var(--plc-dimmer); }
.plc-input:focus{ border-color:rgba(108,140,255,.55); background:rgba(0,0,0,.45); }
.plc-clear{
  position:absolute; right:8px; top:50%; transform:translateY(-50%);
  width:26px; height:26px; border-radius:50%; border:none; cursor:pointer;
  background:rgba(255,255,255,.10); color:var(--plc-dim); display:none; place-items:center;
}
.plc-field.filled .plc-clear{ display:grid; }
.plc-sources{ display:flex; gap:6px; flex-wrap:wrap; margin-top:10px; }
.plc-src{
  font-size:.72rem; font-weight:700; padding:5px 11px; border-radius:999px; cursor:pointer;
  background:rgba(255,255,255,.05); border:1px solid var(--plc-line); color:var(--plc-dim);
  display:inline-flex; align-items:center; gap:6px; transition:all .14s;
}
.plc-src:hover{ background:rgba(255,255,255,.11); color:var(--plc-txt); }
.plc-src.sel{ background:rgba(108,140,255,.2); border-color:rgba(108,140,255,.55); color:#cdd8ff; }

/* ---------- track lists ---------- */
.plc-list{ list-style:none; margin:0; padding:0; }
.plc-item{
  display:flex; align-items:center; gap:12px; padding:9px 18px; min-width:0;
  border-bottom:1px solid rgba(255,255,255,.05); transition:background .12s;
}
.plc-item:last-child{ border-bottom:none; }
.plc-item:hover{ background:rgba(255,255,255,.045); }
.plc-item.now{ background:linear-gradient(90deg, rgba(108,140,255,.16), transparent 70%); }
.plc-idx{ width:22px; text-align:right; font-size:.78rem; color:var(--plc-dimmer); flex:0 0 auto; }
.plc-thumb{
  width:42px; height:42px; border-radius:9px; object-fit:cover; flex:0 0 auto;
  background:rgba(255,255,255,.06);
}
.plc-thumb.ph{ display:grid; place-items:center; color:rgba(255,255,255,.2); }
.plc-item-main{ min-width:0; flex:1 1 auto; }
.plc-item-t{
  font-size:.87rem; font-weight:600; margin:0;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
}
.plc-item-t a{ color:inherit; text-decoration:none; }
.plc-item-t a:hover{ text-decoration:underline; }
.plc-item-s{
  font-size:.76rem; color:var(--plc-dimmer); margin:1px 0 0;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
}
.plc-item-len{ font-size:.76rem; color:var(--plc-dimmer); flex:0 0 auto; }
.plc-item-acts{ display:flex; gap:5px; flex:0 0 auto; opacity:.35; transition:opacity .14s; }
.plc-item:hover .plc-item-acts, .plc-item:focus-within .plc-item-acts{ opacity:1; }
@media (hover:none){ .plc-item-acts{ opacity:1; } }
.plc-scroll{ max-height:min(58vh,520px); overflow-y:auto; overflow-x:hidden;
             overscroll-behavior:contain; min-width:0; }
.plc-scroll::-webkit-scrollbar{ width:9px; }
.plc-scroll::-webkit-scrollbar-thumb{ background:rgba(255,255,255,.13); border-radius:9px; }

.plc-empty{
  padding:34px 18px; text-align:center; color:var(--plc-dimmer); font-size:.85rem;
}
.plc-empty i{ display:block; font-size:1.9rem; opacity:.35; margin-bottom:9px; }

/* ---------- listeners ---------- */
.plc-faces{ display:flex; align-items:center; }
.plc-face{
  width:26px; height:26px; border-radius:50%; object-fit:cover;
  border:2px solid #12162a; margin-left:-8px; background:#2a2f4a;
}
.plc-face:first-child{ margin-left:0; }
.plc-face.more{
  display:grid; place-items:center; font-size:.62rem; font-weight:800; color:var(--plc-dim);
}

/* ---------- server bar ---------- */
.plc-serverbar{
  display:flex; align-items:center; gap:14px; flex-wrap:wrap;
  padding:10px 14px; border-radius:var(--plc-r);
  background:var(--plc-bg); border:1px solid var(--plc-line);
}
.plc-switch{ position:relative; flex:0 0 auto; }
.plc-server{
  display:flex; align-items:center; gap:10px; cursor:pointer;
  height:44px; padding:0 13px; border-radius:12px; font:inherit; font-weight:700;
  color:var(--plc-txt); background:rgba(255,255,255,.05);
  border:1px solid var(--plc-line); max-width:min(340px, 60vw);
  transition:background .14s, border-color .14s;
}
.plc-server:hover{ background:rgba(255,255,255,.12); border-color:var(--plc-line-2); }
.plc-server[aria-expanded="true"]{ background:rgba(108,140,255,.18); border-color:rgba(108,140,255,.5); }
.plc-server-icon, .plc-server-fallback{
  width:26px; height:26px; border-radius:8px; object-fit:cover; flex:0 0 auto;
  background:rgba(255,255,255,.09);
}
.plc-server-fallback{
  display:grid; place-items:center; font-size:.8rem; font-weight:800; color:var(--plc-dim);
}
.plc-server-name{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:.92rem; }
.plc-server i{ opacity:.55; flex:0 0 auto; }
.plc-server-menu{
  position:absolute; top:calc(100% + 6px); left:0; z-index:60;
  width:max(280px, 100%); max-width:min(380px, 90vw); padding:9px;
  border-radius:14px; background:#12162a; border:1px solid var(--plc-line-2);
  box-shadow:0 18px 44px rgba(0,0,0,.6);
}
.plc-server-search{
  width:100%; height:36px; padding:0 11px; margin-bottom:7px; border-radius:9px;
  background:rgba(0,0,0,.34); border:1px solid var(--plc-line);
  color:var(--plc-txt); font:inherit; font-size:.85rem; outline:none;
}
.plc-server-search:focus{ border-color:rgba(108,140,255,.55); }
.plc-server-list{ list-style:none; margin:0; padding:0; max-height:min(46vh,320px); overflow-y:auto; }
.plc-server-list li{ margin:0; }
.plc-server-opt{
  display:flex; align-items:center; gap:9px; width:100%; padding:7px 9px;
  border:none; border-radius:9px; background:transparent; cursor:pointer;
  color:var(--plc-txt); font:inherit; font-size:.85rem; text-align:left;
}
.plc-server-opt:hover{ background:rgba(255,255,255,.08); }
.plc-server-opt.here{ background:rgba(108,140,255,.18); font-weight:700; }
.plc-server-opt img, .plc-server-opt .ph{
  width:22px; height:22px; border-radius:7px; object-fit:cover; flex:0 0 auto;
  background:rgba(255,255,255,.09); display:grid; place-items:center;
  font-size:.66rem; font-weight:800; color:var(--plc-dim);
}
.plc-server-opt span{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

.plc-stats{ display:flex; align-items:center; gap:7px; flex-wrap:wrap; }
.plc-stat{
  display:inline-flex; align-items:baseline; gap:6px;
  padding:6px 11px; border-radius:10px;
  background:rgba(255,255,255,.05); border:1px solid var(--plc-line);
}
.plc-stat b{ font-size:.92rem; font-weight:750; }
.plc-stat span{ font-size:.68rem; text-transform:uppercase; letter-spacing:.05em; color:var(--plc-dimmer); }
.plc-stat.live b{ color:var(--plc-good); }

/* margin-left:auto parks the balance on the right of the bar. Once the bar
   wraps, that auto margin is still pushing it right on a line of its own,
   which reads as a stray floating box - so below the wrap point it goes. */
@media (max-width:900px){ .plc-balance{ margin-left:0 !important; } }
.plc-balance{
  display:flex; align-items:center; gap:9px; flex-wrap:wrap; margin-left:auto;
  padding:6px 13px; border-radius:12px;
  background:linear-gradient(90deg, rgba(108,140,255,.16), rgba(160,108,255,.06));
  border:1px solid rgba(108,140,255,.3);
}
.plc-balance > i{ color:#8ea6ff; }
.plc-balance b{ font-size:1rem; }
.plc-prices{ display:flex; gap:6px; flex-wrap:wrap; }
.plc-price{
  font-size:.68rem; padding:3px 9px; border-radius:999px; color:var(--plc-dim);
  background:rgba(255,255,255,.05); border:1px solid var(--plc-line);
}
.plc-price b{ color:#b7c6ff; font-size:.68rem; }

/* ---------- misc ---------- */
.plc-skeleton{ animation:plcSk 1.3s ease-in-out infinite; }
@keyframes plcSk{ 0%,100%{ opacity:.4; } 50%{ opacity:.75; } }
.plc-kbd{
  font-size:.66rem; padding:1px 5px; border-radius:5px; font-weight:700;
  background:rgba(255,255,255,.09); border:1px solid var(--plc-line-2); color:var(--plc-dim);
}
.plc-offline{
  display:none; align-items:center; gap:9px; padding:10px 16px; border-radius:12px;
  background:rgba(255,180,84,.12); border:1px solid rgba(255,180,84,.35);
  color:#ffd9a3; font-size:.83rem;
}
#plcRoot.stale .plc-offline{ display:flex; }

@media (max-width:640px){
  .plc-hero{ grid-template-columns:1fr; justify-items:center; text-align:center; padding:18px; }
  .plc-art{ width:120px; height:120px; }
  .plc-track-title{ font-size:1.2rem; white-space:normal; }
  .plc-eyebrow, .plc-chips{ justify-content:center; }
  .plc-transport{ justify-content:center; }
  .plc-item-len{ display:none; }
  /* The groups wrap onto their own rows here, so the dividers between them
     end up dangling at the end of a line instead of separating anything. */
  .plc-transport .plc-sep{ display:none; }
  .plc-bar-row{ justify-content:center; }
  .plc-vol{ margin-left:0 !important; width:100%; }
}
</style>
<div id="plcRoot"
     data-csrf="{{ csrf_token_value }}"
     data-staff="{% if is_staff %}1{% else %}0{% endif %}">

  <script type="application/json" id="plc-boot">{{ boot|safe }}</script>

  <div class="plc-offline">
    <i class="fa fa-plug"></i>
    <span>Lost contact with the bot &mdash; showing the last known state. Retrying&hellip;</span>
  </div>

  <!-- Server identity, a few stats, and the balance. This is what replaces
       the dashboard's own guild banner on this page. -->
  <div class="plc-serverbar">
    <div class="plc-switch">
      <button class="plc-server" id="plcServerBtn" type="button"
              aria-haspopup="listbox" aria-expanded="false">
        <img class="plc-server-icon" id="plcServerIcon" alt="" hidden />
        <span class="plc-server-fallback" id="plcServerFallback">?</span>
        <span class="plc-server-name" id="plcServerName">&mdash;</span>
        <i class="fa fa-angle-down"></i>
      </button>
      <div class="plc-server-menu" id="plcServerMenu" hidden role="listbox">
        <input class="plc-server-search" id="plcServerSearch" type="text"
               autocomplete="off" placeholder="Find a server&hellip;" />
        <ul class="plc-server-list" id="plcServerList"></ul>
      </div>
    </div>

    <div class="plc-stats" id="plcStats"></div>

    <div class="plc-balance" id="plcWallet" hidden>
      <i class="fa fa-diamond"></i>
      <span><b id="plcBal">0</b> <span id="plcCur">credits</span></span>
      <span class="plc-sub" id="plcWalletNote"></span>
      <div class="plc-prices" id="plcPrices"></div>
    </div>
  </div>

  <!-- ============ NOW PLAYING ============ -->
  <div class="plc-hero">
    <div class="plc-hero-bg" id="plcHeroBg"></div>
    <div class="plc-hero-veil"></div>

    <div class="plc-art-wrap" id="plcArtWrap">
      <img class="plc-art" id="plcArt" alt="" hidden />
      <div class="plc-art ph" id="plcArtPh"><i class="fa fa-music"></i></div>
    </div>

    <div class="plc-info">
      <p class="plc-eyebrow">
        <span class="plc-live"><span class="plc-dot" id="plcDot"></span><span id="plcStatus">Now playing</span></span>
        <span id="plcVoice"></span>
      </p>
      <h3 class="plc-track-title" id="plcTitle">&mdash;</h3>
      <p class="plc-track-author" id="plcAuthor"></p>
      <div class="plc-chips" id="plcChips"></div>

      <div class="plc-seek">
        <div class="plc-bar" id="plcSeek" role="slider" tabindex="0"
             aria-label="Seek" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
          <div class="plc-scrub" id="plcScrub">0:00</div>
          <div class="plc-bar-track">
            <div class="plc-bar-buffer"></div>
            <div class="plc-bar-fill" id="plcFill"></div>
          </div>
          <div class="plc-bar-knob" id="plcKnob"></div>
        </div>
        <div class="plc-times">
          <span id="plcPos">0:00</span>
          <span id="plcDur">0:00</span>
        </div>
      </div>
    </div>
  </div>

  <!-- ============ TRANSPORT ============ -->
  <div class="plc-bar-row">
    <div class="plc-transport">
      <div class="plc-transport-grp">
        <button class="plc-btn icon" data-act="previous" title="Previous (Shift+&larr;)" aria-label="Previous">
          <i class="fa fa-step-backward"></i></button>
        <button class="plc-btn primary play" data-act="pause" id="plcPlay" title="Play / pause (Space)" aria-label="Play or pause">
          <i class="fa fa-pause" id="plcPlayIcon"></i></button>
        <button class="plc-btn icon" data-act="skip" title="Skip (Shift+&rarr;)" aria-label="Skip">
          <i class="fa fa-step-forward"></i></button>
      </div>

      <span class="plc-sep"></span>

      <div class="plc-transport-grp">
        <button class="plc-btn icon" data-act="shuffle" id="plcShuffle" title="Shuffle the queue" aria-label="Shuffle">
          <i class="fa fa-random"></i></button>
        <button class="plc-btn" data-act="repeat_cycle" id="plcRepeat" title="Repeat: off / track / queue">
          <i class="fa fa-repeat"></i> <span id="plcRepeatLabel">Off</span></button>
        <button class="plc-btn icon" data-act="autoplay" id="plcAutoplay" title="Autoplay similar tracks when the queue runs dry" aria-label="Autoplay">
          <i class="fa fa-magic"></i></button>
        <button class="plc-btn icon" data-act="fav_add" id="plcFav" title="Save this track to the guild favourites" aria-label="Favourite">
          <i class="fa fa-star-o"></i></button>
      </div>
    </div>

    <div class="plc-vol" style="margin-left:auto;">
      <button class="plc-btn icon sm" data-act="mute" id="plcMute" title="Mute / unmute (M)" aria-label="Mute">
        <i class="fa fa-volume-up" id="plcMuteIcon"></i></button>
      <input type="range" id="plcVol" min="0" max="150" step="1" value="100" aria-label="Volume" />
      <span class="plc-vol-num" id="plcVolNum">100%</span>
    </div>

    <div class="plc-transport-grp" id="plcStaffBar" hidden>
      <span class="plc-sep"></span>
      <button class="plc-btn icon danger" data-act="stop" title="Stop and clear" aria-label="Stop">
        <i class="fa fa-stop"></i></button>
      <button class="plc-btn icon danger" data-act="disconnect" title="Disconnect the bot" aria-label="Disconnect">
        <i class="fa fa-sign-out"></i></button>
    </div>
  </div>

  <!-- ============ SEARCH + QUEUE ============ -->
  <div class="plc-grid">

    <div class="plc-card">
      <div class="plc-card-head">
        <h5><i class="fa fa-search"></i> Find something to play</h5>
        <span class="plc-spacer"></span>
        <span class="plc-sub" id="plcSearchMeta"></span>
      </div>
      <div class="plc-card-body">
        <div class="plc-searchbar">
          <label class="plc-field" id="plcSearchField">
            <i class="fa fa-search"></i>
            <!-- type=text, not search: a search input draws its own clear
                 button on top of ours, which reads as two X's in a row. -->
            <input class="plc-input" id="plcQuery" type="text" autocomplete="off"
                   placeholder="Song, artist, playlist or any link&hellip;" />
            <button class="plc-clear" id="plcQueryClear" type="button" aria-label="Clear"><i class="fa fa-times"></i></button>
          </label>
          <button class="plc-btn primary wide" id="plcSearchGo">
            <i class="fa fa-search"></i> Search <span class="plc-cost" data-cost="search" hidden></span></button>
        </div>
        <div class="plc-sources" id="plcSources"></div>
        <p class="plc-sub" style="margin-top:9px;">
          Paste any link &mdash; YouTube, Spotify, a radio stream, a direct <code>.mp3</code> &mdash; and press
          <span class="plc-kbd">Enter</span> to queue it straight away.
        </p>
      </div>
      <div class="plc-card-body flush">
        <div class="plc-scroll"><ul class="plc-list" id="plcResults"></ul></div>
      </div>
    </div>

    <div style="display:flex; flex-direction:column; gap:16px; min-width:0;">
      <div class="plc-card">
        <div class="plc-card-head">
          <h5><i class="fa fa-list-ol"></i> Queue</h5>
          <span class="plc-count" id="plcQueueCount">0</span>
          <span class="plc-spacer"></span>
          <span class="plc-sub" id="plcQueueTime"></span>
          <button class="plc-btn sm danger" data-act="clear_queue" id="plcClearQueue" title="Empty the queue" hidden>
            <i class="fa fa-trash-o"></i></button>
        </div>
        <div class="plc-card-body flush">
          <div class="plc-scroll"><ul class="plc-list" id="plcQueue"></ul></div>
        </div>
      </div>

      <div class="plc-card">
        <div class="plc-card-head">
          <h5><i class="fa fa-star"></i> Server favourites</h5>
          <span class="plc-count" id="plcFavCount">0</span>
          <span class="plc-spacer"></span>
          <button class="plc-btn sm" data-act="fav_queue" title="Queue every favourite"><i class="fa fa-plus"></i> Queue all</button>
          <button class="plc-btn sm primary" data-act="fav_play" title="Play the favourites now"><i class="fa fa-play"></i></button>
          <button class="plc-btn sm danger" data-act="fav_clear" id="plcFavClear" title="Remove every favourite" hidden>
            <i class="fa fa-trash-o"></i></button>
        </div>
        <div class="plc-card-body flush">
          <div class="plc-scroll" style="max-height:min(38vh,340px);"><ul class="plc-list" id="plcFavs"></ul></div>
        </div>
      </div>
    </div>
  </div>

  <p class="plc-sub" style="text-align:center;">
    <span class="plc-kbd">Space</span> play/pause &nbsp;
    <span class="plc-kbd">&larr;</span><span class="plc-kbd">&rarr;</span> seek 10s &nbsp;
    <span class="plc-kbd">Shift</span>+<span class="plc-kbd">&larr;</span><span class="plc-kbd">&rarr;</span> track &nbsp;
    <span class="plc-kbd">&uarr;</span><span class="plc-kbd">&darr;</span> volume &nbsp;
    <span class="plc-kbd">M</span> mute &nbsp;
    <span class="plc-kbd">/</span> search
    <span id="plcRoleNote"></span>
  </p>

</div>

<noscript>
  <div style="padding:16px; border-radius:14px; border:1px solid rgba(255,255,255,.14); margin-top:14px;">
    <p><b>JavaScript is off</b>, so the live player cannot run. These basic controls still work:</p>
    <form method="POST" style="display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <button class="plc-btn" name="action" value="resume"><i class="fa fa-play"></i> Play</button>
      <button class="plc-btn" name="action" value="pause"><i class="fa fa-pause"></i> Pause</button>
      <button class="plc-btn" name="action" value="skip"><i class="fa fa-step-forward"></i> Skip</button>
      <button class="plc-btn" name="action" value="shuffle"><i class="fa fa-random"></i> Shuffle</button>
      {% if is_staff %}
        <button class="plc-btn danger" name="action" value="stop"><i class="fa fa-stop"></i> Stop</button>
        <button class="plc-btn danger" name="action" value="disconnect"><i class="fa fa-sign-out"></i> Disconnect</button>
      {% endif %}
    </form>
    <form method="POST" style="display:flex; gap:8px; flex-wrap:wrap; margin-top:10px;">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <input class="plc-input" style="padding-left:13px;" type="text" name="query"
             placeholder="Song, artist or link" value="{{ search_term or '' }}" />
      <button class="plc-btn" name="action" value="search"><i class="fa fa-search"></i> Search</button>
      <button class="plc-btn" name="action" value="play"><i class="fa fa-plus"></i> Queue it</button>
    </form>
    {% if search_results %}
      <ul style="margin-top:12px;">
        {% for r in search_results %}
          <li style="margin-bottom:6px;">
            {{ r.title }} &mdash; {{ r.author }} ({{ r.duration }})
            <form method="POST" style="display:inline;">
              <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
              <input type="hidden" name="identifier" value="{{ r.identifier }}" />
              <button class="plc-btn sm" name="action" value="play">Queue</button>
            </form>
          </li>
        {% endfor %}
      </ul>
    {% endif %}
    {% if player_state.current %}
      <p style="margin-top:12px;">Now playing: <b>{{ player_state.current.title }}</b>
        &mdash; {{ player_state.current.author }} ({{ player_state.position }} / {{ player_state.current.duration }})</p>
    {% else %}
      <p style="margin-top:12px;">Nothing is playing.</p>
    {% endif %}
  </div>
</noscript>
{% raw %}
<script>
(function () {
  "use strict";

  var ROOT = document.getElementById("plcRoot");
  if (!ROOT || ROOT.dataset.wired) return;
  ROOT.dataset.wired = "1";

  var CSRF = ROOT.dataset.csrf || "";
  var ENDPOINT = window.location.pathname;
  var $ = function (id) { return document.getElementById(id); };

  // ---------------------------------------------------------------- state
  var S = {};                 // last server frame
  var isStaff = ROOT.dataset.staff === "1";
  var sources = [];
  var results = [];
  var searchTerm = "";
  var searchSource = "ytsearch";
  // The playhead is extrapolated between polls; `anchor` is the last point we
  // actually heard from the bot, so the clock never drifts further than one
  // poll interval no matter how long the page stays open.
  var anchor = { pos: 0, at: 0, running: false, duration: 0 };
  var dragging = false;
  var volumeHeld = false;     // user has the slider grabbed - do not overwrite
  var pendingPoll = null;
  var failures = 0;
  var lastKey = null;

  try { S = JSON.parse($("plc-boot").textContent) || {}; } catch (e) { S = {}; }
  sources = S.search_sources || [];
  searchSource = S.search_source || "ytsearch";

  // ---------------------------------------------------------------- utils
  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function fmt(ms) {
    if (!ms || ms < 0) ms = 0;
    var t = Math.floor(ms / 1000), h = Math.floor(t / 3600),
        m = Math.floor((t % 3600) / 60), s = t % 60;
    var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
    return h ? h + ":" + pad(m) + ":" + pad(s) : m + ":" + pad(s);
  }
  function longFmt(ms) {
    var t = Math.floor((ms || 0) / 1000), h = Math.floor(t / 3600), m = Math.round((t % 3600) / 60);
    if (h && m) return h + "h " + m + "m";
    if (h) return h + "h";
    return m + "m";
  }
  function show(el, on) { if (el) el.hidden = !on; }

  // ---------------------------------------------------------------- toasts
  // One stack for the whole dashboard, top-right, defined in the shared
  // helpers. Falling back to the console rather than defining a second
  // implementation keeps every page's notifications looking the same.
  function toast(message, category) {
    if (!message) return;
    if (window.dzToast) window.dzToast(message, category || "success");
    else console.log("[" + (category || "info") + "] " + message);
  }

  // ---------------------------------------------------------------- transport
  // Every exchange with the bot goes through the page's own route, so the
  // session cookie and the CSRF token that the server already issued are the
  // only credentials involved. The reply is a full HTML document with our
  // payload buried in it; DOMParser digs it back out.
  function extract(html) {
    var doc = new DOMParser().parseFromString(html, "text/html");
    var node = doc.getElementById("plc-api-payload");
    if (!node) throw new Error("no payload in response");
    return JSON.parse(node.textContent);
  }

  function pull() {
    return fetch(ENDPOINT + "?plc_api=1&_=" + Date.now(), {
      credentials: "same-origin",
      headers: { "X-Requested-With": "XMLHttpRequest" }
    }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.text();
    }).then(extract);
  }

  function act(action, extra) {
    var fd = new FormData();
    fd.append("csrf_token", CSRF);
    fd.append("plc_api", "1");
    fd.append("action", action);
    if (extra) for (var k in extra) if (extra.hasOwnProperty(k)) fd.append(k, extra[k]);
    return fetch(ENDPOINT, { method: "POST", body: fd, credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      })
      .then(extract)
      .then(function (data) {
        (data.notifications || []).forEach(function (n) { toast(n.message, n.category); });
        apply(data);
        return data;
      })
      .catch(function (err) {
        toast("Could not reach the bot: " + err.message, "danger");
        throw err;
      });
  }

  // ---------------------------------------------------------------- polling
  var POLL_ACTIVE = 2000;     // something is playing and the tab is visible
  var POLL_IDLE = 6000;       // nothing playing - no need to be eager
  var POLL_HIDDEN = 20000;    // tab in the background

  function pollDelay() {
    if (document.hidden) return POLL_HIDDEN;
    if (failures) return Math.min(POLL_ACTIVE * Math.pow(2, failures), 30000);
    return (S.state && S.state.playing && !S.state.paused) ? POLL_ACTIVE : POLL_IDLE;
  }

  function schedule(delay) {
    clearTimeout(pendingPoll);
    pendingPoll = setTimeout(loop, delay == null ? pollDelay() : delay);
  }

  function loop() {
    pull().then(function (data) {
      failures = 0;
      ROOT.classList.remove("stale");
      apply(data);
      schedule();
    }).catch(function () {
      failures = Math.min(failures + 1, 5);
      if (failures >= 2) ROOT.classList.add("stale");
      setDot();
      schedule();
    });
  }

  // A frame arriving out of order would make the playhead jump backwards, so
  // anything older than what we already have is dropped.
  function apply(data) {
    if (!data || !data.state) return;
    // The token is reissued on every frame, so a tab left open all evening
    // keeps working rather than failing on its first click after an expiry.
    if (data.csrf) CSRF = data.csrf;
    if (S.state && data.state.stamp && S.state.stamp && data.state.stamp < S.state.stamp) return;
    S.state = data.state;
    if (data.is_staff !== undefined) isStaff = !!data.is_staff;
    if (data.guild !== undefined) S.guild = data.guild;
    if (data.guilds !== undefined) S.guilds = data.guilds;
    if (data.favourites !== undefined) S.favourites = data.favourites;
    if (data.wallet !== undefined) S.wallet = data.wallet;
    if (data.economy !== undefined) S.economy = data.economy;
    if (data.search_results !== undefined) {
      results = data.search_results;
      searchTerm = data.search_term || "";
      if (data.search_source) searchSource = data.search_source;
      renderResults();
      renderSources();
    }
    reanchor();
    render();
  }

  function reanchor() {
    var st = S.state || {};
    var cur = st.current;
    anchor.duration = cur ? (cur.duration_ms || 0) : 0;
    anchor.pos = st.position_ms || 0;
    anchor.at = performance.now();
    // A stream has no length to run along, and a paused track does not move.
    anchor.running = !!(st.connected && st.playing && !st.paused && cur && !cur.stream && anchor.duration > 0);
  }

  function livePos() {
    if (!anchor.running) return anchor.pos;
    return Math.min(anchor.pos + (performance.now() - anchor.at), anchor.duration);
  }

  // ---------------------------------------------------------------- render
  var elFill = $("plcFill"), elKnob = $("plcKnob"), elPos = $("plcPos"),
      elDur = $("plcDur"), elSeek = $("plcSeek"), elScrub = $("plcScrub");

  function paintSeek() {
    var st = S.state || {}, cur = st.current;
    var seekable = !!(cur && !cur.stream && anchor.duration > 0);
    elSeek.classList.toggle("disabled", !seekable);
    if (dragging) return;
    var pos = livePos();
    var pct = seekable ? Math.max(0, Math.min(100, (pos / anchor.duration) * 100)) : 0;
    elFill.style.width = pct + "%";
    elKnob.style.left = pct + "%";
    elPos.textContent = cur ? fmt(pos) : "0:00";
    elDur.textContent = cur ? (cur.stream ? "LIVE" : fmt(anchor.duration)) : "0:00";
    elSeek.setAttribute("aria-valuenow", Math.round(pct));
  }

  function setDot() {
    var dot = $("plcDot"), label = $("plcStatus");
    var st = S.state || {};
    dot.className = "plc-dot" + (failures >= 4 ? " dead" : failures >= 2 ? " stale" : "");
    if (failures >= 2) { label.textContent = "Reconnecting"; return; }
    if (!st.connected) { label.textContent = "Not connected"; return; }
    if (!st.current) { label.textContent = "Idle"; return; }
    label.textContent = st.paused ? "Paused" : "Now playing";
  }

  function render() {
    var st = S.state || {}, cur = st.current || null;

    setDot();

    // ---- artwork, title, artist
    var art = $("plcArt"), ph = $("plcArtPh"), bg = $("plcHeroBg");
    if (cur && cur.artwork) {
      if (art.getAttribute("src") !== cur.artwork) {
        art.setAttribute("src", cur.artwork);
        bg.style.backgroundImage = 'url("' + cur.artwork.replace(/"/g, "%22") + '")';
      }
      art.hidden = false; ph.hidden = true;
    } else {
      art.removeAttribute("src");
      art.hidden = true; ph.hidden = false; bg.style.backgroundImage = "";
    }
    $("plcArtWrap").classList.toggle("spin", !!(cur && st.playing && !st.paused));

    var title = $("plcTitle");
    if (cur) {
      title.innerHTML = cur.uri
        ? '<a href="' + esc(cur.uri) + '" target="_blank" rel="noopener">' + esc(cur.title) + "</a>"
        : esc(cur.title);
      title.setAttribute("title", cur.title || "");
    } else {
      title.textContent = st.connected ? "Nothing playing" : "Not connected";
      title.removeAttribute("title");
    }
    $("plcAuthor").textContent = cur ? (cur.author || "") :
      (st.connected ? "Search below and it will start straight away."
                    : "Join a voice channel, then queue something — I will connect on my own.");

    // ---- chips
    var chips = [];
    if (cur && cur.stream) chips.push('<span class="plc-chip live"><i class="fa fa-dot-circle-o"></i> Live</span>');
    if (st.paused) chips.push('<span class="plc-chip"><i class="fa fa-pause"></i> Paused</span>');
    if (st.repeat === "track") chips.push('<span class="plc-chip on"><i class="fa fa-repeat"></i> Repeat track</span>');
    if (st.repeat === "queue") chips.push('<span class="plc-chip on"><i class="fa fa-repeat"></i> Repeat queue</span>');
    if (st.autoplay) chips.push('<span class="plc-chip on"><i class="fa fa-magic"></i> Autoplay</span>');
    if (cur && cur.requester) chips.push('<span class="plc-chip"><i class="fa fa-user"></i> ' + esc(cur.requester) + "</span>");
    if (cur && cur.source) chips.push('<span class="plc-chip"><i class="fa fa-cloud"></i> ' + esc(cur.source) + "</span>");
    $("plcChips").innerHTML = chips.join("");

    // ---- voice channel + who is listening
    var voice = $("plcVoice");
    if (st.connected && st.channel) {
      var people = st.listeners || [];
      var faces = people.slice(0, 5).map(function (p) {
        return p.avatar
          ? '<img class="plc-face" src="' + esc(p.avatar) + '" alt="" title="' + esc(p.name) + '" />'
          : '<span class="plc-face more" title="' + esc(p.name) + '">' + esc((p.name || "?").slice(0, 1)) + "</span>";
      }).join("");
      if (people.length > 5) faces += '<span class="plc-face more">+' + (people.length - 5) + "</span>";
      voice.innerHTML =
        '<span style="display:inline-flex;align-items:center;gap:8px;">' +
        '<i class="fa fa-volume-up"></i> ' + esc(st.channel) +
        (faces ? '<span class="plc-faces">' + faces + "</span>" : "") + "</span>";
    } else {
      voice.textContent = "";
    }

    // ---- transport state
    var playing = st.connected && st.playing && !st.paused;
    var playBtn = $("plcPlay");
    playBtn.dataset.act = playing ? "pause" : "resume";
    $("plcPlayIcon").className = "fa fa-" + (playing ? "pause" : "play");
    playBtn.setAttribute("aria-label", playing ? "Pause" : "Play");

    var repeatLabel = st.repeat === "track" ? "Track" : st.repeat === "queue" ? "Queue" : "Off";
    $("plcRepeatLabel").textContent = repeatLabel;
    $("plcRepeat").classList.toggle("on", st.repeat !== "off" && st.repeat !== undefined);
    $("plcAutoplay").classList.toggle("on", !!st.autoplay);

    var favIds = (S.favourites || []).map(function (f) { return f.identifier; });
    var isFav = !!(cur && cur.identifier && favIds.indexOf(cur.identifier) !== -1);
    $("plcFav").classList.toggle("on", isFav);
    $("plcFav").querySelector("i").className = "fa fa-star" + (isFav ? "" : "-o");

    // Nothing to act on means the button says so rather than failing on click.
    $("plcPlay").disabled = !st.connected;
    ROOT.querySelectorAll('[data-act="previous"]').forEach(function (b) { b.disabled = !st.history_length; });
    ROOT.querySelectorAll('[data-act="skip"]').forEach(function (b) { b.disabled = !cur; });
    $("plcShuffle").disabled = !st.queue_length;
    $("plcFav").disabled = !cur;
    $("plcRepeat").disabled = !st.connected;
    $("plcAutoplay").disabled = !st.connected;

    // ---- volume
    if (!volumeHeld) {
      var vol = st.connected ? st.volume : 0;
      $("plcVol").value = vol;
      $("plcVolNum").textContent = vol + "%";
      $("plcMuteIcon").className = "fa fa-volume-" + (vol === 0 ? "off" : vol < 50 ? "down" : "up");
    }

    // ---- staff-only controls
    // Repeat and autoplay are moderator-only on the server, exactly as they
    // are on the Discord controller. Members still see the *state* of both in
    // the chips above, they just cannot change it, so the buttons go rather
    // than sit there waiting to refuse.
    show($("plcStaffBar"), isStaff);
    show($("plcRepeat"), isStaff);
    show($("plcAutoplay"), isStaff);
    show($("plcClearQueue"), isStaff && st.queue_length > 0);
    show($("plcFavClear"), isStaff && (S.favourites || []).length > 0);
    $("plcRoleNote").innerHTML = isStaff
      ? ""
      : ' &nbsp;&middot;&nbsp; <i class="fa fa-info-circle"></i> Stopping and disconnecting are moderator-only.';

    renderServer();
    renderQueue();
    renderFavs();
    renderWallet();
    paintSeek();
  }

  // ---- queue -------------------------------------------------------------
  function renderQueue() {
    var st = S.state || {}, q = st.queue || [];
    $("plcQueueCount").textContent = st.queue_length || 0;
    $("plcQueueTime").textContent = q.length ? longFmt(st.queue_duration_ms) + " left" : "";
    var box = $("plcQueue");
    if (!q.length) {
      box.innerHTML = '<li class="plc-empty"><i class="fa fa-list-ol"></i>' +
        (st.connected ? "Nothing queued. Add something from the search." : "Not connected to a voice channel.") + "</li>";
      return;
    }
    var html = q.map(function (item, i) {
      // Reordering and removing are both moderator-only on the server, so the
      // buttons are not offered to anyone who would only be told no.
      var acts = isStaff
        ? '<button class="plc-btn sm icon" data-act="move_top" data-index="' + i +
          '" title="Play this next"><i class="fa fa-level-up"></i></button>' +
          '<button class="plc-btn sm icon danger" data-act="remove_track" data-index="' + i +
          '" title="Remove"><i class="fa fa-times"></i></button>'
        : "";
      return '<li class="plc-item">' +
        '<span class="plc-idx">' + (i + 1) + "</span>" +
        '<div class="plc-item-main">' +
          '<p class="plc-item-t">' + (item.uri
            ? '<a href="' + esc(item.uri) + '" target="_blank" rel="noopener">' + esc(item.title) + "</a>"
            : esc(item.title)) + "</p>" +
          '<p class="plc-item-s">' + esc(item.author || "") + "</p>" +
        "</div>" +
        '<span class="plc-item-len">' + esc(item.duration) + "</span>" +
        '<span class="plc-item-acts">' + acts + "</span>" +
        "</li>";
    }).join("");
    if (st.queue_truncated) {
      html += '<li class="plc-empty" style="padding:14px;">and ' + st.queue_truncated + " more not shown</li>";
    }
    box.innerHTML = html;
  }

  // ---- search ------------------------------------------------------------
  function renderSources() {
    var box = $("plcSources");
    if (!sources.length) { box.innerHTML = ""; return; }
    box.innerHTML = sources.map(function (s) {
      var key = s[0], label = s[1], icon = s[2];
      return '<button type="button" class="plc-src' + (key === searchSource ? " sel" : "") +
        '" data-source="' + esc(key) + '"><i class="fa ' + esc(icon) + '"></i>' + esc(label) + "</button>";
    }).join("");
  }

  function renderResults() {
    var box = $("plcResults");
    $("plcSearchMeta").textContent = results.length
      ? results.length + " result" + (results.length === 1 ? "" : "s") + " for “" + searchTerm + "”"
      : "";
    if (!results.length) {
      box.innerHTML = searchTerm
        ? '<li class="plc-empty"><i class="fa fa-search"></i>Nothing found for “' + esc(searchTerm) + "”.</li>"
        : '<li class="plc-empty"><i class="fa fa-music"></i>Search for a song, or paste a link.</li>';
      return;
    }
    box.innerHTML = results.map(function (r, i) {
      var thumb = r.artwork
        ? '<img class="plc-thumb" src="' + esc(r.artwork) + '" alt="" loading="lazy" />'
        : '<span class="plc-thumb ph"><i class="fa fa-music"></i></span>';
      return '<li class="plc-item">' +
        thumb +
        '<div class="plc-item-main">' +
          '<p class="plc-item-t">' + (r.uri
            ? '<a href="' + esc(r.uri) + '" target="_blank" rel="noopener">' + esc(r.title) + "</a>"
            : esc(r.title)) + "</p>" +
          '<p class="plc-item-s">' + esc(r.author || "") + (r.source ? " · " + esc(r.source) : "") + "</p>" +
        "</div>" +
        '<span class="plc-item-len">' + (r.stream ? "LIVE" : esc(r.duration)) + "</span>" +
        '<span class="plc-item-acts">' +
          '<button class="plc-btn sm icon" data-act="fav_add" data-result="' + i +
            '" title="Save to server favourites"><i class="fa fa-star-o"></i></button>' +
          '<button class="plc-btn sm icon" data-act="play" data-result="' + i +
            '" title="Add to the queue"><i class="fa fa-plus"></i></button>' +
          '<button class="plc-btn sm icon primary" data-act="play_now" data-result="' + i +
            '" title="Play it now"><i class="fa fa-play"></i></button>' +
        "</span>" +
        "</li>";
    }).join("");
  }

  // ---- favourites --------------------------------------------------------
  function renderFavs() {
    var favs = S.favourites || [];
    $("plcFavCount").textContent = favs.length;
    var box = $("plcFavs");
    if (!favs.length) {
      box.innerHTML = '<li class="plc-empty"><i class="fa fa-star-o"></i>No favourites yet. ' +
        "Star a track and it lands here for everyone.</li>";
      return;
    }
    box.innerHTML = favs.map(function (f, i) {
      return '<li class="plc-item">' +
        '<span class="plc-idx">' + (i + 1) + "</span>" +
        '<div class="plc-item-main"><p class="plc-item-t">' + esc(f.title) + "</p></div>" +
        '<span class="plc-item-acts">' +
          '<button class="plc-btn sm icon" data-act="play" data-fav="' + i +
            '" title="Add to the queue"><i class="fa fa-plus"></i></button>' +
          '<button class="plc-btn sm icon primary" data-act="play_now" data-fav="' + i +
            '" title="Play it now"><i class="fa fa-play"></i></button>' +
          (isStaff
            ? '<button class="plc-btn sm icon danger" data-act="fav_remove" data-fav="' + i +
              '" title="Remove"><i class="fa fa-times"></i></button>'
            : "") +
        "</span></li>";
    }).join("");
  }

  // ---- server bar --------------------------------------------------------
  function renderServer() {
    var g = S.guild || {};
    $("plcServerName").textContent = g.name || "This server";
    var icon = $("plcServerIcon"), fallback = $("plcServerFallback");
    if (g.icon) {
      if (icon.getAttribute("src") !== g.icon) icon.setAttribute("src", g.icon);
      icon.hidden = false; fallback.hidden = true;
    } else {
      icon.hidden = true; fallback.hidden = false;
      fallback.textContent = (g.name || "?").slice(0, 1).toUpperCase();
    }

    var st = S.state || {};
    var stats = [];
    var num = function (v) { return Number(v || 0).toLocaleString(); };
    if (g.online != null) stats.push(['<span class="plc-stat live"><b>' + num(g.online) + "</b>", "online"]);
    if (g.members != null) stats.push(['<span class="plc-stat"><b>' + num(g.members) + "</b>", "members"]);
    if (g.channels != null) stats.push(['<span class="plc-stat"><b>' + num(g.channels) + "</b>", "channels"]);
    if (g.roles != null) stats.push(['<span class="plc-stat"><b>' + num(g.roles) + "</b>", "roles"]);
    // What the page is actually about goes last, where the eye lands.
    if (st.connected) {
      stats.push(['<span class="plc-stat"><b>' + (st.listeners || []).length + "</b>", "listening"]);
    }
    $("plcStats").innerHTML = stats.map(function (row) {
      return row[0] + "<span>" + row[1] + "</span></span>";
    }).join("");
  }

  // The switcher is what replaces the dashboard's guild banner, so it has to
  // reach every server this account can open - hence the filter box.
  var menuEl = $("plcServerMenu"), menuBtn = $("plcServerBtn"), menuSearch = $("plcServerSearch");

  function renderServerList(filter) {
    var guilds = S.guilds || [];
    var here = (S.guild || {}).id;
    var needle = (filter || "").toLowerCase();
    var rows = guilds.filter(function (g) {
      return !needle || g.name.toLowerCase().indexOf(needle) !== -1;
    });
    if (!rows.length) {
      $("plcServerList").innerHTML =
        '<li class="plc-empty" style="padding:16px;">No server matches that.</li>';
      return;
    }
    $("plcServerList").innerHTML = rows.map(function (g) {
      var art = g.icon
        ? '<img src="' + esc(g.icon) + '" alt="" loading="lazy" />'
        : '<span class="ph">' + esc(g.name.slice(0, 1).toUpperCase()) + "</span>";
      return '<li><button type="button" class="plc-server-opt' + (g.id === here ? " here" : "") +
        '" data-guild="' + esc(g.id) + '" role="option"' +
        (g.id === here ? ' aria-selected="true"' : "") + ">" +
        art + "<span>" + esc(g.name) + "</span>" +
        (g.id === here ? ' <i class="fa fa-check" style="margin-left:auto;opacity:.7;"></i>' : "") +
        "</button></li>";
    }).join("");
  }

  function openMenu(open) {
    menuEl.hidden = !open;
    menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      renderServerList(menuSearch.value);
      menuSearch.focus();
      menuSearch.select();
    }
  }

  menuBtn.addEventListener("click", function (ev) {
    ev.stopPropagation();
    openMenu(menuEl.hidden);
  });
  menuSearch.addEventListener("input", function () { renderServerList(menuSearch.value); });
  menuSearch.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") { openMenu(false); menuBtn.focus(); }
    if (ev.key === "Enter") {
      var first = $("plcServerList").querySelector(".plc-server-opt");
      if (first) first.click();
    }
  });
  document.addEventListener("click", function (ev) {
    if (!menuEl.hidden && !menuEl.contains(ev.target) && ev.target !== menuBtn) openMenu(false);
  });
  $("plcServerList").addEventListener("click", function (ev) {
    var opt = ev.target.closest("[data-guild]");
    if (!opt) return;
    var id = opt.dataset.guild;
    var here = (S.guild || {}).id;
    if (!here || id === here) { openMenu(false); return; }
    // Same page, different server: the guild id is one path segment, so
    // swapping it keeps whatever route the dashboard mounted this page at.
    window.location.pathname = window.location.pathname.replace("/" + here, "/" + id);
  });

  // ---- wallet ------------------------------------------------------------
  function renderWallet() {
    var w = S.wallet || {}, eco = S.economy;
    if (!w.enabled) { show($("plcWallet"), false); return; }
    show($("plcWallet"), true);
    $("plcBal").textContent = Number(w.balance || 0).toLocaleString();
    $("plcCur").textContent = w.currency || "credits";
    $("plcWalletNote").textContent = isStaff
      ? "— what these cost members. Staff are not charged."
      : "— some actions cost credits here.";
    // The list is shown to everyone: staff need to see what the server is
    // charging in order to tune it, even though it never comes out of their
    // own balance.
    var costs = (eco && eco.costs) || w.costs || {};
    $("plcPrices").innerHTML = Object.keys(costs).map(function (k) {
      return '<span class="plc-price">' + esc(k.replace(/_/g, " ")) + " <b>" + esc(costs[k]) + "</b></span>";
    }).join("");
    // The per-button tags are the other way round: a price on a button you are
    // about to press means "this will cost you", so staff do not get those.
    ROOT.querySelectorAll("[data-cost]").forEach(function (tag) {
      var price = isStaff ? 0 : costs[tag.dataset.cost];
      tag.hidden = !price;
      if (price) tag.textContent = price;
    });
  }

  // ---------------------------------------------------------------- events
  // One delegated handler for every button that maps straight to an action.
  ROOT.addEventListener("click", function (ev) {
    var src = ev.target.closest(".plc-src");
    if (src) {
      searchSource = src.dataset.source;
      renderSources();
      if ($("plcQuery").value.trim()) runSearch();
      return;
    }
    var btn = ev.target.closest("[data-act]");
    if (!btn || btn.disabled) return;
    ev.preventDefault();

    var action = btn.dataset.act;
    var extra = {};
    if (btn.dataset.index !== undefined) extra.index = btn.dataset.index;
    if (btn.dataset.result !== undefined) {
      var r = results[Number(btn.dataset.result)];
      if (!r) return;
      // Favouriting wants the encoded blob, which is the only handle that
      // survives a source with no public URL; playing wants the identifier.
      extra.identifier = action === "fav_add" ? (r.encoded || r.identifier) : r.identifier;
    }
    if (btn.dataset.fav !== undefined) {
      var f = (S.favourites || [])[Number(btn.dataset.fav)];
      if (!f) return;
      extra.identifier = f.identifier;
    }
    if (action === "fav_clear" && !window.confirm("Remove every server favourite?")) return;
    if (action === "clear_queue" && !window.confirm("Empty the whole queue?")) return;

    busy(btn, true);
    act(action, extra).catch(function () {}).then(function () {
      busy(btn, false);
      schedule(400);   // catch the follow-on state a beat later
    });
  });

  function busy(btn, on) {
    if (!btn) return;
    if (on) {
      btn.dataset.busy = "1";
      btn.style.opacity = ".55";
      btn.style.pointerEvents = "none";
    } else {
      delete btn.dataset.busy;
      btn.style.opacity = "";
      btn.style.pointerEvents = "";
    }
  }

  // ---- seek bar: click anywhere, or grab and drag -------------------------
  function ratioAt(ev) {
    var box = elSeek.getBoundingClientRect();
    var x = (ev.touches ? ev.touches[0].clientX : ev.clientX) - box.left;
    return Math.max(0, Math.min(1, x / box.width));
  }
  function paintDrag(ratio) {
    var pct = ratio * 100;
    elFill.style.width = pct + "%";
    elKnob.style.left = pct + "%";
    elPos.textContent = fmt(ratio * anchor.duration);
    elScrub.style.left = pct + "%";
    elScrub.textContent = fmt(ratio * anchor.duration);
  }
  elSeek.addEventListener("pointermove", function (ev) {
    if (dragging || !anchor.duration) return;
    var ratio = ratioAt(ev);
    elScrub.style.left = ratio * 100 + "%";
    elScrub.textContent = fmt(ratio * anchor.duration);
  });
  elSeek.addEventListener("pointerdown", function (ev) {
    if (!anchor.duration || elSeek.classList.contains("disabled")) return;
    dragging = true;
    elSeek.classList.add("dragging");
    elSeek.setPointerCapture(ev.pointerId);
    paintDrag(ratioAt(ev));
  });
  elSeek.addEventListener("pointermove", function (ev) {
    if (dragging) paintDrag(ratioAt(ev));
  });
  function endDrag(ev) {
    if (!dragging) return;
    dragging = false;
    elSeek.classList.remove("dragging");
    var seconds = Math.round(ratioAt(ev) * anchor.duration / 1000);
    // Move the local playhead immediately; the next frame confirms it.
    anchor.pos = seconds * 1000;
    anchor.at = performance.now();
    act("seek", { position: seconds }).catch(function () {}).then(function () { schedule(500); });
  }
  elSeek.addEventListener("pointerup", endDrag);
  elSeek.addEventListener("pointercancel", function () {
    dragging = false; elSeek.classList.remove("dragging"); paintSeek();
  });
  elSeek.addEventListener("keydown", function (ev) {
    if (!anchor.duration) return;
    var step = ev.shiftKey ? 30000 : 5000;
    if (ev.key === "ArrowRight") { ev.preventDefault(); nudge(step); }
    if (ev.key === "ArrowLeft") { ev.preventDefault(); nudge(-step); }
  });

  function nudge(deltaMs) {
    if (!anchor.duration) return;
    var target = Math.max(0, Math.min(anchor.duration, livePos() + deltaMs));
    anchor.pos = target;
    anchor.at = performance.now();
    paintSeek();
    act("seek", { position: Math.round(target / 1000) }).catch(function () {});
  }

  // ---- volume: applies on release, never mid-drag -------------------------
  var volEl = $("plcVol"), volTimer = null;
  volEl.addEventListener("pointerdown", function () { volumeHeld = true; });
  volEl.addEventListener("input", function () {
    volumeHeld = true;
    $("plcVolNum").textContent = volEl.value + "%";
    $("plcMuteIcon").className = "fa fa-volume-" +
      (Number(volEl.value) === 0 ? "off" : Number(volEl.value) < 50 ? "down" : "up");
    // Keyboard and click-to-jump fire input with no pointerup to follow, so a
    // debounce is what actually commits the value in those cases.
    clearTimeout(volTimer);
    volTimer = setTimeout(commitVolume, 420);
  });
  volEl.addEventListener("change", function () { clearTimeout(volTimer); commitVolume(); });
  function commitVolume() {
    clearTimeout(volTimer);
    var value = Number(volEl.value);
    volumeHeld = false;
    if (S.state && S.state.volume === value) return;
    act("volume_set", { volume: value }).catch(function () {});
  }

  // ---- search ------------------------------------------------------------
  var queryEl = $("plcQuery"), fieldEl = $("plcSearchField");
  function markField() { fieldEl.classList.toggle("filled", !!queryEl.value); }
  queryEl.addEventListener("input", markField);
  $("plcQueryClear").addEventListener("click", function () {
    queryEl.value = ""; markField(); results = []; searchTerm = ""; renderResults(); queryEl.focus();
  });
  queryEl.addEventListener("keydown", function (ev) {
    if (ev.key !== "Enter") return;
    ev.preventDefault();
    var value = queryEl.value.trim();
    // A pasted link has nothing to search for - queue it and be done.
    if (/^(https?:\/\/|spotify:)/i.test(value)) {
      act("play", { identifier: value }).catch(function () {}).then(function () { schedule(400); });
      return;
    }
    runSearch();
  });
  $("plcSearchGo").addEventListener("click", function (ev) { ev.preventDefault(); runSearch(); });

  function runSearch() {
    var value = queryEl.value.trim();
    if (!value) { queryEl.focus(); return; }
    var box = $("plcResults");
    box.innerHTML = '<li class="plc-empty plc-skeleton"><i class="fa fa-circle-o-notch fa-spin"></i>Searching…</li>';
    act("search", { query: value, source: searchSource }).catch(function () {
      box.innerHTML = '<li class="plc-empty"><i class="fa fa-exclamation-triangle"></i>The search did not come back.</li>';
    });
  }

  // ---- keyboard ----------------------------------------------------------
  document.addEventListener("keydown", function (ev) {
    var el = document.activeElement;
    var typing = el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable);
    if (ev.key === "/" && !typing) { ev.preventDefault(); queryEl.focus(); return; }
    if (typing || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    var hit = function (action, extra) { ev.preventDefault(); act(action, extra).catch(function () {}); };
    switch (ev.key) {
      case " ": hit(S.state && S.state.paused ? "resume" : "pause"); break;
      case "ArrowRight": ev.shiftKey ? hit("skip") : (ev.preventDefault(), nudge(10000)); break;
      case "ArrowLeft": ev.shiftKey ? hit("previous") : (ev.preventDefault(), nudge(-10000)); break;
      case "ArrowUp": ev.preventDefault(); stepVolume(5); break;
      case "ArrowDown": ev.preventDefault(); stepVolume(-5); break;
      case "m": case "M": hit("mute"); break;
      default: break;
    }
  });
  function stepVolume(delta) {
    var value = Math.max(0, Math.min(150, Number(volEl.value) + delta));
    volEl.value = value;
    volEl.dispatchEvent(new Event("input"));
  }

  // Cover art comes from whatever source served the track, so a dead CDN link
  // has to fall back to the placeholder rather than leave a broken image.
  $("plcArt").addEventListener("error", function () {
    this.hidden = true;
    this.removeAttribute("src");
    $("plcArtPh").hidden = false;
    $("plcHeroBg").style.backgroundImage = "";
  });

  // ---- lifecycle ---------------------------------------------------------
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) { failures = 0; schedule(0); }
    else schedule();
  });

  // The playhead is redrawn locally so the clock ticks smoothly instead of
  // stepping once per poll.
  setInterval(function () {
    paintSeek();
    // A track that has run past its own length means the next one started
    // without us; ask early rather than waiting out the poll interval.
    if (anchor.running && anchor.duration && livePos() >= anchor.duration - 250) {
      anchor.running = false;
      schedule(600);
    }
  }, 250);

  // A track change from anywhere - Discord included - should feel instant on
  // the parts of the page that are not the playhead.
  setInterval(function () {
    var key = (S.state && S.state.current && S.state.current.key) || null;
    if (key !== lastKey) { lastKey = key; render(); }
  }, 500);

  // ---- go ----------------------------------------------------------------
  renderSources();
  renderResults();
  reanchor();
  render();
  markField();
  schedule(1200);
})();
</script>
{% endraw %}
"""


SETTINGS_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<style>
  .plcs-img { display:flex; flex-direction:column; gap:4px; align-items:flex-start; }
  .plcs-pick { display:inline-flex; align-items:center; gap:6px; cursor:pointer;
               font-size:.74rem; padding:5px 10px; border-radius:7px;
               border:1px solid rgba(255,255,255,.14); background:rgba(255,255,255,.04); }
  .plcs-pick:hover { background:rgba(255,255,255,.09); }
  .plcs-pick.set { border-color:rgba(59,165,93,.5); color:#3ba55d; }
  .plcs-clear { display:inline-flex; align-items:center; gap:5px;
                font-size:.7rem; opacity:.6; cursor:pointer; }
  .plcs-btn { display:inline-flex; align-items:center; gap:6px; font-size:.8rem;
              font-weight:500; color:#fff; padding:8px 14px; border-radius:8px;
              background:#4e5058; white-space:nowrap; }
  .plcs-btn.primary { background:#5865f2; }
  .plcs-btn.success { background:#248046; }
  .plcs-btn.danger  { background:#da373c; }
</style>

{% macro image_field(b) %}
  <div class="plcs-img">
    <label class="plcs-pick">
      <input type="file" accept="image/png,image/jpeg,image/gif,image/webp"
             data-target="img_{{ b.key }}" hidden />
      <i class="fa fa-upload"></i> <span class="plcs-pick-name">
        {%- if b.image %}replace{% else %}upload{% endif -%}
      </span>
    </label>
    <input type="hidden" name="img_{{ b.key }}" id="img_{{ b.key }}" value="" />
    {% if b.image %}
      <label class="plcs-clear">
        <input type="checkbox" name="clear_img_{{ b.key }}" /> remove
      </label>
    {% endif %}
  </div>
{% endmacro %}

<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-sliders"></i> Controller settings for {{ guild_name }}</h4>
    <p>
      How the controller looks and what each action costs.
      {% if posted %}The panel picks changes up on its next refresh.
      {% else %}No controller has been posted yet.{% endif %}
    </p>
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-hand-pointer-o"></i> Buttons</h5>
      <p class="dz-hint">
        Every button on the controller, as the listener sees it. Blank fields
        fall back to the default. Paste a custom emoji as
        <code>&lt;:name:id&gt;</code>, or upload a picture and it becomes one.
        <b>An uploaded picture is added to this server&#39;s emoji</b> and takes a
        slot &mdash; Discord only accepts a server emoji on a button, so there is
        no way around that. Max 256&nbsp;KB, square works best.
      </p>

      <div class="dz-label" style="margin-top:6px;">Put an uploaded picture in</div>
      <select class="dz-select" name="upload_target" style="max-width:420px;">
        <option value="guild" {% if upload_target == 'guild' %}selected{% endif %}>
          this server's emoji &mdash; costs a slot, known to work
        </option>
        <option value="bot" {% if upload_target == 'bot' %}selected{% endif %}>
          the bot's own emoji &mdash; costs no slot, Discord may refuse it
        </option>
      </select>
      <div style="font-size:.72rem; opacity:.45; margin:4px 0 12px;">
        The bot's own emoji are shared across every server it is in and use none
        of your slots, but Discord has been seen rejecting them on a button. If
        that happens the button says so below and you can switch back.
      </div>

      <div style="overflow-x:auto;">
      <table class="dz-t" style="min-width:680px;">
        <thead>
          <tr><th style="width:1%;">Preview</th><th>Picture</th><th>Emoji</th>
              <th>Label</th><th>Colour</th></tr>
        </thead>
        <tbody>
          {% for b in button_rows %}
            <tr>
              <td style="white-space:nowrap;">
                <span class="plcs-btn {{ b.style }}">
                  {% if b.image %}<img class="dz-emoji" src="{{ b.image }}" alt="" />
                  {%- else %}{{ b.effective }}{% endif %}
                  {{ b.label_override or b.default_label }}
                </span>
                {% if b.broken %}<span class="dz-tag bad">unusable emoji</span>{% endif %}
                <div style="font-size:.72rem; opacity:.45;">{{ b.blurb }}</div>
                {% if b.rejected %}
                  <div style="font-size:.72rem; color:#ff8b8b; max-width:280px;
                              white-space:normal;">
                    <i class="fa fa-exclamation-triangle"></i> {{ b.rejected }}
                  </div>
                {% endif %}
              </td>
              <td style="width:18%;">{{ image_field(b) }}</td>
              <td style="width:17%;">
                <input class="dz-input" type="text" name="e_{{ b.key }}"
                       value="{{ b.current }}" placeholder="{{ b.default }}" />
              </td>
              <td style="width:24%;">
                <input class="dz-input" type="text" name="l_{{ b.key }}" maxlength="80"
                       value="{{ b.label_override }}" placeholder="{{ b.default_label }}" />
              </td>
              <td style="width:18%;">
                <select class="dz-select" name="s_{{ b.key }}">
                  {% for opt in style_options %}
                    <option value="{{ opt }}" {% if opt == b.style %}selected{% endif %}>
                      {{ opt }}{% if opt == 'secondary' %} (transparent){% endif %}
                    </option>
                  {% endfor %}
                </select>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
      </div>

      {% if guild_emojis %}
        <div class="dz-label" style="margin-top:14px;">
          Available custom emoji ({{ guild_emojis|length }})
        </div>
        <div class="dz-row" style="max-height:120px; overflow-y:auto;">
          {% for g in guild_emojis %}
            <span class="dz-tag" title="{{ g.id }}"
                  style="cursor:pointer; display:inline-flex; align-items:center; gap:5px;"
                  onclick="navigator.clipboard && navigator.clipboard.writeText('{{ g.id }}');">
              <img class="dz-emoji" src="{{ g.url }}" alt="" /> {{ g.name }}
            </span>
          {% endfor %}
        </div>
        <div style="font-size:.72rem; opacity:.45; margin-top:5px;">
          Click one to copy its token, then paste it into a field above.
        </div>
      {% endif %}

      <div class="dz-save">
        <button class="dz-btn primary" name="action" value="save_buttons">
          <i class="fa fa-save"></i> Save buttons
        </button>
        <button class="dz-btn" name="action" value="reset_buttons"
                onclick="return confirm('Put every button back to its default?');">
          <i class="fa fa-undo"></i> Reset to defaults
        </button>
      </div>
    </div>
  </form>

  <div class="dz-panel">
    <h5><i class="fa fa-stethoscope"></i> What the bot is actually showing</h5>
    <p class="dz-hint">
      The buttons as they exist in the running controller right now. If a row
      says <b>default</b> while you have a picture saved above, the setting is
      not reaching the view; if it says <b>custom</b> but Discord still shows
      the plain icon, Discord is refusing it.
    </p>

    {% if not live.has_styling_code %}
      <p style="margin:0 0 9px; color:#ff8b8b;">
        <i class="fa fa-exclamation-triangle"></i>
        <b>This bot is running an older copy of the cog</b> &mdash; it has no
        button-styling code at all, so nothing here will ever change. Redeploy
        <code>redbot/cogs/plcontroller/</code> and reload the cog.
      </p>
    {% endif %}
    {% if live.note %}
      <p class="dz-hint" style="color:#f0aa3c;">{{ live.note }}</p>
    {% endif %}

    {% if live.rows %}
      <div style="overflow-x:auto;">
      <table class="dz-t" style="min-width:520px;">
        <thead>
          <tr><th>Button</th><th>Label in use</th><th>Emoji in use</th>
              <th>On the panel</th></tr>
        </thead>
        <tbody>
          {% for r in live.rows %}
            <tr>
              <td style="opacity:.7;">{{ r.key }}</td>
              <td>{{ r.label }}</td>
              <td>
                {% if r.custom %}<span class="dz-tag good">custom</span>
                {% else %}<span class="dz-tag">default</span>{% endif %}
                <code style="font-size:.72rem;">{{ r.emoji }}</code>
              </td>
              <td style="opacity:.7;">{{ 'yes' if r.attached else 'no' }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
      </div>
      <div style="font-size:.72rem; opacity:.45; margin-top:6px;">
        Channel: {{ live.channel or 'not set' }} &middot;
        message attached: {{ 'yes' if live.posted else 'no' }}
      </div>
    {% endif %}
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-money"></i> What actions cost</h5>
      <p class="dz-hint">
        Charged in {{ currency }}, on the buttons here and in Discord alike.
        Staff are never charged. 0 makes an action free.
      </p>

      <label class="dz-toggle">
        <input type="checkbox" name="economy_enabled"
               {% if economy_enabled %}checked{% endif %} />
        <span>Charge for using the player</span>
      </label>

      <div class="dz-grid three" style="margin-top:12px;">
        {% for c in cost_rows %}
          <div>
            <div class="dz-label">{{ c.label }}</div>
            <input class="dz-input" type="number" min="0" name="c_{{ c.key }}"
                   value="{{ c.value }}" />
          </div>
        {% endfor %}
      </div>

      <div class="dz-save">
        <button class="dz-btn primary" name="action" value="save_costs">
          <i class="fa fa-save"></i> Save prices
        </button>
      </div>
    </div>
  </form>
</div>

<script>
(function () {
  // reddash forwards request.form but not request.files, so the picture is read
  // here and posted as an ordinary base64 field instead of a real upload.
  var MAX = 256 * 1024;
  function wire() {
  document.querySelectorAll('input[type=file][data-target]').forEach(function (input) {
    input.addEventListener('change', function () {
      var target = document.getElementById(input.dataset.target);
      var pick = input.closest('.plcs-pick');
      var name = pick ? pick.querySelector('.plcs-pick-name') : null;
      var file = input.files && input.files[0];
      if (!target) { return; }
      if (!file) { target.value = ''; return; }
      if (file.size > MAX) {
        if (name) { name.textContent = Math.round(file.size / 1024) + ' KB - too big'; }
        pick && pick.classList.remove('set');
        input.value = '';
        target.value = '';
        return;
      }
      var reader = new FileReader();
      reader.onload = function () {
        target.value = reader.result;
        if (name) { name.textContent = file.name.slice(0, 18); }
        pick && pick.classList.add('set');
      };
      reader.readAsDataURL(file);
    });
  });
  }
  // The script can sit before the inputs in the rendered page, in which case
  // querySelectorAll finds nothing and no picture ever uploads. Wait for the
  // document when it is still parsing, and run straight away when it is not.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wire);
  } else {
    wire();
  }
})();
</script>
"""
)
