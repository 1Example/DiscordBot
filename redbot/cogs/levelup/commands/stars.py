import asyncio
import typing as t
from datetime import datetime, timedelta

import discord
from redbot.core import app_commands, commands
from redbot.core.i18n import Translator

from ..abc import MixinMeta
from ..common import formatter
from ..views.dynamic_menu import DynamicMenu

_ = Translator("LevelUp", __file__)


class Stars(MixinMeta):
    @app_commands.command(name="stars", extras={"red_force_enable": True})
    @app_commands.guild_only()
    @app_commands.describe(user="The member to give a star to.")
    async def give_stars(
        self, interaction: discord.Interaction, user: t.Optional[discord.Member] = None
    ):
        """Reward a good noodle"""
        ctx = await commands.Context.from_interaction(interaction)
        if user and user.id == ctx.author.id:
            return await ctx.send(_("You can't give stars to yourself!"), ephemeral=True)
        if user and user.bot and self.db.ignore_bots:
            return await ctx.send(_("You can't give stars to bots!"), ephemeral=True)

        last_used = self.stars.setdefault(ctx.guild.id, {}).get(ctx.author.id)
        conf = self.db.get_conf(ctx.guild)
        now = datetime.now()

        if not user and not last_used:
            # User has not given a star yet, just send help
            return await ctx.send("Name someone to give a star to.")

        elif not user and last_used:
            # User has given a star, but they didnt mention anyone
            can_use_after = last_used + timedelta(seconds=conf.starcooldown)
            if now > can_use_after:
                txt = _("You can give more stars now! Just mention a user in this command.")
            else:
                ts = f"<t:{int(can_use_after.timestamp())}:R>"
                txt = _("You can give more stars {}").format(ts)
            return await ctx.send(txt, ephemeral=True)

        elif not user:
            return await ctx.send(_("You need to mention a user to give them a star!"))
        elif last_used:
            can_use_after = last_used + timedelta(seconds=conf.starcooldown)
            if now < can_use_after:
                ts = f"<t:{int(can_use_after.timestamp())}:R>"
                return await ctx.send(
                    _("You can give more stars {}").format(ts),
                    ephemeral=True,
                )

        self.stars[ctx.guild.id][ctx.author.id] = now
        profile = conf.get_profile(user)
        profile.stars += 1
        if conf.weeklysettings.on:
            weekly = conf.get_weekly_profile(user)
            weekly.stars += 1
        self.save(False)
        name = user.mention if conf.starmention else f"**{user.display_name}**"
        kwargs = {"ephemeral": True}
        if conf.starmentionautodelete:
            kwargs["delete_after"] = conf.starmentionautodelete
        await ctx.send(_("You just gave a star to {}!").format(name), **kwargs)

    @app_commands.command(name="startop", extras={"red_force_enable": True})
    @app_commands.guild_only()
    @app_commands.describe(
        globalstats="Rank across every server instead of this one.",
        displayname="Show nicknames instead of usernames.",
    )
    async def startop(
        self,
        interaction: discord.Interaction,
        globalstats: bool = False,
        displayname: bool = True,
    ):
        """View the Star Leaderboard"""
        ctx = await commands.Context.from_interaction(interaction)
        stat = "stars"
        pages = await asyncio.to_thread(
            formatter.get_leaderboard,
            bot=self.bot,
            guild=ctx.guild,
            db=self.db,
            stat=stat,
            lbtype="lb",
            is_global=globalstats,
            member=ctx.author,
            use_displayname=displayname,
            color=await self.bot.get_embed_color(ctx),
        )
        if isinstance(pages, str):
            return await ctx.send(pages)
        await DynamicMenu(ctx, pages).refresh()


