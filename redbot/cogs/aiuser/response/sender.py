"""
Delivery of a finished pipeline result to the Discord channel.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional, Set, Tuple

import discord
from discord.utils import MISSING
from redbot.core import commands

from ..config.constants import REGEX_RUN_TIMEOUT
from ..utils.utilities import to_thread
from ..utils.reply_metadata import strip_reply_context_prefix

if TYPE_CHECKING:
    from ..core.services import AIUserServices
    from ..response.pipeline import PipelineResult

logger = logging.getLogger("red.bz_cogs.aiuser")

DISCORD_MESSAGE_CHARACTERS_LIMIT = 2000
REPLY_IF_OLDER_THAN_SECONDS = 8
RANDOM_REPLY_CHANCE = 0.25


async def deliver(
    services: "AIUserServices",
    ctx: commands.Context,
    result: "PipelineResult",
    can_reply: bool,
    force_reply: bool = False,
) -> Optional[discord.Message]:
    response = ""
    mentioned_members: List[discord.Member] = []
    if result.completion:
        response = await _remove_patterns_from_response(
            ctx, services, result.completion
        )
        response, mentioned_members = _linkify_mentions(ctx, response)
    if not response and not result.files_to_send:
        return None

    files = result.files_to_send
    allowed_users = {ctx.message.author.id: ctx.message.author}
    for member in mentioned_members:
        allowed_users.setdefault(member.id, member)
    allowed = discord.AllowedMentions(
        everyone=False, roles=False, users=list(allowed_users.values())
    )
    chunks = _chunk_message(response)
    last = len(chunks) - 1

    first_files = files if last == 0 else MISSING
    if can_reply and (force_reply or await _should_reply(ctx)):
        try:
            sent_message = await ctx.message.reply(
                chunks[0],
                mention_author=False,
                allowed_mentions=allowed,
                files=first_files,
            )
        except discord.HTTPException:
            # trigger message got deleted; deliver without the reply link
            sent_message = await ctx.send(
                chunks[0], allowed_mentions=allowed, files=first_files
            )
    elif ctx.interaction:
        sent_message = await ctx.interaction.followup.send(
            chunks[0], allowed_mentions=allowed, files=first_files, wait=True
        )
    else:
        sent_message = await ctx.send(
            chunks[0], allowed_mentions=allowed, files=first_files
        )

    for idx, chunk in enumerate(chunks[1:], start=1):
        sent_message = await ctx.send(
            chunk, allowed_mentions=allowed, files=files if idx == last else MISSING
        )

    return sent_message


def _linkify_mentions(
    ctx: commands.Context, text: str
) -> Tuple[str, List[discord.Member]]:
    """Turn @DisplayName text the model wrote into a real Discord mention.

    Incoming mentions are converted to plain "@DisplayName" text for the
    model to read (see mention_to_text() / format_text_content()), since it
    has no way to understand a raw <@id> snowflake. The model naturally
    imitates that exact style back in its own output - but plain
    "@DisplayName" text was never converted back. It doesn't ping, doesn't
    render as a clickable pill, and isn't distinguishable from someone just
    typing an "@" followed by a name. This resolves any @DisplayName that
    matches an actual member into a real mention, and reports who got
    mentioned so the caller can actually allow that ping through - a
    generic prompt instruction can't make text into a real mention; only
    code that knows the guild's member list can.
    """
    if not ctx.guild or "@" not in text:
        return text, []

    # Longest display name first, so "Baca - Frugerul Gros" is matched
    # whole before a shorter "Baca" would grab just the prefix.
    candidates = sorted(
        (m for m in ctx.guild.members if not m.bot and m.display_name),
        key=lambda m: len(m.display_name),
        reverse=True,
    )

    mentioned: List[discord.Member] = []
    for member in candidates:
        pattern = re.compile(
            r"(?<!\w)@" + re.escape(member.display_name) + r"(?!\w)", re.IGNORECASE
        )
        if pattern.search(text):
            text = pattern.sub(member.mention, text)
            mentioned.append(member)

    return text, mentioned



async def _should_reply(ctx: commands.Context) -> bool:
    if ctx.interaction:
        return False

    age_seconds = (datetime.now(timezone.utc) - ctx.message.created_at).total_seconds()
    if age_seconds > REPLY_IF_OLDER_THAN_SECONDS:
        return True
    if random.random() < RANDOM_REPLY_CHANCE:
        return True

    # if the latest channel message is our own, reply to show which
    # message this answers
    async for last_msg in ctx.message.channel.history(limit=1):
        if last_msg.author == ctx.guild.me:
            return True
    return False


def _chunk_message(response: str) -> List[str]:
    if not response:
        return [""]

    chunks: List[str] = []
    remaining = response
    while len(remaining) > DISCORD_MESSAGE_CHARACTERS_LIMIT:
        cut = remaining.rfind("\n", 1, DISCORD_MESSAGE_CHARACTERS_LIMIT)
        if cut == -1:
            cut = remaining.rfind(" ", 1, DISCORD_MESSAGE_CHARACTERS_LIMIT)
        if cut == -1:
            cut = DISCORD_MESSAGE_CHARACTERS_LIMIT
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip(" \n")
    if remaining:
        chunks.append(remaining)
    return chunks


# --- cleanup ---


async def _remove_patterns_from_response(
    ctx: commands.Context, services: "AIUserServices", response: str
) -> str:
    """Strip internal reply labels and the guild's removelist regexes."""
    cleaned = strip_reply_context_prefix(response.strip(" \n"))
    patterns = await services.config.guild(ctx.guild).removelist_regexes()
    if not patterns:
        return cleaned

    botname = ctx.guild.me.nick or ctx.guild.me.display_name
    patterns = [p.replace(r"{botname}", re.escape(botname)) for p in patterns]
    patterns = await _expand_authorname_patterns(ctx, patterns)

    for pattern in patterns:
        try:
            cleaned = await _compile_and_apply(pattern, cleaned)
        except asyncio.TimeoutError:
            logger.warning(f"Timeout applying regex pattern: {pattern}")
        except Exception:
            logger.warning(f"Error applying regex pattern: {pattern}", exc_info=True)
    # A configured cleanup may have removed a speaker prefix in front of a label.
    return strip_reply_context_prefix(cleaned)


async def _expand_authorname_patterns(
    ctx: commands.Context, patterns: List[str]
) -> List[str]:
    """Turn each {authorname} pattern into one pattern per recent chatter.

    Author names come from a channel history fetch; only pay for it when
    some pattern actually uses them.
    """
    if not any("{authorname}" in pattern for pattern in patterns):
        return patterns

    authors: Set[str] = {
        msg.author.display_name
        async for msg in ctx.channel.history(limit=10)
        if msg.author != ctx.guild.me
    }

    expanded: List[str] = []
    for pattern in patterns:
        if "{authorname}" in pattern:
            expanded.extend(
                pattern.replace(r"{authorname}", re.escape(author))
                for author in authors
            )
        else:
            expanded.append(pattern)
    return expanded


@to_thread(timeout=REGEX_RUN_TIMEOUT)
def _compile_and_apply(pattern_str: str, text: str) -> str:
    pattern = re.compile(pattern_str)
    return pattern.sub("", text).strip(" \n")
