import io
import re
import aiohttp
import discord
from typing import Optional, Union, List
from redbot.core import commands, app_commands
from .dashboard_integration import DashboardIntegration

IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
STICKER_KB = 512
STICKER_DIM = 320
STICKER_TIME = 5

MISSING_EMOJIS = "Can't find emojis or stickers in that message."
MISSING_REFERENCE = "Reply to a message using this command to steal an emote."
MESSAGE_FAIL = "I can't find that message, sorry."
UPLOADED_BY = "Uploaded by"
STICKER_DESC = "Stolen sticker"
STICKER_EMOJI = "😶"
STICKER_FAIL = "❌ Failed to upload sticker"
STICKER_SUCCESS = "✅ Uploaded sticker"
STICKER_SLOTS = "⚠️ This server doesn't have any more space for stickers!"
EMOJI_FAIL = "❌ Failed to upload"
EMOJI_SLOTS = "⚠️ This server doesn't have any more space for emojis!"
INVALID_EMOJI = "Invalid emoji or emoji ID."
STICKER_TOO_BIG = f"Stickers may only be up to {STICKER_KB} KB and {STICKER_DIM}x{STICKER_DIM} pixels and last up to {STICKER_TIME} seconds."
STICKER_ATTACHMENT = """\
>>> For a non-moving sticker, simply use this command and attach a PNG image.
For a moving sticker, Discord limitations make it very annoying. Follow these steps:
1. Scale down and optimize your video/gif in <https://ezgif.com>
2. Convert it to APNG in that same website.
3. Download it and put it inside a zip file.
4. Use this command and attach that zip file.
This prevents Discord from replacing your animated PNG with a static PNG.
\n**Important:** """ + STICKER_TOO_BIG


class EmojiSteal(DashboardIntegration, commands.Cog):
    """Steals emojis and stickers sent by other people and optionally uploads them to your own server."""

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        # Force-enabled, or Red's [p]slash gate keeps them hidden - and with
        # the prefix commands gone these are the only way in.
        self.steal_context_menu = app_commands.ContextMenu(
            name="Steal Emotes",
            callback=self.steal_app_command,
            extras={"red_force_enable": True},
        )
        self.steal_upload_context_menu = app_commands.ContextMenu(
            name="Steal+Upload Emotes",
            callback=self.steal_upload_app_command,
            extras={"red_force_enable": True},
        )
        self.bot.tree.add_command(self.steal_context_menu)
        self.bot.tree.add_command(self.steal_upload_context_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.steal_context_menu.name, type=self.steal_context_menu.type)
        self.bot.tree.remove_command(self.steal_upload_context_menu.name, type=self.steal_upload_context_menu.type)

    @staticmethod
    def get_emojis(content: str) -> Optional[List[discord.PartialEmoji]]:
        results = re.findall(r"(<(a?):(\w+):(\d{10,20})>)", content)
        return [discord.PartialEmoji.from_str(result[0]) for result in results]
    
    @staticmethod
    def available_emoji_slots(guild: discord.Guild, animated: bool) -> int:
        current_emojis = len([em for em in guild.emojis if em.animated == animated])
        return guild.emoji_limit - current_emojis

    async def steal_ctx(self, ctx: commands.Context) -> Optional[Union[List[discord.PartialEmoji], List[discord.StickerItem]]]:
        reference = ctx.message.reference
        if not reference or not reference.message_id:
            await ctx.send(MISSING_REFERENCE)
            return None
        message = await ctx.channel.fetch_message(reference.message_id)
        if not message:
            await ctx.send(MESSAGE_FAIL)
            return None
        if message.stickers:
            return message.stickers
        if not (emojis := self.get_emojis(message.content)):
            await ctx.send(MISSING_EMOJIS)
            return None
        return emojis


    # context menu added in __init__
    async def steal_app_command(self, ctx: discord.Interaction, message: discord.Message):
        if message.stickers:
            emojis = message.stickers
        elif not (emojis := self.get_emojis(message.content)):
            return await ctx.response.send_message(MISSING_EMOJIS, ephemeral=True)

        response = '\n'.join([emoji.url for emoji in emojis])
        await ctx.response.send_message(content=response, ephemeral=True)


    # context menu added in __init__
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_emojis=True)
    @app_commands.checks.bot_has_permissions(manage_emojis=True)
    async def steal_upload_app_command(self, ctx: discord.Interaction, message: discord.Message):
        assert ctx.guild
        if message.stickers:
            emojis_or_stickers = message.stickers
        else:
            emojis_or_stickers = self.get_emojis(message.content)

        if not emojis_or_stickers:
            return await ctx.response.send_message(MISSING_EMOJIS, ephemeral=True)
        
        await ctx.response.defer(thinking=True)
        
        if isinstance(emojis_or_stickers[0], discord.StickerItem):
            if len(ctx.guild.stickers) >= ctx.guild.sticker_limit:
                return await ctx.edit_original_response(content=STICKER_SLOTS)

            sticker = emojis_or_stickers[0]
            fp = io.BytesIO()
            try:
                await sticker.save(fp)
                await ctx.guild.create_sticker(
                    name=sticker.name, description=STICKER_DESC, emoji=STICKER_EMOJI, file=discord.File(fp))

            except discord.DiscordException as error:
                return await ctx.edit_original_response(content=f"{STICKER_FAIL}, {type(error).__name__}: {error}")

            return await ctx.edit_original_response(content=f"{STICKER_SUCCESS}: {sticker.name}")

        added_emojis = []
        emojis: List[discord.PartialEmoji] = list(dict.fromkeys(emojis_or_stickers))  # type: ignore
        async with aiohttp.ClientSession() as session:
            for emoji in emojis:
                if not self.available_emoji_slots(ctx.guild, emoji.animated):
                    response = EMOJI_SLOTS
                    if added_emojis:
                        response = ' '.join([str(e) for e in added_emojis]) + '\n' + response
                    return await ctx.edit_original_response(content=response)

                try:
                    async with session.get(emoji.url) as resp:
                        resp.raise_for_status()
                        image = io.BytesIO(await resp.read()).read()
                    added = await ctx.guild.create_custom_emoji(name=emoji.name, image=image)

                except (aiohttp.ClientError, discord.DiscordException) as error:
                    response = f"{EMOJI_FAIL} {emoji.name}, {type(error).__name__}: {error}"
                    if added_emojis:
                        response = ' '.join([str(e) for e in added_emojis]) + '\n' + response
                    return await ctx.edit_original_response(content=response)

                added_emojis.append(added)
        
        response = ' '.join([str(e) for e in added_emojis])
        await ctx.edit_original_response(content=response)


