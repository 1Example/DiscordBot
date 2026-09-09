from __future__ import annotations

from pathlib import Path

from redbot.core import Config

from redbot.core.i18n import Translator, cog_i18n

from pylav.logging import getLogger
from pylav.type_hints.bot import DISCORD_COG_TYPE_MIXIN
from .dashboard_integration import EffectsDashboard

LOGGER = getLogger("PyLav.cog.Effects")

_ = Translator("PyLavEffects", Path(__file__))


@cog_i18n(_)
class PyLavEffects(EffectsDashboard, DISCORD_COG_TYPE_MIXIN):
    """Apply filters and effects to the PyLav player"""

    __version__ = "1.0.0"

    def _effects_init(self) -> None:
        self._effects_config = Config.get_conf(
            None, identifier=208903205982044161, cog_name="PyLavEffects"
        )
        self._effects_config.register_global(enable_slash=True)
        self._effects_config.register_guild(persist_fx=False, persist_eq=False)

