import logging
import discord
from redbot.core import commands, Config
from redbot.core.i18n import Translator, cog_i18n
from .dashboard_integration import DashboardIntegration

_ = Translator("RoleSyncer", __file__)


@cog_i18n(_)


class RoleSyncer(DashboardIntegration, commands.Cog):
    """Sync Roles"""

    __version__ = "2.1.1"

    def format_help_for_context(self, ctx: commands.Context) -> str:
        # Thanks Sinbad! And Trusty in whose cogs I found this.
        pre_processed = super().format_help_for_context(ctx)
        return f"{pre_processed}\n\nVersion: {self.__version__}"

    async def red_delete_data_for_user(self, *, requester, user_id):
        # This cog doesn't store EUD
        return

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=235228062020)
        default_guild = {"onesync": [], "twosync": []}
        self.config.register_guild(**default_guild)
        self.log = logging.getLogger("red.cog.dav-cogs.rolesyncer")

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if before.roles != after.roles:
            guild = before.guild
            roles = await self.config.guild(after.guild).all()
            for r in roles["onesync"]:
                r1, r2 = guild.get_role(r[0]), guild.get_role(r[1])
                if r1 in after.roles:
                    try:
                        await after.add_roles(
                            r2,
                            reason=_("One-way rolesync / {r1name} added.").format(r1name=r1.name),
                        )
                    except discord.Forbidden as f_to_pay_respect:
                        self.log.warning(
                            "Couldn't assign %s to %s. Missing permissions.\n%s",
                            r2.name,
                            after.name,
                            f_to_pay_respect,
                        )
                    except discord.HTTPException as exception:
                        self.log.exception(exception, exc_info=True)
                elif r1 in before.roles:
                    try:
                        await after.remove_roles(
                            r2,
                            reason=_("One-way rolesync / {r1name} removed.").format(
                                r1name=r1.name
                            ),
                        )
                    except discord.HTTPException as exception:
                        self.log.exception(exception, exc_info=True)
            for r in roles["twosync"]:
                r1, r2 = guild.get_role(r[0]), guild.get_role(r[1])
                if r1 in before.roles and r2 in before.roles:
                    if not r1 in after.roles:
                        try:
                            await after.remove_roles(
                                r2,
                                reason=_("Two-way rolesync / {r1name} removed.").format(
                                    r1name=r1.name
                                ),
                            )
                        except discord.HTTPException as exception:
                            self.log.exception(exception, exc_info=True)
                    elif not r2 in after.roles:
                        try:
                            await after.remove_roles(
                                r1,
                                reason=_("Two-way rolesync / {r2name} removed.").format(
                                    r2name=r2.name
                                ),
                            )
                        except discord.HTTPException as exception:
                            self.log.exception(exception, exc_info=True)
                elif r1 in after.roles:
                    try:
                        await after.add_roles(r2, reason=_("Two-way rolesync"))
                    except discord.HTTPException as exception:
                        self.log.exception(exception, exc_info=True)
                elif r2 in after.roles:
                    try:
                        await after.add_roles(r1, reason=_("Two-way rolesync"))
                    except discord.HTTPException as exception:
                        self.log.exception(exception, exc_info=True)
