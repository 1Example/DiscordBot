"""The EmbedUtils dashboard page.

Built for the dashboard rather than adapted to it. The previous version framed
Glitchii's standalone embed builder - a complete HTML document with ~91KB of
CSS written against `html`/`body`, panes sized as percentages of a fixed box
and controls pinned with hard-coded pixel offsets. None of that survives being
placed inside a card, and every override fought a rule this repo does not own,
so it is replaced outright by `studio.html`.

The page talks to this module over one POST endpoint that returns JSON: the
dashboard's third-party route hands back ``result["data"]`` verbatim when a
handler provides it, so the browser gets a plain API instead of a full
re-render for every action.
"""

import json
import os
import re
import typing

import discord
from redbot.core import commands
from redbot.core.bot import Red
from redbot.core.i18n import Translator

_: Translator = Translator("EmbedUtils", __file__)


def dashboard_page(*args, **kwargs):
    def decorator(func: typing.Callable):
        func.__dashboard_decorator_params__ = (args, kwargs)
        return func

    return decorator


_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "studio.html")
_TEMPLATE = None

# Discord's own limits. The page keeps a copy so the counters can run without a
# round trip; this is the authority for the checks that must not be bypassed by
# editing the DOM.
LIMITS = {
    "content": 2000,
    "title": 256,
    "description": 4096,
    "author": 256,
    "footer": 2048,
    "field_name": 256,
    "field_value": 1024,
    "fields": 25,
    "embeds": 10,
    "total": 6000,
}

MESSAGE_LINK = re.compile(
    r"(?:https?://)?(?:\w+\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild>\d+)/(?P<channel>\d+)/(?P<message>\d+)"
)


def _template() -> str:
    global _TEMPLATE
    if _TEMPLATE is None:
        with open(_TEMPLATE_PATH, encoding="utf-8") as file:
            _TEMPLATE = file.read()
    return _TEMPLATE


def _payload_length(payload: dict) -> int:
    """The character count Discord applies its 6000 limit to."""
    total = len(payload.get("content") or "")
    for embed in payload.get("embeds") or []:
        total += len(embed.get("title") or "")
        total += len(embed.get("description") or "")
        total += len((embed.get("author") or {}).get("name") or "")
        total += len((embed.get("footer") or {}).get("text") or "")
        for field in embed.get("fields") or []:
            total += len(field.get("name") or "") + len(field.get("value") or "")
    return total


def validate(payload: dict) -> list:
    """Everything that would make Discord refuse this message.

    The page runs the same checks live so Send can be disabled with a reason,
    but they are re-run here: the browser's copy is a convenience, not a
    guarantee.
    """
    problems = []
    content = payload.get("content") or ""
    embeds = payload.get("embeds") or []

    if len(content) > LIMITS["content"]:
        problems.append("Message content is over %s characters." % LIMITS["content"])
    if len(embeds) > LIMITS["embeds"]:
        problems.append("Discord allows at most %s embeds per message." % LIMITS["embeds"])
    if not content.strip() and not embeds:
        problems.append("There is nothing to send.")
    if _payload_length(payload) > LIMITS["total"]:
        problems.append(
            "The message is over Discord's %s-character total." % LIMITS["total"]
        )

    for index, embed in enumerate(embeds, start=1):
        label = "Embed %s" % index
        fields = embed.get("fields") or []
        has_something = any(
            (
                embed.get("title"),
                embed.get("description"),
                fields,
                (embed.get("image") or {}).get("url"),
                (embed.get("author") or {}).get("name"),
            )
        )
        if not has_something:
            problems.append("%s is empty - it would not post." % label)
        if len(embed.get("title") or "") > LIMITS["title"]:
            problems.append("%s title is over %s characters." % (label, LIMITS["title"]))
        if len(embed.get("description") or "") > LIMITS["description"]:
            problems.append(
                "%s description is over %s characters." % (label, LIMITS["description"])
            )
        if len(fields) > LIMITS["fields"]:
            problems.append("%s has more than %s fields." % (label, LIMITS["fields"]))
        for position, field in enumerate(fields, start=1):
            if not (field.get("name") or "").strip() or not (field.get("value") or "").strip():
                problems.append(
                    "%s field %s needs both a name and a value." % (label, position)
                )
    return problems


