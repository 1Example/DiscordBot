import asyncio
import random
import typing as t
from io import BytesIO

import discord
from redbot.core import app_commands, commands
from redbot.core.app_commands import checks as app_checks
from redbot.core.i18n import Translator

from ..abc import MixinMeta
from ..common import utils
from ..generator import imgtools, levelalert

_ = Translator("LevelUp", __file__)


class Owner(MixinMeta):


    @app_commands.command(name="mocklvl", extras={"red_force_enable": True})
    @app_checks.is_owner()
    @app_checks.bot_has_permissions(attach_files=True)
    async def test_levelup(self, interaction: discord.Interaction):
        """Test LevelUp Image Generation"""
        ctx = await commands.Context.from_interaction(interaction)
        conf = self.db.get_conf(ctx.guild)
        profile = conf.get_profile(ctx.author)

        async with ctx.typing():
            avatar = await ctx.author.display_avatar.read()
            banner = await ctx.author.banner.read() if ctx.author.banner else None
            if not banner:
                banner_url = await self.get_banner(ctx.author.id)
                if banner_url:
                    banner = await utils.get_content_from_url(banner_url)

            level = random.randint(1, 100)
            fonts = list(imgtools.DEFAULT_FONTS.glob("*.ttf"))
            font = str(random.choice(fonts))
            if profile.font:
                if (self.fonts / profile.font).exists():
                    font = str(self.fonts / profile.font)
                elif (self.custom_fonts / profile.font).exists():
                    font = str(self.custom_fonts / profile.font)

            def _run() -> t.Tuple[bytes, bool]:
                return levelalert.generate_level_img(
                    background_bytes=banner,
                    avatar_bytes=avatar,
                    level=level,
                    font_path=font,
                    color=ctx.author.color.to_rgb(),
                    render_gif=self.db.render_gifs,
                )

            img_bytes, animated = await asyncio.to_thread(_run)
            img_bytes, animated, ext = imgtools.fit_discord_upload_limit(img_bytes, ctx.guild.filesize_limit)
            file = discord.File(BytesIO(img_bytes), filename=f"levelup.{ext}")
            await ctx.send(file=file)
