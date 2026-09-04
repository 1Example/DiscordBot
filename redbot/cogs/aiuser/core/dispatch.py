"""Wires message / slash-command events to reply decisions and the reply queue."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from redbot.core import commands

from ..core.decision import (
    ResponseKind,
    decide_response,
    get_percentage,
    is_valid_message,
)
from ..core.reply_queue import ResponseRequest, get_or_create_channel_reply_state
from ..utils.logging_context import with_discord_log_context

if TYPE_CHECKING:
    from ..core.services import AIUserServices

logger = logging.getLogger("red.bz_cogs.aiuser")


@with_discord_log_context("slash-command")


@with_discord_log_context("message")
async def handle_message(services: "AIUserServices", message: discord.Message):
    """Handle regular message events"""
    if message.author.id == services.bot.user.id:
        return

    ctx: commands.Context = await services.bot.get_context(message)

    decision = await decide_response(services, ctx, message)
    if decision is None:
        return

    state = get_or_create_channel_reply_state(services, ctx.channel.id)
    if decision.kind is ResponseKind.DIRECT:
        await state.cancel_pending_burst()
        await state.enqueue(
            services,
            ResponseRequest(
                kind=ResponseKind.DIRECT,
                channel_id=ctx.channel.id,
                message_id=message.id,
            ),
        )
    else:
        await state.arm_burst(services, ctx, decision.chance, decision.burst_mode)
