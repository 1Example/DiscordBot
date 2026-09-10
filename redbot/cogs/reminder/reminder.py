"""Remind yourself of something later.

Ported from aikaterna's Reminder, itself from ZeLarpMaster's, to this fork's
slash-only commands. The stored shape is unchanged - a list of
{content, start_time, end_time} per user - so data written by the original
carries straight over.

Times are handled as timezone-aware UTC throughout. The original mixed
`datetime.utcnow()` with `datetime.fromtimestamp()`, which reads a stored
timestamp in the machine's *local* zone: on any host not set to UTC, every
reminder restored at startup fired at the wrong time, off by the offset.
"""
from __future__ import annotations

import asyncio
import collections
import datetime
import hashlib
import logging
import re
import typing as t
from itertools import islice
from math import ceil

import discord

from redbot.core import Config, app_commands, commands
from redbot.core.bot import Red
from redbot.core.utils.menus import DEFAULT_CONTROLS, menu

log = logging.getLogger("red.reminder")

PER_PAGE = 7
MAX_CONTENT = 1000


class Reminder(commands.Cog):
    """Utilities to remind yourself of whatever you want."""

    __author__ = ["ZeLarpMaster", "aikaterna"]

    TIME_AMNT_REGEX = re.compile("([1-9][0-9]*)([a-z]+)", re.IGNORECASE)
    TIME_QUANTITIES = collections.OrderedDict(
        [
            ("seconds", 1),
            ("minutes", 60),
            ("hours", 3600),
            ("days", 86400),
            ("weeks", 604800),
            ("months", 2628000),
            ("years", 31540000),
        ]
    )
    MAX_SECONDS = TIME_QUANTITIES["years"] * 2

    remind = app_commands.Group(
        name="remind",
        description="Have me remind you of something later.",
        extras={"red_force_enable": True},
    )
    forget = app_commands.Group(
        name="forget",
        description="Drop reminders you no longer want.",
        parent=remind,
    )

    def __init__(self, bot: Red) -> None:
        super().__init__()
        self.bot = bot
        # Derived exactly as upstream does, from a hash of the original
        # author's name, so that data written by either earlier version of this
        # cog is found by this one. Writing the number out by hand would only
        # be a chance to get it wrong.
        identifier = int(
            hashlib.sha512(("ZeLarpMaster#0818" + "@" + "Reminder").encode()).hexdigest(), 16
        )
        self.config = Config.get_conf(self, identifier=identifier, force_registration=True)
        self.config.register_user(reminders=[], offset=0)
        self.futures: dict[int, list[asyncio.Future]] = {}
        self._startup = asyncio.ensure_future(self.start_saved_reminders())

    async def cog_unload(self) -> None:
        self._startup.cancel()
        for user_futures in self.futures.values():
            for future in user_futures:
                future.cancel()

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        for future in self.futures.pop(user_id, []):
            future.cancel()
        await self.config.user_from_id(user_id).clear()

    async def red_get_data_for_user(self, *, user_id: int) -> dict:
        import io
        import json

        data = await self.config.user_from_id(user_id).all()
        if not data.get("reminders"):
            return {}
        return {"reminders.json": io.BytesIO(json.dumps(data, indent=2).encode())}

    # ---------------- commands ----------------

    @remind.command(name="me", description="Remind me of something later.")
    @app_commands.describe(
        when="How long from now: 10m, 1h30m, 2d, 1y2mo3w. m means minutes, mo means months.",
        text="What to remind you about.",
    )
    async def remind_me(self, interaction: discord.Interaction, when: str, text: str) -> None:
        """Remind yourself of something in a given amount of time."""
        ctx = await commands.Context.from_interaction(interaction)
        seconds = self.get_seconds(when)
        if seconds is None:
            return await ctx.send(
                "I could not read that as a length of time. Try something like `10m`,"
                " `1h30m` or `2d`.",
                ephemeral=True,
            )
        if seconds >= self.MAX_SECONDS:
            return await ctx.send("That is further off than two years.", ephemeral=True)
        text = text.strip()[:MAX_CONTENT]
        if not text:
            return await ctx.send("Tell me what to remind you about.", ephemeral=True)

        now = discord.utils.utcnow()
        end_time = now + datetime.timedelta(seconds=seconds)
        reminder = {
            "content": text,
            "start_time": now.timestamp(),
            "end_time": end_time.timestamp(),
        }
        async with self.config.user(ctx.author).reminders() as reminders:
            reminders.append(reminder)
        future = asyncio.ensure_future(
            self.remind_later(ctx.author, seconds, text, reminder)
        )
        # Tagged so /remind forget one can find and cancel exactly this task.
        future._reminder = reminder
        self.futures.setdefault(ctx.author.id, []).append(future)

        stamp = int(end_time.timestamp() + await self._offset_seconds(ctx.author))
        if seconds > 86400:
            reply = f"\N{WHITE HEAVY CHECK MARK} I will remind you on <t:{stamp}:F>."
        else:
            reply = (
                "\N{WHITE HEAVY CHECK MARK} I will remind you in "
                f"{self.time_from_seconds(seconds)}."
            )
        # Nobody else in the channel needs someone's reminders.
        await ctx.send(reply, ephemeral=True)

    @remind.command(name="list", description="Show the reminders you have set.")
    async def remind_list(self, interaction: discord.Interaction) -> None:
        """List your reminders."""
        ctx = await commands.Context.from_interaction(interaction)
        user_data = await self.config.user(ctx.author).all()
        if not user_data["reminders"]:
            return await ctx.send("You have no reminders set.", ephemeral=True)
        pages = await self.create_remind_list_embeds(ctx, user_data)
        if len(pages) == 1:
            return await ctx.send(embed=pages[0], ephemeral=True)
        # A paged menu needs a message it can edit, so this one is not private.
        await menu(ctx, pages, DEFAULT_CONTROLS)

    @forget.command(name="one", description="Forget a single reminder.")
    @app_commands.describe(number="Its number in /remind list.")
    async def forget_one(
        self, interaction: discord.Interaction, number: app_commands.Range[int, 1, 10_000]
    ) -> None:
        """Forget one reminder, by its number in the list."""
        ctx = await commands.Context.from_interaction(interaction)
        async with self.config.user(ctx.author).all() as user_data:
            if not user_data["reminders"]:
                return await ctx.send("You have no reminders set.", ephemeral=True)
            ordered = sorted(user_data["reminders"], key=lambda r: r["end_time"])
            try:
                removed = ordered.pop(number - 1)
            except IndexError:
                return await ctx.send(
                    f"You have no reminder number {number}.", ephemeral=True
                )
            user_data["reminders"] = ordered
            offset = int(user_data["offset"] * 3600)
        # The task for it is still pending, so cancel it or it fires anyway.
        self._cancel_future(ctx.author.id, removed)
        stamp = round(removed["end_time"] + offset)
        await ctx.send(
            f"\N{PUT LITTER IN ITS PLACE SYMBOL} Forgot reminder **#{number}**\n"
            f"Was due <t:{stamp}:f>\nContent: `{removed['content']}`",
            ephemeral=True,
        )

    @forget.command(name="all", description="Forget every reminder you have set.")
    async def forget_all(self, interaction: discord.Interaction) -> None:
        """Forget all of your reminders."""
        ctx = await commands.Context.from_interaction(interaction)
        for future in self.futures.pop(ctx.author.id, []):
            future.cancel()
        async with self.config.user(ctx.author).reminders() as reminders:
            count = len(reminders)
            reminders.clear()
        if not count:
            return await ctx.send("You had no reminders set.", ephemeral=True)
        await ctx.send(
            f"\N{PUT LITTER IN ITS PLACE SYMBOL} Forgot all {count} of your reminders.",
            ephemeral=True,
        )

    @remind.command(name="offset", description="Set your hours from UTC, for /remind list.")
    @app_commands.describe(hours="Between -23.75 and 23.75. For example -5 or +5.5.")
    async def remind_offset(self, interaction: discord.Interaction, hours: str) -> None:
        """Set a plain hour offset from UTC, used when listing your reminders."""
        ctx = await commands.Context.from_interaction(interaction)
        offset = self.remind_offset_check(hours)
        if offset is None:
            return await ctx.send(
                "That is not an hour offset I can read. Give something between"
                " -23.75 and 23.75, such as `-5` or `+5.5`.",
                ephemeral=True,
            )
        await self.config.user(ctx.author).offset.set(offset)
        shown = str(offset).replace(".0", "")
        await ctx.send(f"Your offset is now {shown} hours from UTC.", ephemeral=True)

    # ---------------- internals ----------------

    async def _offset_seconds(self, user: discord.abc.User) -> int:
        return int(await self.config.user(user).offset() * 3600)

    def _cancel_future(self, user_id: int, reminder: dict) -> None:
        """Cancel the pending task for one reminder, if it can be identified."""
        for future in list(self.futures.get(user_id, [])):
            if getattr(future, "_reminder", None) == reminder:
                future.cancel()
                self.futures[user_id].remove(future)
                return

    def get_seconds(self, time: str) -> t.Optional[int]:
        """The length of time `time` describes, or None if it describes none."""
        seconds = 0
        for match in self.TIME_AMNT_REGEX.finditer(time):
            amount = int(match.group(1))
            abbrev = match.group(2)
            quantity = discord.utils.find(
                lambda q: q[0].startswith(abbrev), self.TIME_QUANTITIES.items()
            )
            if quantity is not None:
                seconds += amount * quantity[1]
        return None if seconds == 0 else seconds

    async def remind_later(
        self, user: discord.abc.User, time: float, content: str, reminder: dict
    ) -> None:
        """Sleep, then DM the reminder, then drop it from storage."""
        await asyncio.sleep(time)
        try:
            embed = discord.Embed(
                title="Reminder", description=content, color=discord.Colour.blue()
            )
            await user.send(embed=embed)
        except discord.HTTPException as e:
            # Closed DMs used to leave the reminder in storage, where it was
            # restored on every restart and failed again forever.
            log.info("Could not deliver a reminder to %s: %s", user, e)
        finally:
            async with self.config.user(user).reminders() as reminders:
                if reminder in reminders:
                    reminders.remove(reminder)

    @staticmethod
    def remind_offset_check(offset: str) -> t.Optional[float]:
        try:
            value = float(offset.replace("+", "").strip())
        except ValueError:
            return None
        value = round(value * 4) / 4.0
        if not -23.75 <= value <= 23.75:
            return None
        return value

    async def start_saved_reminders(self) -> None:
        """Re-arm everything that was pending when the bot last stopped."""
        await self.bot.wait_until_red_ready()
        try:
            user_configs = await self.config.all_users()
        except Exception:
            log.exception("Could not read saved reminders")
            return
        now = discord.utils.utcnow()
        for user_id, user_config in list(user_configs.items()):
            user = self.bot.get_user(user_id)
            if user is None:
                # No mutual server any more, so there is nowhere to deliver.
                await self.config.user_from_id(user_id).clear()
                continue
            for reminder in user_config["reminders"]:
                end = datetime.datetime.fromtimestamp(
                    reminder["end_time"], tz=datetime.timezone.utc
                )
                delay = max(0.0, (end - now).total_seconds())
                future = asyncio.ensure_future(
                    self.remind_later(user, delay, reminder["content"], reminder)
                )
                future._reminder = reminder
                self.futures.setdefault(user_id, []).append(future)

    @staticmethod
    async def chunker(rows: list, chunk_size: int) -> list[list]:
        chunks = []
        iterator = iter(rows)
        while chunk := list(islice(iterator, chunk_size)):
            chunks.append(chunk)
        return chunks

    async def create_remind_list_embeds(
        self, ctx: commands.Context, user_data: dict
    ) -> list[discord.Embed]:
        offset = int(user_data["offset"] * 3600)
        ordered = sorted(user_data["reminders"], key=lambda r: r["end_time"])
        width = len(str(len(ordered)))

        rows = []
        for index, reminder in enumerate(ordered, 1):
            end = round(reminder["end_time"] + offset)
            content = reminder["content"]
            shown = content if len(content) < 200 else f"{content[:200]} [...]"
            rows.append(
                f"`{str(index).zfill(width)}`. <t:{end}:f>, <t:{end}:R>:\n{shown}\n\n"
            )

        pages = await self.chunker(rows, PER_PAGE)
        total = ceil(len(rows) / PER_PAGE)
        shown_offset = str(user_data["offset"]).replace(".0", "")
        offset_text = f" • UTC offset of {shown_offset}h applied" if offset else ""
        embeds = []
        for chunk in pages:
            embed = discord.Embed(description="".join(chunk))
            # display_avatar, because avatar is None for anyone still on a
            # default one and the original raised AttributeError on them.
            embed.set_author(
                name=f"Reminders for {ctx.author}", icon_url=ctx.author.display_avatar.url
            )
            embed.set_footer(text=f"Page {len(embeds) + 1} of {total}{offset_text}")
            embeds.append(embed)
        return embeds

    @staticmethod
    def time_from_seconds(seconds: int) -> str:
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        parts = []
        if hours:
            parts.append(f"{hours} hour" + ("" if hours == 1 else "s"))
        if minutes:
            parts.append(f"{minutes} minute" + ("" if minutes == 1 else "s"))
        if seconds and not hours:
            parts.append(f"{seconds} second" + ("" if seconds == 1 else "s"))
        if not parts:
            return "no time at all"
        if len(parts) == 1:
            return parts[0]
        return " and ".join((", ".join(parts[:-1]), parts[-1]))
