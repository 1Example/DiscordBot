import discord
from discord.ext import tasks
import asyncio
from typing import Optional
from redbot.core import commands, Config
from redbot.core.i18n import Translator, cog_i18n
from .dashboard_integration import DashboardIntegration

_ = Translator("Botstatus", __file__)


@cog_i18n(_)
class Botstatus(DashboardIntegration, commands.Cog):
    """Set a status the bot reapplies after every restart.

    Configured entirely from the dashboard - the [p]botstatus group it used to
    take twenty commands to express is one form there.
    """

    __version__ = "2.0.0"

    def format_help_for_context(self, ctx: commands.Context) -> str:
        # Thanks Sinbad! And Trusty in whose cogs I found this.
        pre_processed = super().format_help_for_context(ctx)
        return f"{pre_processed}\n\nVersion: {self.__version__}"

    async def red_delete_data_for_user(self, *, requester, user_id):
        # This cog stores no EUD
        return

    def __init__(self, bot):
        self.ready = False
        self.bot = bot
        self.config = Config.get_conf(self, identifier=30052000, force_registration=True)
        standard = {"status": (None, None, None)}
        self.config.register_global(**standard)
        self.ready = True
        self.start_task: Optional[asyncio.Task] = None
        self._update_task.start()

    def init(self):
        self.start_task = asyncio.create_task(self.fromconf())

    def cog_unload(self):
        self._update_task.cancel()
        if self.start_task:
            self.start_task.cancel()

    @tasks.loop(minutes=10)
    async def _update_task(self):
        await self.fromconf()

    async def setfunc(self, sType, status, text):
        # Both of these keep older saved values working.
        if sType == "game":
            sType = "playing"
        if status == "away":
            # `[p]botstatus competing away` stored this, but discord.Status has
            # no `away` member, so the lookup below returned False and the
            # status was silently never applied - on every restart too.
            status = "idle"
        if sType == "streaming":
            await self.bot.change_presence(activity=discord.Streaming(name=text, url=status))
        else:
            t = getattr(discord.ActivityType, sType, False)
            s = getattr(discord.Status, status, False)
            if not (t and s):
                return
            activity = discord.Activity(name=text, type=t)
            await self.bot.change_presence(status=s, activity=activity)

    async def fromconf(self):
        await self.bot.wait_until_ready()
        value = await self.config.status()
        if value[0] and value[1] and value[2]:
            await self.setfunc(value[0], value[1], value[2])
