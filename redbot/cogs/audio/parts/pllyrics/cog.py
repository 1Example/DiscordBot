from __future__ import annotations

from pathlib import Path

from redbot.core import commands
from redbot.core.i18n import Translator, cog_i18n
from tabulate import tabulate
from pylav.type_hints.bot import DISCORD_COG_TYPE_MIXIN

from .dashboard_integration import LyricsDashboard
from pylav.logging import getLogger

LOGGER = getLogger("PyLav.cog.PyLavLyrics")

_ = Translator("PyLavUtils", Path(__file__))


@cog_i18n(_)
class PyLavLyrics(LyricsDashboard, DISCORD_COG_TYPE_MIXIN):
    """Lyrics commands for PyLav"""

    __version__ = "1.0.0"


