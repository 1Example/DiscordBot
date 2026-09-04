import asyncio

import discord
from redbot.core import app_commands, commands
from redbot.core.app_commands import checks as app_checks
from redbot.core.i18n import Translator

from ..abc import MixinMeta
from ..common import formatter
from ..views.dynamic_menu import DynamicMenu

_ = Translator("LevelUp", __file__)


class Weekly(MixinMeta):
    @app_commands.command(name="weekly", extras={"red_force_enable": True})
    @app_commands.guild_only()
    @app_commands.describe(
        stat="Which statistic to rank by.",
        displayname="Show nicknames instead of usernames.",
    )
    async def weekly(
        self,
        interaction: discord.Interaction,
        stat: str = "exp",
        displayname: bool = True,
    ):
        """View Weekly Leaderboard"""
        ctx = await commands.Context.from_interaction(interaction)
        conf = self.db.get_conf(ctx.guild)
        if not conf.weeklysettings.on:
            txt = _("Weekly stats are not enabled on this server")
            return await ctx.send(txt)
        stat = stat.lower()
        pages = await asyncio.to_thread(
            formatter.get_leaderboard,
            bot=self.bot,
            guild=ctx.guild,
            db=self.db,
            stat=stat,
            lbtype="weekly",
            is_global=False,
            member=ctx.author,
            use_displayname=displayname,
            color=await self.bot.get_embed_color(ctx),
        )
        if isinstance(pages, str):
            return await ctx.send(pages)
        await DynamicMenu(ctx, pages).refresh()

    @app_commands.command(name="lastweekly", extras={"red_force_enable": True})
    @app_commands.guild_only()
    @app_checks.bot_has_permissions(embed_links=True)
    async def lastweekly(self, interaction: discord.Interaction):
        """View Last Week's Leaderboard"""
        ctx = await commands.Context.from_interaction(interaction)
        conf = self.db.get_conf(ctx.guild)
        if not conf.weeklysettings.on:
            return await ctx.send(_("Weekly stats are not enabled on this server"))
        if not conf.weeklysettings.last_embed:
            return await ctx.send(_("There is no recorded weekly embed saved"))
        embed = discord.Embed.from_dict(conf.weeklysettings.last_embed)
        embed.title = _("Last Weekly Leaderboard")
        new_desc = _("{}\n`Last Reset:      `{}").format(embed.description, f"<t:{conf.weeklysettings.last_reset}:R>")
        embed.description = new_desc
        await ctx.send(embed=embed)


