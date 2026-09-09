import logging


from redbot.core import Config, commands
from redbot.core.i18n import Translator, cog_i18n

from .dashboard_integration import DashboardIntegration

log = logging.getLogger("red.admin")

_ = Translator("Admin", __file__)


@cog_i18n(_)
class Admin(DashboardIntegration, commands.Cog):
    """A collection of server administration utilities."""

    def __init__(self, bot):
        self.bot = bot

        self.config = Config.get_conf(self, 8237492837454039, force_registration=True)

        self.config.register_global(serverlocked=False, schema_version=0)

        self.config.register_guild(
            announce_channel=None,  # Integer ID
            selfroles=[],  # List of integer ID's
        )

        self.__current_announcer = None

    async def cog_load(self) -> None:
        await self.handle_migrations()

    async def red_delete_data_for_user(self, **kwargs):
        """Nothing to delete"""
        return

    async def handle_migrations(self):
        lock = self.config.get_guilds_lock()
        async with lock:
            # This prevents the edge case of someone loading admin,
            # unloading it, loading it again during a migration
            current_schema = await self.config.schema_version()

            if current_schema == 0:
                await self.migrate_config_from_0_to_1()
                await self.config.schema_version.set(1)

    async def migrate_config_from_0_to_1(self) -> None:
        all_guilds = await self.config.all_guilds()

        for guild_id, guild_data in all_guilds.items():
            if guild_data.get("announce_ignore", False):
                async with self.config.guild_from_id(guild_id).all(
                    acquire_lock=False
                ) as guild_config:
                    guild_config.pop("announce_channel", None)
                    guild_config.pop("announce_ignore", None)

    def cog_unload(self):
        try:
            self.__current_announcer.cancel()
        except AttributeError:
            pass

    def is_announcing(self) -> bool:
        """
        Is the bot currently announcing something?
        :return:
        """
        if self.__current_announcer is None:
            return False

        return self.__current_announcer.active or False

