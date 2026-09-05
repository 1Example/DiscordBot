import logging
from typing import Tuple

import discord
from discord import app_commands

from redbot.core import Config, commands
from redbot.core.app_commands import checks as app_checks
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.mod import get_audit_reason

from .dashboard_integration import DashboardIntegration

log = logging.getLogger("red.admin")

T_ = Translator("Admin", __file__)

_ = lambda s: s
GENERIC_FORBIDDEN = _(
    "I attempted to do something that Discord denied me permissions for."
    " Your command failed to successfully complete."
)

HIERARCHY_ISSUE_ADD = _(
    "I can not give {role.name} to {member.display_name}"
    " because that role is higher than or equal to my highest role"
    " in the Discord hierarchy."
)

HIERARCHY_ISSUE_REMOVE = _(
    "I can not remove {role.name} from {member.display_name}"
    " because that role is higher than or equal to my highest role"
    " in the Discord hierarchy."
)

ROLE_HIERARCHY_ISSUE = _(
    "I can not edit {role.name}"
    " because that role is higher than my or equal to highest role"
    " in the Discord hierarchy."
)

USER_HIERARCHY_ISSUE_ADD = _(
    "I can not let you give {role.name} to {member.display_name}"
    " because that role is higher than or equal to your highest role"
    " in the Discord hierarchy."
)

USER_HIERARCHY_ISSUE_REMOVE = _(
    "I can not let you remove {role.name} from {member.display_name}"
    " because that role is higher than or equal to your highest role"
    " in the Discord hierarchy."
)

ROLE_USER_HIERARCHY_ISSUE = _(
    "I can not let you edit {role.name}"
    " because that role is higher than or equal to your highest role"
    " in the Discord hierarchy."
)

NEED_MANAGE_ROLES = _('I need the "Manage Roles" permission to do that.')