class DashboardIntegration:
    bot: Red

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog: commands.Cog) -> None:
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    # ------------------------------------------------------------------ pages

    @dashboard_page(name=None, description="Build rich embeds.")
    async def dashboard_editor(self, **kwargs) -> None:
        # Without a guild there is nowhere to send and nothing to store, so the
        # global entry only points at the per-server page.
        return {
            "status": 0,
            "web_content": {
                "source": (
                    '<div class="dz"><div class="dz-head">'
                    '<h4><i class="fa fa-code"></i> Embed Studio</h4>'
                    "<p>Pick a server from the Dashboard to build and send an embed.</p>"
                    "</div></div>"
                ),
            },
        }

    @dashboard_page(
        name="create",
        description="Build an embed and send it to a channel in this server.",
        methods=("GET", "POST"),
    )
    async def dashboard_guild(self, user: discord.User, guild: discord.Guild, **kwargs) -> None:
        # `user` rather than `member`: it maps to the `user_id` context id, which
        # the dashboard fills from the session. Asking for `member` would add a
        # `member_id` context id that only a `?member_id=` in the URL satisfies,
        # and a link from the Modules card never has one.
        member = guild.get_member(user.id)
        if member is None:
            return {
                "status": 0,
                "error_code": 403,
                "message": _("You are not a member of this server."),
            }

        is_owner = member.id in self.bot.owner_ids
        if not (
            is_owner or await self.bot.is_mod(member) or member.guild_permissions.manage_guild
        ):
            return {
                "status": 0,
                "error_code": 403,
                "message": _("You don't have permissions to access this page."),
            }

        if kwargs.get("method") == "POST":
            return await self._handle_action(member, guild, kwargs)

        channels = self._channels(member, guild)
        if not channels:
            return {
                "status": 0,
                "error_code": 403,
                "message": _("There is no channel here that both of us can post in."),
            }

        return {
            "status": 0,
            "web_content": {
                "source": _template(),
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "action_url": (kwargs.get("request_url") or "").split("?", 1)[0],
                "studio_channels": channels,
                "studio_saved": await self._saved(guild),
                "studio_limits": LIMITS,
                "studio_bot": {
                    "name": guild.me.display_name,
                    "avatar": guild.me.display_avatar.url,
                },
                "studio_guild": {"name": guild.name, "id": str(guild.id)},
            },
        }

    # --------------------------------------------------------------- actions

    async def _handle_action(self, member, guild, kwargs) -> dict:
        form = (kwargs.get("data") or {}).get("form") or {}
        action = form.get("action")
        raw = form.get("payload") or ""

        try:
            payload = json.loads(raw) if raw else {}
        except ValueError as error:
            return {"data": {"ok": False, "error": "That is not valid JSON: %s" % error}}
        if not isinstance(payload, dict):
            return {"data": {"ok": False, "error": "The payload must be a JSON object."}}

        try:
            if action == "send":
                return {"data": await self._send(member, guild, payload, form)}
            if action == "save":
                return {"data": await self._save(member, guild, payload, form)}
            if action == "delete":
                return {"data": await self._delete(member, guild, form)}
            if action == "load_message":
                return {"data": await self._load_message(guild, form)}
        except Exception as error:  # noqa: BLE001
            self.logger.exception("Embed Studio action %r failed", action)
            return {"data": {"ok": False, "error": str(error)}}

        return {"data": {"ok": False, "error": "Unknown action: %s" % action}}

    @staticmethod
    def _many(form, key):
        """Read a repeated form field, whatever container the RPC handed over."""
        getlist = getattr(form, "getlist", None)
        if getlist is not None:
            return [v for v in getlist(key) if v]
        value = form.get(key)
        if isinstance(value, (list, tuple)):
            return [v for v in value if v]
        return [v for v in (value or "").split(",") if v]

    async def _send(self, member, guild, payload: dict, form) -> dict:
        problems = validate(payload)
        if problems:
            return {"ok": False, "error": problems[0], "problems": problems}

        wanted = self._many(form, "channels")
        if not wanted:
            return {"ok": False, "error": "Pick at least one channel."}

        content = payload.get("content") or None
        try:
            embeds = [discord.Embed.from_dict(e) for e in payload.get("embeds") or []]
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "error": "Discord rejected the embed data: %s" % error}

        # One result per channel rather than a burst of toasts, so a partial
        # failure is legible.
        results = []
        for raw_id in wanted:
            channel = guild.get_channel(int(raw_id)) if raw_id.isdigit() else None
            if channel is None:
                results.append(
                    {"channel": raw_id, "ok": False, "why": "Channel no longer exists."}
                )
                continue
            name = "#%s" % channel.name
            if not channel.permissions_for(member).send_messages:
                results.append({"channel": name, "ok": False, "why": "You cannot post there."})
                continue
            if not channel.permissions_for(guild.me).send_messages:
                results.append({"channel": name, "ok": False, "why": "I cannot post there."})
                continue
            try:
                message = await channel.send(content=content, embeds=embeds)
            except discord.HTTPException as error:
                results.append({"channel": name, "ok": False, "why": str(error)})
            else:
                results.append({"channel": name, "ok": True, "link": message.jump_url})

        sent = sum(1 for r in results if r["ok"])
        self.logger.trace(
            "Embed Studio: %s/%s deliveries in %s (%s) by %s (%s).",
            sent,
            len(results),
            guild.name,
            guild.id,
            member.display_name,
            member.id,
        )
        return {"ok": sent > 0, "results": results, "sent": sent, "total": len(results)}

    async def _save(self, member, guild, payload: dict, form) -> dict:
        name = (form.get("name") or "").strip().lower()
        if not name:
            return {"ok": False, "error": "Give the template a name."}
        if len(name) > 32:
            return {"ok": False, "error": "Names are limited to 32 characters."}
        embeds = payload.get("embeds") or []
        if not embeds:
            return {"ok": False, "error": "There is no embed to save."}

        async with self.config.guild(guild).stored_embeds() as stored:
            existing = stored.get(name)
            if existing and existing.get("locked") and member.id not in self.bot.owner_ids:
                return {"ok": False, "error": "`%s` is locked and cannot be replaced." % name}
            if not existing and len(stored) >= 100:
                return {
                    "ok": False,
                    "error": "This server has reached the 100 stored embed limit.",
                }
            # The same record shape `[p]embed store` writes, so templates saved
            # here and in chat stay interchangeable.
            stored[name] = {
                "author": member.id,
                "embed": embeds[0],
                "locked": bool(existing.get("locked")) if existing else False,
                "uses": int(existing.get("uses", 0)) if existing else 0,
            }
        return {"ok": True, "saved": name, "list": await self._saved(guild)}

    async def _delete(self, member, guild, form) -> dict:
        name = (form.get("name") or "").strip().lower()
        async with self.config.guild(guild).stored_embeds() as stored:
            record = stored.get(name)
            if record is None:
                return {"ok": False, "error": "There is no template called `%s`." % name}
            if record.get("locked") and member.id not in self.bot.owner_ids:
                return {"ok": False, "error": "`%s` is locked." % name}
            del stored[name]
        return {"ok": True, "list": await self._saved(guild)}

    async def _load_message(self, guild, form) -> dict:
        """Pull the content and embeds off a posted message so they can be edited."""
        reference = (form.get("reference") or "").strip()
        match = MESSAGE_LINK.search(reference)
        if not match:
            return {
                "ok": False,
                "error": "Paste a message link (right-click a message, Copy Message Link).",
            }
        if int(match.group("guild")) != guild.id:
            return {"ok": False, "error": "That message is in a different server."}

        channel = guild.get_channel_or_thread(int(match.group("channel")))
        if channel is None:
            return {"ok": False, "error": "I cannot see that channel."}
        try:
            message = await channel.fetch_message(int(match.group("message")))
        except discord.NotFound:
            return {"ok": False, "error": "That message no longer exists."}
        except discord.Forbidden:
            return {"ok": False, "error": "I am not allowed to read that channel."}

        return {
            "ok": True,
            "payload": {
                "content": message.content or "",
                "embeds": [e.to_dict() for e in message.embeds],
            },
        }

    # ------------------------------------------------------------------ data

    def _channels(self, member, guild) -> list:
        """Channels both the member and the bot can post an embed in."""
        out = []
        kinds = (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)
        for channel in sorted(guild.channels, key=lambda c: (c.position, c.name)):
            if not isinstance(channel, kinds):
                continue
            mine = channel.permissions_for(member)
            ours = channel.permissions_for(guild.me)
            if not (mine.send_messages and ours.send_messages and ours.embed_links):
                continue
            out.append(
                {
                    "id": str(channel.id),
                    "name": channel.name,
                    "category": channel.category.name if channel.category else "",
                }
            )
        return out

    async def _saved(self, guild) -> list:
        stored = await self.config.guild(guild).stored_embeds()
        return sorted(
            (
                {
                    "name": name,
                    "uses": int(record.get("uses", 0)),
                    "locked": bool(record.get("locked")),
                    "embed": record.get("embed") or {},
                }
                for name, record in stored.items()
            ),
            key=lambda r: r["name"],
        )
