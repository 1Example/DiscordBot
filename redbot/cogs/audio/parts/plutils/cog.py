from __future__ import annotations

from pathlib import Path

import discord
from redbot.core import commands
from redbot.core.i18n import Translator, cog_i18n
from rich.tree import Tree
from tabulate import tabulate
from pylav.helpers.format.ascii import EightBitANSI
from pylav.logging import getLogger
from pylav.type_hints.bot import DISCORD_BOT_TYPE, DISCORD_COG_TYPE_MIXIN

from .dashboard_integration import UtilsDashboard

LOGGER = getLogger("PyLav.cog.Utils")

_ = Translator("PyLavUtils", Path(__file__))



@cog_i18n(_)
class PyLavUtils(UtilsDashboard, DISCORD_COG_TYPE_MIXIN):
    """Utility commands for PyLav"""

    __version__ = "1.0.0"


