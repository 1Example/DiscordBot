from __future__ import annotations

from pathlib import Path

from redbot.core.i18n import Translator, cog_i18n

from tabulate import tabulate
from pylav.logging import getLogger
from pylav.type_hints.bot import DISCORD_COG_TYPE_MIXIN


from .dashboard_integration import ManagedNodeDashboard
from pylav.core.client import Client

LOGGER = getLogger("PyLav.cog.ManagedNode")

_ = Translator("PyLavManagedNode", Path(__file__))


@cog_i18n(_)
class PyLavManagedNode(ManagedNodeDashboard, DISCORD_COG_TYPE_MIXIN):
    """Configure the managed Lavalink node used by PyLav"""

    __version__ = "1.0.0"
    lavalink: Client


