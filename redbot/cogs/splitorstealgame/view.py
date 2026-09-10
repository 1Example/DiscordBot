import asyncio
import datetime
import random
import typing

import contextlib

import discord

from redbot.core import bank, commands, errors
from redbot.core.i18n import Translator

_: Translator = Translator("SplitOrSteal", __file__)


class SplitOrStealGameView(discord.ui.View):
    def __init__(
        self,
        cog: commands.Cog,
    ) -> None:
        super().__init__(timeout=120 + 60 + 10)
        self.ctx: commands.Context = None
        self.cog: commands.Cog = cog

        self.initial_players: list[discord.Member] = []
        self.players: dict[discord.Member, typing.Literal["split", "steal"]] = {}

        # What each person paid to join, so anyone who does not end up playing
        # - or whose game falls apart - can be given it back.
        self.stake: int = 0
        self.house_bonus: int = 0
        self.currency: str = "credits"
        self.paid: dict[discord.Member, int] = {}
        # Set as soon as a second player is in, so the join window ends when
        # the game can actually be played rather than a minute later.
        self._ready: asyncio.Event = asyncio.Event()

        self._message: discord.Message = None
        self._mode: typing.Literal["join", "play"] = None

    async def refund_all(self) -> None:
        """Give everyone who paid their stake back. Safe to call twice."""
        while self.paid:
            member, amount = self.paid.popitem()
            with contextlib.suppress(errors.BalanceTooHigh, RuntimeError):
                await bank.deposit_credits(member, amount)

    async def safe_refund(self) -> None:
        """Refund even while this task is being torn down.

        A bot restart or a cog reload cancels the command mid-game, and a
        cancelled task cannot reliably await anything - so the refund is
        shielded from the cancellation, and handed to the loop as its own task
        if it is cancelled anyway. Losing someone's stake to a restart is not
        an acceptable way for a game to end.
        """
        if not self.paid:
            return
        try:
            await asyncio.shield(self.refund_all())
        except asyncio.CancelledError:
            self.cog.bot.loop.create_task(self.refund_all())
            raise

    def stakes_returned(self) -> str:
        """A line saying the money came back, when any of it did."""
        if not self.stake:
            return ""
        return " " + _("Your stake has been returned.")

    @property
    def pot(self) -> int:
        """What the two players are playing for."""
        return sum(self.paid.values()) + (self.house_bonus if self.paid else 0)

    async def payout(self, member: discord.Member, amount: int) -> None:
        if amount <= 0:
            return
        with contextlib.suppress(errors.BalanceTooHigh, RuntimeError):
            await bank.deposit_credits(member, amount)

    async def start(
        self, ctx: commands.Context, stake: int | None = None
    ) -> discord.Message:
        self.ctx: commands.Context = ctx
        configured, self.house_bonus, self.currency = await self.cog.pot_settings(ctx.guild)
        # A named amount wins over the server default; 0 is a real choice, so
        # only an omitted one falls back.
        self.stake = configured if stake is None else max(0, int(stake))

        # Whoever ran the command is playing. Charge them before anything is
        # posted, so somebody who cannot cover their own wager is told plainly
        # rather than watching a game they are not in.
        if self.stake:
            if not await bank.can_spend(ctx.author, self.stake):
                raise commands.UserFeedbackCheckFailure(
                    _("Starting this costs **{stake} {currency}** and you do not have it.").format(
                        stake=self.stake, currency=self.currency
                    )
                )
            await bank.withdraw_credits(ctx.author, self.stake)
            self.paid[ctx.author] = self.stake
        elif self.house_bonus:
            self.paid[ctx.author] = 0
        self.initial_players.append(ctx.author)
        embed: discord.Embed = discord.Embed(
            title=_("Split Or Steal Game"),
            color=await self.ctx.embed_color(),
        )
        embed.description = _(
            "{author} is waiting for someone to play. The game starts the moment"
            " somebody joins."
        ).format(author=ctx.author.mention)
        if self.stake:
            embed.description += "\n" + _(
                "It costs **{stake} {currency}** to join, for a pot of **{pot} {currency}**."
            ).format(
                stake=self.stake,
                currency=self.currency,
                pot=self.stake * 2 + self.house_bonus,
            )
        elif self.house_bonus:
            embed.description += "\n" + _(
                "The winner takes **{pot} {currency}**."
            ).format(pot=self.house_bonus, currency=self.currency)
        end_time = datetime.datetime.now(tz=datetime.timezone.utc) + datetime.timedelta(seconds=60)
        embed.add_field(
            name=_("End time for joining:"),
            value=f"{discord.utils.format_dt(end_time, style='T')} ({discord.utils.format_dt(end_time, style='R')})",
        )
        self.clear_items()
        self.add_item(self.join_button)
        self._mode = "join"
        self._message: discord.Message = await self.ctx.send(embed=embed, view=self)
        self.cog.views[self._message] = self
        if self.stake:
            with contextlib.suppress(discord.HTTPException):
                await self.ctx.send(
                    _(
                        "You are in, and **{stake} {currency}** has been taken for your"
                        " stake. You get it back if nobody joins."
                    ).format(stake=self.stake, currency=self.currency),
                    ephemeral=True,
                )
        try:
            return await self._run()
        except commands.UserFeedbackCheckFailure:
            raise
        except BaseException:
            # Cancellation included. Nobody pays for a game the bot abandoned.
            await self.safe_refund()
            raise

    async def _run(self) -> discord.Message:
        # One minute to find an opponent, but no longer than it takes.
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(self._ready.wait(), timeout=60)
        self._mode = "play"
        initial_players = self.initial_players.copy()
        if len(initial_players) < 2:
            await self.safe_refund()
            await self.on_timeout()
            self.stop()
            raise commands.UserFeedbackCheckFailure(
                _("At least two players are needed to play.") + self.stakes_returned()
            )
        player_A = random.choice(initial_players)
        initial_players.remove(player_A)
        player_B = random.choice(initial_players)
        initial_players.remove(player_B)
        self.players = {player_A: None, player_B: None}
        # Everyone who paid but is not playing gets it back; what is left in
        # `paid` is the pot.
        for member in list(self.paid):
            if member not in self.players:
                amount = self.paid.pop(member)
                await self.payout(member, amount)
        embed: discord.Embed = discord.Embed(
            title="SplitOrSteal Game",
            color=await self.ctx.embed_color(),
        )
        embed.description = _(
            "The two players are {player_A.mention} and {player_B.mention}.\n"
            "You have to click the button that you choose (`split` or `steal`).\n"
            "• If you both choose `split`, you share the pot.\n"
            "• If you both choose `steal`, the pot is gone and you both lose.\n"
            "• If one chooses `split` and the other `steal`, whoever stole takes it all.",
        ).format(player_A=player_A, player_B=player_B)
        if self.pot:
            # The moment it matters most: say what is actually on the table.
            embed.add_field(
                name=_("The pot:"),
                value=_("**{pot} {currency}** — {share} each if you both split.").format(
                    pot=self.pot, currency=self.currency, share=self.pot // 2
                ),
                inline=False,
            )
        end_time = datetime.datetime.now(tz=datetime.timezone.utc) + datetime.timedelta(seconds=60)
        embed.add_field(
            name=_("End time for play:"),
            value=f"{discord.utils.format_dt(end_time, style='T')} ({discord.utils.format_dt(end_time, style='R')})",
        )
        self.clear_items()
        self.add_item(self.join_button)
        self.join_button.disabled = True
        self.add_item(self.split_button)
        self.add_item(self.steal_button)
        self._message: discord.Message = await self._message.edit(embed=embed, view=self)

        async def check_conditions():
            while self.players[player_A] is None or self.players[player_B] is None:
                await asyncio.sleep(1)
            return True

        try:
            await asyncio.wait_for(check_conditions(), timeout=60)
        except TimeoutError:
            await self.safe_refund()
            await self.on_timeout()
            self.stop()
            raise commands.UserFeedbackCheckFailure(
                _("At least one player has stopped playing.") + self.stakes_returned()
            )

        pot = self.pot
        self.paid.clear()

        def won(winner) -> str:
            """What the winner walks away with, if there was anything to win."""
            if not pot:
                return ""
            return " " + _("{winner} takes the whole pot: **{pot} {currency}**.").format(
                winner=winner.display_name, pot=pot, currency=self.currency
            )

        def lost(loser) -> str:
            """And what it cost the other one. Losing quietly is how this
            looked like the credits had gone nowhere."""
            if not self.stake:
                return ""
            return " " + _("{loser} loses their **{stake} {currency}**.").format(
                loser=loser.display_name, stake=self.stake, currency=self.currency
            )

        if self.players[player_A] == "split" and self.players[player_B] == "split":
            share = pot // 2
            await self.payout(player_A, share)
            await self.payout(player_B, share)
            text = _(
                "{player_A.display_name} and {player_B.display_name}, you both chose"
                " `split` and therefore you both win.",
            ).format(player_A=player_A, player_B=player_B)
            if share:
                text += " " + _("The pot splits: **{share} {currency}** each.").format(
                    share=share, currency=self.currency
                )
        elif self.players[player_A] == "steal" and self.players[player_B] == "steal":
            text = _(
                "{player_A.display_name} and {player_B.display_name}, you both chose"
                " `steal` and therefore you both lose.",
            ).format(player_A=player_A, player_B=player_B)
            if pot:
                text += " " + _("The **{pot} {currency}** is gone.").format(
                    pot=pot, currency=self.currency
                )
            if self.stake:
                # The pot can be house money alone, in which case nobody put
                # anything in and there is nothing for them to have lost.
                text += " " + _("You each lose your **{stake} {currency}**.").format(
                    stake=self.stake, currency=self.currency
                )
        elif self.players[player_A] == "steal" and self.players[player_B] == "split":
            await self.payout(player_A, pot)
            text = (
                _(
                    "{player_A.display_name} chose `steal` and"
                    " {player_B.display_name} chose `split`, and therefore"
                    " {player_A.display_name} wins.",
                ).format(player_A=player_A, player_B=player_B)
                + won(player_A)
                + lost(player_B)
            )
        else:
            await self.payout(player_B, pot)
            text = (
                _(
                    "{player_B.display_name} chose `steal` and"
                    " {player_A.display_name} chose `split`, and therefore"
                    " {player_B.display_name} wins.",
                ).format(player_A=player_A, player_B=player_B)
                + won(player_B)
                + lost(player_A)
            )
        await self._message.reply(text)
        return self._message

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self._mode == "play" and interaction.user not in self.players:
            await interaction.response.send_message(
                _("You are not allowed to use this interaction."),
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        await self.refund_all()
        for child in self.children:
            child: discord.ui.Item
            if hasattr(child, "disabled") and not (
                isinstance(child, discord.ui.Button) and child.style == discord.ButtonStyle.url
            ):
                child.disabled = True
        try:
            await self._message.edit(view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Join Game", emoji="🎮", style=discord.ButtonStyle.success)
    async def join_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.user in self.initial_players:
            await interaction.response.send_message(
                _("You have already joined this game."),
                ephemeral=True,
            )
            return
        if self.stake:
            if not await bank.can_spend(interaction.user, self.stake):
                await interaction.response.send_message(
                    _("Joining costs **{stake} {currency}** and you do not have it.").format(
                        stake=self.stake, currency=self.currency
                    ),
                    ephemeral=True,
                )
                return
            await bank.withdraw_credits(interaction.user, self.stake)
            self.paid[interaction.user] = self.stake
        elif self.house_bonus:
            # No stake, but there is still a prize; mark them as in the pot so
            # the bonus is paid out when a game actually happens.
            self.paid[interaction.user] = 0
        self.initial_players.append(interaction.user)
        if len(self.initial_players) >= 2:
            self._ready.set()
        await interaction.response.send_message(
            _("You have joined this game.")
            + (
                " " + _("**{stake} {currency}** taken.").format(
                    stake=self.stake, currency=self.currency
                )
                if self.stake
                else ""
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="Split", style=discord.ButtonStyle.secondary)
    async def split_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.players[interaction.user] is not None:
            await interaction.response.send_message(
                _("You have already chose `{original_response}`.").format(
                    original_response=self.players[interaction.user],
                ),
                ephemeral=True,
            )
            return
        self.players[interaction.user] = "split"
        await interaction.response.send_message(_("You have chose `split`."), ephemeral=True)

    @discord.ui.button(label="Steal", style=discord.ButtonStyle.secondary)
    async def steal_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.players[interaction.user] is not None:
            await interaction.response.send_message(
                _("You have already chose `{original_response}`.").format(
                    original_response=self.players[interaction.user],
                ),
                ephemeral=True,
            )
            return
        self.players[interaction.user] = "steal"
        await interaction.response.send_message(_("You have chose `steal`."), ephemeral=True)
