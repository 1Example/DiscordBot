"""A small base for cogs that track views and background loops.

Five cogs in this fork were built on AAA3A_utils' `Cog`. What they actually
took from it is the four things below. What they also got, on every load, was
a `SharedCog` injected into the bot carrying a hidden hybrid command group, a
Sentry client, and a GET to a third-party hit counter - none of which any of
them asked for, and the first of which puts prefix commands back into a bot
that is in the middle of removing them.

This has the four things.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import tasks

from redbot.core import commands
from redbot.core.bot import Red

__all__ = ["CogBase"]


class CogBase(commands.Cog):
    """`self.bot`, a logger, a view registry and a loop registry.

    Anything in `self.views` is stopped and dropped when the cog unloads, and
    anything in `self.loops` is cancelled, so an unloaded cog leaves nothing
    running behind it.
    """

    def __init__(self, bot: Red) -> None:
        super().__init__()
        self.bot: Red = bot
        self.logger: logging.Logger = logging.getLogger(
            f"red.{type(self).__name__.lower()}"
        )
        self.loops: list[tasks.Loop] = []
        # A str key is a persistent view that belongs to no one message.
        self.views: dict[
            discord.Message | discord.PartialMessage | str, discord.ui.View
        ] = {}

    async def cog_unload(self) -> None:
        for loop in self.loops:
            loop.cancel()
        self.loops.clear()
        for view in self.views.values():
            if view.is_finished():
                continue
            try:
                # on_timeout is how these views tidy up after themselves -
                # settling an unfinished game, disabling buttons - and an
                # unload is the last chance they get to run it.
                await view.on_timeout()
            except Exception:
                self.logger.exception("A view failed to clean up on unload.")
            view.stop()
        self.views.clear()