RUNNING_ANNOUNCEMENT = _(
    "I am already announcing something. If you would like to make a"
    " different announcement please use `{prefix}announce cancel`"
    " first."
)
_ = T_


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

    @staticmethod
    def pass_hierarchy_check(ctx: commands.Context, role: discord.Role) -> bool:
        """
        Determines if the bot has a higher role than the given one.
        :param ctx:
        :param role: Role object.
        :return:
        """
        return ctx.guild.me.top_role > role

    @staticmethod
    def pass_user_hierarchy_check(ctx: commands.Context, role: discord.Role) -> bool:
        """
        Determines if a user is allowed to add/remove/edit the given role.
        :param ctx:
        :param role:
        :return:
        """
        return ctx.author.top_role > role or ctx.author == ctx.guild.owner

    async def _addrole(
        self, ctx: commands.Context, member: discord.Member, role: discord.Role, *, check_user=True
    ):
        if member.get_role(role.id) is not None:
            await ctx.send(
                _("{member.display_name} already has the role {role.name}.").format(
                    role=role, member=member
                )
            )
            return
        if check_user and not self.pass_user_hierarchy_check(ctx, role):
            await ctx.send(_(USER_HIERARCHY_ISSUE_ADD).format(role=role, member=member))
            return
        if not self.pass_hierarchy_check(ctx, role):
            await ctx.send(_(HIERARCHY_ISSUE_ADD).format(role=role, member=member))
            return
        if not ctx.guild.me.guild_permissions.manage_roles:
            await ctx.send(_(NEED_MANAGE_ROLES))
            return
        try:
            reason = get_audit_reason(ctx.author)
            await member.add_roles(role, reason=reason)
        except discord.Forbidden:
            await ctx.send(_(GENERIC_FORBIDDEN))
        else:
            await ctx.send(
                _("I successfully added {role.name} to {member.display_name}").format(
                    role=role, member=member
                )
            )

    async def _removerole(
        self, ctx: commands.Context, member: discord.Member, role: discord.Role, *, check_user=True
    ):
        if member.get_role(role.id) is None:
            await ctx.send(
                _("{member.display_name} does not have the role {role.name}.").format(
                    role=role, member=member
                )
            )
            return
        if check_user and not self.pass_user_hierarchy_check(ctx, role):
            await ctx.send(_(USER_HIERARCHY_ISSUE_REMOVE).format(role=role, member=member))
            return
        if not self.pass_hierarchy_check(ctx, role):
            await ctx.send(_(HIERARCHY_ISSUE_REMOVE).format(role=role, member=member))
            return
        if not ctx.guild.me.guild_permissions.manage_roles:
            await ctx.send(_(NEED_MANAGE_ROLES))
            return
        try:
            reason = get_audit_reason(ctx.author)
            await member.remove_roles(role, reason=reason)
        except discord.Forbidden:
            await ctx.send(_(GENERIC_FORBIDDEN))
        else:
            await ctx.send(
                _("I successfully removed {role.name} from {member.display_name}").format(
                    role=role, member=member
                )
            )

    @app_commands.command(
        name="addrole",
        description="Give a role to someone.",
        extras={"red_force_enable": True},
    )
    @app_commands.guild_only()
    @app_checks.admin_or_permissions(manage_roles=True)
    @app_commands.describe(
        rolename="The role to give.",
        user="Who to give it to. Defaults to you.",
    )
    async def addrole(
        self,
        interaction: discord.Interaction,
        rolename: discord.Role,
        user: discord.Member = None,
    ):
        """
        Add a role to a user.

        Use double quotes if the role contains spaces.
        If user is left blank it defaults to the author of the command.
        """
        ctx = await commands.Context.from_interaction(interaction)
        await self._addrole(ctx, user or ctx.author, rolename)

    @app_commands.command(
        name="removerole",
        description="Take a role away from someone.",
        extras={"red_force_enable": True},
    )
    @app_commands.guild_only()
    @app_checks.admin_or_permissions(manage_roles=True)
    @app_commands.describe(
        rolename="The role to take away.",
        user="Who to take it from. Defaults to you.",
    )
    async def removerole(
        self,
        interaction: discord.Interaction,
        rolename: discord.Role,
        user: discord.Member = None,
    ):
        """
        Remove a role from a user.

        Use double quotes if the role contains spaces.
        If user is left blank it defaults to the author of the command.
        """
        ctx = await commands.Context.from_interaction(interaction)
        await self._removerole(ctx, user or ctx.author, rolename)

    async def _valid_selfroles(self, guild: discord.Guild) -> Tuple[discord.Role]:
        """
        Returns a tuple of valid selfroles
        :param guild:
        :return:
        """
        selfrole_ids = set(await self.config.guild(guild).selfroles())
        guild_roles = guild.roles

        valid_roles = tuple(r for r in guild_roles if r.id in selfrole_ids)
        valid_role_ids = set(r.id for r in valid_roles)

        if selfrole_ids != valid_role_ids:
            await self.config.guild(guild).selfroles.set(list(valid_role_ids))

        # noinspection PyTypeChecker
        return valid_roles

    @app_commands.command(
        name="selfrole",
        description="Give yourself a self-assignable role, or take it off.",
        extras={"red_force_enable": True},
    )
    @app_commands.guild_only()
    @app_commands.describe(role="Which role. Only the ones set up here are offered.")
    async def selfrole(self, interaction: discord.Interaction, role: str):
        """Add or remove a selfrole from yourself.

        The role comes from what the server has made self-assignable, so the
        case-sensitivity and ambiguous-name errors the prefix version needed
        are gone.
        """
        ctx = await commands.Context.from_interaction(interaction)
        valid = await self._valid_selfroles(ctx.guild)
        chosen = discord.utils.get(valid, id=int(role)) if role.isdigit() else None
        if chosen is None:
            await ctx.send(_("That is not one of this server's selfroles."))
            return
        if ctx.author.get_role(chosen.id) is not None:
            await self._removerole(ctx, ctx.author, chosen, check_user=False)
        else:
            await self._addrole(ctx, ctx.author, chosen, check_user=False)

    @selfrole.autocomplete("role")
    async def _selfrole_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice]:
        """Only the roles this server has made self-assignable."""
        if interaction.guild is None:
            return []
        current = current.casefold()
        return [
            app_commands.Choice(name=role.name[:100], value=str(role.id))
            for role in await self._valid_selfroles(interaction.guild)
            if current in role.name.casefold()
        ][:25]
