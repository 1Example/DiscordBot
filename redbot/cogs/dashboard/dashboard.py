import argparse
import asyncio
import typing

import discord
from discord import app_commands

# import importlib
# import sys
from fernet import Fernet

from redbot.core import Config, commands
from redbot.core.app_commands import checks as app_checks
from redbot.core.bot import Red
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.cog_base import CogBase

from .logs import DashboardLogHandler
from .rpc import DashboardRPC

# Credits:
# General repo credits.
# Thank you very much to Neuro Assassin for the original code (https://github.com/NeuroAssassin/Toxic-Cogs/tree/master/dashboard)!

_: Translator = Translator("Dashboard", __file__)


class SecretModal(discord.ui.Modal, title="Discord OAuth Secret"):
    """Takes the OAuth secret without it ever being typed into a channel."""

    secret: discord.ui.TextInput = discord.ui.TextInput(
        label=_("Discord Secret"),
        style=discord.TextStyle.short,
        custom_id="discord_secret",
    )

    def __init__(self, cog: "Dashboard") -> None:
        super().__init__()
        self.cog: "Dashboard" = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.config.webserver.core.secret.set(self.secret.value)
        await interaction.response.send_message(
            _("Discord OAuth secret set."), ephemeral=True
        )


@cog_i18n(_)
class Dashboard(CogBase):
    """Interact with your bot through a web Dashboard!

    **Installation guide:** https://red-web-dashboard.readthedocs.io/en/latest
    ⚠️ This package is a fork of Neuro Assassin's work, and isn't endorsed by the Org at all.
    """

    __authors__: list[str] = ["AAA3A", "Neuro Assassin"]

    def __init__(self, bot: Red) -> None:
        super().__init__(bot=bot)

        self.config: Config = Config.get_conf(
            self,
            identifier=205192943327321000143939875896557571750,
            force_registration=True,
        )
        self.CONFIG_SCHEMA: int = 2
        self.config.register_global(
            CONFIG_SCHEMA=None,
            all_in_one=False,
            flask_flags=[],
            webserver={
                "core": {
                    "secret_key": None,
                    "jwt_secret_key": None,
                    "secret": None,
                    "redirect_uri": None,
                    "allow_unsecure_http_requests": False,
                    "blacklisted_ips": [],
                    # Revocation for dashboard logins, as unix timestamps: a
                    # login token issued before the timestamp that applies to
                    # it is refused. "global" covers everyone (the Admin page's
                    # "Refresh sessions"), and a user id covers one person.
                    # Persisted so that revoking a session actually outlives a
                    # restart - and so a restart on its own stops being a
                    # revocation, which is what used to sign everybody out.
                    "session_epochs": {},
                },
                "ui": {
                    "meta": {
                        "title": None,
                        "icon": None,
                        "website_description": None,
                        "description": None,
                        "support_server": None,
                        "default_color": "success",
                        "default_background_theme": "white",
                        "default_sidenav_theme": "white",
                    },
                    "sidenav": [
                        {
                            "pos": 1,
                            "name": "builtin-home",
                            "icon": "ni ni-atom text-success",
                            "route": "base_blueprint.index",
                            "session": None,
                            "owner": False,
                            "locked": True,
                            "hidden": False,
                        },
                        {
                            "pos": 2,
                            "name": "builtin-commands",
                            "icon": "ni ni-bullet-list-67 text-danger",
                            "route": "base_blueprint.commands",
                            "session": None,
                            "owner": False,
                            "locked": False,
                            "hidden": False,
                        },
                        {
                            "pos": 3,
                            "name": "builtin-dashboard",
                            "icon": "ni ni-settings text-primary",
                            "route": "base_blueprint.dashboard",
                            "session": True,
                            "owner": False,
                            "locked": False,
                            "hidden": False,
                        },
                        {
                            "pos": 4,
                            "name": "builtin-third_parties",
                            "icon": "ni ni-diamond text-success",
                            "route": "third_parties_blueprint.third_parties",
                            "session": True,
                            "owner": False,
                            "locked": False,
                            "hidden": False,
                        },
                        {
                            "pos": 5,
                            "name": "builtin-admin",
                            "icon": "ni ni-badge text-danger",
                            "route": "base_blueprint.admin",
                            "session": True,
                            "owner": True,
                            "locked": True,
                            "hidden": False,
                        },
                        {
                            "pos": 6,
                            "name": "builtin-cog-management",
                            "icon": "fa fa-cogs text-primary",
                            "route": "base_blueprint.cog_management",
                            "session": True,
                            "owner": True,
                            "locked": True,
                            "hidden": False,
                        },
                        {
                            "pos": 7,
                            "name": "builtin-credits",
                            "icon": "ni ni-book-bookmark text-info",
                            "route": "base_blueprint.credits",
                            "session": None,
                            "owner": False,
                            "locked": True,
                            "hidden": False,
                        },
                        {
                            "pos": 8,
                            "name": "builtin-login",
                            "icon": "ni ni-key-25 text-success",
                            "route": "login_blueprint.login",
                            "session": False,
                            "owner": False,
                            "locked": True,
                            "hidden": False,
                        },
                        {
                            "pos": 9,
                            "name": "builtin-logout",
                            "icon": "ni ni-user-run text-warning",
                            "route": "login_blueprint.logout",
                            "session": True,
                            "owner": False,
                            "locked": True,
                            "hidden": False,
                        },
                    ],
                },
                "disabled_third_parties": [],
                "custom_pages": [],
            },
        )

        self.app: typing.Any | None = None
        # Feeds the owner-only log viewer. Constructed here but only attached
        # to the root logger in `cog_load`, so an unloaded cog costs nothing.
        self.log_handler: DashboardLogHandler = DashboardLogHandler()
        self.rpc: DashboardRPC = DashboardRPC(bot=self.bot, cog=self)

    async def cog_load(self) -> None:
        await super().cog_load()
        await self.edit_config_schema()
        self.log_handler.install()
        self.logger.info("Loading cog...")
        asyncio.create_task(self.create_app(flask_flags=await self.config.flask_flags()))

    async def edit_config_schema(self) -> None:
        CONFIG_SCHEMA = await self.config.CONFIG_SCHEMA()
        if CONFIG_SCHEMA is None:
            CONFIG_SCHEMA = 1
            await self.config.CONFIG_SCHEMA(CONFIG_SCHEMA)
        if CONFIG_SCHEMA == self.CONFIG_SCHEMA:
            return
        if CONFIG_SCHEMA == 1:
            global_group = self.config._get_base_group(self.config.GLOBAL)
            async with global_group() as global_data:
                if "default_sidebar_theme" in global_data:
                    global_data["default_sidenav_theme"] = global_data.pop("default_sidebar_theme")
            CONFIG_SCHEMA = 2
            await self.config.CONFIG_SCHEMA.set(CONFIG_SCHEMA)
        if CONFIG_SCHEMA < self.CONFIG_SCHEMA:
            CONFIG_SCHEMA = self.CONFIG_SCHEMA
            await self.config.CONFIG_SCHEMA.set(CONFIG_SCHEMA)
        self.logger.info(
            f"The Config schema has been successfully modified to {self.CONFIG_SCHEMA} for the {self.qualified_name} cog.",
        )

    async def cog_unload(self) -> None:
        self.logger.info("Unloading cog...")
        self.log_handler.uninstall()
        if self.app is not None and self.app.server_thread is not None:
            await asyncio.to_thread(self.app.server_thread.shutdown)
            await asyncio.to_thread(self.app.tasks_manager.stop_tasks)
        self.rpc.unload()
        await super().cog_unload()

    async def create_app(self, flask_flags: str) -> None:
        await self.bot.wait_until_red_ready()
        if await self.config.webserver.core.secret_key() is None:
            await self.config.webserver.core.secret_key.set(Fernet.generate_key().decode())
        if await self.config.webserver.core.jwt_secret_key() is None:
            await self.config.webserver.core.jwt_secret_key.set(Fernet.generate_key().decode())
        if await self.config.all_in_one():
            try:
                # for module_name in ("flask", "reddash"):
                #     modules = sorted(
                #         [module for module in sys.modules if module.split(".")[0] == module_name], reverse=True
                #     )
                #     for module in modules:
                #         try:
                #             importlib.reload(sys.modules[module])
                #         except ModuleNotFoundError:
                #             pass
                from reddash import FlaskApp

                parser: argparse.ArgumentParser = argparse.ArgumentParser(exit_on_error=False)
                parser.add_argument("--host", dest="host", type=str, default="0.0.0.0")
                parser.add_argument("--port", dest="port", type=int, default=42356)
                # parser.add_argument("--rpc-port", dest="rpcport", type=int, default=6133)
                parser.add_argument(
                    "--interval",
                    dest="interval",
                    type=int,
                    default=5,
                    help=argparse.SUPPRESS,
                )
                parser.add_argument(
                    "--development",
                    dest="dev",
                    action="store_true",
                    help=argparse.SUPPRESS,
                )
                # parser.add_argument("--instance", dest="instance", type=str, default=None)
                args = vars(parser.parse_args(args=flask_flags))
                self.app: FlaskApp = FlaskApp(cog=self, **args)
                await self.app.create_app()
                await self.app.run_app()
            except Exception as e:
                self.logger.critical("Error when creating the Flask webserver app.", exc_info=e)

    @app_commands.command(
        name="dashboard",
        description="Get the link to the Dashboard.",
        extras={"red_force_enable": True},
    )
    @app_commands.checks.bot_has_permissions(embed_links=True)
    async def dashboard(self, interaction: discord.Interaction) -> None:
        """Get the link to the Dashboard."""
        ctx: commands.Context = await commands.Context.from_interaction(interaction)
        if (dashboard_url := getattr(ctx.bot, "dashboard_url", None)) is None:
            await ctx.send(
                _(
                    "Red-Web-Dashboard is not installed. Check <https://red-web-dashboard.readthedocs.io>.",
                ),
            )
            return
        if not dashboard_url[1] and ctx.author.id not in ctx.bot.owner_ids:
            await ctx.send(_("You can't access the Dashboard."))
            return
        embed: discord.Embed = discord.Embed(
            title=_("Red-Web-Dashboard"),
            color=await ctx.embed_color(),
        )
        url = dashboard_url[0]
        if ctx.guild is not None and (
            ctx.author.id in ctx.bot.owner_ids or await self.bot.is_mod(ctx.author)
        ):
            url += f"/dashboard/{ctx.guild.id}"
            embed.set_footer(text=ctx.guild.name, icon_url=ctx.guild.icon)
        embed.url = url
        await ctx.send(embed=embed)

    # Only what you need before the dashboard will let you in. Everything else
    # is on its Admin page, under Dashboard Settings.
    setdashboard = app_commands.Group(
        name="setdashboard",
        description="Configure the Dashboard's login and webserver.",
        extras={"red_force_enable": True},
    )

    @setdashboard.command(
        name="secret",
        description="Set the client secret needed for Discord OAuth.",
    )
    @app_checks.is_owner()
    async def secret(self, interaction: discord.Interaction) -> None:
        """Set the client secret needed for Discord OAuth.

        Asked for in a modal, so the secret is never typed into a channel.
        """
        await interaction.response.send_modal(SecretModal(self))

    @setdashboard.command(
        name="redirect-uri",
        description="Set the redirect URI to use for the Discord OAuth.",
    )
    @app_commands.describe(uri="The full callback URL, ending in /callback.")
    @app_checks.is_owner()
    async def redirect_uri(self, interaction: discord.Interaction, uri: str) -> None:
        """Set the redirect URI to use for the Discord OAuth."""
        uri = uri.strip()
        if not uri.startswith("http"):
            await interaction.response.send_message(
                _("This is not a valid URL."), ephemeral=True
            )
            return
        if not uri.endswith("/callback"):
            await interaction.response.send_message(
                _("This is not a valid Dashboard redirect URI: it must end with `/callback`."),
                ephemeral=True,
            )
            return
        await self.config.webserver.core.redirect_uri.set(uri)
        await interaction.response.send_message(
            _("Redirect URI set to <{uri}>.").format(uri=uri), ephemeral=True
        )

    @setdashboard.command(
        name="allow-unsecure-http",
        description="Allow plain http. Only for when you cannot set up an SSL certificate.",
    )
    @app_commands.describe(enabled="Whether to serve the dashboard over plain http.")
    @app_checks.is_owner()
    async def allow_unsecure_http_requests(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        """Allow plain http requests."""
        await self.config.webserver.core.allow_unsecure_http_requests.set(enabled)
        await interaction.response.send_message(
            _("Plain http is now allowed.")
            if enabled
            else _("Plain http is no longer allowed."),
            ephemeral=True,
        )

    @setdashboard.command(
        name="all-in-one",
        description="Run the webserver inside the bot instead of a separate process.",
    )
    @app_commands.describe(enabled="Whether the bot should host the webserver itself.")
    @app_checks.is_owner()
    async def all_in_one(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Run the webserver in the bot process.

        Needs Red-Web-Dashboard installed in the bot's venv, and a reload.
        """
        await self.config.all_in_one.set(enabled)
        await interaction.response.send_message(
            _(
                "All-in-one mode is now {state}. Reload the cog to apply it, and make sure "
                "Red-Web-Dashboard is installed in the bot's venv."
            ).format(state=_("enabled") if enabled else _("disabled")),
            ephemeral=True,
        )

    @setdashboard.command(
        name="flask-flags",
        description="The reddash cli flags used when all-in-one is enabled.",
    )
    @app_commands.describe(
        flags="Space-separated reddash flags, without --rpc-port. Leave empty to clear."
    )
    @app_checks.is_owner()
    async def flask_flags(self, interaction: discord.Interaction, flags: str = "") -> None:
        """The flags used to start the webserver if `all_in_one` is enabled."""
        parsed = flags.split()
        await self.config.flask_flags.set(parsed)
        await interaction.response.send_message(
            _("Flask flags set to `{flags}`.").format(flags=" ".join(parsed))
            if parsed
            else _("Flask flags cleared."),
            ephemeral=True,
        )

    @setdashboard.command(
        name="view",
        description="Show the settings that have to be set from here.",
    )
    @app_checks.is_owner()
    async def view_settings(self, interaction: discord.Interaction) -> None:
        """Show the login and webserver settings.

        The secret is only ever reported as set or not.
        """
        core = await self.config.webserver.core.all()
        embed: discord.Embed = discord.Embed(
            title=_("Dashboard setup"),
            description=_(
                "Everything else is on the Dashboard's own Admin page, "
                "under Dashboard Settings."
            ),
            color=await self.bot.get_embed_color(interaction.channel),
        )
        embed.add_field(
            name=_("Discord OAuth secret"),
            value=_("Set") if core["secret"] else _("Not set"),
            inline=False,
        )
        embed.add_field(
            name=_("Redirect URI"),
            value=core["redirect_uri"] or _("Not set (the login page guesses one)"),
            inline=False,
        )
        embed.add_field(
            name=_("Allow unsecure http"),
            value=_("Yes") if core["allow_unsecure_http_requests"] else _("No"),
            inline=False,
        )
        embed.add_field(
            name=_("All in one"),
            value=_("Yes") if await self.config.all_in_one() else _("No"),
            inline=False,
        )
        flags = await self.config.flask_flags()
        embed.add_field(
            name=_("Flask flags"),
            value=f"`{' '.join(flags)}`" if flags else _("None"),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
