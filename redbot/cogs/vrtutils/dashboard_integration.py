from __future__ import annotations

import asyncio
import logging
import os
import typing as t
from datetime import datetime, timezone

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    channel_options,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
    role_options,
)

log = logging.getLogger("red.vrt.vrtutils.dashboard")


class DashboardIntegration:
    """VrtUtils' diagnostics and lookups on the dashboard.

    Brings the host and bot diagnostics (``botinfo``, ``botip``, ``ispeed``,
    ``diskspeed``, ``cogsizes``, ``codesizes``, ``cleantmp``, ``pip``,
    ``runshell``, ``viewapikeys``/``deleteapikey``), the ID lookups
    (``getguildid``, ``getchannel``, ``getuser``, ``getmessage``,
    ``getwebhook``), the server reports (``oldestchannels``, ``oldestmembers``,
    ``oldestaccounts``, ``rolemembers``) and the cleanup commands
    (``nohoist``, ``wipevcs``, ``wipethreads``) onto one page.
    """

    bot: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering VrtUtils as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Bot diagnostics, ID lookups and server reports.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_vrtutils_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        staff = await is_staff(self.bot, user, member, guild)
        owner = await self.bot.is_owner(user)

        notifications: list[dict] = []
        result: dict = {}
        if kwargs.get("method") == "POST":
            notifications, result = await self._vrt_handle_post(
                member, guild, staff, owner, kwargs
            )

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": VRTUTILS_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_staff": staff,
                "is_owner": owner,
                "host": self._vrt_host_stats() if owner else {},
                "bot_stats": self._vrt_bot_stats(),
                "role_options": role_options(guild),
                "channel_options": channel_options(guild),
                "result": result,
            },
        }

    def _vrt_bot_stats(self) -> dict:
        latency = self.bot.latency * 1000 if self.bot.latency else 0
        return {
            "name": str(self.bot.user) if self.bot.user else "",
            "avatar": str(self.bot.user.display_avatar) if self.bot.user else "",
            "latency": round(latency, 2),
            "guilds": len(self.bot.guilds),
            "users": len(self.bot.users),
            "shards": self.bot.shard_count or 1,
            "cogs": len(self.bot.cogs),
            "commands": len(set(self.bot.walk_commands())),
        }

    def _vrt_host_stats(self) -> dict:
        try:
            import psutil
        except ImportError:  # noqa: BLE001 - psutil is optional at runtime
            return {}

        from .common.utils import get_size

        process = psutil.Process(os.getpid())
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage(os.getcwd())
        boot = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc)
        return {
            "cpu_percent": psutil.cpu_percent(interval=0.2),
            "cpu_count": psutil.cpu_count(),
            "ram_used": get_size(ram.used),
            "ram_total": get_size(ram.total),
            "ram_percent": ram.percent,
            "disk_used": get_size(disk.used),
            "disk_total": get_size(disk.total),
            "disk_percent": disk.percent,
            "bot_ram": get_size(process.memory_info().rss),
            "uptime_since": boot.strftime("%d %b %Y, %H:%M UTC"),
        }

    async def _vrt_handle_post(
        self,
        member: discord.Member,
        guild: discord.Guild,
        staff: bool,
        owner: bool,
        kwargs: dict,
    ) -> tuple[list[dict], dict]:
        field = form_reader(kwargs)
        action = field("action")

        try:
            # ---- lookups: any member of the server may run these ----
            if action == "lookup":
                return await self._vrt_lookup(field)

            if action == "role_members":
                role = guild.get_role(field.integer("role_id", 0) or 0)
                if role is None:
                    return [{"message": "Pick a role.", "category": "warning"}], {}
                members = sorted(role.members, key=lambda m: m.display_name.lower())
                return [], {
                    "title": f"{len(members)} member(s) with {role.name}",
                    "rows": [
                        {"a": m.display_name, "b": str(m.id), "c": str(m)}
                        for m in members[:500]
                    ],
                    "headers": ("Member", "ID", "Handle"),
                }

            if action == "oldest_channels":
                channels = sorted(
                    [c for c in guild.channels if not isinstance(c, discord.CategoryChannel)],
                    key=lambda c: c.created_at,
                )
                return [], {
                    "title": "Oldest channels",
                    "rows": [
                        {
                            "a": c.name,
                            "b": c.created_at.strftime("%d %b %Y"),
                            "c": str(c.id),
                        }
                        for c in channels[:50]
                    ],
                    "headers": ("Channel", "Created", "ID"),
                }

            if action in ("oldest_members", "oldest_accounts"):
                include_bots = field.checked("include_bots")
                members = [m for m in guild.members if include_bots or not m.bot]
                if action == "oldest_members":
                    members.sort(key=lambda m: m.joined_at or datetime.now(timezone.utc))
                    label, stamp = "Longest-standing members", "Joined"
                    date_of = lambda m: m.joined_at  # noqa: E731
                else:
                    members.sort(key=lambda m: m.created_at)
                    label, stamp = "Oldest Discord accounts", "Registered"
                    date_of = lambda m: m.created_at  # noqa: E731
                return [], {
                    "title": label,
                    "rows": [
                        {
                            "a": m.display_name,
                            "b": date_of(m).strftime("%d %b %Y") if date_of(m) else "?",
                            "c": str(m.id),
                        }
                        for m in members[:50]
                    ],
                    "headers": ("Member", stamp, "ID"),
                }

            # ---- server maintenance: administrators only ----
            if action in ("nohoist", "wipevcs", "wipethreads"):
                if not staff:
                    return [
                        {"message": "Only server administrators can do that.",
                         "category": "danger"}
                    ], {}
                return await self._vrt_maintenance(action, guild)

            # ---- host and bot maintenance: owner only ----
            if not owner:
                return [
                    {"message": "Only the bot owner can do that.", "category": "danger"}
                ], {}

            if action == "botip":
                import aiohttp

                async with aiohttp.ClientSession() as session:
                    async with session.get("https://api.ipify.org?format=json") as response:
                        data = await response.json()
                return [], {"title": "Public IP address", "text": data["ip"]}

            if action == "speedtest":
                import speedtest

                test = speedtest.Speedtest(secure=True)
                await asyncio.to_thread(test.get_best_server)
                await asyncio.to_thread(test.download)
                await asyncio.to_thread(test.upload)
                results = test.results.dict()
                return [], {
                    "title": "Internet speed test",
                    "rows": [
                        {"a": "Ping", "b": f"{results['ping']} ms", "c": ""},
                        {
                            "a": "Download",
                            "b": f"{results['download'] / 1_000_000:.2f} Mbps",
                            "c": "",
                        },
                        {
                            "a": "Upload",
                            "b": f"{results['upload'] / 1_000_000:.2f} Mbps",
                            "c": "",
                        },
                    ],
                    "headers": ("Measure", "Result", ""),
                }

            if action == "diskspeed":
                from .common.diskspeed import get_disk_speed

                speeds = await asyncio.to_thread(get_disk_speed, str(self.path), 128, 1048576)
                return [], {
                    "title": "Disk speed",
                    "rows": [
                        {"a": key.replace("_", " ").title(), "b": str(value), "c": ""}
                        for key, value in speeds.items()
                    ],
                    "headers": ("Measure", "Result", ""),
                }

            if action in ("cogsizes", "codesizes"):
                from .common.utils import calculate_directory_size, get_size

                cog_mgr = self.bot._cog_mgr
                install_path = await cog_mgr.install_path()
                sizes: dict[str, int] = {}
                if action == "cogsizes":
                    # Saved data lives two levels above the install path.
                    title = "Saved cog data"
                    for entry in install_path.parent.parent.iterdir():
                        if not entry.is_dir():
                            continue
                        sizes[entry.name] = await asyncio.to_thread(
                            calculate_directory_size, entry
                        )
                else:
                    title = "Cog code sizes"
                    for path in [install_path, *await cog_mgr.user_defined_paths()]:
                        if not path.exists():
                            continue
                        for entry in path.iterdir():
                            if not entry.is_dir() or entry.name.startswith((".", "_")):
                                continue
                            sizes[entry.name] = await asyncio.to_thread(
                                calculate_directory_size, entry
                            )
                ordered = sorted(sizes.items(), key=lambda s: -s[1])
                return [], {
                    "title": title,
                    "rows": [
                        {
                            "a": name,
                            "b": get_size(total),
                            "c": "loaded" if self.bot.get_cog(name) else "",
                        }
                        for name, total in ordered
                    ],
                    "headers": ("Cog", "Size", ""),
                }

            if action == "cleantmp":
                import shutil
                import tempfile

                removed = 0
                temp = tempfile.gettempdir()
                for entry in os.listdir(temp):
                    path = os.path.join(temp, entry)
                    try:
                        if os.path.isdir(path):
                            shutil.rmtree(path, ignore_errors=True)
                        else:
                            os.remove(path)
                        removed += 1
                    except OSError:
                        continue
                return [
                    {"message": f"Removed {removed} item(s) from the temp folder.",
                     "category": "success"}
                ], {}

            if action in ("pip", "shell"):
                from .common.utils import do_shell_command

                command = (field("command") or "").strip()
                if not command:
                    return [{"message": "Enter a command.", "category": "warning"}], {}
                if action == "pip":
                    command = f"pip {command}"
                output = await do_shell_command(command)
                return [], {"title": f"$ {command}", "text": output or "(no output)"}

            if action == "view_keys":
                keys = await self.bot.get_shared_api_tokens()
                return [], {
                    "title": "Configured API services",
                    "rows": [
                        {
                            "a": service,
                            "b": ", ".join(sorted(values.keys())),
                            "c": "",
                        }
                        for service, values in sorted(keys.items())
                    ],
                    "headers": ("Service", "Keys stored", ""),
                    "note": "Key names only; the values are never sent to the browser.",
                }

            if action == "delete_key":
                service = (field("service") or "").strip()
                if not service:
                    return [{"message": "Enter a service name.", "category": "warning"}], {}
                await self.bot.remove_shared_api_tokens(service)
                return [
                    {"message": f"Removed the stored keys for {service}.",
                     "category": "success"}
                ], {}
        except Exception as exc:  # noqa: BLE001
            log.exception("VrtUtils dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}], {}

        return [{"message": f"Unknown action: {action}", "category": "warning"}], {}

    async def _vrt_lookup(self, field) -> tuple[list[dict], dict]:
        kind = field("lookup_kind")
        raw = (field("lookup_value") or "").strip()
        if not raw:
            return [{"message": "Enter an ID or a name.", "category": "warning"}], {}

        if kind == "guild":
            try:
                guild = self.bot.get_guild(int(raw))
            except ValueError:
                guild = discord.utils.find(
                    lambda g: raw.lower() in g.name.lower(), self.bot.guilds
                )
            if guild is None:
                return [{"message": "No server matched.", "category": "info"}], {}
            return [], {
                "title": guild.name,
                "rows": [
                    {"a": "ID", "b": str(guild.id), "c": ""},
                    {"a": "Owner", "b": str(guild.owner), "c": ""},
                    {"a": "Members", "b": str(guild.member_count), "c": ""},
                    {"a": "Created", "b": guild.created_at.strftime("%d %b %Y"), "c": ""},
                ],
                "headers": ("Field", "Value", ""),
                "thumbnail": str(guild.icon.url) if guild.icon else "",
            }

        if kind == "channel":
            channel = self.bot.get_channel(field.integer("lookup_value", 0) or 0)
            if channel is None:
                return [{"message": "No channel with that ID.", "category": "info"}], {}
            return [], {
                "title": f"#{channel.name}",
                "rows": [
                    {"a": "ID", "b": str(channel.id), "c": ""},
                    {"a": "Type", "b": str(channel.type), "c": ""},
                    {"a": "Server", "b": getattr(channel.guild, "name", ""), "c": ""},
                    {"a": "Created", "b": channel.created_at.strftime("%d %b %Y"), "c": ""},
                ],
                "headers": ("Field", "Value", ""),
            }

        if kind == "user":
            user_id = field.integer("lookup_value", 0) or 0
            target = self.bot.get_user(user_id)
            if target is None:
                try:
                    target = await self.bot.fetch_user(user_id)
                except discord.HTTPException:
                    target = None
            if target is None:
                return [{"message": "No user with that ID.", "category": "info"}], {}
            return [], {
                "title": str(target),
                "rows": [
                    {"a": "ID", "b": str(target.id), "c": ""},
                    {"a": "Bot", "b": "yes" if target.bot else "no", "c": ""},
                    {"a": "Created", "b": target.created_at.strftime("%d %b %Y"), "c": ""},
                    {
                        "a": "Shared servers",
                        "b": str(sum(1 for g in self.bot.guilds if g.get_member(target.id))),
                        "c": "",
                    },
                ],
                "headers": ("Field", "Value", ""),
                "thumbnail": str(target.display_avatar),
            }

        if kind == "webhook":
            try:
                webhook = await self.bot.fetch_webhook(int(raw))
            except (ValueError, discord.HTTPException):
                return [{"message": "No webhook with that ID.", "category": "info"}], {}
            return [], {
                "title": webhook.name or "Webhook",
                "rows": [
                    {"a": "ID", "b": str(webhook.id), "c": ""},
                    {"a": "Channel", "b": str(webhook.channel_id), "c": ""},
                    {"a": "Server", "b": str(webhook.guild_id), "c": ""},
                    {"a": "Created", "b": webhook.created_at.strftime("%d %b %Y"), "c": ""},
                ],
                "headers": ("Field", "Value", ""),
            }

        if kind == "message":
            # Accepts `channel_id-message_id`, the format Discord's copy-ID gives.
            parts = raw.replace(" ", "").split("-")
            if len(parts) != 2 or not all(p.isdigit() for p in parts):
                return [
                    {"message": "Use the channelid-messageid format.",
                     "category": "warning"}
                ], {}
            channel = self.bot.get_channel(int(parts[0]))
            if channel is None:
                return [{"message": "That channel is not visible to me.",
                         "category": "info"}], {}
            try:
                message = await channel.fetch_message(int(parts[1]))
            except discord.HTTPException:
                return [{"message": "That message could not be fetched.",
                         "category": "info"}], {}
            return [], {
                "title": f"Message by {message.author}",
                "text": message.content or "(no text content)",
                "rows": [
                    {"a": "ID", "b": str(message.id), "c": ""},
                    {"a": "Channel", "b": getattr(channel, "name", ""), "c": ""},
                    {
                        "a": "Sent",
                        "b": message.created_at.strftime("%d %b %Y, %H:%M"),
                        "c": "",
                    },
                    {"a": "Attachments", "b": str(len(message.attachments)), "c": ""},
                ],
                "headers": ("Field", "Value", ""),
                "link": message.jump_url,
            }

        return [{"message": f"Unknown lookup: {kind}", "category": "warning"}], {}

    async def _vrt_wipe_rooms(self, guild: discord.Guild) -> list[dict]:
        """Delete the empty PrivateRooms channels, tracked or not.

        Deliberately not "every empty voice channel in the server": the AFK
        channel, staff rooms and the hubs people join to create a room are all
        empty most of the time, and deleting those breaks the server.

        Going by the tracked list alone is not enough either. PrivateRooms drops
        a room from that list *before* it deletes the channel, so any delete
        that fails leaves a channel nothing knows about - which is exactly the
        rubbish this button is for. So the rooms category is swept as well, with
        the hubs and the AFK channel held back.
        """
        rooms_cog = self.bot.get_cog("PrivateRooms")
        if rooms_cog is None:
            return [
                {
                    "message": "PrivateRooms is not loaded, so there are no rooms to "
                    "clean up. Nothing was deleted.",
                    "category": "warning",
                }
            ]

        try:
            settings = await rooms_cog.config.guild(guild).all()
        except Exception as exc:  # noqa: BLE001 - another cog's config is not ours to trust
            log.exception("Could not read PrivateRooms settings")
            return [
                {"message": f"Could not read the PrivateRooms settings: {exc}",
                 "category": "danger"}
            ]

        spared = {settings.get("hub_private"), settings.get("hub_public"),
                  getattr(guild.afk_channel, "id", None)}
        spared.discard(None)

        tracked = settings.get("rooms") or {}
        candidates: dict[int, str] = {}
        stale = 0
        # Snapshot the keys: dropping a stale entry below writes to the same
        # mapping this loop is walking.
        for raw_id in list(tracked):
            try:
                channel_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if channel_id in spared:
                continue
            if guild.get_channel(channel_id) is None:
                # The channel is gone but the entry is not; drop it so the
                # numbers stop lying.
                async with rooms_cog.config.guild(guild).rooms() as rooms:
                    rooms.pop(str(raw_id), None)
                stale += 1
                continue
            candidates[channel_id] = "tracked"

        # Everything else sitting in the rooms category. Without a category
        # there is nothing safe to sweep, so the tracked list is all we use.
        category = self._vrt_rooms_category(guild, settings)
        if category is not None:
            for channel in category.voice_channels:
                if channel.id in spared or channel.id in candidates:
                    continue
                candidates[channel.id] = "untracked"

        deleted = {"tracked": 0, "untracked": 0}
        occupied = 0
        failed = 0
        for channel_id, source in candidates.items():
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                continue
            if channel.members:
                occupied += 1
                continue
            try:
                await channel.delete(reason="Empty private room, wiped from the dashboard")
            except discord.HTTPException:
                failed += 1
                continue
            async with rooms_cog.config.guild(guild).rooms() as rooms:
                rooms.pop(str(channel_id), None)
            deleted[source] += 1

        total = deleted["tracked"] + deleted["untracked"]
        out = [
            {"message": f"Deleted {total} empty private room(s).",
             "category": "success" if total else "info"}
        ]
        if deleted["untracked"]:
            out.append(
                {
                    "message": f"{deleted['untracked']} of those were leftovers "
                    f"PrivateRooms had lost track of in {category.name}.",
                    "category": "info",
                }
            )
        if not total and category is None and not tracked:
            out.append(
                {
                    "message": "PrivateRooms is not tracking any rooms and has no "
                    "category set, so there is nothing I can safely sweep. Set one "
                    "in the PrivateRooms section of this page.",
                    "category": "warning",
                }
            )
        if occupied:
            out.append(
                {"message": f"{occupied} room(s) still have someone in them and were left "
                            "alone.", "category": "info"}
            )
        if stale:
            out.append(
                {"message": f"Forgot {stale} room(s) that no longer exist.",
                 "category": "info"}
            )
        if failed:
            out.append(
                {"message": f"{failed} room(s) could not be deleted; check that I have "
                            "Manage Channels.", "category": "warning"}
            )
        return out

    @staticmethod
    def _vrt_rooms_category(guild: discord.Guild, settings: dict):
        """Where PrivateRooms puts new rooms, which is where leftovers collect.

        Mirrors how the cog picks it when creating one: the configured category,
        otherwise whatever category the hubs live in.
        """
        category = guild.get_channel(settings.get("category") or 0)
        if isinstance(category, discord.CategoryChannel):
            return category
        for key in ("hub_private", "hub_public"):
            hub = guild.get_channel(settings.get(key) or 0)
            if hub is not None and isinstance(
                getattr(hub, "category", None), discord.CategoryChannel
            ):
                return hub.category
        return None

    async def _vrt_maintenance(
        self, action: str, guild: discord.Guild
    ) -> tuple[list[dict], dict]:
        if action == "nohoist":
            if not guild.me.guild_permissions.manage_nicknames:
                return [
                    {"message": "I need the Manage Nicknames permission.",
                     "category": "warning"}
                ], {}
            renamed = 0
            failed = 0
            for target in guild.members:
                name = target.display_name
                if not name or name[0].isalnum():
                    continue
                stripped = name.lstrip("".join(c for c in name if not c.isalnum()))
                if not stripped:
                    stripped = "No Hoist"
                try:
                    await target.edit(nick=stripped, reason="Dehoisted from the dashboard")
                    renamed += 1
                except discord.HTTPException:
                    failed += 1
            out = [{"message": f"Renamed {renamed} member(s).", "category": "success"}]
            if failed:
                out.append(
                    {"message": f"{failed} member(s) could not be renamed.",
                     "category": "warning"}
                )
            return out, {}

        if action == "wipevcs":
            return await self._vrt_wipe_rooms(guild), {}

        if action == "wipethreads":
            deleted = 0
            for thread in list(guild.threads):
                try:
                    await thread.delete(reason="Wiped from the dashboard")
                    deleted += 1
                except discord.HTTPException:
                    continue
            return [
                {"message": f"Deleted {deleted} thread(s).", "category": "success"}
            ], {}

        return [{"message": f"Unknown action: {action}", "category": "warning"}], {}


VRTUTILS_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-tachometer"></i> Utilities</h4>
    <p>Diagnostics, ID lookups and server reports for {{ guild_name }}.</p>
  </div>

  {{ stats([('Servers', bot_stats.guilds),
            ('Users', bot_stats.users),
            ('Cogs', bot_stats.cogs),
            ('Commands', bot_stats.commands),
            ('Latency', bot_stats.latency ~ ' ms')]) }}

  {% if result %}
    <div class="dz-panel">
      <h5><i class="fa fa-info-circle"></i> {{ result.title }}</h5>
      {% if result.thumbnail %}
        <img src="{{ result.thumbnail }}" alt=""
             style="width:96px; border-radius:12px; margin-bottom:10px;" />
      {% endif %}
      {% if result.text %}
        <pre class="dz-text" style="max-height:420px; overflow:auto;">{{ result.text }}</pre>
      {% endif %}
      {% if result.rows %}
        <table class="dz-t">
          <tr>{% for h in result.headers %}<th>{{ h }}</th>{% endfor %}</tr>
          {% for r in result.rows %}
            <tr><td>{{ r.a }}</td><td>{{ r.b }}</td><td>{{ r.c }}</td></tr>
          {% endfor %}
        </table>
      {% endif %}
      {% if result.note %}<p class="dz-hint">{{ result.note }}</p>{% endif %}
      {% if result.link %}
        <p><a class="dz-btn" href="{{ result.link }}" target="_blank" rel="noopener">
          <i class="fa fa-external-link"></i> Open in Discord</a></p>
      {% endif %}
    </div>
  {% endif %}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-search"></i> Look something up</h5>
      <p class="dz-hint">Messages use the <code>channelid-messageid</code> format
         Discord's "copy message ID" gives you.</p>
      <div class="dz-row">
        <select class="dz-select" name="lookup_kind" style="max-width:180px;">
          <option value="user">User</option>
          <option value="guild">Server</option>
          <option value="channel">Channel</option>
          <option value="message">Message</option>
          <option value="webhook">Webhook</option>
        </select>
        <input class="dz-input" type="text" name="lookup_value"
               placeholder="ID or name" style="flex:1 1 220px;" />
        <button class="dz-btn primary" name="action" value="lookup">
          <i class="fa fa-search"></i> Look up
        </button>
      </div>
    </div>
  </form>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-bar-chart"></i> Server reports</h5>
      <div class="dz-grid two">
        <div>
          <label class="dz-label">Members with a role</label>
          {{ picker('role_id', role_options, false, 6, 'Search roles...') }}
          <div class="dz-save">
            <button class="dz-btn" name="action" value="role_members">
              <i class="fa fa-users"></i> List members
            </button>
          </div>
        </div>
        <div>
          <label class="dz-label">Oldest first</label>
          <label class="dz-toggle">
            <input type="checkbox" name="include_bots" />
            <span>Include bots</span>
          </label>
          <div class="dz-row">
            <button class="dz-btn" name="action" value="oldest_channels">
              <i class="fa fa-hashtag"></i> Channels
            </button>
            <button class="dz-btn" name="action" value="oldest_members">
              <i class="fa fa-user"></i> Members
            </button>
            <button class="dz-btn" name="action" value="oldest_accounts">
              <i class="fa fa-clock-o"></i> Accounts
            </button>
          </div>
        </div>
      </div>
    </div>
  </form>

  {% if is_staff %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-broom"></i> Server cleanup</h5>
        <p class="dz-hint">
          These change the server immediately and cannot be undone. The room
          cleanup only touches channels PrivateRooms created, never the hubs,
          the AFK channel or anything you made yourself.
        </p>
        <div class="dz-row">
          {{ confirm('Dehoist nicknames', 'nohoist',
                     'Rename every member whose name starts with a symbol so they stop sorting to the top?',
                     '', 'fa-sort-alpha-asc') }}
          {{ confirm('Delete empty private rooms', 'wipevcs',
                     'Delete the PrivateRooms channels that nobody is sitting in?') }}
          {{ confirm('Delete all threads', 'wipethreads',
                     'Delete every thread in this server?') }}
        </div>
      </div>
    </form>
  {% endif %}

  {% if is_owner %}
    {% if host %}
      <div class="dz-panel">
        <h5><i class="fa fa-server"></i> Host</h5>
        <div class="dz-grid two">
          <table class="dz-t">
            <tr><th>CPU</th>
                <td>{{ host.cpu_percent }}% over {{ host.cpu_count }} core(s)</td></tr>
            <tr><th>Memory</th>
                <td>{{ host.ram_used }} / {{ host.ram_total }} ({{ host.ram_percent }}%)</td></tr>
            <tr><th>Bot memory</th><td>{{ host.bot_ram }}</td></tr>
          </table>
          <table class="dz-t">
            <tr><th>Disk</th>
                <td>{{ host.disk_used }} / {{ host.disk_total }} ({{ host.disk_percent }}%)</td></tr>
            <tr><th>Booted</th><td>{{ host.uptime_since }}</td></tr>
            <tr><th>Shards</th><td>{{ bot_stats.shards }}</td></tr>
          </table>
        </div>
      </div>
    {% endif %}

    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-heartbeat"></i> Diagnostics</h5>
        <p class="dz-hint">Speed tests take a while and hold the page until they finish.</p>
        <div class="dz-row">
          <button class="dz-btn" name="action" value="botip">
            <i class="fa fa-globe"></i> Public IP
          </button>
          <button class="dz-btn" name="action" value="speedtest">
            <i class="fa fa-tachometer"></i> Internet speed
          </button>
          <button class="dz-btn" name="action" value="diskspeed">
            <i class="fa fa-hdd-o"></i> Disk speed
          </button>
          <button class="dz-btn" name="action" value="cogsizes">
            <i class="fa fa-database"></i> Cog data sizes
          </button>
          <button class="dz-btn" name="action" value="codesizes">
            <i class="fa fa-code"></i> Code sizes
          </button>
          {{ confirm('Clean temp folder', 'cleantmp',
                     'Delete everything in the system temp folder?') }}
        </div>
      </div>
    </form>

    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-terminal"></i> Run a command</h5>
        <p class="dz-hint">Both buttons run inside the bot's venv as
           <code>python -m &lt;command&gt;</code>, with the bot's privileges.</p>
        <div class="dz-row">
          <input class="dz-input" type="text" name="command"
                 placeholder="install requests   /   pip list" style="flex:1 1 260px;" />
          <button class="dz-btn" name="action" value="pip"
                  onclick="return confirm('Run pip with these arguments?');">
            <i class="fa fa-cube"></i> pip
          </button>
          <button class="dz-btn danger" name="action" value="shell"
                  onclick="return confirm('Run this as a shell command on the host?');">
            <i class="fa fa-terminal"></i> shell
          </button>
        </div>
      </div>
    </form>

    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-key"></i> API keys</h5>
        <p class="dz-hint">Only the service and key names are shown, never the values.</p>
        <div class="dz-row">
          <button class="dz-btn" name="action" value="view_keys">
            <i class="fa fa-list"></i> List services
          </button>
          <input class="dz-input" type="text" name="service"
                 placeholder="service name" style="max-width:220px;" />
          <button class="dz-btn danger" name="action" value="delete_key"
                  onclick="return confirm('Delete the stored keys for this service?');">
            <i class="fa fa-trash-o"></i> Delete keys
          </button>
        </div>
      </div>
    </form>
  {% endif %}
</div>
"""
)
