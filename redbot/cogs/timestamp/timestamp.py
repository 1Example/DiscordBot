from datetime import datetime, timedelta
from enum import Enum
from typing import List, Literal, Optional
from zoneinfo import ZoneInfo, available_timezones

import discord
from babel.dates import format_time, get_timezone_name
from discord import app_commands
from discord.utils import format_dt, snowflake_time
from red_commons.logging import getLogger
from redbot.core import commands, i18n
from redbot.core.commands.converter import parse_relativedelta
from redbot.core.config import Config
from redbot.core.utils.chat_formatting import box, humanize_timedelta
from .dashboard_integration import DashboardIntegration

TIMESTAMP_STYLES = ["R", "D", "d", "T", "t", "F", "f"]

RELATIVE_UNITS = ["years", "months", "weeks", "days", "hours", "minutes", "seconds"]

_ = i18n.Translator("Timestamp", __file__)

log = getLogger("red.trusty-cogs.timestamp")


class TimestampStyle(Enum):
    relative = "R"
    short_time = "t"
    long_time = "T"
    short_date = "d"
    long_date = "D"
    short_date_time = "f"
    long_date_time = "F"

    def __str__(self) -> str:
        return self.value


class BadTimezone(app_commands.TransformerError):
    """A transformer error that says what went wrong.

    Red prints `str(error)` for these, and the base class only knows how
    to say "Failed to convert X to Y".
    """

    def __init__(self, value: str, transformer: app_commands.Transformer):
        super().__init__(value, discord.AppCommandOptionType.string, transformer)
        self.args = (
            _(
                "`{value}` is not a timezone I know. Pick one of the suggestions, "
                "or give a full name like `Europe/Bucharest`."
            ).format(value=value),
        )


class TimezoneConverter(app_commands.Transformer):
    def find(self, bot, argument: str) -> ZoneInfo:
        """The zone `argument` names, by key or by any of its display names."""
        if "/" in argument:
            try:
                return ZoneInfo(argument)
            except Exception:
                raise BadTimezone(argument, self)
        locale = i18n.get_babel_locale()
        now = datetime.now()
        cog = bot.get_cog("Timestamp")
        at = cog.at if cog is not None else available_timezones()
        for zone in at:
            tnow = now.astimezone(ZoneInfo(zone))
            try:
                name = get_timezone_name(tnow, locale=locale)
                short = get_timezone_name(tnow, width="short", locale=locale)
                tz = f"{short} {name} ({zone})"
            except LookupError:
                continue
            if len(argument) <= 3 and argument.lower() == short.lower():
                return ZoneInfo(zone)
            elif argument.lower() in tz.lower():
                return ZoneInfo(zone)
        raise BadTimezone(argument, self)

    async def transform(self, interaction: discord.Interaction, argument: str) -> ZoneInfo:
        return self.find(interaction.client, argument)

    async def autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice]:
        g_locale = await i18n.get_locale_from_guild(interaction.client, interaction.guild)
        locale = i18n.get_babel_locale(g_locale)
        choices = []
        now = datetime.now()
        cog = interaction.client.get_cog("Timestamp")
        at = cog.at
        for zone in at:
            tnow = now.astimezone(ZoneInfo(zone))
            zone_name = zone
            try:
                name = get_timezone_name(tnow, locale=locale)
                short = get_timezone_name(tnow, width="short", locale=locale)
                ts_now = format_time(tnow, format="short", locale=locale)
                zone_name = f"{short} {name} ({zone}) {ts_now}"
            except Exception:
                pass
            if current.lower() in zone_name.lower():
                choices.append(app_commands.Choice(name=zone_name, value=zone))
        return choices[:25]


