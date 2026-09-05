from redbot.core import Config
from redbot.core.bot import Red
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.cog_base import CogBase

from .dashboard_integration import DashboardIntegration

# Credits:
# General repo credits.
# Thanks to Phen for the original code (https://github.com/phenom4n4n/phen-cogs/tree/master/embedutils)!
# Thanks to Max for hosting an embeds creator (https://embedutils.com/)!

_: Translator = Translator("EmbedUtils", __file__)


@cog_i18n(_)
class EmbedUtils(DashboardIntegration, CogBase):
    """Create, send, and store rich embeds, from Red-Web-Dashboard too!"""

    __authors__: list[str] = ["PhenoM4n4n", "AAA3A"]

    def __init__(self, bot: Red) -> None:
        super().__init__(bot=bot)

        self.config: Config = Config.get_conf(
            self,
            identifier=205192943327321000143939875896557571750,
            force_registration=True,
        )
        self.config.register_global(stored_embeds={})
        self.config.register_guild(stored_embeds={})
