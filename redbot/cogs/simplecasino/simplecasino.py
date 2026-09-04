import logging
import asyncio
import discord
import calendar
import aiofiles
from typing import List, Optional, Union
from datetime import datetime
from redbot.core import commands, app_commands, bank
from redbot.core.bot import Red
from redbot.core.data_manager import bundled_data_path
from redbot.cogs.economy.economy import Economy
from redbot.core.utils.chat_formatting import humanize_number
from redbot.core.utils.chat_formatting import humanize_timedelta

from .base import EMOJI_DEFAULTS, BaseCasinoCog
from .slots import slots
from .poker import PokerGame
from .blackjack import Blackjack
from .utils import DISCORD_RED, POKER_MAX_PLAYERS, POKER_RULES
from .views.again_view import AgainView
from .views.replace_view import ReplaceView
from .dashboard_integration import DashboardIntegration

log = logging.getLogger("red.crab-cogs.simplecasino")

old_slot: Optional[commands.Command] = None
old_payouts: Optional[commands.Command] = None

MAX_APP_EMOJIS = 2000
POKER_AFK_LIMIT = 10  # minutes
STARTING = "Starting game..."

# Fallback only; the live cap is read from config per guild.
MAX_CONCURRENT_SLOTS = 3


class SimpleCasino(DashboardIntegration, BaseCasinoCog):
    """Gamble virtual currency with Poker, Blackjack, and Slot Machines."""

    def __init__(self, bot: Red):
        super().__init__(bot)
        # Spins currently animating. A spin holds a task for a few seconds
        # while it sleeps between reel reveals and edits its response, so this
        # counts every one of them - the check this feeds used to exempt slash
        # interactions, which made it dead once the cog stopped taking prefix
        # commands, even though the work it guards against is unchanged.
        self.concurrent_slots = 0

    async def cog_load(self) -> None:
        # Load existing games
        all_channels = await self.config.all_channels()
        for cid, conf in all_channels.items():
            try:
                channel = self.bot.get_channel(cid)
                # A saved game whose channel was deleted, or that lives in a
                # guild the bot has left, is an ordinary condition rather than a
                # programming error - skip it instead of asserting. The assert
                # used to run before this check, so it fired on every such game
                # and logged a traceback.
                if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                    continue
                game_config = conf.get("game", {})
                game = await PokerGame.from_config(self, channel, game_config)
                if game and game.players and not game.is_finished:
                    self.poker_games[cid] = game
                    if game.view:
                        self.bot.add_view(game.view)
            except Exception:
                log.error(f"Loading game in {cid}", exc_info=True)

        # Upload the bundled marker emojis once and point the config at them.
        #
        # This only fills in markers still holding their shipped default. It
        # used to write unconditionally, which meant every reload overwrote
        # whatever the owner had chosen - making the markers effectively
        # uneditable, since the next restart put them back.
        all_emojis = await self.bot.fetch_application_emojis()
        for emoji_name in ("dealer", "smallblind", "bigblind", "spades", "clubs"):
            key = "emoji_" + emoji_name
            stored = await self.config.__getattr__(key)()
            if stored != EMOJI_DEFAULTS.get(key):
                continue  # customised - leave it alone
            emoji = next((emoji for emoji in all_emojis if emoji.name == emoji_name), None)
            if not emoji and len(all_emojis) < MAX_APP_EMOJIS:
                async with aiofiles.open(bundled_data_path(self) / f"{emoji_name}.png", "rb") as fp:
                    image = await fp.read()
                emoji = await self.bot.create_application_emoji(name=emoji_name, image=image)
            if emoji:
                await self.config.__getattr__(key).set(str(emoji))

    def cog_unload(self):
        global old_slot, old_payouts
        # clear views
        for game in self.poker_games.values():
            if game.view:
                game.view.stop()
        # restore old commands
        if old_slot:
            self.bot.remove_command(old_slot.name)
            self.bot.add_command(old_slot)
        if old_payouts:
            self.bot.remove_command(old_payouts.name)
            self.bot.add_command(old_payouts)

    async def get_economy_cog(self, ctx: Union[discord.Interaction, commands.Context]) -> Optional[Economy]:
        cog: Optional[Economy] = self.bot.get_cog("Economy")  # type: ignore
        if cog:
            return cog
        reply = ctx.reply if isinstance(ctx, commands.Context) else ctx.response.send_message
        await reply("Economy cog not loaded! Contact the bot owner for more information.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    

    @app_commands.command(name="blackjack", extras={"red_force_enable": True})
    @app_commands.describe(bet="How much currency to bet.")
    @app_commands.guild_only()
    async def blackjack_app(self, interaction: discord.Interaction, bet: int):
        """Play Blackjack against the bot. Get as close to 21 as possible!"""
        await self.blackjack(interaction, bet)

    async def blackjack(self, ctx: Union[discord.Interaction, commands.Context], bet: int):
        author = ctx.author if isinstance(ctx, commands.Context) else ctx.user
        reply = ctx.reply if isinstance(ctx, commands.Context) else ctx.response.send_message
        assert ctx.guild and isinstance(author, discord.Member) and isinstance(ctx.channel, (discord.TextChannel, discord.Thread))

        minimum_bid = await self.config.bjmin() if await bank.is_global() else await self.config.guild(ctx.guild).bjmin()
        maximum_bid = await self.config.bjmax() if await bank.is_global() else await self.config.guild(ctx.guild).bjmax()
        currency_name = await bank.get_currency_name(ctx.guild)
        if bet < 1 or bet < minimum_bid:
            return await reply(f"Your bet must be at least {humanize_number(minimum_bid)} {currency_name}", ephemeral=True)
        elif bet > maximum_bid:
            return await reply(f"Your bet cannot be greater than {humanize_number(maximum_bid)} {currency_name}", ephemeral=True)
        if not await bank.can_spend(author, bet):
            return await reply("You ain't got enough money, friend.", ephemeral=True)
        
        await bank.withdraw_credits(author, bet)
        include_author = isinstance(ctx, discord.Interaction) and ctx.type == discord.InteractionType.component
        blackjack = Blackjack(self, author, ctx.channel, bet, await self.bot.get_embed_color(ctx.channel), include_author)
        await blackjack.check_payout()
        view = AgainView(self.blackjack, bet, None, currency_name) if blackjack.is_over() else blackjack
        message = await reply(embed=await blackjack.get_embed(), view=view, allowed_mentions=discord.AllowedMentions.none())
        if isinstance(view, AgainView):
            view.message = message if isinstance(ctx, commands.Context) else await ctx.original_response()  # type: ignore


    @app_commands.command(name="slot", extras={"red_force_enable": True})
    @app_commands.guild_only()
    @app_commands.describe(bet="How much currency to put in the slot machine.")
    async def slot_app(self, interaction: discord.Interaction, bet: int):
        """Play the slot machine."""
        max_concurrent = MAX_CONCURRENT_SLOTS
        if interaction.guild is not None:
            scope = (
                self.config if await bank.is_global()
                else self.config.guild(interaction.guild)
            )
            configured = await scope.max_concurrent_slots()
            if configured is not None:
                max_concurrent = configured

        if self.concurrent_slots >= max_concurrent:
            return await interaction.response.send_message(
                "The slot machine is busy right now - try again in a few seconds.",
                ephemeral=True,
            )

        self.concurrent_slots += 1
        try:
            await self.slot(interaction, bet)
        finally:
            self.concurrent_slots -= 1

    async def slot(self, ctx: Union[discord.Interaction, commands.Context], bet: int):
        author = ctx.author if isinstance(ctx, commands.Context) else ctx.user
        reply = ctx.reply if isinstance(ctx, commands.Context) else ctx.response.send_message
        assert ctx.guild and isinstance(author, discord.Member)

        if not (economy := await self.get_economy_cog(ctx)):
            return

        is_global = await bank.is_global()

        # The old concurrency cap lived here. It throttled prefix spins only -
        # slash interactions were always exempt, since each gets its own
        # response - so with the cog slash-only it could never fire.

        if is_global:
            min_bid = await economy.config.SLOT_MIN()
            max_bid = await economy.config.SLOT_MAX()
            slot_time = await economy.config.SLOT_TIME()
            last_slot = await economy.config.user(author).last_slot()
        else:
            min_bid = await economy.config.guild(ctx.guild).SLOT_MIN()
            max_bid = await economy.config.guild(ctx.guild).SLOT_MAX()
            slot_time = await economy.config.guild(ctx.guild).SLOT_TIME()
            last_slot = await economy.config.member(author).last_slot()
        
        created_at = ctx.created_at if isinstance(ctx, discord.Interaction) else ctx.message.created_at
        cur_time = calendar.timegm(created_at.utctimetuple())
        currency_name = await bank.get_currency_name(ctx.guild)

        if (cur_time - last_slot) < max(3, slot_time):
            await reply("You're on cooldown, try again in a few seconds.")
            return
        if bet < min_bid:
            await reply(f"Your bet must be at least {humanize_number(min_bid)} {currency_name}")
            return
        if bet > max_bid:
            await reply(f"Your bet cannot be greater than {humanize_number(max_bid)} {currency_name}")
            return
        if not await bank.can_spend(author, bet):
            await reply("You ain't got enough money, friend.")
            return
        
        if is_global:
            await economy.config.user(author).last_slot.set(cur_time)
        else:
            await economy.config.member(author).last_slot.set(cur_time)

        await slots(self, ctx, bet)


    poker_app = app_commands.Group(
        name="poker",
        description="Play Texas Hold'em Poker with up to 8 people!",
        guild_only=True,
        extras={"red_force_enable": True},
    )

    @poker_app.command(name="new")
    @app_commands.describe(starting_bet="This bet may increase during the game.")
    async def poker_app_new(self, interaction: discord.Interaction, starting_bet: Optional[int]):
        """Start a new game of Poker with no players."""
        ctx = await commands.Context.from_interaction(interaction)
        assert isinstance(ctx.author, discord.Member)
        await self.poker(ctx, [ctx.author], starting_bet)

    @poker_app.command(name="rules")
    async def poker_app_rules(self, interaction: discord.Interaction):
        """Show the rules for Poker in this bot."""
        embed = discord.Embed(color=DISCORD_RED)
        bigblind_emoji = await self.config.emoji_bigblind()
        embed.title = f"{bigblind_emoji} Texas Hold'em Poker - Rules summary"
        embed.description = POKER_RULES
        filename = "pokerhands.jpg"
        file = discord.File(bundled_data_path(self) / filename, filename=filename)
        embed.set_image(url=f"attachment://{filename}")
        await interaction.response.send_message(embed=embed, file=file, ephemeral=True)

    async def poker(self, ctx: Union[discord.Interaction, commands.Context], players: List[discord.Member], starting_bet: Optional[int]) -> bool:
        author = ctx.author if isinstance(ctx, commands.Context) else ctx.user
        assert ctx.guild and isinstance(author, discord.Member) and isinstance(ctx.channel, (discord.TextChannel, discord.Thread))
        
        reply = ctx.reply if isinstance(ctx, commands.Context) else ctx.response.send_message

        minimum_starting_bet: int = await self.config.pokermin() if await bank.is_global() else await self.config.guild(ctx.guild).pokermin()
        maximum_starting_bet: int = await self.config.pokermax() if await bank.is_global() else await self.config.guild(ctx.guild).pokermax()
        max_players: int = await self.config.poker_max_players() if await bank.is_global() else await self.config.guild(ctx.guild).poker_max_players()
        if max_players is None:
            max_players = POKER_MAX_PLAYERS
        currency_name = await bank.get_currency_name(ctx.guild)
        if starting_bet is None:
            starting_bet = minimum_starting_bet
        elif starting_bet < minimum_starting_bet:
            await reply(f"The starting bet must be at least {minimum_starting_bet} {currency_name}.")
            return False
        elif starting_bet > maximum_starting_bet:
            await reply(f"The starting bet must not be greater than {maximum_starting_bet} {currency_name}.")
            return False
        if not await bank.can_spend(author, starting_bet):
            await reply(f"You don't have enough {currency_name} to make that bet.")
            return False

        # Game already exists
        if ctx.channel.id in self.poker_games and not self.poker_games[ctx.channel.id].is_finished:
            if len(players) > 1:  # rematch
                await reply("Another game of Poker has already begun in this channel.", ephemeral=True)
                return False
            
            old_game = self.poker_games[ctx.channel.id]
            try:
                old_message = await ctx.channel.fetch_message(old_game.message.id) if old_game.message else None # re-fetch
            except discord.NotFound:
                old_message = None

            if not old_message:
                await old_game.update_message()
                old_message = old_game.message
                assert old_message

            seconds_passed = int((datetime.now() - old_game.last_interacted).total_seconds())
            if seconds_passed // 60 >= POKER_AFK_LIMIT:
                async def callback():
                    nonlocal ctx, author, old_game, old_message
                    assert isinstance(author, discord.Member) and isinstance(ctx.channel, (discord.TextChannel, discord.Thread))
                    await old_game.cancel()
                    if old_message:
                        try:
                            await old_message.delete()
                        except discord.NotFound:
                            pass
                    game = PokerGame(self, players, ctx.channel, starting_bet, max_players)
                    self.poker_games[ctx.channel.id] = game
                    await game.update_message()

                content = f"Someone else is playing Checkers in this channel, here: {old_message.jump_url}, " \
                          f"but {humanize_timedelta(seconds=seconds_passed)} have passed since their last interaction. Do you want to start a new game?"
                embed = discord.Embed(title="Confirmation", description=content, color=await self.bot.get_embed_color(ctx.channel))
                view = ReplaceView(self, callback, author)
                message = await reply(embed=embed, view=view)
                view.message = message if isinstance(ctx, commands.Context) else await ctx.original_response()  # type: ignore
                return False
            
            else:
                content = f"There is still an active game in this channel, here: {old_message.jump_url}\nTry again in a few minutes"
                permissions = ctx.channel.permissions_for(author)
                content += " or consider creating a thread." if permissions.create_public_threads or permissions.create_private_threads else "."
                await reply(content, ephemeral=True)
                return False
        
        if isinstance(ctx, discord.Interaction):
            await ctx.response.send_message(STARTING, ephemeral=True)
        elif ctx.interaction:
            await ctx.interaction.response.send_message(STARTING, ephemeral=True)

        # New game
        game = PokerGame(self, players, ctx.channel, starting_bet, max_players)
        self.poker_games[ctx.channel.id] = game
        await game.update_message()
        return True


    async def blackjackstats(self, ctx: commands.Context, member: Optional[discord.Member]):
        """View your own or someone else's stats in Blackjack."""
        assert isinstance(ctx.author, discord.Member)
        member = member or ctx.author
        stats = await self.config.user(member).all() if await bank.is_global() else await self.config.member(member).all()
        currency_name = await bank.get_currency_name(ctx.guild)
        embed = discord.Embed(title="2️⃣1️⃣ Blackjack Stats", color=await self.bot.get_embed_color(ctx.channel))
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        embed.add_field(name="Times played", value=humanize_number(stats["bjcount"]))
        embed.add_field(name="Total betted", value=f"{humanize_number(stats['bjbetted'])} {currency_name}")
        embed.add_field(name="Total payout", value=f"{humanize_number(stats['bjprofit'])} {currency_name}")
        embed.add_field(name="Wins", value=humanize_number(stats["bjwincount"]))
        embed.add_field(name="Losses", value=humanize_number(stats["bjlosscount"]))
        embed.add_field(name="Ties", value=humanize_number(stats["bjtiecount"]))
        embed.add_field(name="21s gotten", value=humanize_number(stats["bj21count"]))
        embed.add_field(name="Blackjacks gotten", value=humanize_number(stats["bjnatural21count"]))
        await ctx.send(embed=embed)

    async def slotstats(self, ctx: commands.Context, member: Optional[discord.Member]):
        """View your own or someone else's stats in the Slot machine."""
        assert ctx.guild and isinstance(ctx.author, discord.Member)
        member = member or ctx.author
        is_global = await bank.is_global()
        stats = await self.config.user(member).all() if is_global else await self.config.member(member).all()
        currency_name = await bank.get_currency_name(ctx.guild)
        embed = discord.Embed(title="7️⃣ Slot Machine Stats", color=await self.bot.get_embed_color(ctx.channel))
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        embed.add_field(name="Times played", value=humanize_number(stats["slotcount"]))
        embed.add_field(name="Total betted", value=f"{humanize_number(stats['slotbetted'])} {currency_name}")
        embed.add_field(name="Total payout", value=f"{humanize_number(stats['slotprofit'])} {currency_name}")
        freespinenabled = await self.config.coinfreespin() if is_global else await self.config.guild(ctx.guild).coinfreespin()
        if freespinenabled:
            embed.add_field(name="Free spins", value=humanize_number(stats["slotfreespincount"]))
        embed.add_field(name="2 symbol payouts", value=humanize_number(stats["slot2symbolcount"]))
        embed.add_field(name="3 symbol payouts", value=humanize_number(stats["slot3symbolcount"]))
        embed.add_field(name="Jackpots", value=humanize_number(stats["slotjackpotcount"]))
        embed.add_field(name="Jackpot near-misses", value=humanize_number(stats["slotjackpotwhiffcount"]))
        await ctx.send(embed=embed)


    casinostats_app = app_commands.Group(
        name="casinostats",
        description="View your stats in Blackjack and Slots.",
        guild_only=True,
        extras={"red_force_enable": True},
    )

    @casinostats_app.command(name="blackjack")
    @app_commands.describe(member="The user to view stats for. Views your own stats by default.")
    async def blackjackstats_app(self, interaction: discord.Interaction, member: Optional[discord.Member]):
        """View your or someone else's stats with Blackjack."""
        ctx = await commands.Context.from_interaction(interaction)
        await self.blackjackstats(ctx, member)

    @casinostats_app.command(name="slot")
    @app_commands.describe(member="The user to view stats for. Views your own stats by default.")
    async def slotstats_app(self, interaction: discord.Interaction, member: Optional[discord.Member]):
        """View your or someone else's stats with the Slot Machine."""
        ctx = await commands.Context.from_interaction(interaction)
        await self.slotstats(ctx, member)


async def setup(bot: Red):
    async def add_cog():
        global old_slot, old_payouts
        await asyncio.sleep(1)  # hopefully economy cog has finished loading

        # Economy's own slot machine is a different game with different
        # settings, and its `payouts` command describes that other machine, so
        # both are taken down while this cog owns slots - otherwise `[p]slot`
        # and `/slot` would quietly be two different games.
        if old_slot := bot.get_command("slot"):
            bot.remove_command(old_slot.name)
        if old_payouts := bot.get_command("payouts"):
            bot.remove_command(old_payouts.name)
        # jumper-plugins' `[p]blackjack` used to collide with this cog's own
        # prefix command. This cog is slash-only now, so there is nothing to
        # collide with and theirs is left alone.

        await bot.add_cog(SimpleCasino(bot))
        await bot.tree.red_check_enabled()  # type: ignore  # register slash commands

    _ = asyncio.create_task(add_cog())
