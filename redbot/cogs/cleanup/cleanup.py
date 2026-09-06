import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Union

import discord

from discord import app_commands

from redbot.core import commands, Config
from redbot.core.bot import Red
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.chat_formatting import humanize_number
from redbot.core.utils.mod import mass_purge
from redbot.core.utils.views import confirm
from .dashboard_integration import DashboardIntegration

_ = Translator("Cleanup", __file__)

log = logging.getLogger("red.cleanup")


@cog_i18n(_)
class Cleanup(DashboardIntegration, commands.Cog):
    """Bulk-delete messages from a channel.

    Filter by author, by content, by age or by count. Discord refuses to
    mass delete anything older than two weeks.
    """

    def __init__(self, bot: Red):
        super().__init__()
        self.bot = bot
        self.config = Config.get_conf(self, 8927348724, force_registration=True)
        self.config.register_guild(notify=True)

    async def red_delete_data_for_user(self, **kwargs):
        """Nothing to delete"""
        return

    cleanup = app_commands.Group(
        name="cleanup",
        description="Bulk-delete messages from this channel.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_messages=True),
        extras={"red_force_enable": True},
    )

    Count = app_commands.Range[int, 1, 10000]

    @staticmethod
    def _snowflake(raw: str) -> Optional[int]:
        """A message ID from an option.

        These arrive as text because a snowflake does not fit in the integer
        an option can carry - 2**53 - without losing its low digits.
        """
        raw = raw.strip()
        return int(raw) if raw.isdigit() else None

    async def _purge(
        self,
        interaction: discord.Interaction,
        to_delete: List[discord.Message],
        *,
        what: str,
    ) -> None:
        """Delete the gathered messages and say how many went."""
        channel, author = interaction.channel, interaction.user
        reason = "{} ({}) deleted {} messages in channel #{}.".format(
            author, author.id, len(to_delete), channel.name
        )
        log.info(reason)
        await mass_purge(to_delete, channel, reason=reason)
        await interaction.followup.send(
            _("Deleted {number} {what}.").format(
                number=humanize_number(len(to_delete)), what=what
            ),
            ephemeral=True,
        )
        await self.send_optional_notification(len(to_delete), channel)

    async def _confirm_large(self, interaction: discord.Interaction, number: int) -> bool:
        """Ask before going over a hundred, as the prefix commands did."""
        if number <= 100:
            return True
        ctx = await commands.Context.from_interaction(interaction)
        return await confirm(
            ctx,
            _(
                "Are you sure you want to delete {number} messages? "
                "This cannot be undone."
            ).format(number=humanize_number(number)),
            ephemeral=True,
        )

    @cleanup.command(name="messages", description="Delete the last X messages here.")
    @app_commands.describe(
        number="How many messages to delete.",
        delete_pinned="Delete pinned messages too. Off by default.",
    )
    async def cleanup_messages(
        self, interaction: discord.Interaction, number: Count, delete_pinned: bool = False
    ):
        """Delete the last X messages in this channel."""
        await interaction.response.defer(ephemeral=True)
        if not await self._confirm_large(interaction, number):
            return
        to_delete = await self.get_messages_for_deletion(
            channel=interaction.channel, number=number, delete_pinned=delete_pinned
        )
        await self._purge(interaction, to_delete, what=_("messages"))

    @cleanup.command(name="user", description="Delete a member's recent messages.")
    @app_commands.describe(
        user="Whose messages to delete.",
        number="How many of their messages to delete.",
        delete_pinned="Delete pinned messages too. Off by default.",
    )
    async def cleanup_user(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        number: Count,
        delete_pinned: bool = False,
    ):
        """Delete the last X messages from one member."""
        await interaction.response.defer(ephemeral=True)
        if not await self._confirm_large(interaction, number):
            return
        to_delete = await self.get_messages_for_deletion(
            channel=interaction.channel,
            number=number,
            check=lambda m: m.author.id == user.id,
            delete_pinned=delete_pinned,
        )
        await self._purge(
            interaction, to_delete, what=_("messages from {user}").format(user=user)
        )

    @cleanup.command(name="text", description="Delete recent messages containing some text.")
    @app_commands.describe(
        text="The text to look for. Case sensitive, as it always was.",
        number="How many matching messages to delete.",
        delete_pinned="Delete pinned messages too. Off by default.",
    )
    async def cleanup_text(
        self,
        interaction: discord.Interaction,
        text: str,
        number: Count,
        delete_pinned: bool = False,
    ):
        """Delete the last X messages that contain some text."""
        await interaction.response.defer(ephemeral=True)
        if not await self._confirm_large(interaction, number):
            return
        to_delete = await self.get_messages_for_deletion(
            channel=interaction.channel,
            number=number,
            check=lambda m: text in m.content,
            delete_pinned=delete_pinned,
        )
        await self._purge(interaction, to_delete, what=_("messages"))

    @cleanup.command(name="bot", description="Delete my messages and command calls.")
    @app_commands.describe(
        number="How many to delete.",
        delete_pinned="Delete pinned messages too. Off by default.",
    )
    async def cleanup_bot(
        self, interaction: discord.Interaction, number: Count, delete_pinned: bool = False
    ):
        """Delete the last X messages from bots."""
        await interaction.response.defer(ephemeral=True)
        if not await self._confirm_large(interaction, number):
            return
        to_delete = await self.get_messages_for_deletion(
            channel=interaction.channel,
            number=number,
            check=lambda m: m.author.bot,
            delete_pinned=delete_pinned,
        )
        await self._purge(interaction, to_delete, what=_("bot messages"))

    @cleanup.command(name="self", description="Delete your own recent messages.")
    @app_commands.describe(
        number="How many of your messages to delete.",
        contains="Only delete the ones containing this text.",
        delete_pinned="Delete pinned messages too. Off by default.",
    )
    async def cleanup_self(
        self,
        interaction: discord.Interaction,
        number: Count,
        contains: str = None,
        delete_pinned: bool = False,
    ):
        """Delete the last X messages you sent."""
        await interaction.response.defer(ephemeral=True)
        if not await self._confirm_large(interaction, number):
            return
        author_id = interaction.user.id

        def check(message: discord.Message) -> bool:
            if message.author.id != author_id:
                return False
            return contains in message.content if contains else True

        to_delete = await self.get_messages_for_deletion(
            channel=interaction.channel,
            number=number,
            check=check,
            delete_pinned=delete_pinned,
        )
        await self._purge(interaction, to_delete, what=_("of your messages"))

    @cleanup.command(name="duplicates", description="Delete repeated messages.")
    @app_commands.describe(number="How many recent messages to look through.")
    async def cleanup_duplicates(
        self, interaction: discord.Interaction, number: Count = 50
    ):
        """Delete duplicate messages from the last X, keeping the first of each."""
        await interaction.response.defer(ephemeral=True)
        seen = set()

        def check(message: discord.Message) -> bool:
            content = (message.author.id, message.content, [e.to_dict() for e in message.embeds])
            key = str(content)
            if not message.content and not message.embeds:
                return False
            if key in seen:
                return True
            seen.add(key)
            return False

        to_delete = await self.get_messages_for_deletion(
            channel=interaction.channel, limit=number, check=check
        )
        await self._purge(interaction, to_delete, what=_("duplicate messages"))

    @cleanup.command(name="after", description="Delete everything after a message.")
    @app_commands.describe(
        message_id="The ID of the message to start after. It is not deleted.",
        delete_pinned="Delete pinned messages too. Off by default.",
    )
    async def cleanup_after(
        self, interaction: discord.Interaction, message_id: str, delete_pinned: bool = False
    ):
        """Delete every message sent after the one you name."""
        await interaction.response.defer(ephemeral=True)
        after = self._snowflake(message_id)
        if after is None:
            await interaction.followup.send(_("That is not a message ID."), ephemeral=True)
            return
        try:
            after_message = await interaction.channel.fetch_message(after)
        except discord.NotFound:
            await interaction.followup.send(
                _("I cannot find that message in this channel."), ephemeral=True
            )
            return
        to_delete = await self.get_messages_for_deletion(
            channel=interaction.channel, after=after_message, delete_pinned=delete_pinned
        )
        await self._purge(interaction, to_delete, what=_("messages"))

    @cleanup.command(name="before", description="Delete messages sent before a message.")
    @app_commands.describe(
        message_id="The ID of the message to work back from. It is not deleted.",
        number="How many messages before it to delete.",
        delete_pinned="Delete pinned messages too. Off by default.",
    )
    async def cleanup_before(
        self,
        interaction: discord.Interaction,
        message_id: str,
        number: Count,
        delete_pinned: bool = False,
    ):
        """Delete X messages sent before the one you name."""
        await interaction.response.defer(ephemeral=True)
        before = self._snowflake(message_id)
        if before is None:
            await interaction.followup.send(_("That is not a message ID."), ephemeral=True)
            return
        if not await self._confirm_large(interaction, number):
            return
        try:
            before_message = await interaction.channel.fetch_message(before)
        except discord.NotFound:
            await interaction.followup.send(
                _("I cannot find that message in this channel."), ephemeral=True
            )
            return
        to_delete = await self.get_messages_for_deletion(
            channel=interaction.channel,
            number=number,
            before=before_message,
            delete_pinned=delete_pinned,
        )
        await self._purge(interaction, to_delete, what=_("messages"))

    @cleanup.command(name="between", description="Delete everything between two messages.")
    @app_commands.describe(
        first="The ID of the earlier message. It is not deleted.",
        second="The ID of the later message. It is not deleted.",
        delete_pinned="Delete pinned messages too. Off by default.",
    )
    async def cleanup_between(
        self,
        interaction: discord.Interaction,
        first: str,
        second: str,
        delete_pinned: bool = False,
    ):
        """Delete every message between the two you name."""
        await interaction.response.defer(ephemeral=True)
        one, two = self._snowflake(first), self._snowflake(second)
        if one is None or two is None:
            await interaction.followup.send(_("Those are not message IDs."), ephemeral=True)
            return
        try:
            mone = await interaction.channel.fetch_message(one)
            mtwo = await interaction.channel.fetch_message(two)
        except discord.NotFound:
            await interaction.followup.send(
                _("I cannot find one of those messages in this channel."), ephemeral=True
            )
            return
        to_delete = await self.get_messages_for_deletion(
            channel=interaction.channel,
            before=mtwo,
            after=mone,
            delete_pinned=delete_pinned,
        )
        await self._purge(interaction, to_delete, what=_("messages"))

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

