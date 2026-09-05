import discord
from discord import app_commands

from redbot.core import commands
from redbot.core.i18n import Translator

__all__ = ("trivia_stop_check",)

_ = Translator("Trivia", __file__)


def trivia_stop_check():
    """Only someone who can end this session may end it.

    An application command check: the prefix `commands.permissions_check` this
    used to be is accepted on an app command and then never consulted, so the
    session was stoppable by anyone.
    """

    async def predicate(interaction: discord.Interaction) -> bool:
        cog = interaction.client.get_cog("Trivia")
        session = cog and cog._get_trivia_session(interaction.channel)
        if session is None:
            raise commands.UserFeedbackCheckFailure(
                _("There is no ongoing trivia session in this channel.")
            )

        author = interaction.user
        guild = interaction.guild
        auth_checks = (
            await interaction.client.is_owner(author),
            await interaction.client.is_mod(author),
            await interaction.client.is_admin(author),
            guild is not None and author == guild.owner,
            author == session.ctx.author,
        )
        return any(auth_checks)

    return app_commands.check(predicate)
