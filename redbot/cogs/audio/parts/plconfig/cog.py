from __future__ import annotations

from pathlib import Path

import discord
from redbot.core.i18n import Translator, cog_i18n
from tabulate import tabulate

from pylav.helpers.format.strings import shorten_string
from pylav.logging import getLogger
from pylav.type_hints.bot import DISCORD_BOT_TYPE, DISCORD_COG_TYPE_MIXIN

from .view import InfoView
from pylav.core.client import Client
from .dashboard_integration import PyLavConfigDashboard

LOGGER = getLogger("PyLav.cog.Configurator")

_ = Translator("PyLavConfigurator", Path(__file__))


@cog_i18n(_)
class PyLavConfigurator(PyLavConfigDashboard, DISCORD_COG_TYPE_MIXIN):
    """Configure PyLav library settings"""

    lavalink: Client

    __version__ = "1.0.0"


