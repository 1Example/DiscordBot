import logging
import os
from datetime import datetime
from typing import Any, Optional

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path

from ..config.defaults import (
    DEFAULT_CHANNEL,
    DEFAULT_GLOBAL,
    DEFAULT_GUILD,
    DEFAULT_MEMBER,
    DEFAULT_ROLE,
)
from ..core.dispatch import handle_message
from ..core.random_message_task import RandomMessageTask
from ..core.reply_queue import cancel_reply_state_tasks
from ..core.services import AIUserServices
from ..dashboard.base import DashboardIntegration
from ..providers.llm.openai_compatible.client import (
    get_openai_client,
    invalidate_openai_client,
)
from ..settings.base import Settings
from ..types.abc import CompositeMetaClass

logger = logging.getLogger("red.bz_cogs.aiuser")
logging.getLogger("httpcore").setLevel(logging.WARNING)


class AIUser(
    DashboardIntegration,
    Settings,
    commands.Cog,
    metaclass=CompositeMetaClass,
):
    """
    Human-like Discord interactions powered by OpenAI (or compatible endpoints) for messages (and images).
    """

    __version__ = "2.4.6"

    def __init__(self, bot: Red):
        super().__init__()
        self.bot: Red = bot
        self.config = Config.get_conf(self, identifier=754070)
        self.services: Optional[AIUserServices] = None
        self.random_task: Optional[RandomMessageTask] = None

        self.config.register_member(**DEFAULT_MEMBER)
        self.config.register_role(**DEFAULT_ROLE)
        self.config.register_channel(**DEFAULT_CHANNEL)
        self.config.register_guild(**DEFAULT_GUILD)
        self.config.register_global(**DEFAULT_GLOBAL)

    async def cog_load(self):
        os.environ["TOKENIZERS_PARALLELISM"] = "false"

        # hide for better debugging experience
        logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)
        logging.getLogger("openai._base_client").setLevel(logging.WARNING)
        logging.getLogger("aiosqlite").setLevel(logging.WARNING)
        logging.getLogger("hpack").setLevel(logging.WARNING)

        self.services = await AIUserServices.create(
            self.bot, self.config, cog_data_path(self), cog=self
        )
        await get_openai_client(self.services)

        debug_guild_id = os.environ.get("AIUSER_DEBUG_GUILD")
        if debug_guild_id and debug_guild_id.isdigit():
            # for development: reset prompt start time for a test guild
            self.services.override_prompt_start_time[int(debug_guild_id)] = (
                datetime.now()
            )

        self.random_task = RandomMessageTask(self.services)
        self.random_task.start()

    async def cog_unload(self):
        if self.services:
            cancel_reply_state_tasks(self.services)
            await invalidate_openai_client(self.services)
        if self.random_task:
            self.random_task.cancel()

    def format_help_for_context(self, ctx: commands.Context) -> str:
        pre_processed = super().format_help_for_context(ctx)
        return f"{pre_processed}\nCog Version: {self.__version__}"

    async def red_delete_data_for_user(self, *, requester: Any, user_id: int):
        for guild in self.bot.guilds:
            await self.config.member_from_ids(guild.id, user_id).clear()

        await self.services.consent.remove_user_data(user_id)

        await self.services.memories.delete_user_memories(user_id)

    @commands.Cog.listener()
    async def on_red_api_tokens_update(self, service_name: str, _):
        if service_name in ["openai", "openrouter"]:
            await invalidate_openai_client(self.services)

    @commands.Cog.listener()
    async def on_message_without_command(self, message: discord.Message):
        if self.services is None:
            return
        await handle_message(self.services, message)
