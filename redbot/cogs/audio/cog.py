from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import discord
from discord import AppCommandType
from expiringdict import ExpiringDict
from redbot.core import Config
from redbot.core.i18n import Translator, cog_i18n

from pylav.extension.red.utils import CompositeMetaClass
from pylav.type_hints.bot import DISCORD_BOT_TYPE

from .config_commands import ConfigCommands
from .context_menus import ContextMenus
from .hybrid_commands import HybridCommands
from .player_commands import PlayerCommands
from .slash_commands import SlashCommands
from .dashboard_integration import DashboardIntegration
from .shared import player as player_group
from .parts.plconfig import PyLavConfigurator
from .parts.plcontroller import PyLavController
from .parts.pleffects import PyLavEffects
from .parts.pllocal import PyLavLocalFiles
from .parts.pllyrics import PyLavLyrics
from .parts.plmanagednode import PyLavManagedNode
from .parts.plnodes import PyLavNodes
from .parts.plnotifier import PyLavNotifier
from .parts.plplaylists import PyLavPlaylists
from .parts.plradio import PyLavRadio
from .parts.plutils import PyLavUtils
from .parts.plytradio import PyLavYouTubeRadio

_ = Translator("PyLavPlayer", Path(__file__))


# Everything PyLav does lives here now: what were thirteen separate cogs
# sharing one Lavalink client are mixins of this one. Three things about
# them are worth knowing before adding another.
#
# * Each part keeps its own package under parts/ so that its
#   Translator(..., Path(__file__)) still resolves to the locales it was
#   translated into.
# * The four parts with a Config of their own pin cog_name to the class
#   they used to be. Config keys storage on the cog class name, and they
#   share this cog's identifier, so without the pin their settings would
#   both move and collide.
# * __init__, cog_unload, initialize and red_delete_data_for_user were
#   defined by several of them. Only one of each survives the MRO, so
#   each part's is named for itself and called below.
@cog_i18n(_)
class Audio(
    DashboardIntegration,
    PyLavController,
    PyLavPlaylists,
    PyLavEffects,
    PyLavRadio,
    PyLavYouTubeRadio,
    PyLavLyrics,
    PyLavLocalFiles,
    PyLavNotifier,
    PyLavNodes,
    PyLavManagedNode,
    PyLavConfigurator,
    PyLavUtils,
    HybridCommands,
    PlayerCommands,
    ConfigCommands,
    ContextMenus,
    SlashCommands,
    metaclass=CompositeMetaClass,
):
    """A media player using the PyLav library.

    Playback, playlists, effects, radio, lyrics, local files, event
    notifications, nodes and the PyLav library settings - all of it.
    """

    __version__ = "1.0.0"

    # Defined in shared.py, where the two modules that hang commands off it
    # can import it; bound here so discord.py collects it.
    player = player_group

    def __init__(self, bot: DISCORD_BOT_TYPE, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.bot = bot
        # cog_name pinned for the same reason the parts below pin theirs: the
        # class used to be called PyLavPlayer, and Config keys storage on it.
        self._config = Config.get_conf(
            self, identifier=208903205982044161, cog_name="PyLavPlayer"
        )
        self._config.register_guild(enable_slash=True, enable_context=True)
        self._config.register_global(
            enable_slash=False,
            enable_context=False,
        )
        self.context_user_play = discord.app_commands.ContextMenu(
            name=_("Play from activity"),
            callback=self._context_user_play,
            type=AppCommandType.user,
            extras={"red_force_enable": True},
        )
        self.context_message_play = discord.app_commands.ContextMenu(
            name=_("Play from message"),
            callback=self._context_message_play,
            type=AppCommandType.message,
            extras={"red_force_enable": True},
        )
        self.bot.tree.add_command(self.context_user_play)
        self.bot.tree.add_command(self.context_message_play)
        self._track_cache = ExpiringDict(max_len=float("inf"), max_age_seconds=60)  # type: ignore

        # The parts that set up more than self.bot.
        self._controller_init()
        self._playlists_init()
        self._effects_init()
        self._notifier_init()
        self._ytradio_init()

    async def initialize(self, *args: Any, **kwargs: Any) -> None:
        """Called by PyLav once the client is up."""
        await self._controller_initialize()
        await self._notifier_initialize()

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.context_user_play, type=AppCommandType.user)
        self.bot.tree.remove_command(self.context_message_play, type=AppCommandType.message)
        await self._controller_unload()
        await self._notifier_unload()
        await self._ytradio_unload()

    async def red_delete_data_for_user(
        self,
        *,
        requester: Literal["discord_deleted_user", "owner", "user", "user_strict"],
        user_id: int,
    ) -> None:
        # One call per store: the parts that kept a Config of their own
        # still have it, pinned to the name it was written under.
        await self._config.user_from_id(user_id).clear()
        await self._controller_config.user_from_id(user_id).clear()
        await self._playlists_config.user_from_id(user_id).clear()
        await self._effects_config.user_from_id(user_id).clear()
        await self._notifier_config.user_from_id(user_id).clear()
        await self._ytradio_config.user_from_id(user_id).clear()