class Timestamp(DashboardIntegration, commands.Cog):
    """Build Discord timestamps, which read in each viewer's own timezone."""

    __author__ = ["TrustyJAID"]
    __version__ = "1.4.0"

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, 218773382617890828)
        self.config.register_user(timezone=None)
        self._repo = ""
        self._commit = ""
        self.at = available_timezones()
        # cache these since it opens a lot of files

    def format_help_for_context(self, ctx: commands.Context) -> str:
        """
        Thanks Sinbad!
        """
        pre_processed = super().format_help_for_context(ctx)
        ret = f"{pre_processed}\n\n- Cog Version: {self.__version__}\n"
        # we'll only have a repo if the cog was installed through Downloader at some point
        if self._repo:
            ret += f"- Repo: {self._repo}\n"
        # we should have a commit if we have the repo but just incase
        if self._commit:
            ret += f"- Commit: [{self._commit[:9]}]({self._repo}/tree/{self._commit})"
        return ret

    async def red_delete_data_for_user(
        self,
        *,
        requester: Literal["discord_deleted_user", "owner", "user", "user_strict"],
        user_id: int,
    ):
        await self.config.user_from_id(user_id).clear()

    async def cog_before_invoke(self, ctx: commands.Context):
        await self._get_commit()

    async def _get_commit(self):
        if self._repo:
            return
        downloader = self.bot.get_cog("Downloader")
        if not downloader:
            return
        cogs = await downloader.installed_cogs()
        for cog in cogs:
            if cog.name == "timestamp":
                if cog.repo is not None:
                    self._repo = cog.repo.clean_url
                self._commit = cog.commit

    discord_timestamp = app_commands.Group(
        name="timestamp",
        description="Make your very own discord timestamp.",
        extras={"red_force_enable": True},
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
    )

    async def send_all_styles(self, ctx: commands.Context, new_time: datetime, *, msg: str = ""):
        msg += f"ISO 8601 Timestamp\n{box(new_time.isoformat())}"
        for style in TimestampStyle:
            ts = format_dt(new_time, style.value)
            name = style.name.replace("_", " ").title()
            msg += f"{name}: {ts}\n{box(ts)}\n"
        await ctx.maybe_send_embed(msg)

    @discord_timestamp.command(
        name="absolute",
        description="Produce a timestamp for a specific date and time.",
    )
    @app_commands.describe(
        year="The year. Defaults to the current year.",
        month="The month. Defaults to the current month.",
        day="The day. Defaults to the current day.",
        hour="The hour, 0-23. Defaults to the current hour.",
        minute="The minute. Defaults to 0.",
        second="The second. Defaults to 0.",
        timezone="The timezone the time above is in. Defaults to the one you set.",
        style="Which style to post. Leave this out to see every style at once.",
    )
    async def absolute_timestamp(
        self,
        interaction: discord.Interaction,
        year: Optional[app_commands.Range[int, 1970, 9999]] = None,
        month: Optional[app_commands.Range[int, 1, 12]] = None,
        day: Optional[app_commands.Range[int, 1, 31]] = None,
        hour: Optional[app_commands.Range[int, 0, 23]] = None,
        minute: app_commands.Range[int, 0, 59] = 0,
        second: app_commands.Range[int, 0, 59] = 0,
        timezone: Optional[app_commands.Transform[ZoneInfo, TimezoneConverter]] = None,
        style: Optional[TimestampStyle] = None,
    ):
        """Produce an absolute timestamp.

        Anything left out is taken from right now, in your timezone.
        """
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        msg = ""
        usertz = await self.config.user(ctx.author).timezone()
        if timezone is not None:
            zone = timezone
        elif usertz:
            zone = ZoneInfo(usertz)
        else:
            zone = ZoneInfo("UTC")
            msg += _(
                "You haven't set your timezone, so this is UTC. "
                "Set one on the dashboard, or pass `timezone:` here."
            )
        now = datetime.now(tz=zone)
        try:
            new_time = datetime(
                year=now.year if year is None else year,
                month=now.month if month is None else month,
                day=now.day if day is None else day,
                hour=now.hour if hour is None else hour,
                minute=minute,
                second=second,
                tzinfo=zone,
            )
        except ValueError as e:
            # e.g. the 31st of February.
            await ctx.send(str(e))
            return
        utc_time = new_time.astimezone(ZoneInfo("UTC"))
        try:
            locale = i18n.get_babel_locale()
            from_tz = get_timezone_name(new_time, locale=locale)
            short_from_tz = get_timezone_name(new_time, width="short", locale=locale)
            to_tz = get_timezone_name(utc_time, locale=locale)
            short_to_tz = get_timezone_name(utc_time, width="short", locale=locale)
        except ValueError as e:
            await ctx.send(str(e))
            return
        if style is not None:
            await ctx.send(format_dt(utc_time, style.value))
            return
        await self.send_all_styles(
            ctx, utc_time, msg=f"{msg}\n{from_tz} ({short_from_tz}) to {to_tz} ({short_to_tz})\n"
        )

    @discord_timestamp.command(
        name="relative",
        description="Produce a timestamp relative to right now.",
    )
    @app_commands.describe(
        relative_time="How far from now. e.g. 2 hours, or 1 day 6 hours.",
        style="Which style to post. Leave this out to see every style at once.",
    )
    async def relative_timestamp(
        self,
        interaction: discord.Interaction,
        relative_time: str,
        style: Optional[TimestampStyle] = None,
    ):
        """Produce a timestamp relative to right now.

        Accepts years, months, weeks, days, hours, minutes and seconds.
        """
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        try:
            delta = parse_relativedelta(relative_time, allowed_units=RELATIVE_UNITS)
        except commands.BadArgument as e:
            # Out of range, or a unit this command does not accept.
            await ctx.send(str(e))
            return
        if delta is None:
            await ctx.send(
                _(
                    "`{value}` is not an amount of time I understand. "
                    "Try something like `2 hours` or `1 day 6 hours`."
                ).format(value=relative_time[:100])
            )
            return
        now = interaction.created_at
        new_time = now + delta
        # convert back to a timedelta from the relativedelta and humanize
        td = humanize_timedelta(timedelta=new_time - now)
        if style is not None:
            await ctx.send(format_dt(new_time, style.value))
            return
        await self.send_all_styles(ctx, new_time, msg=f"{td}\n")

    @discord_timestamp.command(
        name="snowflake",
        description="Show when a Discord ID was created.",
    )
    @app_commands.describe(
        snowflake="A Discord ID: a user, channel, server or message ID.",
        style="Which style to post. Leave this out to see every style at once.",
    )
    async def snowflake_timestamp(
        self,
        interaction: discord.Interaction,
        snowflake: str,
        style: Optional[TimestampStyle] = None,
    ):
        """Produce a snowflake's timestamp.

        Discord IDs carry the time they were created. Taken as text because
        they are bigger than a slash command's integer option can hold.
        """
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        try:
            flake = int(snowflake.strip())
            new_time = snowflake_time(flake)
        except (ValueError, OverflowError, OSError):
            await ctx.send(_("`{value}` is not a Discord ID.").format(value=snowflake[:100]))
            return
        if style is not None:
            await ctx.send(format_dt(new_time, style.value))
            return
        await self.send_all_styles(ctx, new_time, msg=f"Discord ID `{flake}`\n")
