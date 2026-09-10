import contextlib

import discord

from redbot.core import Config, bank, commands
from redbot.core.bot import Red
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.cog_base import CogBase

from .dashboard_integration import DashboardIntegration
from .view import SplitOrStealGameView

# Credits:
# General repo credits.
# Thanks to Lemon for the cog idea!

_: Translator = Translator("SplitOrStealGame", __file__)


@cog_i18n(_)
class SplitOrStealGame(DashboardIntegration, CogBase):
    """A cog to play a match of Split Or Steal game!"""

    def __init__(self, bot: Red) -> None:
        super().__init__(bot)
        self.config: Config = Config.get_conf(
            self, identifier=947_202_531_115, force_registration=True
        )
        # A stake of 0 keeps the old behaviour: the game is played for nothing.
        self.config.register_guild(stake=0, house_bonus=0)

    @property
    def games(self) -> dict[discord.Message, SplitOrStealGameView]:
        return self.views

    async def pot_settings(self, guild: discord.Guild) -> tuple[int, int, str]:
        """The stake, what the server adds, and what to call the money."""
        settings = await self.config.guild(guild).all()
        currency = await bank.get_currency_name(guild)
        return int(settings["stake"] or 0), int(settings["house_bonus"] or 0), currency

    async def start_game(self, ctx: commands.Context, amount: int | None = None) -> None:
        """Run a match. Called by /fun splitorsteal, which lives in General.

        Ending early - too few players, someone not choosing - is an outcome,
        not a failure, so it is reported rather than raised. Raising logged a
        traceback every time somebody wandered off.
        """
        try:
            await SplitOrStealGameView(cog=self).start(ctx, stake=amount)
        except commands.UserFeedbackCheckFailure as e:
            with contextlib.suppress(discord.HTTPException):
                await ctx.send(str(e))
