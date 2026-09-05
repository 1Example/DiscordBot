from __future__ import annotations

from pathlib import Path

from redbot.core.i18n import Translator, cog_i18n
from tabulate import tabulate
from pylav.extension.red.ui.menus.generic import PaginatingMenu
from pylav.extension.red.ui.prompts.nodes import maybe_prompt_for_node
from pylav.helpers.format.ascii import EightBitANSI
from pylav.logging import getLogger
from pylav.type_hints.bot import DISCORD_BOT_TYPE, DISCORD_COG_TYPE_MIXIN

from .dashboard_integration import NodesDashboard

LOGGER = getLogger("PyLav.cog.Nodes")

_ = Translator("PyLavNodes", Path(__file__))


@cog_i18n(_)
class PyLavNodes(NodesDashboard, DISCORD_COG_TYPE_MIXIN):
    """Manage the nodes used by PyLav"""

    __version__ = "1.0.0"


