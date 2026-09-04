
import random
import asyncio
import discord
from enum import Enum
from typing import Iterable, Union, cast
from collections import deque
from redbot.core import commands, bank, errors
from redbot.core.utils.chat_formatting import humanize_number

from .base import BaseCasinoCog
from .views.again_view import AgainView

# Fallbacks only. The live values come from config via `get_payouts` so a
# server can tune its own house edge; these are what a fresh install starts on
# and what is used if a key somehow reads back as None.
JACKPOT_AMOUNT = 100
TRIPLE = 3
DOUBLE = 2

class SlotMachine(Enum):
    cherries = "🍒"
    bell = "🔔"
    clover = "🍀"
    apple = "🍎"
    lemon = "🍋"
    seven = "7️⃣"
    watermelon = "🍉"
    grapes = "🍇"
    coin = "🪙"
    heart = "🩷"

# The shape of the payout table: which combination each config key pays for,
# and what a fresh install pays. `key` is what the dashboard edits.
PAYOUT_KEYS = (
    ("payout_seven3", (SlotMachine.seven,) * 3, 100),
    ("payout_clover3", (SlotMachine.clover,) * 3, 25),
    ("payout_cherries3", (SlotMachine.cherries,) * 3, 20),
    ("payout_seven2", (SlotMachine.seven,) * 2, 5),
    ("payout_clover2", (SlotMachine.clover,) * 2, 4),
    ("payout_cherries2", (SlotMachine.cherries,) * 2, 3),
    ("payout_triple", TRIPLE, 10),
    ("payout_double", DOUBLE, 2),
)

PAYOUTS = {combo: default for _key, combo, default in PAYOUT_KEYS}


async def get_payouts(cog, guild, is_global: bool):
    """(payout table, jackpot threshold) for this guild.

    A seven triple is the headline win, so its payout is also the bar the
    jackpot banner is measured against - raise it and only bigger wins
    celebrate; lower it and more do.
    """
    scope = cog.config if is_global else cog.config.guild(guild)
    saved = await scope.all()
    table = {}
    for key, combo, default in PAYOUT_KEYS:
        value = saved.get(key)
        table[combo] = default if value is None else int(value)
    return table, table[(SlotMachine.seven,) * 3]

