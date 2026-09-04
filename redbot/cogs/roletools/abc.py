from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

import discord
from red_commons.logging import getLogger
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.commands import Context
from redbot.core.i18n import Translator

from .converter import (
    ButtonStyleConverter,
    RawUserIds,
    RoleEmojiConverter,
    RoleHierarchyConverter,
    SelfRoleConverter,
)

if TYPE_CHECKING:
    from .buttons import ButtonRole
    from .select import SelectRole


log = getLogger("red.trusty-cogs.ReTrigger")
_ = Translator("Roletools", __file__)


class RoleToolsMixin(ABC):
    """
    Base class for well behaved type hint detection with composite class.

    Basically, to keep developers sane when not all attributes are defined in each mixin.
    """

    def __init__(self, *_args):
        super().__init__()
        self.config: Config
        self.bot: Red
        self.settings: Dict[Any, Any]
        self._ready: asyncio.Event
        self.views: Dict[int, Dict[str, discord.ui.View]]
        self.is_discord: bool

    roletools = app_commands.Group(
        name="roletools",
        description="Assign roles, post role menus and see how roles are set up.",
        guild_only=True,
        extras={"red_force_enable": True},
    )

    #######################################################################
    # roletools.py                                                        #
    #######################################################################

    @abstractmethod
    async def confirm_selfassignable(
        self, ctx: commands.Context, roles: List[discord.Role]
    ) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def selfrole(self, ctx: commands.Context, *, role: SelfRoleConverter) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def selfrole_add(self, ctx: commands.Context, *, role: SelfRoleConverter) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def selfrole_remove(self, ctx: commands.Context, *, role: SelfRoleConverter) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def giverole(
        self,
        ctx: commands.Context,
        role: RoleHierarchyConverter,
        *who: Union[discord.Role, discord.TextChannel, discord.Member, str],
    ) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def removerole(
        self,
        ctx: commands.Context,
        role: RoleHierarchyConverter,
        *who: Union[discord.Role, discord.TextChannel, discord.Member, str],
    ) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def forcerole(
        self,
        ctx: commands.Context,
        users: commands.Greedy[Union[discord.Member, RawUserIds]],
        *,
        role: RoleHierarchyConverter,
    ) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def forceroleremove(
        self,
        ctx: commands.Context,
        users: commands.Greedy[Union[discord.Member, RawUserIds]],
        *,
        role: RoleHierarchyConverter,
    ) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def viewroles(self, ctx: commands.Context, *, role: Optional[discord.Role]) -> None:
        raise NotImplementedError()


    #######################################################################
    # inclusive.py                                                        #
    #######################################################################


    #######################################################################
    # exclusive.py                                                        #
    #######################################################################


    #######################################################################
    # requires.py                                                         #
    #######################################################################


    #######################################################################
    # settings.py                                                         #
    #######################################################################


    #######################################################################
    # reactions.py                                                        #
    #######################################################################


    #######################################################################
    # events.py                                                           #
    #######################################################################

    @abstractmethod
    async def check_guild_verification(
        self, member: discord.Member, guild: discord.Guild
    ) -> Union[bool, int]:
        raise NotImplementedError()

    @abstractmethod
    async def wait_for_verification(self, member: discord.Member, guild: discord.Guild) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def check_atomicity(self, guild: discord.Guild) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def give_roles(
        self,
        member: discord.Member,
        roles: List[discord.Role],
        reason: Optional[str] = None,
        *,
        check_required: bool = True,
        check_exclusive: bool = True,
        check_inclusive: bool = True,
        check_cost: bool = True,
    ) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def remove_roles(
        self,
        member: discord.Member,
        roles: List[discord.Role],
        reason: Optional[str] = None,
        *,
        check_inclusive: bool = True,
    ) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _auto_give(self, member: discord.Member) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _sticky_leave(self, member: discord.Member) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _sticky_join(self, member: discord.Member) -> None:
        raise NotImplementedError()

    #######################################################################
    # buttons.py                                                          #
    #######################################################################

    @abstractmethod
    async def initialize_buttons(self):
        raise NotImplementedError()


    #######################################################################
    # select.py                                                           #
    #######################################################################

    @abstractmethod
    async def initialize_select(self) -> None:
        raise NotImplementedError()


    #######################################################################
    # messages.py                                                         #
    #######################################################################

    @abstractmethod
    async def save_settings(
        self,
        guild: discord.Guild,
        message_key: str,
        *,
        buttons: List[ButtonRole] = [],
        select_menus: List[SelectRole] = [],
    ):
        raise NotImplementedError()


    @abstractmethod
    async def check_and_replace_existing(self, guild_id: int, message_key: str):
        raise NotImplementedError()


