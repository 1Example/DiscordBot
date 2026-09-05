from __future__ import annotations

import linecache
import tracemalloc
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


def get_top(snapshot, key_type="lineno", limit=10):
    snapshot = snapshot.filter_traces(
        (
            tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
            tracemalloc.Filter(False, "<unknown>"),
        )
    )
    top_stats = snapshot.statistics(key_type)
    response = ""
    response += f"Top {limit} lines"
    for index, stat in enumerate(top_stats[:limit], 1):
        frame = stat.traceback[0]
        response += f"\n\n#{index}: {frame.filename}:{frame.lineno}: {stat.size / 1024:.1f} KiB"
        if line := linecache.getline(frame.filename, frame.lineno).strip():
            response += f"\n    {line}"

    if other := top_stats[limit:]:
        size = sum(stat.size for stat in other)
        response += f"\n\n{len(other)} other: {size / 1024:.1f} KiB"
    total = sum(stat.size for stat in top_stats)
    response += f"\n\nTotal allocated size: {total / 1024:.1f} KiB"
    return response


@cog_i18n(_)
class PyLavUtils(UtilsDashboard, DISCORD_COG_TYPE_MIXIN):
    """Utility commands for PyLav"""

    __version__ = "1.0.0"


