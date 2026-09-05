import discord

from redbot.core import app_commands, commands
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

    @property
    def games(self) -> dict[discord.Message, SplitOrStealGameView]:
        return self.views

    @app_commands.command(
        name="splitorsteal",
        description="Play a round of Split or Steal.",
        extras={"red_force_enable": True},
    )
    @app_commands.guild_only()
    async def splitorstealgame(self, interaction: discord.Interaction) -> None:
        """
        Play a match of Split Or Steal game.

        Two player will have to click the button that they choose (`split` or `steal`).
        • If both choose `split` both of them win.
        • If both choose `steal`, both loose.
        • if one chooses `split` and one chooses `steal`, the one who choose `steal` will win.
        """
        ctx = await commands.Context.from_interaction(interaction)
        await SplitOrStealGameView(cog=self).start(ctx)
