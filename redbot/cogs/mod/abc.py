from abc import ABC, abstractmethod
from typing import Optional

import discord
from redbot.core import Config, app_commands, commands
from redbot.core.bot import Red


class MixinMeta(ABC):
    """
    Base class for well behaved type hint detection with composite class.

    Basically, to keep developers sane when not all attributes are defined in each mixin.
    """

    def __init__(self, *_args):
        self.config: Config
        self.bot: Red
        self.cache: dict

    # The moderation actions are top-level commands, since a moderator reaches
    # for them constantly and `/ban` beats `/mod ban`. modlog is grouped
    # because its members belong together and are used far less often.
    modlog = app_commands.Group(
        name="modlog",
        description="Look up moderation cases and correct their reasons.",
        guild_only=True,
        extras={"red_force_enable": True},
    )

    @staticmethod
    @abstractmethod
    async def _voice_perm_check(
        ctx: commands.Context, user_voice_state: Optional[discord.VoiceState], **perms: bool
    ) -> bool:
        raise NotImplementedError()
