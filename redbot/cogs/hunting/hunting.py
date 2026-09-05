import asyncio
import logging
import math
import random
from datetime import datetime
from typing import Literal

import discord
from discord import app_commands
from redbot.core import Config, bank, commands
from redbot.core.bot import Red
from redbot.core.errors import BalanceTooHigh
from redbot.core.utils.chat_formatting import (
    bold,
    box,
    humanize_list,
    humanize_number,
    pagify,
)
from redbot.core.utils.menus import DEFAULT_CONTROLS, menu
from .dashboard_integration import DashboardIntegration

__version__ = "3.5.2"
log = logging.getLogger("red.vrt.hunting")


class Hunting(DashboardIntegration, commands.Cog):
    """Hunting, it hunts birds and things that fly."""

    async def red_delete_data_for_user(
        self,
        *,
        requester: Literal["discord", "owner", "user", "user_strict"],
        user_id: int,
    ):
        await self.config.user_from_id(user_id).clear()

    def __init__(self, bot, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bot: Red = bot
        self.config = Config.get_conf(self, 2784481002, force_registration=True)

        self.animals = {
            "dove": ":dove: **_Coo!_**",
            "penguin": ":penguin: **_Noot!_**",
            "chicken": ":chicken: **_Bah-gawk!_**",
            "duck": ":duck: **_Quack!_**",
            "turkey": ":turkey: **_Gobble-Gobble!_**",
            "owl": ":owl: **_Hoo-Hooo!_**",
            "eagle": ":eagle: **_Caw!_**",
            "dodo": ":dodo: **_Squak!_**",
        }
        self.in_game = set()

        self.next_bang = {}

        default_guild = {
            "hunt_interval_minimum": 900,
            "hunt_interval_maximum": 3600,
            "wait_for_bang_timeout": 20,
            "channels": [],
            "bang_time": False,
            "bang_words": True,
            "reward_range": [],
            "eagle": False,  # Lose credits for shooting
        }
        default_global = {
            "reward_range": [],  # For bots with global banks
        }
        default_user = {"score": {}, "total": 0}
        self.config.register_user(**default_user)
        self.config.register_guild(**default_guild)
        self.config.register_global(**default_global)

    hunting = app_commands.Group(
        name="hunting",
        description="Hunting, it hunts birds and things that fly.",
        extras={"red_force_enable": True},
        guild_only=True,
    )

    @hunting.command(
        name="leaderboard", description="Show the top hunters."
    )
    @app_commands.checks.bot_has_permissions(embed_links=True)
    @app_commands.describe(
        global_leaderboard="Show every server rather than just this one."
    )
    async def leaderboard(
        self, interaction: discord.Interaction, global_leaderboard: bool = False
    ):
        """
        This will show the top 50 hunters for the server.
        Use True for the global_leaderboard variable to show the global leaderboard.
        """
        ctx = await commands.Context.from_interaction(interaction)
        userinfo = await self.config.all_users()
        if not userinfo:
            return await ctx.send(bold("Please shoot something before you can brag about it."))

        async with ctx.typing():
            sorted_acc: list = sorted(userinfo.items(), key=lambda x: (x[1]["total"]), reverse=True)[:50]

        if not hasattr(ctx.guild, "members"):
            global_leaderboard = True

        # pound_len = len(str(len(sorted_acc)))
        score_len = 10
        header = "{score:{score_len}}{name:2}\n".format(
            score="# Birds Shot",
            score_len=score_len + 5,
            name="Name" if str(ctx.author.mobile_status) not in ["online", "idle", "dnd"] else "Name",
        )
        temp_msg = header
        for account in sorted_acc:
            if account[1]["total"] == 0:
                continue
            if global_leaderboard or (account[0] in [member.id for member in ctx.guild.members]):
                user_obj = self.bot.get_user(account[0]) or account[0]
            else:
                continue
            if isinstance(user_obj, discord.User) and len(str(user_obj)) > 28:
                user_name = f"{user_obj.name[:19]}...#{user_obj.discriminator}"
            else:
                user_name = str(user_obj)
            if user_obj == ctx.author:
                temp_msg += f"{humanize_number(account[1]['total']) + '   ': <{score_len + 4}} <<{user_name}>>\n"
            else:
                temp_msg += f"{humanize_number(account[1]['total']) + '   ': <{score_len + 4}} {user_name}\n"

        page_list = []
        pages = 1
        for page in pagify(temp_msg, delims=["\n"], page_length=800):
            if global_leaderboard:
                title = "Global Hunting Leaderboard"
            else:
                title = f"Hunting Leaderboard For {ctx.guild.name}"
            embed = discord.Embed(
                colour=await ctx.bot.get_embed_color(location=ctx.channel),
                description=box(title, lang="prolog") + (box(page, lang="md")),
            )
            embed.set_footer(text=f"Page {humanize_number(pages)}/{humanize_number(math.ceil(len(temp_msg) / 800))}")
            pages += 1
            page_list.append(embed)
        if len(page_list) == 1:
            await ctx.send(embed=page_list[0])
        else:
            await menu(ctx, page_list, DEFAULT_CONTROLS)

    @hunting.command(name="score", description="Show a hunter's score.")
    @app_commands.describe(member="Whose score. Defaults to yours.")
    async def score(
        self, interaction: discord.Interaction, member: discord.Member = None
    ):
        """This will show the score of a hunter."""
        ctx = await commands.Context.from_interaction(interaction)
        if not member:
            member = ctx.author
        score = await self.config.user(member).score()
        total = 0
        kill_list = []
        message = "Something went wrong?"
        if not score:
            message = "Please shoot something before you can brag about it."

        for animal in score.items():
            total = total + animal[1]
            if animal[1] == 1 or animal[0][-1] == "s":
                kill_list.append(f"{animal[1]} {animal[0].capitalize()}")
            else:
                kill_list.append(f"{animal[1]} {animal[0].capitalize()}s")
            message = f"{member.name} shot a total of {total} animals ({humanize_list(kill_list)})"
        await ctx.send(bold(message))

    async def add_score(self, author: discord.User, avian: str):
        user_data = await self.config.user(author).all()
        try:
            user_data["score"][avian] += 1
        except KeyError:
            user_data["score"][avian] = 1
        user_data["total"] += 1
        await self.config.user(author).set_raw(value=user_data)

    async def do_tha_bang(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        conf: dict,
        wait: int,
    ):
        try:
            await asyncio.sleep(wait)
            await self._wait_for_bang(guild, channel, conf)
        except Exception as e:
            log.error("Failed to wait for bang", exc_info=e)
        finally:
            self.in_game.discard(channel.id)

    async def _wait_for_bang(self, guild: discord.Guild, channel: discord.TextChannel, conf: dict):
        bang = ["ðŸ’¥", "\N{COLLISION SYMBOL}"]
        salute = ["ðŸ«¡", "\N{SALUTING FACE}"]
        animal = random.choice(list(self.animals.keys()))
        animal_message = await channel.send(self.animals[animal])

        def bang_mcheck(m: discord.Message):
            if m.guild != guild or m.channel != channel or not m.content:
                return False
            return "bang" in m.content.lower().strip()

        def salute_mcheck(m: discord.Message):
            if m.guild != guild or m.channel != channel or not m.content:
                return False
            return any(s in m.content.lower().strip() for s in salute)

        def bang_rcheck(r: discord.Reaction, u: discord.Member):
            if u.bot or r.message.guild != guild or r.message.channel != channel:
                return False
            return str(r.emoji) in bang

        def salute_rcheck(r: discord.Reaction, u: discord.Member):
            if u.bot or r.message.guild != guild or r.message.channel != channel:
                return False
            return str(r.emoji) in salute

        now = datetime.now().timestamp()
        timeout = conf["wait_for_bang_timeout"]
        # Wait for whatever comes first, a message with bang or a reaction with bang emoji
        # Use asyncio.FIRST_COMPLETED to return the first completed future
        futures: list[asyncio.Future] = []
        futures.append(asyncio.ensure_future(self.bot.wait_for("message", check=bang_mcheck)))
        futures.append(asyncio.ensure_future(self.bot.wait_for("reaction_add", check=bang_rcheck)))
        if animal == "eagle":
            futures.append(asyncio.ensure_future(self.bot.wait_for("message", check=salute_mcheck)))
            futures.append(asyncio.ensure_future(self.bot.wait_for("reaction_add", check=salute_rcheck)))

        if not conf["bang_words"]:
            await animal_message.add_reaction("\N{COLLISION SYMBOL}")

        escaped = f"The {animal} flew away!"
        if animal == "penguin":
            escaped = "The penguin waddled away!"

        try:
            done, pending = await asyncio.wait(futures, return_when=asyncio.FIRST_COMPLETED, timeout=timeout)
        except asyncio.TimeoutError:
            return await channel.send(escaped)
        try:
            for future in pending:
                future.cancel()
        except Exception as e:
            log.info(f"Failed to cancel pending futures: {e}")
        if not done:
            return await channel.send(escaped)
        res = done.pop().result()

        if isinstance(res, discord.Message):
            author: discord.Member = res.author
            saluted = False
            if any(s in res.content.lower().strip() for s in salute) and animal == "eagle":
                saluted = True
        else:
            reaction: discord.Reaction = res[0]
            author: discord.Member = res[1]
            saluted = False
            if str(reaction.emoji) in salute and animal == "eagle":
                saluted = True

        bang_now = datetime.now().timestamp()
        time_for_bang = round(bang_now - now, 1)
        bangtime = "" if not await self.config.guild(guild).bang_time() else f" in {time_for_bang}s"

        if random.randrange(0, 17) > 1:
            if animal == "eagle" and conf["eagle"]:
                # Shooting bad, salute good
                if saluted:
                    await self.add_score(author, animal)
                    reward = await self.maybe_send_reward(guild, author)
                    if reward:
                        cur_name = await bank.get_currency_name(guild)
                        msg = f"{author.display_name} saluted the eagle{bangtime} and earned {reward} {cur_name}!"
                    else:
                        msg = f"{author.display_name} saluted the eagle{bangtime}!"
                else:
                    punish = await self.maybe_send_reward(guild, author, True)
                    if punish:
                        cur_name = await bank.get_currency_name(guild)
                        msg = f"Oh no! {author.display_name} shot an eagle{bangtime} and paid {punish} {cur_name} in fines!"
                    else:
                        msg = f"Oh no! {author.display_name} shot an eagle{bangtime}!"
            else:
                await self.add_score(author, animal)
                reward = await self.maybe_send_reward(guild, author)
                if reward:
                    cur_name = await bank.get_currency_name(guild)
                    msg = f"{author.display_name} shot a {animal}{bangtime} and earned {reward} {cur_name}!"
                else:
                    msg = f"{author.display_name} shot a {animal}{bangtime}!"
        else:
            msg = f"{author.display_name} missed the shot and the {animal} got away!"
            if conf["eagle"] and animal == "eagle" and saluted:
                msg = f"{author.display_name} saluted the eagle but it just flew away!"

        await channel.send(bold(msg))

    async def maybe_send_reward(self, guild, author, take: bool = False) -> int:
        if await bank.is_global():
            amounts = await self.config.reward_range()
        else:
            amounts = await self.config.guild(guild).reward_range()

        if amounts:
            to_give_take = random.randint(amounts[0], amounts[1] + 1)
        else:
            to_give_take = 0
        user_bal = await bank.get_balance(author)
        if take:
            if to_give_take > user_bal:
                to_give_take = user_bal
            await bank.withdraw_credits(author, to_give_take)
        else:
            max_bal = await bank.get_max_balance(guild)
            if to_give_take + user_bal > max_bal:
                to_give_take = max_bal - user_bal
            try:
                await bank.deposit_credits(author, to_give_take)
            except BalanceTooHigh as e:  # This shouldn't throw since we already compare to max bal
                await bank.set_balance(author, e.max_balance)
        return to_give_take

    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.guild:
            return
        if message.author.bot:
            return
        if not message.channel.permissions_for(message.guild.me).send_messages:
            return
        if message.channel.id in self.in_game:
            return

        guild_data = await self.config.guild(message.guild).all()
        if not guild_data["channels"]:
            return
        if message.channel.id not in guild_data["channels"]:
            return

        wait_time = random.randint(
            guild_data["hunt_interval_minimum"],
            guild_data["hunt_interval_maximum"],
        )
        if message.guild.id not in self.next_bang:
            self.next_bang[message.guild.id] = datetime.now().timestamp() + wait_time
            return

        n = self.next_bang[message.guild.id]
        if datetime.now().timestamp() < n:
            return

        self.in_game.add(message.channel.id)
        self.next_bang[message.guild.id] = datetime.now().timestamp() + wait_time
        asyncio.create_task(self.do_tha_bang(message.guild, message.channel, guild_data, wait_time))
