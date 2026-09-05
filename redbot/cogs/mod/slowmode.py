import discord
from .abc import MixinMeta
from datetime import timedelta
from redbot.core import app_commands, commands, i18n
from redbot.core.app_commands import checks as app_checks
from redbot.core.utils.chat_formatting import humanize_timedelta, inline

_ = i18n.Translator("Mod", __file__)


class Slowmode(MixinMeta):
    """
    Commands regarding channel slowmode management.
    """

    @app_commands.command(
        name="slowmode",
        description="Set how long members must wait between messages here.",
        extras={"red_force_enable": True},
    )
    @app_commands.guild_only()
    @app_checks.bot_has_permissions(manage_channels=True)
    @app_checks.admin_or_permissions(manage_channels=True)
    @app_commands.describe(
        interval="How long between messages, e.g. 30s, 5m, 1h. 0 turns it off. Max 6 hours."
    )
    async def slowmode(
        self, interaction: discord.Interaction, interval: str = "0"
    ):
        """Change this thread's or text channel's slowmode setting.

        Interval can be anything from 0 seconds to 6 hours.
        """
        ctx = await commands.Context.from_interaction(interaction)
        # This was a TimedeltaConverter, which read a bare number as seconds
        # and rejected anything over six hours with a message of its own.
        raw = interval.strip()
        try:
            interval = commands.parse_timedelta(
                raw + "seconds" if raw.isdecimal() else raw,
                minimum=timedelta(seconds=0),
                maximum=timedelta(hours=6),
            )
        except commands.BadArgument as exc:
            await ctx.send(str(exc))
            return
        if interval is None:
            await ctx.send(
                _(
                    "{value} is not an interval I understand. Try something like "
                    "30s, 5m or 1h, up to 6 hours."
                ).format(value=inline(raw))
            )
            return
        seconds = interval.total_seconds()
        await ctx.channel.edit(slowmode_delay=seconds)
        if seconds > 0:
            await ctx.send(
                _("Slowmode interval is now {interval}.").format(
                    interval=humanize_timedelta(timedelta=interval)
                )
            )
        else:
            await ctx.send(_("Slowmode has been disabled."))
