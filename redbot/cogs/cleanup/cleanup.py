import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Union

import discord

from redbot.core import commands, Config
from redbot.core.bot import Red
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.chat_formatting import humanize_number
from .dashboard_integration import DashboardIntegration

_ = Translator("Cleanup", __file__)

log = logging.getLogger("red.cleanup")


@cog_i18n(_)
class Cleanup(DashboardIntegration, commands.Cog):
    """This cog contains commands used for "cleaning up" (deleting) messages.

    This is designed as a moderator tool and offers many convenient use cases.
    All cleanup commands only apply to the channel the command is executed in.

    Messages older than two weeks cannot be mass deleted.
    This is a limitation of the API.
    """

    def __init__(self, bot: Red):
        super().__init__()
        self.bot = bot
        self.config = Config.get_conf(self, 8927348724, force_registration=True)
        self.config.register_guild(notify=True)

    async def red_delete_data_for_user(self, **kwargs):
        """Nothing to delete"""
        return

    @staticmethod
    async def get_messages_for_deletion(
        *,
        channel: Union[
            discord.TextChannel,
            discord.VoiceChannel,
            discord.StageChannel,
            discord.DMChannel,
            discord.Thread,
        ],
        number: Optional[int] = None,
        check: Callable[[discord.Message], bool] = lambda x: True,
        limit: Optional[int] = None,
        before: Union[discord.Message, datetime] = None,
        after: Union[discord.Message, datetime] = None,
        delete_pinned: bool = False,
    ) -> List[discord.Message]:
        """
        Gets a list of messages meeting the requirements to be deleted.
        Generally, the requirements are:
        - We don't have the number of messages to be deleted already
        - The message passes a provided check (if no check is provided,
          this is automatically true)
        - The message is less than 14 days old
        - The message is not pinned

        Warning: Due to the way the API hands messages back in chunks,
        passing after and a number together is not advisable.
        If you need to accomplish this, you should filter messages on
        the entire applicable range, rather than use this utility.
        """

        # This isn't actually two weeks ago to allow some wiggle room on API limits
        two_weeks_ago = datetime.now(timezone.utc) - timedelta(days=14, minutes=-5)

        def message_filter(message):
            return (
                check(message)
                and message.created_at > two_weeks_ago
                and (delete_pinned or not message.pinned)
            )

        if after:
            if isinstance(after, discord.Message):
                after = after.created_at
            after = max(after, two_weeks_ago)

        collected = []
        async for message in channel.history(
            limit=limit, before=before, after=after, oldest_first=False
        ):
            if message.created_at < two_weeks_ago:
                break
            if message_filter(message):
                collected.append(message)
                if number is not None and number <= len(collected):
                    break

        return collected

    async def send_optional_notification(
        self,
        num: int,
        channel: Union[
            discord.TextChannel,
            discord.VoiceChannel,
            discord.StageChannel,
            discord.DMChannel,
            discord.Thread,
        ],
        *,
        subtract_invoking: bool = False,
    ) -> None:
        """
        Sends a notification to the channel that a certain number of messages have been deleted.
        """
        if not channel.guild or await self.config.guild(channel.guild).notify():
            if subtract_invoking:
                num -= 1
            if num == 1:
                await channel.send(_("1 message was deleted."), delete_after=5)
            else:
                await channel.send(
                    _("{num} messages were deleted.").format(num=humanize_number(num)),
                    delete_after=5,
                )

