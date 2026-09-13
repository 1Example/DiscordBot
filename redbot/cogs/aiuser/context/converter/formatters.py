import logging
from typing import Optional

from discord import Message, MessageType

from ...utils.utilities import mention_to_text

logger = logging.getLogger("red.bz_cogs.aiuser.context")


def _reply_hint(message: Message) -> str:
    """Describe who/what a message is replying to, when it's a reply.

    Only the newest triggering message's reply target ever reached the
    model - every other reply sitting in the backread window looked like an
    ordinary new message. When two or more sub-conversations run in the same
    channel at once (a joke thread, a support request), the model had no way
    to tell which one a given message actually continued, and would blend
    them into one reply. Surfacing the reply target for every message fixes
    that at the source instead of trying to prompt around it.
    """
    ref = getattr(message, "reference", None)
    target = getattr(ref, "resolved", None)
    if not isinstance(target, Message):
        return ""
    snippet = (target.content or "").strip().replace("\n", " ")
    if len(snippet) > 80:
        snippet = snippet[:77] + "..."
    if not snippet:
        return f' (replying to {target.author.display_name})'
    return f' (replying to {target.author.display_name}: "{snippet}")'


def format_text_content(message: Message) -> Optional[str]:
    if message.type == MessageType.new_member:
        return f'User "{message.author.display_name}" has joined the server. Their Discord ID is {message.author.id}'
    if not message.content or message.content == "" or message.content.isspace():
        return None
    content = mention_to_text(message)
    reply_hint = _reply_hint(message)
    if message.author.id == message.guild.me.id:
        return f"{reply_hint.strip()} {content}".strip() if reply_hint else content
    return f'User "{message.author.display_name}"{reply_hint} said: {content}'


def format_image_placeholder(message: Message) -> str:
    filenames = ", ".join(
        f'"{attachment.filename}"'
        for attachment in message.attachments
        if (attachment.content_type or "").startswith("image/")
    )
    filenames = filenames or f'"{message.attachments[0].filename}"'
    if message.author.id == message.guild.me.id:
        return f"[Images: {filenames}]"
    return f'User "{message.author.display_name}" sent: [Images: {filenames}]'


async def format_sticker_content(message: Message) -> str:
    try:
        sticker = await message.stickers[0].fetch()
        description = sticker.description or ""
        description_text = f' and description "{description}"' if description else ""
        return f'User "{message.author.display_name}" sent: [Sticker with name "{sticker.name}"{description_text}]'
    except Exception:
        sticker_name = message.stickers[0].name
        return f'User "{message.author.display_name}" sent: [Sticker with name "{sticker_name}"]'