async def slots(cog: BaseCasinoCog, ctx: Union[discord.Interaction, commands.Context], bet: int):
    author = ctx.author if isinstance(ctx, commands.Context) else ctx.user
    assert ctx.guild and isinstance(author, discord.Member) and isinstance(ctx.channel, discord.abc.Messageable)
    interaction = ctx if isinstance(ctx, discord.Interaction) else ctx.interaction
    currency_name = await bank.get_currency_name(ctx.guild)
    is_global = await bank.is_global()
    easy = await cog.config.sloteasy() if is_global else await cog.config.guild(ctx.guild).sloteasy()

    payouts, jackpot_at = await get_payouts(cog, ctx.guild, is_global)

    default_reel = deque(list(cast(Iterable, SlotMachine))[:9 if easy else 10])
    reels = []
    for _ in range(3):
        default_reel.rotate(random.randint(-999, 999))  # weeeeee
        new_reel = deque(default_reel, maxlen=3)
        reels.append(new_reel)

    center_line = (reels[0][1], reels[1][1], reels[2][1])

    multiplier = payouts.get(center_line,
                 payouts.get(center_line[1:],
                 payouts.get(center_line[:-1])))

    if not multiplier:
        has_three = center_line[0] == center_line[1] == center_line[2]
        has_two = center_line[0] == center_line[1] or center_line[1] == center_line[2]
        if has_three:
            multiplier = payouts[TRIPLE]
        elif has_two:
            multiplier = payouts[DOUBLE]
    
    coinfreespin = await cog.config.coinfreespin() if is_global else await cog.config.guild(ctx.guild).coinfreespin()
    if coinfreespin and not multiplier and SlotMachine.coin in center_line:
        multiplier = 1

    if multiplier:
        if multiplier == 1:
            phrase = "Free spin"
            balance = await bank.get_balance(author)
        else:
            phrase = f"**×{multiplier}**"
            old_balance = await bank.get_balance(author)
            winnings = bet * (multiplier - 1)
            balance = old_balance + winnings
            try:
                await bank.deposit_credits(author, winnings)
            except errors.BalanceTooHigh as exc:
                await bank.set_balance(author, exc.max_balance)
    else:
        old_balance = await bank.get_balance(author)
        await bank.withdraw_credits(author, bet)
        balance = old_balance - bet
        phrase = "*None*"

    jackpot_whiff = False
    if center_line.count(SlotMachine.seven) == 2:
            if (reels[0][1] == reels[1][1] == reels[2][0]  # xx^
                or reels[0][1] == reels[1][1] == reels[2][2]  # xxv
                or reels[0][0] == reels[1][1] == reels[2][1]  # ^xx
                or reels[0][2] == reels[1][1] == reels[2][1]  # vxx
                or reels[0][1] == reels[1][0] == reels[2][1]  # x^x
                or reels[0][1] == reels[1][2] == reels[2][1]  # xvx
            ):
                jackpot_whiff = True
    
    # stats
    statconfig = cog.config.user(author) if is_global else cog.config.member(author)
    async with statconfig.all() as stats:
        stats["slotcount"] += 1
        stats["slotbetted"] += bet
        if multiplier and multiplier > 0:
            stats["slotprofit"] += bet * multiplier
        if center_line[0] == center_line[1] == center_line[2]:
            stats["slot3symbolcount"] += 1
        elif center_line[0] == center_line[1] or center_line[1] == center_line[2]:
            stats["slot2symbolcount"] += 1
        if multiplier == 1:
            stats["slotfreespincount"] += 1
        elif multiplier and multiplier >= jackpot_at:
            stats["slotjackpotcount"] += 1
        elif jackpot_whiff:
            stats["slotjackpotwhiffcount"] += 1

    embed = discord.Embed(title="Slot Machine", color=await cog.bot.get_embed_color(ctx.channel))
    embed.add_field(name="Bet", value=f"{humanize_number(bet)} {currency_name}")
    if interaction and interaction.type == discord.InteractionType.component:
        embed.set_footer(text=author.display_name, icon_url=author.display_avatar.url)

    first = f"┃ {reels[0][0].value} ⬛ ⬛ ┃\n" \
            f"┣ {reels[0][1].value} ⬛ ⬛ ┫\n" \
            f"┃ {reels[0][2].value} ⬛ ⬛ ┃"
    second = f"┃ {reels[0][0].value} {reels[1][0].value} ⬛ ┃\n" \
                f"┣ {reels[0][1].value} {reels[1][1].value} ⬛ ┫\n" \
                f"┃ {reels[0][2].value} {reels[1][2].value} ⬛ ┃"
    third = f"┃ {reels[0][0].value} {reels[1][0].value} {reels[2][0].value} ┃\n" \
            f"┣ {reels[0][1].value} {reels[1][1].value} {reels[2][1].value} ┫\n" \
            f"┃ {reels[0][2].value} {reels[1][2].value} {reels[2][2].value} ┃"
    
    def prepare_final_embed():
        nonlocal currency_name, balance, phrase
        embed.add_field(name="Winnings", value=phrase)
        embed.add_field(name="Balance", value=f"{humanize_number(balance)} {currency_name}")
        if multiplier and multiplier >= jackpot_at:
            embed.title = "🎆 JACKPOT!!! 🎆"
        elif jackpot_whiff:
            embed.title = "💀 So close..."

    if interaction:
        embed.description = first
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        await asyncio.sleep(1)
        embed.description = second
        await interaction.edit_original_response(embed=embed)
        await asyncio.sleep(1)
        if reels[0][1] == reels[1][1]:
            await asyncio.sleep(0.5)  # extra suspense
        embed.description = third
        prepare_final_embed()
        view = AgainView(cog.slot, bet, await interaction.original_response(), currency_name)
        await interaction.edit_original_response(embed=embed, view=view)
        # pin jackpots if possible
        if multiplier and multiplier >= jackpot_at:
            try:
                message = await interaction.original_response()
                await asyncio.sleep(1)
                await message.pin()
            except discord.DiscordException:
                pass
    else:
        embed.description = first
        message = await ctx.reply(embed=embed, allowed_mentions=discord.AllowedMentions.none())  # type: ignore
        await asyncio.sleep(1)
        embed.description = second
        await message.edit(embed=embed)
        await asyncio.sleep(1)
        embed.description = third
        prepare_final_embed()
        view = AgainView(cog.slot, bet, message, currency_name)
        await message.edit(embed=embed, view=view)
        # pin jackpots if possible
        if multiplier and multiplier >= jackpot_at:
            try:
                await asyncio.sleep(1)
                await message.pin()
            except discord.DiscordException:
                pass
