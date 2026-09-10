import asyncio
import datetime
import importlib
import itertools
import json
import keyword
import logging
import io
import random
import re
import zipfile
import sys
import platform
import time
import traceback
from pathlib import Path
from redbot.core import app_commands
from redbot.core.app_commands import checks as app_checks
from redbot.core.utils.menus import menu
from redbot.core.commands import GuildConverter
from string import ascii_letters, digits
from typing import TYPE_CHECKING, Union, List, Optional, Iterable, Sequence, Dict, Set, Literal

import aiohttp
import discord
from packaging.version import Version

from . import __version__, commands, errors, i18n, modlog, _downloader
from ._diagnoser import IssueDiagnoser
from .config import Config
from .utils import AsyncIter, can_user_send_messages_in
from .utils._internal_utils import fetch_latest_red_version
from .utils.predicates import MessagePredicate
from .utils.chat_formatting import box, humanize_list, humanize_timedelta, inline, pagify, warning

if TYPE_CHECKING:
    from redbot.core.bot import Red

__all__ = ["Core"]

log = logging.getLogger("red")

_ = i18n.Translator("Core", __file__)

TokenConverter = commands.get_dict_converter(delims=[" ", ",", ";"])

MAX_PREFIX_LENGTH = 25
MINIMUM_PREFIX_LENGTH = 1


class CoreLogic:
    def __init__(self, bot: "Red"):
        self.bot = bot
        # (command, user id) -> when it last did something. mydata keeps its
        # own cooldowns because an app command's cannot be reset.
        self._mydata_stamps: Dict[tuple, float] = {}
        self.bot.register_rpc_handler(self._load)
        self.bot.register_rpc_handler(self._unload)
        self.bot.register_rpc_handler(self._reload)
        self.bot.register_rpc_handler(self._name)
        self.bot.register_rpc_handler(self._prefixes)
        self.bot.register_rpc_handler(self._version_info)
        self.bot.register_rpc_handler(self._invite_url)

    async def _load(self, pkg_names: Iterable[str]) -> Dict[str, Union[List[str], Dict[str, str]]]:
        """
        Loads packages by name.

        Parameters
        ----------
        pkg_names : `list` of `str`
            List of names of packages to load.

        Returns
        -------
        dict
            Dictionary with keys:
              ``loaded_packages``: List of names of packages that loaded successfully
              ``failed_packages``: List of names of packages that failed to load without specified reason
              ``invalid_pkg_names``: List of names of packages that don't have a valid package name
              ``notfound_packages``: List of names of packages that weren't found in any cog path
              ``alreadyloaded_packages``: List of names of packages that are already loaded
              ``failed_with_reason_packages``: Dictionary of packages that failed to load with
              a specified reason with mapping of package names -> failure reason
              ``repos_with_shared_libs``: List of repo names that use deprecated shared libraries
        """
        failed_packages = []
        loaded_packages = []
        invalid_pkg_names = []
        notfound_packages = []
        alreadyloaded_packages = []
        failed_with_reason_packages = {}
        repos_with_shared_libs = set()

        bot = self.bot

        pkg_specs = []

        for name in pkg_names:
            if not name.isidentifier() or keyword.iskeyword(name):
                invalid_pkg_names.append(name)
                continue
            try:
                spec = await bot._cog_mgr.find_cog(name)
                if spec:
                    pkg_specs.append((spec, name))
                else:
                    notfound_packages.append(name)
            except Exception as e:
                log.exception("Package import failed", exc_info=e)

                exception_log = "Exception during import of package\n"
                exception_log += "".join(traceback.format_exception(type(e), e, e.__traceback__))
                bot._last_exception = exception_log
                failed_packages.append(name)

        async for spec, name in AsyncIter(pkg_specs, steps=10):
            try:
                self._cleanup_and_refresh_modules(spec.name)
                await bot.load_extension(spec)
            except errors.PackageAlreadyLoaded:
                alreadyloaded_packages.append(name)
            except errors.CogLoadError as e:
                failed_with_reason_packages[name] = str(e)
            except Exception as e:
                if isinstance(e, commands.CommandRegistrationError):
                    if e.alias_conflict:
                        error_message = _(
                            "Alias {alias_name} is already an existing command"
                            " or alias in one of the loaded cogs."
                        ).format(alias_name=inline(e.name))
                    else:
                        error_message = _(
                            "Command {command_name} is already an existing command"
                            " or alias in one of the loaded cogs."
                        ).format(command_name=inline(e.name))
                    failed_with_reason_packages[name] = error_message
                    continue

                log.exception("Package loading failed", exc_info=e)

                exception_log = "Exception during loading of package\n"
                exception_log += "".join(traceback.format_exception(type(e), e, e.__traceback__))
                bot._last_exception = exception_log
                failed_packages.append(name)
            else:
                await bot.add_loaded_package(name)
                loaded_packages.append(name)
                try:
                    maybe_repo = await _downloader._shared_lib_load_check(name)
                except Exception:
                    log.exception(
                        "Shared library check failed,"
                        " if you're not using modified Downloader, report this issue."
                    )
                    maybe_repo = None
                if maybe_repo is not None:
                    repos_with_shared_libs.add(maybe_repo.name)

        return {
            "loaded_packages": loaded_packages,
            "failed_packages": failed_packages,
            "invalid_pkg_names": invalid_pkg_names,
            "notfound_packages": notfound_packages,
            "alreadyloaded_packages": alreadyloaded_packages,
            "failed_with_reason_packages": failed_with_reason_packages,
            "repos_with_shared_libs": list(repos_with_shared_libs),
        }

    @staticmethod
    def _cleanup_and_refresh_modules(module_name: str) -> None:
        """Internally reloads modules so that changes are detected."""
        splitted = module_name.split(".")

        def maybe_reload(new_name):
            try:
                lib = sys.modules[new_name]
            except KeyError:
                pass
            else:
                importlib._bootstrap._exec(lib.__spec__, lib)

        # noinspection PyTypeChecker
        modules = itertools.accumulate(splitted, "{}.{}".format)
        for m in modules:
            maybe_reload(m)

        children = {
            name: lib
            for name, lib in sys.modules.items()
            if name == module_name or name.startswith(f"{module_name}.")
        }
        for child_name, lib in children.items():
            importlib._bootstrap._exec(lib.__spec__, lib)

    async def _unload(self, pkg_names: Iterable[str]) -> Dict[str, List[str]]:
        """
        Unloads packages with the given names.

        Parameters
        ----------
        pkg_names : `list` of `str`
            List of names of packages to unload.

        Returns
        -------
        dict
            Dictionary with keys:
              ``unloaded_packages``: List of names of packages that unloaded successfully.
              ``notloaded_packages``: List of names of packages that weren't unloaded
              because they weren't loaded.
        """
        notloaded_packages = []
        unloaded_packages = []

        bot = self.bot

        for name in pkg_names:
            if name in bot.extensions:
                await bot.unload_extension(name)
                await bot.remove_loaded_package(name)
                unloaded_packages.append(name)
            else:
                notloaded_packages.append(name)

        return {"unloaded_packages": unloaded_packages, "notloaded_packages": notloaded_packages}

    async def _reload(
        self, pkg_names: Sequence[str]
    ) -> Dict[str, Union[List[str], Dict[str, str]]]:
        """
        Reloads packages with the given names.

        Parameters
        ----------
        pkg_names : `list` of `str`
            List of names of packages to reload.

        Returns
        -------
        dict
            Dictionary with keys as returned by `CoreLogic._load()`
        """
        await self._unload(pkg_names)

        return await self._load(pkg_names)

    async def _name(self, name: Optional[str] = None) -> str:
        """
        Gets or sets the bot's username.

        Parameters
        ----------
        name : str
            If passed, the bot will change it's username.

        Returns
        -------
        str
            The current (or new) username of the bot.
        """
        if name is not None:
            return (await self.bot.user.edit(username=name)).name

        return self.bot.user.name

    async def _prefixes(self, prefixes: Optional[Sequence[str]] = None) -> List[str]:
        """
        Gets or sets the bot's global prefixes.

        Parameters
        ----------
        prefixes : list of str
            If passed, the bot will set it's global prefixes.

        Returns
        -------
        list of str
            The current (or new) list of prefixes.
        """
        if prefixes:
            await self.bot.set_prefixes(guild=None, prefixes=prefixes)
            return prefixes
        return await self.bot._prefix_cache.get_prefixes(guild=None)

    @classmethod
    async def _version_info(cls) -> Dict[str, str]:
        """
        Version information for Red and discord.py

        Returns
        -------
        dict
            `redbot` and `discordpy` keys containing version information for both.
        """
        return {"redbot": __version__, "discordpy": discord.__version__}

    async def _invite_url(self) -> str:
        """
        Generates the invite URL for the bot.

        Returns
        -------
        str
            Invite URL.
        """
        return await self.bot.get_invite_url()

    @staticmethod
    async def _can_get_invite_url(ctx):
        is_owner = await ctx.bot.is_owner(ctx.author)
        is_invite_public = await ctx.bot._config.invite_public()
        return is_owner or is_invite_public


@i18n.cog_i18n(_)
class Core(commands.commands._RuleDropper, commands.Cog, CoreLogic):
    """
    The Core cog has many commands related to core functions.

    These commands come loaded with every Red bot, and cover some of the most basic usage of the bot.
    """

    botinfo = app_commands.Group(
        name="bot",
        description="About me, and the levers only my owner can pull.",
        extras={"red_force_enable": True},
    )

    async def red_delete_data_for_user(self, **kwargs):
        """Nothing to delete (Core Config is handled in a bot method)"""
        return

    async def _resolve_guilds(self, ctx, raw: str):
        """The guilds `raw` names, or None once the complaint is sent.

        `leave` took *servers: GuildConverter, which a slash command cannot
        express; this reads the same IDs and names out of one string. An empty
        string means the server the command was run in, which is what leaving
        with no arguments did.
        """
        wanted = raw.split()
        if not wanted:
            return []
        found = []
        for item in wanted:
            guild = None
            if item.isdigit():
                guild = self.bot.get_guild(int(item))
            if guild is None:
                guild = discord.utils.get(self.bot.guilds, name=item)
            if guild is None:
                await ctx.send(
                    _("I could not find a server called `{name}`.").format(name=item[:100])
                )
                return None
            found.append(guild)
        return found

    @staticmethod
    async def _as_id(ctx, raw: str):
        """A snowflake from a string, or None once the complaint is sent.

        Discord IDs are larger than a slash command's integer option holds, so
        they arrive as text.
        """
        try:
            return int(raw.strip())
        except ValueError:
            await ctx.send(_("`{value}` is not a user ID.").format(value=raw[:100]))
            return None

    @botinfo.command(
        name="ping",
        description="Check how fast I answer Discord.",
        extras={"red_force_enable": True},
    )
    async def ping(
        self,
        interaction: discord.Interaction,
    ):
        """Pong."""
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.send("Pong.")

    @botinfo.command(
        name="info",
        description="Show information about me.",
        extras={"red_force_enable": True},
    )
    async def info(
        self,
        interaction: discord.Interaction,
    ):
        """Shows info about [botname]."""
        ctx = await commands.Context.from_interaction(interaction)
        embed_links = await ctx.embed_requested()
        author_repo = "https://github.com/Twentysix26"
        red_repo = "https://github.com/Cog-Creators/Red-DiscordBot"
        contributors_url = red_repo + "/graphs/contributors"
        red_pypi = "https://pypi.org/project/Red-DiscordBot"
        support_server_url = "https://discord.gg/red"
        fork_repo = "https://github.com/1Example/DiscordBot"
        fork_owner_url = "https://discord.com/users/212320651334254593"
        dpy_repo = "https://github.com/Rapptz/discord.py"
        python_url = "https://www.python.org/"
        since = datetime.datetime(2016, 1, 2, 0, 0)
        days_since = (datetime.datetime.utcnow() - since).days

        app_info = await self.bot.application_info()
        if app_info.team:
            owner = app_info.team.name
        else:
            owner = app_info.owner
        custom_info = await self.bot._config.custom_info()

        try:
            latest = await fetch_latest_red_version()
        except (aiohttp.ClientError, TimeoutError) as exc:
            log.error("Failed to fetch latest version information from PyPI.", exc_info=exc)
            pypi_version = None
        else:
            pypi_version = latest.version
        outdated = pypi_version and pypi_version > Version(__version__)

        if embed_links:
            dpy_version = "[{}]({})".format(discord.__version__, dpy_repo)
            python_version = "[{}.{}.{}]({})".format(*sys.version_info[:3], python_url)
            red_version = "[{}]({})".format(__version__, red_pypi)

            about = _(
                "This bot runs [CUBIX]({fork}), a heavily modified fork of "
                "[Red, an open source Discord bot]({red}), maintained by "
                "[1Example]({owner}).\n\n"
                "The commands, the web dashboard and most of what you interact "
                "with here have been rewritten or replaced. The foundation is "
                "still Red, created by [Twentysix]({author}) and [improved by "
                "many]({contributors}), and this fork carries the same GNU "
                "GPLv3 licence. Red is backed by a passionate community - "
                "[join them]({support}) and help them improve it.\n\n"
                "(c) Cog Creators"
            ).format(
                fork=fork_repo,
                red=red_repo,
                owner=fork_owner_url,
                author=author_repo,
                contributors=contributors_url,
                support=support_server_url,
            )

            embed = discord.Embed(color=(await ctx.embed_colour()))
            embed.add_field(
                name=_("Instance owned by team") if app_info.team else _("Instance owned by"),
                value=str(owner),
            )
            embed.add_field(name="Python", value=python_version)
            embed.add_field(name="discord.py", value=dpy_version)
            embed.add_field(name=_("Red core"), value=red_version)
            if outdated in (True, None):
                if outdated is True:
                    outdated_value = _("Yes, {version} is available.").format(
                        version=str(pypi_version)
                    )
                else:
                    outdated_value = _("Checking for updates failed.")
                embed.add_field(name=_("Outdated"), value=outdated_value)
            if custom_info:
                embed.add_field(name=_("About this instance"), value=custom_info, inline=False)
            embed.add_field(name=_("About this bot"), value=about, inline=False)

            embed.set_footer(
                text=_("Bringing joy since 02 Jan 2016 (over {} days ago!)").format(days_since)
            )
            await ctx.send(embed=embed)
        else:
            python_version = "{}.{}.{}".format(*sys.version_info[:3])
            dpy_version = "{}".format(discord.__version__)
            red_version = "{}".format(__version__)

            about = _(
                "This bot runs CUBIX (8), a heavily modified fork of Red, an "
                "open source Discord bot (1), maintained by 1Example.\n\n"
                "The commands, the web dashboard and most of what you interact "
                "with here have been rewritten or replaced. The foundation is "
                "still Red, created by Twentysix (2) and improved by many (3), "
                "and this fork carries the same GNU GPLv3 licence. Red is "
                "backed by a passionate community - join them (4) and help "
                "them improve it.\n\n"
                "(c) Cog Creators"
            )
            about = box(about)

            if app_info.team:
                extras = _(
                    "Instance owned by team: [{owner}]\n"
                    "Python:                 [{python_version}] (5)\n"
                    "discord.py:             [{dpy_version}] (6)\n"
                    "Red core:               [{red_version}] (7)\n"
                ).format(
                    owner=owner,
                    python_version=python_version,
                    dpy_version=dpy_version,
                    red_version=red_version,
                )
            else:
                extras = _(
                    "Instance owned by: [{owner}]\n"
                    "Python:            [{python_version}] (5)\n"
                    "discord.py:        [{dpy_version}] (6)\n"
                    "Red core:          [{red_version}] (7)\n"
                ).format(
                    owner=owner,
                    python_version=python_version,
                    dpy_version=dpy_version,
                    red_version=red_version,
                )

            if outdated in (True, None):
                if outdated is True:
                    outdated_value = _("Yes, {version} is available.").format(
                        version=str(pypi_version)
                    )
                else:
                    outdated_value = _("Checking for updates failed.")
                extras += _("Outdated:          [{state}]\n").format(state=outdated_value)

            red = (
                _("**About Red**\n")
                + about
                + "\n"
                + box(extras, lang="ini")
                + "\n"
                + _("Bringing joy since 02 Jan 2016 (over {} days ago!)").format(days_since)
                + "\n\n"
            )

            await ctx.send(red)
            if custom_info:
                custom_info = _("**About this instance**\n") + custom_info + "\n\n"
                await ctx.send(custom_info)
            refs = _(
                "**References**\n"
                "1. <{}>\n"
                "2. <{}>\n"
                "3. <{}>\n"
                "4. <{}>\n"
                "5. <{}>\n"
                "6. <{}>\n"
                "7. <{}>\n"
                "8. <{}>\n"
            ).format(
                red_repo,
                author_repo,
                contributors_url,
                support_server_url,
                python_url,
                dpy_repo,
                red_pypi,
                fork_repo,
            )
            await ctx.send(refs)

    @botinfo.command(
        name="uptime",
        description="Show how long I have been running.",
        extras={"red_force_enable": True},
    )
    async def uptime(
        self,
        interaction: discord.Interaction,
    ):
        """Shows [botname]'s uptime."""
        ctx = await commands.Context.from_interaction(interaction)
        delta = datetime.datetime.utcnow() - self.bot.uptime
        uptime = self.bot.uptime.replace(tzinfo=datetime.timezone.utc)
        uptime_str = humanize_timedelta(timedelta=delta) or _("Less than one second.")
        await ctx.send(
            _("I have been up for: **{time_quantity}** (since {timestamp})").format(
                time_quantity=uptime_str, timestamp=discord.utils.format_dt(uptime, "f")
            )
        )

    cog = app_commands.Group(
        name="cog",
        description="My modules: run them, find them, turn them off.",
        extras={"red_force_enable": True},
        default_permissions=discord.Permissions(administrator=True),
    )
    # /cogpath and /cogset were separate roots saying the same word. The path
    # commands still live in CogManagerUI - two cogs cannot share a group, and
    # moving them would leave their translations behind.
    cog_path = app_commands.Group(
        name="path",
        description="Where I look for cogs.",
        parent=cog,
    )

    async def _cog_manager_ui(self, ctx):
        """The cog holding the path commands. bot.py always adds it."""
        ui = self.bot.get_cog("CogManagerUI")
        if ui is None:
            await ctx.send(_("The cog manager is not available."), ephemeral=True)
        return ui

    @cog_path.command(name="list", description="Every path I search, in order.")
    @app_checks.is_owner()
    async def cog_path_list(self, interaction: discord.Interaction):
        """Show the cog paths, the install path and the core path."""
        ctx = await commands.Context.from_interaction(interaction)
        if (ui := await self._cog_manager_ui(ctx)) is not None:
            await ui.show_paths(ctx)

    @cog_path.command(name="add", description="Add a path to search.")
    @app_checks.is_owner()
    @app_commands.describe(path="A folder on the machine I run on.")
    async def cog_path_add(self, interaction: discord.Interaction, path: str):
        """Add a folder to search for cogs."""
        ctx = await commands.Context.from_interaction(interaction)
        if (ui := await self._cog_manager_ui(ctx)) is not None:
            await ui.add_path(ctx, path)

    @cog_path.command(name="remove", description="Remove a path by its number.")
    @app_checks.is_owner()
    @app_commands.describe(path_numbers="Numbers from the list, separated by spaces.")
    async def cog_path_remove(self, interaction: discord.Interaction, path_numbers: str):
        """Remove one or more paths."""
        ctx = await commands.Context.from_interaction(interaction)
        if (ui := await self._cog_manager_ui(ctx)) is not None:
            await ui.remove_paths(ctx, path_numbers)

    @cog_path.command(name="reorder", description="Move a path up or down the order.")
    @app_checks.is_owner()
    @app_commands.describe(
        from_="The number it has now.",
        to="The number it should have.",
    )
    async def cog_path_reorder(
        self,
        interaction: discord.Interaction,
        from_: app_commands.Range[int, 1, None],
        to: app_commands.Range[int, 1, None],
    ):
        """Change which path is searched first."""
        ctx = await commands.Context.from_interaction(interaction)
        if (ui := await self._cog_manager_ui(ctx)) is not None:
            await ui.reorder_path(ctx, from_, to)

    @cog_path.command(name="install", description="Set where Downloader installs cogs.")
    @app_checks.is_owner()
    @app_commands.describe(path="The folder to install into. See it with /cog path list.")
    async def cog_path_install(self, interaction: discord.Interaction, path: str):
        """Set the install path. Nothing already installed moves."""
        ctx = await commands.Context.from_interaction(interaction)
        if (ui := await self._cog_manager_ui(ctx)) is not None:
            await ui.set_install_path(ctx, path)

    async def _cog_choices(
        self, interaction: discord.Interaction, current: str, loaded: bool
    ) -> List[app_commands.Choice]:
        """Module names, either the running ones or the ones that are not."""
        current = (current or "").lower()
        try:
            available = set(await self.bot._cog_mgr.available_modules())
        except Exception:  # noqa: BLE001
            available = set()
        running = {name for name in self.bot.extensions}
        names = sorted(running if loaded else (available - running))
        return [
            app_commands.Choice(name=name, value=name)
            for name in names
            if current in name.lower()
        ][:25]

    async def _loadable(self, interaction: discord.Interaction, current: str):
        return await self._cog_choices(interaction, current, loaded=False)

    async def _loaded(self, interaction: discord.Interaction, current: str):
        return await self._cog_choices(interaction, current, loaded=True)

    @staticmethod
    def _cog_names(raw: str) -> List[str]:
        """One command takes several names, separated by spaces or commas."""
        return [part for part in re.split(r"[\s,]+", (raw or "").strip()) if part]

    @cog.command(name="load", description="Start a module that is not running.")
    @app_checks.is_owner()
    @app_commands.describe(names="One or more module names, separated by spaces.")
    @app_commands.autocomplete(names=_loadable)
    async def load_modules(self, interaction: discord.Interaction, names: str):
        """Load one or more modules."""
        ctx = await commands.Context.from_interaction(interaction)
        wanted = self._cog_names(names)
        if not wanted:
            return await ctx.send(_("Name at least one module."), ephemeral=True)
        await ctx.defer(ephemeral=True)
        async with ctx.typing():
            outcome = await self._load(wanted)

        lines = []
        if loaded := outcome["loaded_packages"]:
            lines.append(_("Loaded: {names}").format(names=humanize_list(loaded)))
        if already := outcome["alreadyloaded_packages"]:
            lines.append(_("Already running: {names}").format(names=humanize_list(already)))
        if invalid := outcome["invalid_pkg_names"]:
            lines.append(_("Not a module name: {names}").format(names=humanize_list(invalid)))
        if failed := outcome["failed_packages"]:
            lines.append(
                _("Failed, see the console: {names}").format(names=humanize_list(failed))
            )
        for name, reason in (outcome["failed_with_reason_packages"] or {}).items():
            lines.append(_("{name} failed: {reason}").format(name=name, reason=reason))
        if not_found := outcome.get("notfound_packages"):
            lines.append(_("Not installed: {names}").format(names=humanize_list(not_found)))
        for page in pagify("\n".join(lines) or _("Nothing happened."), page_length=1900):
            await ctx.send(page, ephemeral=True)

    @cog.command(name="unload", description="Stop a running module.")
    @app_checks.is_owner()
    @app_commands.describe(names="One or more module names, separated by spaces.")
    @app_commands.autocomplete(names=_loaded)
    async def unload_modules(self, interaction: discord.Interaction, names: str):
        """Unload one or more modules."""
        ctx = await commands.Context.from_interaction(interaction)
        wanted = self._cog_names(names)
        if not wanted:
            return await ctx.send(_("Name at least one module."), ephemeral=True)
        await ctx.defer(ephemeral=True)
        outcome = await self._unload(wanted)

        lines = []
        if unloaded := outcome["unloaded_packages"]:
            lines.append(_("Unloaded: {names}").format(names=humanize_list(unloaded)))
        if notloaded := outcome["notloaded_packages"]:
            lines.append(_("Was not running: {names}").format(names=humanize_list(notloaded)))
        await ctx.send("\n".join(lines) or _("Nothing happened."), ephemeral=True)

    @cog.command(name="reload", description="Restart a running module.")
    @app_checks.is_owner()
    @app_commands.describe(names="One or more module names, separated by spaces.")
    @app_commands.autocomplete(names=_loaded)
    async def reload_modules(self, interaction: discord.Interaction, names: str):
        """Reload one or more modules."""
        ctx = await commands.Context.from_interaction(interaction)
        wanted = self._cog_names(names)
        if not wanted:
            return await ctx.send(_("Name at least one module."), ephemeral=True)
        await ctx.defer(ephemeral=True)
        async with ctx.typing():
            outcome = await self._reload(wanted)

        lines = []
        if loaded := outcome["loaded_packages"]:
            lines.append(_("Reloaded: {names}").format(names=humanize_list(loaded)))
        if invalid := outcome["invalid_pkg_names"]:
            lines.append(_("Not a module name: {names}").format(names=humanize_list(invalid)))
        if failed := outcome["failed_packages"]:
            lines.append(
                _("Failed, see the console: {names}").format(names=humanize_list(failed))
            )
        for name, reason in (outcome["failed_with_reason_packages"] or {}).items():
            lines.append(_("{name} failed: {reason}").format(name=name, reason=reason))
        if not_found := outcome.get("notfound_packages"):
            lines.append(_("Not installed: {names}").format(names=humanize_list(not_found)))
        for page in pagify("\n".join(lines) or _("Nothing happened."), page_length=1900):
            await ctx.send(page, ephemeral=True)

    @cog.command(name="list", description="Show which modules are running.")
    @app_checks.is_owner()
    async def list_modules(self, interaction: discord.Interaction):
        """List the modules that are installed, and which are running."""
        ctx = await commands.Context.from_interaction(interaction)
        try:
            available = set(await self.bot._cog_mgr.available_modules())
        except Exception:  # noqa: BLE001
            available = set()
        running = sorted(self.bot.extensions)
        # Something running from a path that is no longer searched still counts
        # as running, so union rather than subtract in one direction only.
        idle = sorted(available - set(running))

        embed = discord.Embed(
            title=_("Modules"), colour=await ctx.embed_colour()
        )
        embed.add_field(
            name=_("Running ({count})").format(count=len(running)),
            value=", ".join(f"`{n}`" for n in running) or _("none"),
            inline=False,
        )
        embed.add_field(
            name=_("Installed, not running ({count})").format(count=len(idle)),
            value=", ".join(f"`{n}`" for n in idle) or _("none"),
            inline=False,
        )
        await ctx.send(embed=embed, ephemeral=True)

    mydata = app_commands.Group(
        name="mydata",
        description="What I know about you, and getting rid of it.",
        # Reachable even for someone on the blocklist: shutting a person out
        # must not also shut them out of asking to be forgotten.
        extras={"red_force_enable": True, "red_always_available": True},
    )
    mydata_owner = app_commands.Group(
        name="owner", description="Handling data on someone's behalf.", parent=mydata
    )

    async def _mydata_wait(self, ctx, key: str, seconds: int) -> bool:
        """True once the caller has been told to wait.

        The prefix commands used a cooldown and handed it back whenever they
        had not done anything. discord.py keeps an app command's cooldown in a
        closure with no reset, so this is kept here instead and stamped by the
        body, which gives the same behaviour without the dance.
        """
        now = time.time()
        stamps = self._mydata_stamps
        last = stamps.get((key, ctx.author.id))
        if last is not None and now - last < seconds:
            left = humanize_timedelta(seconds=int(seconds - (now - last)))
            await ctx.send(
                _("You have done that recently. Try again in {time}.").format(time=left),
                ephemeral=True,
            )
            return True
        return False

    def _mydata_stamp(self, ctx, key: str) -> None:
        """Start the wait, now that the command has actually done something."""
        self._mydata_stamps[(key, ctx.author.id)] = time.time()

    # 1/10 minutes. It's a static response, but the inability to lock
    # will annoy people if it's spammable
    @mydata.command(name="whatdata", description="What I store about you, and why.")
    async def mydata_whatdata(
        self,
        interaction: discord.Interaction,
    ):
        """
        Find out what type of data [botname] stores and why.

        **Example:**
        - `[p]mydata whatdata`
        """
        ctx = await commands.Context.from_interaction(interaction)
        if await self._mydata_wait(ctx, "whatdata", 600):
            return
        self._mydata_stamp(ctx, "whatdata")

        message = _(
            "This bot stores some data about you, as much as it needs to work and no more. "
            "Mostly that is the ID Discord assigned you, with whatever a feature has to "
            "remember attached to it: settings you chose, counters a feature keeps such as "
            "your level or balance, things you created so it knows they are yours, and "
            "moderation records where a server's staff have used those features.\n\n"
            "It is all held on the server this bot runs on. None of it is sold, shared or "
            "sent anywhere else.\n\n"
            "Use `/mydata getmydata` for a copy of what is stored about you, and "
            "`/mydata forgetme` to ask the bot to forget you."
        )
        # This is a fork with most of its surface rewritten, so it answers for
        # itself rather than pointing at documentation for unmodified Red.
        dashboard_url = getattr(self.bot, "dashboard_url", None)
        if dashboard_url is not None and dashboard_url[1]:
            message += _("\n\nThe full statement is at <{link}>.").format(
                link=f"{dashboard_url[0].rstrip('/')}/data"
            )
        await ctx.send(message)

    async def get_serious_confirmation(self, ctx: commands.Context, prompt: str) -> bool:
        confirm_token = "".join(random.choices((*ascii_letters, *digits), k=8))

        await ctx.send(f"{prompt}\n\n{confirm_token}")
        try:
            message = await ctx.bot.wait_for(
                "message",
                check=lambda m: m.channel.id == ctx.channel.id and m.author.id == ctx.author.id,
                timeout=30,
            )
        except asyncio.TimeoutError:
            await ctx.send(_("Did not get confirmation, cancelling."))
        else:
            if message.content.strip() == confirm_token:
                return True
            else:
                await ctx.send(_("Did not get a matching confirmation, cancelling."))

        return False

    # 1 per day, not stored to config to avoid this being more stored data.
    # large bots shouldn't be restarting so often that this is an issue,
    # and small bots that do restart often don't have enough
    # users for this to be an issue.
    @mydata.command(name="forgetme", description="Have me forget what I know about you.")
    async def mydata_forgetme(
        self,
        interaction: discord.Interaction,
    ):
        """
        Have [botname] forget what it knows about you.

        This may not remove all data about you, data needed for operation,
        such as command cooldowns will be kept until no longer necessary.

        Further interactions with [botname] may cause it to learn about you again.

        **Example:**
        - `[p]mydata forgetme`
        """
        ctx = await commands.Context.from_interaction(interaction)
        if await self._mydata_wait(ctx, "forgetme", 86400):
            return
        if not await self.get_serious_confirmation(
            ctx,
            _(
                "This will cause the bot to get rid of and/or disassociate "
                "data from you. It will not get rid of operational data such "
                "as modlog entries, warnings, or mutes. "
                "If you are sure this is what you want, "
                "please respond with the following:"
            ),
        ):
            return
        self._mydata_stamp(ctx, "forgetme")
        await ctx.send(_("This may take some time."))

        if await ctx.bot._config.datarequests.user_requests_are_strict():
            requester = "user_strict"
        else:
            requester = "user"

        results = await self.bot.handle_data_deletion_request(
            requester=requester, user_id=ctx.author.id
        )

        if results.failed_cogs and results.failed_modules:
            await ctx.send(
                _(
                    "I tried to delete all non-operational data about you "
                    "(that I know how to delete) "
                    "{mention}, however the following modules errored: {modules}. "
                    "Additionally, the following cogs errored: {cogs}.\n"
                    "Please contact the owner of this bot to address this.\n"
                    "Note: Outside of these failures, data should have been deleted."
                ).format(
                    mention=ctx.author.mention,
                    cogs=humanize_list(results.failed_cogs),
                    modules=humanize_list(results.failed_modules),
                )
            )
        elif results.failed_cogs:
            await ctx.send(
                _(
                    "I tried to delete all non-operational data about you "
                    "(that I know how to delete) "
                    "{mention}, however the following cogs errored: {cogs}.\n"
                    "Please contact the owner of this bot to address this.\n"
                    "Note: Outside of these failures, data should have been deleted."
                ).format(mention=ctx.author.mention, cogs=humanize_list(results.failed_cogs))
            )
        elif results.failed_modules:
            await ctx.send(
                _(
                    "I tried to delete all non-operational data about you "
                    "(that I know how to delete) "
                    "{mention}, however the following modules errored: {modules}.\n"
                    "Please contact the owner of this bot to address this.\n"
                    "Note: Outside of these failures, data should have been deleted."
                ).format(mention=ctx.author.mention, modules=humanize_list(results.failed_modules))
            )
        else:
            await ctx.send(
                _(
                    "I've deleted any non-operational data about you "
                    "(that I know how to delete) {mention}"
                ).format(mention=ctx.author.mention)
            )

        if results.unhandled:
            await ctx.send(
                _("{mention} The following cogs did not handle deletion:\n{cogs}.").format(
                    mention=ctx.author.mention, cogs=humanize_list(results.unhandled)
                )
            )

    async def _collect_user_data(self, user_id: int) -> Dict[str, bytes]:
        """Everything the loaded cogs hold under this user's own ID.

        Two sources. red_get_data_for_user is the documented contract, and the
        few cogs that implement it give the friendliest answer. Most do not, so
        each cog's Config is also swept for the scopes keyed by this user: the
        global user scope, and the member scope in each guild they share with
        the bot. Guild-wide and channel settings are nobody's personal data and
        are left out.
        """
        files: Dict[str, bytes] = {}
        shared_guilds = [g for g in self.bot.guilds if g.get_member(user_id) is not None]

        for cog_name, cog in sorted(self.bot.cogs.items()):
            try:
                provided = await cog.red_get_data_for_user(user_id=user_id)
            except commands.RedUnhandledAPI:
                # The base class raising this is how a cog says it never
                # implemented the hook, which is almost all of them. Its
                # Config is still swept below.
                provided = {}
            except TypeError as e:
                # A cog implementing the hook with the wrong signature.
                log.warning("%s does not implement red_get_data_for_user correctly: %s", cog_name, e)
                provided = {}
            except Exception as e:
                log.exception("Failed to collect data from %s for %s", cog_name, user_id)
                files[f"{cog_name}/ERROR.txt"] = str(e).encode()
                provided = {}
            for filename, fp in (provided or {}).items():
                data = fp.read() if hasattr(fp, "read") else bytes(fp)
                files[f"{cog_name}/{filename}"] = data

            payload = {}
            for attr, config in vars(cog).items():
                if not isinstance(config, Config):
                    continue
                scopes = {}
                if user_scope := await config.user_from_id(user_id).all():
                    scopes["user"] = user_scope
                per_guild = {}
                for guild in shared_guilds:
                    if member_scope := await config.member_from_ids(guild.id, user_id).all():
                        per_guild[f"{guild.name} ({guild.id})"] = member_scope
                if per_guild:
                    scopes["per_server"] = per_guild
                if scopes:
                    payload[attr] = scopes
            if payload:
                files[f"{cog_name}/settings.json"] = json.dumps(
                    payload, indent=2, default=str, ensure_ascii=False
                ).encode()

        return files

    # Two hours. Building this walks every cog's config, and nobody needs a
    # fresh copy more often than that.
    @mydata.command(name="getmydata", description="Get a copy of what I know about you.")
    async def mydata_getdata(
        self,
        interaction: discord.Interaction,
    ):
        """Get a copy of what [botname] has stored about you."""
        ctx = await commands.Context.from_interaction(interaction)
        if not ctx.bot_permissions.attach_files:
            return await ctx.send(
                _("I need to be able to attach files (try in DMs?)."), ephemeral=True
            )
        if await self._mydata_wait(ctx, "getmydata", 7200):
            return

        # Only the person who asked ever sees any of this. The deferral has to
        # be ephemeral too: a public one cannot be answered with a private
        # followup.
        await ctx.defer(ephemeral=True)
        files = await self._collect_user_data(ctx.author.id)

        if not files:
            self._mydata_stamp(ctx, "getmydata")
            return await ctx.send(_("I don't have anything stored about you."), ephemeral=True)

        readme = _(
            "This is everything {bot} has stored under your Discord ID ({user_id}), as of"
            " {when}.\n\nEach folder is one part of the bot. settings.json holds what that"
            " part keeps in its own settings store: `user` is what it knows about you"
            " everywhere, `per_server` is what it knows about you in each server you share"
            " with me.\n\nServer-wide settings are not included, because they belong to the"
            " server rather than to you. Nor is anything Discord itself stores - ask Discord"
            " for that.\n\nTo have this removed, use /mydata forgetme."
        ).format(
            bot=ctx.me.display_name,
            user_id=ctx.author.id,
            when=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        )

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("README.txt", readme)
            for filename, data in sorted(files.items()):
                archive.writestr(filename, data)
        buffer.seek(0)

        limit = ctx.guild.filesize_limit if ctx.guild else 10 * 1024 * 1024
        if buffer.getbuffer().nbytes > limit:
            self._mydata_stamp(ctx, "getmydata")
            return await ctx.send(
                _("Your data is too large for me to upload here. Ask the bot owner for it."),
                ephemeral=True,
            )

        self._mydata_stamp(ctx, "getmydata")
        await ctx.send(
            _("Here is everything I have stored about you. Only you can see this message."),
            file=discord.File(buffer, filename=f"mydata-{ctx.author.id}.zip"),
            ephemeral=True,
        )


    @mydata_owner.command(name="allowdeletions", description="Let people delete their own data, or stop letting them.")
    @app_checks.is_owner()
    @app_commands.describe(allowed="Whether a person may ask me to forget them.")
    async def mydata_owner_allow_user_deletions(
        self,
        interaction: discord.Interaction,
        allowed: bool,
    ):
        """Set whether a person may ask to have their own data deleted.

        On by default. This was two commands, allowuserdeletions and
        disallowuserdeletions; it is one with a switch.
        """
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.bot._config.datarequests.allow_user_requests.set(allowed)
        await ctx.send(
            _(
                "Users can delete their own data. This will not include operational data such as blocked users."
            )
            if allowed
            else _("Users can not delete their own data.")
        )


    @mydata_owner.command(name="deletionlevel", description="How thoroughly a person's own deletion request is honoured.")
    @app_checks.is_owner()
    @app_commands.describe(level="0 leaves it to each cog. 1 removes everything a cog does not need.")
    async def mydata_owner_user_deletion_level(
        self,
        interaction: discord.Interaction,
        level: app_commands.Range[int, 0, 1],
    ):
        """
        Sets how user deletions are treated.

        **Example:**
        - `[p]mydata ownermanagement setuserdeletionlevel 1`

        **Arguments:**
        - `<level>` - The strictness level for user deletion. See Level guide below.

        Level:
        - `0`: What users can delete is left entirely up to each cog.
        - `1`: Cogs should delete anything the cog doesn't need about the user.
        """
        ctx = await commands.Context.from_interaction(interaction)

        if level == 1:
            await ctx.bot._config.datarequests.user_requests_are_strict.set(True)
            await ctx.send(
                _(
                    "Cogs will be instructed to remove all non operational "
                    "data upon a user request."
                )
            )
        elif level == 0:
            await ctx.bot._config.datarequests.user_requests_are_strict.set(False)
            await ctx.send(
                _(
                    "Cogs will be informed a user has made a data deletion request, "
                    "and the details of what to delete will be left to the "
                    "discretion of the cog author."
                )
            )
        else:
            await ctx.send(_("I did not understand that."))

    @mydata_owner.command(name="discordrequest", description="Process a deletion request that came from Discord.")
    @app_checks.is_owner()
    @app_commands.describe(user_id="The ID of the deleted user.")
    async def mydata_discord_deletion_request(
        self,
        interaction: discord.Interaction,
        user_id: str,
    ):
        """
        Handle a deletion request from Discord.

        This will cause the bot to get rid of or disassociate all data from the specified user ID.
        You should not use this unless Discord has specifically requested this with regard to a deleted user.
        This will remove the user from various anti-abuse measures.
        If you are processing a manual request from a user, you may want `[p]mydata ownermanagement deleteforuser` instead.

        **Arguments:**
        - `<user_id>` - The id of the user whose data would be deleted.
        """
        ctx = await commands.Context.from_interaction(interaction)
        user_id = await self._as_id(ctx, user_id)
        if user_id is None:
            return

        if not await self.get_serious_confirmation(
            ctx,
            _(
                "This will cause the bot to get rid of or disassociate all data "
                "from the specified user ID. You should not use this unless "
                "Discord has specifically requested this with regard to a deleted user. "
                "This will remove the user from various anti-abuse measures. "
                "If you are processing a manual request from a user, you may want "
                "`/{command_name}` instead."
                "\n\nIf you are sure this is what you intend to do "
                "please respond with the following:"
            ).format(command_name="mydata ownermanagement deleteforuser"),
        ):
            return
        results = await self.bot.handle_data_deletion_request(
            requester="discord_deleted_user", user_id=user_id
        )

        if results.failed_cogs and results.failed_modules:
            await ctx.send(
                _(
                    "I tried to delete all data about that user, "
                    "(that I know how to delete) "
                    "however the following modules errored: {modules}. "
                    "Additionally, the following cogs errored: {cogs}\n"
                    "Please check your logs and contact the creators of "
                    "these cogs and modules.\n"
                    "Note: Outside of these failures, data should have been deleted."
                ).format(
                    cogs=humanize_list(results.failed_cogs),
                    modules=humanize_list(results.failed_modules),
                )
            )
        elif results.failed_cogs:
            await ctx.send(
                _(
                    "I tried to delete all data about that user, "
                    "(that I know how to delete) "
                    "however the following cogs errored: {cogs}.\n"
                    "Please check your logs and contact the creators of "
                    "these cogs and modules.\n"
                    "Note: Outside of these failures, data should have been deleted."
                ).format(cogs=humanize_list(results.failed_cogs))
            )
        elif results.failed_modules:
            await ctx.send(
                _(
                    "I tried to delete all data about that user, "
                    "(that I know how to delete) "
                    "however the following modules errored: {modules}.\n"
                    "Please check your logs and contact the creators of "
                    "these cogs and modules.\n"
                    "Note: Outside of these failures, data should have been deleted."
                ).format(modules=humanize_list(results.failed_modules))
            )
        else:
            await ctx.send(_("I've deleted all data about that user that I know how to delete."))

        if results.unhandled:
            await ctx.send(
                _("{mention} The following cogs did not handle deletion:\n{cogs}.").format(
                    mention=ctx.author.mention, cogs=humanize_list(results.unhandled)
                )
            )

    @mydata_owner.command(name="deleteforuser", description="Delete someone's data on their behalf.")
    @app_checks.is_owner()
    @app_commands.describe(user_id="Whose data to delete.")
    async def mydata_user_deletion_request_by_owner(
        self,
        interaction: discord.Interaction,
        user_id: str,
    ):
        """Delete data [botname] has about a user for a user.

        This will cause the bot to get rid of or disassociate a lot of non-operational data from the specified user.
        Users have access to a different command for this unless they can't interact with the bot at all.
        This is a mostly safe operation, but you should not use it unless processing a request from this user as it may impact their usage of the bot.

        **Arguments:**
        - `<user_id>` - The id of the user whose data would be deleted.
        """
        ctx = await commands.Context.from_interaction(interaction)
        user_id = await self._as_id(ctx, user_id)
        if user_id is None:
            return
        if not await self.get_serious_confirmation(
            ctx,
            _(
                "This will cause the bot to get rid of or disassociate "
                "a lot of non-operational data from the "
                "specified user. Users have access to "
                "different command for this unless they can't interact with the bot at all. "
                "This is a mostly safe operation, but you should not use it "
                "unless processing a request from this "
                "user as it may impact their usage of the bot. "
                "\n\nIf you are sure this is what you intend to do "
                "please respond with the following:"
            ),
        ):
            return

        if await ctx.bot._config.datarequests.user_requests_are_strict():
            requester = "user_strict"
        else:
            requester = "user"

        results = await self.bot.handle_data_deletion_request(requester=requester, user_id=user_id)

        if results.failed_cogs and results.failed_modules:
            await ctx.send(
                _(
                    "I tried to delete all non-operational data about that user, "
                    "(that I know how to delete) "
                    "however the following modules errored: {modules}. "
                    "Additionally, the following cogs errored: {cogs}\n"
                    "Please check your logs and contact the creators of "
                    "these cogs and modules.\n"
                    "Note: Outside of these failures, data should have been deleted."
                ).format(
                    cogs=humanize_list(results.failed_cogs),
                    modules=humanize_list(results.failed_modules),
                )
            )
        elif results.failed_cogs:
            await ctx.send(
                _(
                    "I tried to delete all non-operational data about that user, "
                    "(that I know how to delete) "
                    "however the following cogs errored: {cogs}.\n"
                    "Please check your logs and contact the creators of "
                    "these cogs and modules.\n"
                    "Note: Outside of these failures, data should have been deleted."
                ).format(cogs=humanize_list(results.failed_cogs))
            )
        elif results.failed_modules:
            await ctx.send(
                _(
                    "I tried to delete all non-operational data about that user, "
                    "(that I know how to delete) "
                    "however the following modules errored: {modules}.\n"
                    "Please check your logs and contact the creators of "
                    "these cogs and modules.\n"
                    "Note: Outside of these failures, data should have been deleted."
                ).format(modules=humanize_list(results.failed_modules))
            )
        else:
            await ctx.send(
                _(
                    "I've deleted all non-operational data about that user "
                    "that I know how to delete."
                )
            )

        if results.unhandled:
            await ctx.send(
                _("{mention} The following cogs did not handle deletion:\n{cogs}.").format(
                    mention=ctx.author.mention, cogs=humanize_list(results.unhandled)
                )
            )

    @mydata_owner.command(name="deleteasowner", description="Delete someone's data, including anti-abuse records.")
    @app_checks.is_owner()
    @app_commands.describe(user_id="Whose data to delete.")
    async def mydata_user_deletion_by_owner(
        self,
        interaction: discord.Interaction,
        user_id: str,
    ):
        """Delete data [botname] has about a user.

        This will cause the bot to get rid of or disassociate a lot of data about the specified user.
        This may include more than just end user data, including anti abuse records.

        **Arguments:**
        - `<user_id>` - The id of the user whose data would be deleted.
        """
        ctx = await commands.Context.from_interaction(interaction)
        user_id = await self._as_id(ctx, user_id)
        if user_id is None:
            return
        if not await self.get_serious_confirmation(
            ctx,
            _(
                "This will cause the bot to get rid of or disassociate "
                "a lot of data about the specified user. "
                "This may include more than just end user data, including "
                "anti abuse records."
                "\n\nIf you are sure this is what you intend to do "
                "please respond with the following:"
            ),
        ):
            return
        results = await self.bot.handle_data_deletion_request(requester="owner", user_id=user_id)

        if results.failed_cogs and results.failed_modules:
            await ctx.send(
                _(
                    "I tried to delete all data about that user, "
                    "(that I know how to delete) "
                    "however the following modules errored: {modules}. "
                    "Additionally, the following cogs errored: {cogs}\n"
                    "Please check your logs and contact the creators of "
                    "these cogs and modules.\n"
                    "Note: Outside of these failures, data should have been deleted."
                ).format(
                    cogs=humanize_list(results.failed_cogs),
                    modules=humanize_list(results.failed_modules),
                )
            )
        elif results.failed_cogs:
            await ctx.send(
                _(
                    "I tried to delete all data about that user, "
                    "(that I know how to delete) "
                    "however the following cogs errored: {cogs}.\n"
                    "Please check your logs and contact the creators of "
                    "these cogs and modules.\n"
                    "Note: Outside of these failures, data should have been deleted."
                ).format(cogs=humanize_list(results.failed_cogs))
            )
        elif results.failed_modules:
            await ctx.send(
                _(
                    "I tried to delete all data about that user, "
                    "(that I know how to delete) "
                    "however the following modules errored: {modules}.\n"
                    "Please check your logs and contact the creators of "
                    "these cogs and modules.\n"
                    "Note: Outside of these failures, data should have been deleted."
                ).format(modules=humanize_list(results.failed_modules))
            )
        else:
            await ctx.send(_("I've deleted all data about that user that I know how to delete."))

        if results.unhandled:
            await ctx.send(
                _("{mention} The following cogs did not handle deletion:\n{cogs}.").format(
                    mention=ctx.author.mention, cogs=humanize_list(results.unhandled)
                )
            )

    autoimmune = app_commands.Group(
        name="autoimmune",
        description="Members and roles exempt from my automatic moderation.",
        extras={"red_force_enable": True},
        guild_only=True,
    )
    ownernotifications = app_commands.Group(
        name="ownernotifications",
        description="Where my owner alerts go.",
        extras={"red_force_enable": True},
    )
    embedset = app_commands.Group(
        name="embedset",
        description="Whether my replies use embeds.",
        extras={"red_force_enable": True},
    )
    @embedset.command(
        name="command", description="Set whether one command's replies use embeds."
    )
    @app_commands.describe(
        command="The command, as you would type it.",
        enabled="On, off, or leave empty to clear the override.",
        scope="Whose setting to change. Global is owner only.",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="This server", value="server"),
            app_commands.Choice(name="Global", value="global"),
        ]
    )
    async def embedset_command(
        self,
        interaction: discord.Interaction,
        command: str,
        enabled: bool = None,
        scope: str = "server",
    ) -> None:
        """Set a command's embed setting.

        The prefix version had a subcommand per scope and a bare form that
        guessed one from whether you were the owner. The scope is an option.
        """
        ctx = await commands.Context.from_interaction(interaction)
        found = await self._one_command(ctx, command)
        if found is None:
            return
        if scope == "global":
            if not await ctx.bot.is_owner(ctx.author):
                await ctx.send(_("Only a bot owner can change the global setting."))
                return
            self._check_if_command_requires_embed_links(found)
            await ctx.bot._config.custom("COMMAND", found.qualified_name, 0).embeds.set(enabled)
            await ctx.tick()
            return
        if ctx.guild is None:
            await ctx.send(_("There is no server here."))
            return
        if not (
            ctx.author.guild_permissions.administrator
            or ctx.author.id == ctx.guild.owner_id
            or await ctx.bot.is_owner(ctx.author)
        ):
            await ctx.send(_("You need to be the server owner or an administrator."))
            return
        self._check_if_command_requires_embed_links(found)
        await ctx.bot._config.custom(
            "COMMAND", found.qualified_name, ctx.guild.id
        ).embeds.set(enabled)
        await ctx.tick()

    def _check_if_command_requires_embed_links(self, command_obj: commands.Command) -> None:
        for command in itertools.chain((command_obj,), command_obj.parents):
            if command.requires.bot_perms.embed_links:
                # a slight abuse of this exception to save myself two lines later...
                raise commands.UserFeedbackCheckFailure(
                    _(
                        "The passed command requires Embed Links permission"
                        " and therefore cannot be set to not use embeds."
                    )
                )


    @embedset.command(name="channel", description="Set whether my replies in one channel use embeds.")
    @app_commands.guild_only()
    @app_checks.guildowner_or_permissions(administrator=True)
    @app_commands.describe(
        channel="Which channel.",
        enabled="On, off, or leave empty to clear the override.",
    )
    async def embedset_channel(
        self,
        interaction: discord.Interaction,
        channel: Union[
            discord.TextChannel, discord.VoiceChannel, discord.StageChannel,
            discord.ForumChannel,
        ],
        enabled: bool = None,
    ):
        """
        Set's a channel's embed setting.

        If set, this is used instead of the guild and command defaults to determine whether or not to use embeds.
        This is used for all commands done in a channel.

        If enabled is left blank, the setting will be unset and the guild default will be used instead.

        To see full evaluation order of embed settings, run `[p]help embedset`.

        **Examples:**
        - `[p]embedset channel #text-channel False` - Disables embeds in the #text-channel.
        - `[p]embedset channel #forum-channel disable` - Disables embeds in the #forum-channel.
        - `[p]embedset channel #text-channel` - Resets value to use guild default in the #text-channel.

        **Arguments:**
            - `<channel>` - The text, voice, stage, or forum channel to set embed setting for.
            - `[enabled]` - Whether to use embeds in this channel. Leave blank to reset to default.
        """
        ctx = await commands.Context.from_interaction(interaction)
        if enabled is None:
            await self.bot._config.channel(channel).embeds.clear()
            await ctx.send(_("Embeds will now fall back to the global setting."))
            return

        await self.bot._config.channel(channel).embeds.set(enabled)
        await ctx.send(
            _("Embeds are now {} for this channel.").format(
                _("enabled") if enabled else _("disabled")
            )
        )

    @embedset.command(name="user", description="Set whether my replies to you in DMs use embeds.")
    @app_commands.describe(enabled="On, off, or leave empty to clear your override.")
    async def embedset_user(
        self,
        interaction: discord.Interaction,
        enabled: bool = None,
    ):
        """
        Sets personal embed setting for DMs.

        If set, this is used instead of the global default to determine whether or not to use embeds.
        This is used for all commands executed in a DM with the bot.

        If enabled is left blank, the setting will be unset and the global default will be used instead.

        To see full evaluation order of embed settings, run `[p]help embedset`.

        **Examples:**
        - `[p]embedset user False` - Disables embeds in your DMs.
        - `[p]embedset user` - Resets value to use global default.

        **Arguments:**
        - `[enabled]` - Whether to use embeds in your DMs. Leave blank to reset to default.
        """
        ctx = await commands.Context.from_interaction(interaction)
        if enabled is None:
            await self.bot._config.user(ctx.author).embeds.clear()
            await ctx.send(_("Embeds will now fall back to the global setting."))
            return

        await self.bot._config.user(ctx.author).embeds.set(enabled)
        await ctx.send(
            _("Embeds are now enabled for you in DMs.")
            if enabled
            else _("Embeds are now disabled for you in DMs.")
        )

    @botinfo.command(
        name="traceback",
        description="Show the last exception I hit.",
        extras={"red_force_enable": True},
    )
    @app_checks.is_owner()
    @app_commands.describe(public="Post it in the channel instead of your DMs.")
    async def traceback(
        self,
        interaction: discord.Interaction,
        public: bool = False,
    ):
        """Sends to the owner the last command exception that has occurred.

        If public (yes is specified), it will be sent to the chat instead.

        Warning: Sending the traceback publicly can accidentally reveal sensitive information about your computer or configuration.

        **Examples:**
        - `[p]traceback` - Sends the traceback to your DMs.
        - `[p]traceback True` - Sends the last traceback in the current context.

        **Arguments:**
        - `[public]` - Whether to send the traceback to the current context. Leave blank to send to your DMs.
        """
        ctx = await commands.Context.from_interaction(interaction)
        channel = ctx.channel if public else ctx.author

        if self.bot._last_exception:
            try:
                await self.bot.send_interactive(
                    channel,
                    pagify(self.bot._last_exception, shorten_by=10),
                    user=ctx.author,
                    box_lang="py",
                )
            except discord.HTTPException:
                await ctx.channel.send(
                    "I couldn't send the traceback message to you in DM. "
                    "Either you blocked me or you disabled DMs in this server."
                )
                return
            if not public:
                await ctx.tick()
        else:
            await ctx.send(_("No exception has occurred yet."))

    @botinfo.command(
        name="invite",
        description="Get a link to add me to a server.",
        extras={"red_force_enable": True},
    )
    async def invite(
        self,
        interaction: discord.Interaction,
    ):
        """Shows [botname]'s invite url.

        This will always send the invite to DMs to keep it private.

        This command is locked to the owner unless `[p]inviteset public` is set to True.

        **Example:**
        - `[p]invite`
        """
        ctx = await commands.Context.from_interaction(interaction)
        if not await self._can_get_invite_url(ctx):
            await ctx.send(_("I cannot let you do that."))
            return
        message = await self.bot.get_invite_url()
        if (admin := self.bot.get_cog("Admin")) and await admin.config.serverlocked():
            message += "\n\n" + warning(
                _(
                    "This bot is currently **serverlocked**, meaning that it is locked "
                    "to its current servers and will leave any server it joins."
                )
            )
        try:
            await ctx.author.send(message)
            await ctx.tick()
        except discord.errors.Forbidden:
            await ctx.send(
                "I couldn't send the invite message to you in DM. "
                "Either you blocked me or you disabled DMs in this server."
            )


    @botinfo.command(
        name="leave",
        description="Make me leave a server.",
        extras={"red_force_enable": True},
    )
    @app_checks.is_owner()
    @app_commands.describe(servers="Server IDs or names, separated by spaces. Empty means this one.")
    async def leave(
        self,
        interaction: discord.Interaction,
        servers: str = "",
    ):
        """
        Leaves servers.

        If no server IDs are passed the local server will be left instead.

        Note: This command is interactive.

        **Examples:**
        - `[p]leave` - Leave the current server.
        - `[p]leave "Red - Discord Bot"` - Quotes are necessary when there are spaces in the name.
        - `[p]leave 133049272517001216 240154543684321280` - Leaves multiple servers, using IDs.

        **Arguments:**
        - `[servers...]` - The servers to leave. When blank, attempts to leave the current server.
        """
        ctx = await commands.Context.from_interaction(interaction)
        servers = await self._resolve_guilds(ctx, servers)
        if servers is None:
            return
        guilds = servers
        if ctx.guild is None and not guilds:
            return await ctx.send(_("You need to specify at least one server ID."))

        leaving_local_guild = not guilds
        number = len(guilds)

        if leaving_local_guild:
            guilds = (ctx.guild,)
            msg = (
                _("You haven't passed any server ID. Do you want me to leave this server?")
                + " (yes/no)"
            )
        else:
            if number > 1:
                msg = (
                    _("Are you sure you want me to leave these servers?")
                    + " (yes/no):\n"
                    + "\n".join(f"- {guild.name} (`{guild.id}`)" for guild in guilds)
                )
            else:
                msg = (
                    _("Are you sure you want me to leave this server?")
                    + " (yes/no):\n"
                    + f"- {guilds[0].name} (`{guilds[0].id}`)"
                )

        for guild in guilds:
            if guild.owner.id == ctx.me.id:
                return await ctx.send(
                    _("I cannot leave the server `{server_name}`: I am the owner of it.").format(
                        server_name=guild.name
                    )
                )

        for page in pagify(msg):
            await ctx.send(page)
        pred = MessagePredicate.yes_or_no(ctx)
        try:
            await self.bot.wait_for("message", check=pred, timeout=30)
        except asyncio.TimeoutError:
            await ctx.send(_("Response timed out."))
            return
        else:
            if pred.result is True:
                if leaving_local_guild is True:
                    await ctx.send(_("Alright. Bye :wave:"))
                else:
                    if number > 1:
                        await ctx.send(
                            _("Alright. Leaving {number} servers...").format(number=number)
                        )
                    else:
                        await ctx.send(_("Alright. Leaving one server..."))
                for guild in guilds:
                    log.debug("Leaving guild '%s' (%s)", guild.name, guild.id)
                    await guild.leave()
            else:
                if leaving_local_guild is True:
                    await ctx.send(_("Alright, I'll stay then. :)"))
                else:
                    if number > 1:
                        await ctx.send(_("Alright, I'm not leaving those servers."))
                    else:
                        await ctx.send(_("Alright, I'm not leaving that server."))

    @botinfo.command(
        name="servers",
        description="List the servers I am in.",
        extras={"red_force_enable": True},
    )
    @app_checks.is_owner()
    async def servers(
        self,
        interaction: discord.Interaction,
    ):
        """
        Lists the servers [botname] is currently in.

        Note: This command is interactive.
        """
        ctx = await commands.Context.from_interaction(interaction)
        guilds = sorted(self.bot.guilds, key=lambda s: s.name.lower())
        msg = "\n".join(
            f"{discord.utils.escape_markdown(guild.name)} (`{guild.id}`)\n" for guild in guilds
        )

        pages = list(pagify(msg, ["\n"], page_length=1000))

        if len(pages) == 1:
            await ctx.send(pages[0])
        else:
            await menu(ctx, pages)


    @staticmethod
    def _is_submodule(parent: str, child: str):
        return parent == child or child.startswith(parent + ".")

    # TODO: Guild owner permissions for guild scope slash commands and syncing?


    # -- Bot Metadata Commands -- ###


    async def _set_bot_image(
        self,
        image_type: Literal["avatar", "banner"],
        ctx: commands.Context,
        url: Optional[str] = None,
    ):
        if len(ctx.message.attachments) > 0:  # Attachments take priority
            data = await ctx.message.attachments[0].read()
        elif url is not None:
            if url.startswith("<") and url.endswith(">"):
                url = url[1:-1]

            async with aiohttp.ClientSession() as session:
                try:
                    async with session.get(url) as r:
                        data = await r.read()
                except aiohttp.InvalidURL:
                    return await ctx.send(_("That URL is invalid."))
                except aiohttp.ClientError:
                    return await ctx.send(_("Something went wrong while trying to get the image."))
        else:
            await ctx.send(_("I did not understand that."))
            return

        try:
            async with ctx.typing():
                if image_type == "avatar":
                    await ctx.bot.user.edit(avatar=data)
                else:
                    await ctx.bot.user.edit(banner=data)
        except discord.HTTPException:
            if image_type == "avatar":
                await ctx.send(
                    _(
                        "Failed. Remember that you can edit my avatar "
                        "up to two times a hour. The URL or attachment "
                        "must be a valid image in either JPG, PNG, GIF, or WEBP format."
                    )
                )
            else:
                await ctx.send(
                    _(
                        "Failed. Remember that you can edit my banner "
                        "up to two times a hour. The URL or attachment "
                        "must be a valid image in either JPG, PNG, GIF, or WEBP format."
                    )
                )
        except ValueError:
            await ctx.send(_("JPG / PNG / GIF / WEBP format only."))
        else:
            await ctx.send(_("Done."))


    # -- End Bot Metadata Commands -- ###
    # -- Bot Status Commands -- ###


    async def _set_my_status(self, ctx: commands.Context, status: discord.Status):
        game = ctx.bot.guilds[0].me.activity if len(ctx.bot.guilds) > 0 else None
        await ctx.bot.change_presence(status=status, activity=game)
        return await ctx.send(_("Status changed to {}.").format(status))


    # -- End Bot Status Commands -- ###
    # -- Bot Roles Commands -- ###


    # -- End Set Roles Commands -- ###
    # -- Set Locale Commands -- ###


    # -- End Set Locale Commands -- ###
    # -- Set Api Commands -- ###


    # -- End Set Api Commands -- ###
    # -- Set Ownernotifications Commands -- ###


    @ownernotifications.command(name="optin", description="Start receiving owner alerts yourself.")
    @app_checks.is_owner()
    async def _set_ownernotifications_optin(
        self,
        interaction: discord.Interaction,
    ):
        """
        Opt-in on receiving owner notifications.

        This is the default state.

        Note: This will only resume sending owner notifications to your DMs.
            Additional owners and destinations will not be affected.

        **Example:**
        - `[p]set ownernotifications optin`
        """
        ctx = await commands.Context.from_interaction(interaction)
        async with ctx.bot._config.owner_opt_out_list() as opt_outs:
            if ctx.author.id in opt_outs:
                opt_outs.remove(ctx.author.id)

        await ctx.tick()

    @ownernotifications.command(name="optout", description="Stop receiving owner alerts yourself.")
    @app_checks.is_owner()
    async def _set_ownernotifications_optout(
        self,
        interaction: discord.Interaction,
    ):
        """
        Opt-out of receiving owner notifications.

        Note: This will only stop sending owner notifications to your DMs.
            Additional owners and destinations will still receive notifications.

        **Example:**
        - `[p]set ownernotifications optout`
        """
        ctx = await commands.Context.from_interaction(interaction)
        async with ctx.bot._config.owner_opt_out_list() as opt_outs:
            if ctx.author.id not in opt_outs:
                opt_outs.append(ctx.author.id)

        await ctx.tick()

    @ownernotifications.command(name="adddestination", description="Send owner alerts to a channel as well.")
    @app_checks.is_owner()
    @app_commands.describe(channel="Where to send them.")
    async def _set_ownernotifications_adddestination(
        self,
        interaction: discord.Interaction,
        channel: Union[discord.TextChannel, discord.VoiceChannel, discord.StageChannel],
    ):
        """
        Adds a destination text channel to receive owner notifications.

        **Examples:**
        - `[p]set ownernotifications adddestination #owner-notifications`
        - `[p]set ownernotifications adddestination 168091848718417920` - Accepts channel IDs.

        **Arguments:**
        - `<channel>` - The channel to send owner notifications to.
        """
        ctx = await commands.Context.from_interaction(interaction)
        async with ctx.bot._config.extra_owner_destinations() as extras:
            if channel.id not in extras:
                extras.append(channel.id)

        await ctx.tick()

    @ownernotifications.command(name="removedestination", description="Stop sending owner alerts to a channel.")
    @app_checks.is_owner()
    @app_commands.describe(
        channel="The channel to drop.",
        channel_id="Its ID instead, for a channel I can no longer see.",
    )
    async def _set_ownernotifications_removedestination(
        self,
        interaction: discord.Interaction,
        channel: Union[
            discord.TextChannel, discord.VoiceChannel, discord.StageChannel
        ] = None,
        channel_id: str = None,
    ):
        """
        Removes a destination text channel from receiving owner notifications.

        **Examples:**
        - `[p]set ownernotifications removedestination #owner-notifications`
        - `[p]set ownernotifications deletedestination 168091848718417920` - Accepts channel IDs.

        **Arguments:**
        - `<channel>` - The channel to stop sending owner notifications to.
        """
        ctx = await commands.Context.from_interaction(interaction)
        channel = await self._one_destination(ctx, channel, channel_id)
        if channel is None:
            return

        try:
            channel_id = channel.id
        except AttributeError:
            channel_id = channel

        async with ctx.bot._config.extra_owner_destinations() as extras:
            if channel_id in extras:
                extras.remove(channel_id)

        await ctx.tick()

    @ownernotifications.command(name="listdestinations", description="List where owner alerts go.")
    @app_checks.is_owner()
    async def _set_ownernotifications_listdestinations(
        self,
        interaction: discord.Interaction,
    ):
        """
        Lists the configured extra destinations for owner notifications.

        **Example:**
        - `[p]set ownernotifications listdestinations`
        """
        ctx = await commands.Context.from_interaction(interaction)

        channel_ids = await ctx.bot._config.extra_owner_destinations()

        if not channel_ids:
            await ctx.send(_("There are no extra channels being sent to."))
            return

        data = []

        for channel_id in channel_ids:
            channel = ctx.bot.get_channel(channel_id)
            if channel:
                # This includes the channel name in case the user can't see the channel.
                data.append(f"{channel.mention} {channel} ({channel.id})")
            else:
                data.append(_("Unknown channel with id: {id}").format(id=channel_id))

        output = "\n".join(data)
        for page in pagify(output):
            await ctx.send(page)

    # -- End Set Ownernotifications Commands -- ###


    @botinfo.command(
        name="contact",
        description="Send a message to my owner.",
        extras={"red_force_enable": True},
    )
    @app_commands.checks.cooldown(1, 60)
    @app_commands.describe(message="What to tell them.")
    async def contact(
        self,
        interaction: discord.Interaction,
        message: str,
    ):
        """Sends a message to the owner.

        This is limited to one message every 60 seconds per person.

        **Example:**
        - `[p]contact Help! The bot has become sentient!`

        **Arguments:**
        - `[message]` - The message to send to the owner.
        """
        ctx = await commands.Context.from_interaction(interaction)
        guild = ctx.message.guild
        author = ctx.message.author
        footer = _("User ID: {}").format(author.id)

        if ctx.guild is None:
            source = _("through DM")
        else:
            source = _("from {}").format(guild)
            footer += _(" | Server ID: {}").format(guild.id)

        prefixes = await ctx.bot.get_valid_prefixes()
        prefix = re.sub(rf"<@!?{ctx.me.id}>", f"@{ctx.me.name}".replace("\\", r"\\"), prefixes[0])

        content = _("Use `{}dm {} <text>` to reply to this user").format(prefix, author.id)

        description = _("Sent by {} {}").format(author, source)

        destinations = await ctx.bot.get_owner_notification_destinations()

        if not destinations:
            await ctx.send(_("I've been configured not to send this anywhere."))
            return

        successful = False

        for destination in destinations:
            is_dm = isinstance(destination, discord.User)
            if not is_dm and not destination.permissions_for(destination.guild.me).send_messages:
                continue

            if await ctx.bot.embed_requested(destination, command=ctx.command):
                color = await ctx.bot.get_embed_color(destination)

                e = discord.Embed(colour=color, description=message)
                e.set_author(name=description, icon_url=author.display_avatar)
                e.set_footer(text=f"{footer}\n{content}")

                try:
                    await destination.send(embed=e)
                except discord.Forbidden:
                    log.exception(f"Contact failed to {destination}({destination.id})")
                    # Should this automatically opt them out?
                except discord.HTTPException:
                    log.exception(
                        f"An unexpected error happened while attempting to"
                        f" send contact to {destination}({destination.id})"
                    )
                else:
                    successful = True
            else:
                msg_text = "{}\nMessage:\n\n{}\n{}".format(description, message, footer)

                try:
                    await destination.send("{}\n{}".format(content, box(msg_text)))
                except discord.Forbidden:
                    log.exception(f"Contact failed to {destination}({destination.id})")
                    # Should this automatically opt them out?
                except discord.HTTPException:
                    log.exception(
                        f"An unexpected error happened while attempting to"
                        f" send contact to {destination}({destination.id})"
                    )
                else:
                    successful = True

        if successful:
            await ctx.send(_("Your message has been sent."))
        else:
            await ctx.send(_("I'm unable to deliver your message. Sorry."))

    @botinfo.command(
        name="dm",
        description="Send a direct message as me.",
        extras={"red_force_enable": True},
    )
    @app_checks.is_owner()
    @app_commands.describe(
        user_id="The recipient's user ID.",
        message="What to send.",
    )
    async def dm(
        self,
        interaction: discord.Interaction,
        user_id: str,
        message: str,
    ):
        """Sends a DM to a user.

        This command needs a user ID to work.

        To get a user ID, go to Discord's settings and open the 'Appearance' tab.
        Enable 'Developer Mode', then right click a user and click on 'Copy ID'.

        **Example:**
        - `[p]dm 262626262626262626 Do you like me? Yes / No`

        **Arguments:**
        - `[message]` - The message to dm to the user.
        """
        ctx = await commands.Context.from_interaction(interaction)
        user_id = await self._as_id(ctx, user_id)
        if user_id is None:
            return
        destination = self.bot.get_user(user_id)
        if destination is None or destination.bot:
            await ctx.send(
                _(
                    "Invalid ID, user not found, or user is a bot. "
                    "You can only send messages to people I share "
                    "a server with."
                )
            )
            return

        prefixes = await ctx.bot.get_valid_prefixes()
        prefix = re.sub(rf"<@!?{ctx.me.id}>", f"@{ctx.me.name}".replace("\\", r"\\"), prefixes[0])
        description = _("Owner of {}").format(ctx.bot.user)
        content = _("You can reply to this message with {}contact").format(prefix)
        if await ctx.embed_requested():
            e = discord.Embed(colour=await ctx.embed_colour(), description=message)

            e.set_footer(text=content)
            e.set_author(name=description, icon_url=ctx.bot.user.display_avatar)

            try:
                await destination.send(embed=e)
            except discord.HTTPException:
                await ctx.send(
                    _("Sorry, I couldn't deliver your message to {}").format(destination)
                )
            else:
                await ctx.send(_("Message delivered to {}").format(destination))
        else:
            response = "{}\nMessage:\n\n{}".format(description, message)
            try:
                await destination.send("{}\n{}".format(box(response), content))
            except discord.HTTPException:
                await ctx.send(
                    _("Sorry, I couldn't deliver your message to {}").format(destination)
                )
            else:
                await ctx.send(_("Message delivered to {}").format(destination))

    @botinfo.command(
        name="datapath",
        description="Show where my data is kept.",
        extras={"red_force_enable": True},
    )
    @app_checks.is_owner()
    async def datapath(
        self,
        interaction: discord.Interaction,
    ):
        """Prints the bot's data path."""
        ctx = await commands.Context.from_interaction(interaction)
        from redbot.core.data_manager import basic_config

        data_dir = Path(basic_config["DATA_PATH"])
        msg = _("Data path: {path}").format(path=data_dir)
        await ctx.send(box(msg))

    @botinfo.command(
        name="debuginfo",
        description="Show version and platform information.",
        extras={"red_force_enable": True},
    )
    @app_checks.is_owner()
    async def debuginfo(
        self,
        interaction: discord.Interaction,
    ):
        """Shows debug information useful for debugging."""
        ctx = await commands.Context.from_interaction(interaction)
        from redbot.core._debuginfo import DebugInfo

        await ctx.send(await DebugInfo(self.bot).get_command_text())

    # You may ask why this command is owner-only,
    # cause after all it could be quite useful to guild owners!
    # Truth to be told, that would require us to make some part of this
    # more end-user friendly rather than just bot owner friendly - terms like
    # 'global call once checks' are not of any use to someone who isn't bot owner.
    @botinfo.command(
        name="diagnoseissues",
        description="Work out why a command is not available to someone.",
        extras={"red_force_enable": True},
    )
    @app_checks.is_owner()
    @app_commands.describe(
        member="Who to check for.",
        command_name="The command, as you would type it without the leading slash.",
        channel="Where to check. Defaults to here.",
    )
    async def diagnoseissues(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        command_name: str,
        channel: Union[
            discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.Thread
        ] = None,
    ):
        """
        Diagnose issues with the command checks with ease!

        If you want to diagnose the command from a text channel in a different server,
        you can do so by using the command in DMs.

        **Example:**
        - `[p]diagnoseissues #general @Slime ban` - Diagnose why @Slime can't use `[p]ban` in #general channel.

        **Arguments:**
        - `[channel]` - The text channel that the command should be tested for. Defaults to the current channel.
        - `<member>` - The member that should be considered as the command caller.
        - `<command_name>` - The name of the command to test.
        """
        ctx = await commands.Context.from_interaction(interaction)
        channel = channel or ctx.channel
        if ctx.guild is None:
            await ctx.send(
                _(
                    "A text channel, voice channel, stage channel, or thread needs to be passed"
                    " when using this command in DMs."
                )
            )
            return

        command = self.bot.get_command(command_name)
        if command is None:
            await ctx.send("Command not found!")
            return

        # This is done to allow the bot owner to diagnose a command
        # while not being a part of the server.
        if isinstance(member, discord.User):
            maybe_member = channel.guild.get_member(member.id)
            if maybe_member is None:
                await ctx.send(_("The given user is not a member of the diagnosed server."))
                return
            member = maybe_member

        if not can_user_send_messages_in(member, channel):
            # Let's make Flame happy here
            await ctx.send(
                _(
                    "Don't try to fool me, the given member can't access the {channel} channel!"
                ).format(channel=channel.mention)
            )
            return
        issue_diagnoser = IssueDiagnoser(self.bot, ctx, channel, member, command)
        await ctx.send(await issue_diagnoser.diagnose())

    allowlist = app_commands.Group(
        name="allowlist",
        description="Who may use me at all.",
        extras={"red_force_enable": True},
    )
    allowlist_global = app_commands.Group(
        name="global", description="Everywhere.", parent=allowlist
    )
    allowlist_server = app_commands.Group(
        name="server", description="This server only.", parent=allowlist
    )
    blocklist = app_commands.Group(
        name="blocklist",
        description="Who may not use me.",
        extras={"red_force_enable": True},
    )
    blocklist_global = app_commands.Group(
        name="global", description="Everywhere.", parent=blocklist
    )
    blocklist_server = app_commands.Group(
        name="server", description="This server only.", parent=blocklist
    )

    async def _people_or_ids(self, ctx, raw: str, *, roles: bool = False):
        """Members, roles and bare IDs out of one string, or None once refused.

        These lists took varargs of `Union[Member, int]` so that someone who
        had left, or had never been in the server, could still be named. An
        option cannot be varargs, so the whole lot arrives as text and this
        pulls it apart. An ID that resolves to nothing stays an int, which is
        what the bodies expect.
        """
        found = []
        for token in raw.split():
            token = token.strip()
            if not token:
                continue
            digits = token.strip("<@!&>")
            if digits.isdigit():
                snowflake = int(digits)
                resolved = ctx.bot.get_user(snowflake)
                if resolved is None and ctx.guild is not None:
                    resolved = ctx.guild.get_member(snowflake) or (
                        ctx.guild.get_role(snowflake) if roles else None
                    )
                found.append(resolved if resolved is not None else snowflake)
                continue
            resolved = None
            if ctx.guild is not None:
                resolved = ctx.guild.get_member_named(token)
                if resolved is None and roles:
                    resolved = discord.utils.get(ctx.guild.roles, name=token)
            if resolved is None:
                await ctx.send(
                    _("I could not work out who `{name}` is.").format(name=token[:100])
                )
                return None
            found.append(resolved)
        if not found:
            await ctx.send(_("Name at least one."))
            return None
        return found

    @allowlist_global.command(name="add", description="Add to the allowlist everywhere.")
    @app_checks.is_owner()
    @app_commands.describe(users="Members or IDs, separated by spaces.")
    async def allowlist_add(
        self,
        interaction: discord.Interaction,
        users: str,
    ):
        """
        Adds users to the allowlist.

        **Examples:**
        - `[p]allowlist add @26 @Will` - Adds two users to the allowlist.
        - `[p]allowlist add 262626262626262626` - Adds a user by ID.

        **Arguments:**
        - `<users...>` - The user or users to add to the allowlist.
        """
        ctx = await commands.Context.from_interaction(interaction)
        users = await self._people_or_ids(ctx, users, roles=False)
        if users is None:
            return
        await self.bot.add_to_whitelist(users)
        if len(users) > 1:
            await ctx.send(_("Users have been added to the allowlist."))
        else:
            await ctx.send(_("User has been added to the allowlist."))

    @allowlist_global.command(name="list", description="Show the allowlist everywhere.")
    @app_checks.is_owner()
    async def allowlist_list(
        self,
        interaction: discord.Interaction,
    ):
        """
        Lists users on the allowlist.

        **Example:**
        - `[p]allowlist list`
        """
        ctx = await commands.Context.from_interaction(interaction)
        curr_list = await ctx.bot._config.whitelist()

        if not curr_list:
            await ctx.send("Allowlist is empty.")
            return
        if len(curr_list) > 1:
            msg = _("Users on the allowlist:")
        else:
            msg = _("User on the allowlist:")
        for user_id in curr_list:
            user = self.bot.get_user(user_id)
            if not user:
                user = _("Unknown or Deleted User")
            msg += f"\n\t- {user_id} ({user})"

        for page in pagify(msg):
            await ctx.send(box(page))

    @allowlist_global.command(name="remove", description="Remove from the allowlist everywhere.")
    @app_checks.is_owner()
    @app_commands.describe(users="Members or IDs, separated by spaces.")
    async def allowlist_remove(
        self,
        interaction: discord.Interaction,
        users: str,
    ):
        """
        Removes users from the allowlist.

        The allowlist will be disabled if all users are removed.

        **Examples:**
        - `[p]allowlist remove @26 @Will` - Removes two users from the allowlist.
        - `[p]allowlist remove 262626262626262626` - Removes a user by ID.

        **Arguments:**
        - `<users...>` - The user or users to remove from the allowlist.
        """
        ctx = await commands.Context.from_interaction(interaction)
        users = await self._people_or_ids(ctx, users, roles=False)
        if users is None:
            return
        await self.bot.remove_from_whitelist(users)
        if len(users) > 1:
            await ctx.send(_("Users have been removed from the allowlist."))
        else:
            await ctx.send(_("User has been removed from the allowlist."))

    @allowlist_global.command(name="clear", description="Empty the allowlist everywhere.")
    @app_checks.is_owner()
    async def allowlist_clear(
        self,
        interaction: discord.Interaction,
    ):
        """
        Clears the allowlist.

        This disables the allowlist.

        **Example:**
        - `[p]allowlist clear`
        """
        ctx = await commands.Context.from_interaction(interaction)
        await self.bot.clear_whitelist()
        await ctx.send(_("Allowlist has been cleared."))


    @blocklist_global.command(name="add", description="Add to the blocklist everywhere.")
    @app_checks.is_owner()
    @app_commands.describe(users="Members or IDs, separated by spaces.")
    async def blocklist_add(
        self,
        interaction: discord.Interaction,
        users: str,
    ):
        """
        Adds users to the blocklist.

        **Examples:**
        - `[p]blocklist add @26 @Will` - Adds two users to the blocklist.
        - `[p]blocklist add 262626262626262626` - Blocks a user by ID.

        **Arguments:**
        - `<users...>` - The user or users to add to the blocklist.
        """
        ctx = await commands.Context.from_interaction(interaction)
        users = await self._people_or_ids(ctx, users, roles=False)
        if users is None:
            return
        for user in users:
            if isinstance(user, int):
                user_obj = discord.Object(id=user)
            else:
                user_obj = user
            if await ctx.bot.is_owner(user_obj):
                await ctx.send(_("You cannot add an owner to the blocklist!"))
                return

        await self.bot.add_to_blacklist(users)
        if len(users) > 1:
            await ctx.send(_("Users have been added to the blocklist."))
        else:
            await ctx.send(_("User has been added to the blocklist."))

    @blocklist_global.command(name="list", description="Show the blocklist everywhere.")
    @app_checks.is_owner()
    async def blocklist_list(
        self,
        interaction: discord.Interaction,
    ):
        """
        Lists users on the blocklist.

        **Example:**
        - `[p]blocklist list`
        """
        ctx = await commands.Context.from_interaction(interaction)
        curr_list = await self.bot.get_blacklist()

        if not curr_list:
            await ctx.send("Blocklist is empty.")
            return
        if len(curr_list) > 1:
            msg = _("Users on the blocklist:")
        else:
            msg = _("User on the blocklist:")
        for user_id in curr_list:
            user = self.bot.get_user(user_id)
            if not user:
                user = _("Unknown or Deleted User")
            msg += f"\n\t- {user_id} ({user})"

        for page in pagify(msg):
            await ctx.send(box(page))

    @blocklist_global.command(name="remove", description="Remove from the blocklist everywhere.")
    @app_checks.is_owner()
    @app_commands.describe(users="Members or IDs, separated by spaces.")
    async def blocklist_remove(
        self,
        interaction: discord.Interaction,
        users: str,
    ):
        """
        Removes users from the blocklist.

        **Examples:**
        - `[p]blocklist remove @26 @Will` - Removes two users from the blocklist.
        - `[p]blocklist remove 262626262626262626` - Removes a user by ID.

        **Arguments:**
        - `<users...>` - The user or users to remove from the blocklist.
        """
        ctx = await commands.Context.from_interaction(interaction)
        users = await self._people_or_ids(ctx, users, roles=False)
        if users is None:
            return
        await self.bot.remove_from_blacklist(users)
        if len(users) > 1:
            await ctx.send(_("Users have been removed from the blocklist."))
        else:
            await ctx.send(_("User has been removed from the blocklist."))

    @blocklist_global.command(name="clear", description="Empty the blocklist everywhere.")
    @app_checks.is_owner()
    async def blocklist_clear(
        self,
        interaction: discord.Interaction,
    ):
        """
        Clears the blocklist.

        **Example:**
        - `[p]blocklist clear`
        """
        ctx = await commands.Context.from_interaction(interaction)
        await self.bot.clear_blacklist()
        await ctx.send(_("Blocklist has been cleared."))


    @allowlist_server.command(name="add", description="Add to the allowlist in this server.")
    @app_commands.guild_only()
    @app_checks.admin_or_permissions(administrator=True)
    @app_commands.describe(users_or_roles="Members, roles or IDs, separated by spaces.")
    async def localallowlist_add(
        self,
        interaction: discord.Interaction,
        users_or_roles: str,
    ):
        """
        Adds a user or role to the server allowlist.

        **Examples:**
        - `[p]localallowlist add @26 @Will` - Adds two users to the local allowlist.
        - `[p]localallowlist add 262626262626262626` - Allows a user by ID.
        - `[p]localallowlist add "Super Admins"` - Allows a role with a space in the name without mentioning.

        **Arguments:**
        - `<users_or_roles...>` - The users or roles to remove from the local allowlist.
        """
        ctx = await commands.Context.from_interaction(interaction)
        users_or_roles = await self._people_or_ids(ctx, users_or_roles, roles=True)
        if users_or_roles is None:
            return
        names = [getattr(u_or_r, "name", u_or_r) for u_or_r in users_or_roles]
        uids = {getattr(u_or_r, "id", u_or_r) for u_or_r in users_or_roles}
        if not (ctx.guild.owner == ctx.author or await self.bot.is_owner(ctx.author)):
            current_whitelist = await self.bot.get_whitelist(ctx.guild)
            theoretical_whitelist = current_whitelist.union(uids)
            ids = {i for i in (ctx.author.id, *(getattr(ctx.author, "_roles", [])))}
            if ids.isdisjoint(theoretical_whitelist):
                return await ctx.send(
                    _(
                        "I cannot allow you to do this, as it would "
                        "remove your ability to run commands, "
                        "please ensure to add yourself to the allowlist first."
                    )
                )
        await self.bot.add_to_whitelist(uids, guild=ctx.guild)

        if len(uids) > 1:
            await ctx.send(_("Users and/or roles have been added to the allowlist."))
        else:
            await ctx.send(_("User or role has been added to the allowlist."))

    @allowlist_server.command(name="list", description="Show the allowlist in this server.")
    @app_commands.guild_only()
    @app_checks.admin_or_permissions(administrator=True)
    async def localallowlist_list(
        self,
        interaction: discord.Interaction,
    ):
        """
        Lists users and roles on the server allowlist.

        **Example:**
        - `[p]localallowlist list`
        """
        ctx = await commands.Context.from_interaction(interaction)
        curr_list = await self.bot.get_whitelist(ctx.guild)

        if not curr_list:
            await ctx.send("Server allowlist is empty.")
            return
        if len(curr_list) > 1:
            msg = _("Allowed users and/or roles:")
        else:
            msg = _("Allowed user or role:")
        for obj_id in curr_list:
            user_or_role = self.bot.get_user(obj_id) or ctx.guild.get_role(obj_id)
            if not user_or_role:
                user_or_role = _("Unknown or Deleted User/Role")
            msg += f"\n\t- {obj_id} ({user_or_role})"

        for page in pagify(msg):
            await ctx.send(box(page))

    @allowlist_server.command(name="remove", description="Remove from the allowlist in this server.")
    @app_commands.guild_only()
    @app_checks.admin_or_permissions(administrator=True)
    @app_commands.describe(users_or_roles="Members, roles or IDs, separated by spaces.")
    async def localallowlist_remove(
        self,
        interaction: discord.Interaction,
        users_or_roles: str,
    ):
        """
        Removes user or role from the allowlist.

        The local allowlist will be disabled if all users are removed.

        **Examples:**
        - `[p]localallowlist remove @26 @Will` - Removes two users from the local allowlist.
        - `[p]localallowlist remove 262626262626262626` - Removes a user by ID.
        - `[p]localallowlist remove "Super Admins"` - Removes a role with a space in the name without mentioning.

        **Arguments:**
        - `<users_or_roles...>` - The users or roles to remove from the local allowlist.
        """
        ctx = await commands.Context.from_interaction(interaction)
        users_or_roles = await self._people_or_ids(ctx, users_or_roles, roles=True)
        if users_or_roles is None:
            return
        names = [getattr(u_or_r, "name", u_or_r) for u_or_r in users_or_roles]
        uids = {getattr(u_or_r, "id", u_or_r) for u_or_r in users_or_roles}
        if not (ctx.guild.owner == ctx.author or await self.bot.is_owner(ctx.author)):
            current_whitelist = await self.bot.get_whitelist(ctx.guild)
            theoretical_whitelist = current_whitelist - uids
            ids = {i for i in (ctx.author.id, *(getattr(ctx.author, "_roles", [])))}
            if theoretical_whitelist and ids.isdisjoint(theoretical_whitelist):
                return await ctx.send(
                    _(
                        "I cannot allow you to do this, as it would "
                        "remove your ability to run commands."
                    )
                )
        await self.bot.remove_from_whitelist(uids, guild=ctx.guild)

        if len(uids) > 1:
            await ctx.send(_("Users and/or roles have been removed from the server allowlist."))
        else:
            await ctx.send(_("User or role has been removed from the server allowlist."))

    @allowlist_server.command(name="clear", description="Empty the allowlist in this server.")
    @app_commands.guild_only()
    @app_checks.admin_or_permissions(administrator=True)
    async def localallowlist_clear(
        self,
        interaction: discord.Interaction,
    ):
        """
        Clears the allowlist.

        This disables the local allowlist and clears all entries.

        **Example:**
        - `[p]localallowlist clear`
        """
        ctx = await commands.Context.from_interaction(interaction)
        await self.bot.clear_whitelist(ctx.guild)
        await ctx.send(_("Server allowlist has been cleared."))


    @blocklist_server.command(name="add", description="Add to the blocklist in this server.")
    @app_commands.guild_only()
    @app_checks.admin_or_permissions(administrator=True)
    @app_commands.describe(users_or_roles="Members, roles or IDs, separated by spaces.")
    async def localblocklist_add(
        self,
        interaction: discord.Interaction,
        users_or_roles: str,
    ):
        """
        Adds a user or role to the local blocklist.

        **Examples:**
        - `[p]localblocklist add @26 @Will` - Adds two users to the local blocklist.
        - `[p]localblocklist add 262626262626262626` - Blocks a user by ID.
        - `[p]localblocklist add "Bad Apples"` - Blocks a role with a space in the name without mentioning.

        **Arguments:**
        - `<users_or_roles...>` - The users or roles to add to the local blocklist.
        """
        ctx = await commands.Context.from_interaction(interaction)
        users_or_roles = await self._people_or_ids(ctx, users_or_roles, roles=True)
        if users_or_roles is None:
            return
        for user_or_role in users_or_roles:
            uid = discord.Object(id=getattr(user_or_role, "id", user_or_role))
            if uid.id == ctx.author.id:
                await ctx.send(_("You cannot add yourself to the blocklist!"))
                return
            if uid.id == ctx.guild.owner_id and not await ctx.bot.is_owner(ctx.author):
                await ctx.send(_("You cannot add the guild owner to the blocklist!"))
                return
            if await ctx.bot.is_owner(uid):
                await ctx.send(_("You cannot add a bot owner to the blocklist!"))
                return
        await self.bot.add_to_blacklist(users_or_roles, guild=ctx.guild)

        if len(users_or_roles) > 1:
            await ctx.send(_("Users and/or roles have been added from the server blocklist."))
        else:
            await ctx.send(_("User or role has been added from the server blocklist."))

    @blocklist_server.command(name="list", description="Show the blocklist in this server.")
    @app_commands.guild_only()
    @app_checks.admin_or_permissions(administrator=True)
    async def localblocklist_list(
        self,
        interaction: discord.Interaction,
    ):
        """
        Lists users and roles on the server blocklist.

        **Example:**
        - `[p]localblocklist list`
        """
        ctx = await commands.Context.from_interaction(interaction)
        curr_list = await self.bot.get_blacklist(ctx.guild)

        if not curr_list:
            await ctx.send("Server blocklist is empty.")
            return
        if len(curr_list) > 1:
            msg = _("Blocked users and/or roles:")
        else:
            msg = _("Blocked user or role:")
        for obj_id in curr_list:
            user_or_role = self.bot.get_user(obj_id) or ctx.guild.get_role(obj_id)
            if not user_or_role:
                user_or_role = _("Unknown or Deleted User/Role")
            msg += f"\n\t- {obj_id} ({user_or_role})"

        for page in pagify(msg):
            await ctx.send(box(page))

    @blocklist_server.command(name="remove", description="Remove from the blocklist in this server.")
    @app_commands.guild_only()
    @app_checks.admin_or_permissions(administrator=True)
    @app_commands.describe(users_or_roles="Members, roles or IDs, separated by spaces.")
    async def localblocklist_remove(
        self,
        interaction: discord.Interaction,
        users_or_roles: str,
    ):
        """
        Removes user or role from local blocklist.

        **Examples:**
        - `[p]localblocklist remove @26 @Will` - Removes two users from the local blocklist.
        - `[p]localblocklist remove 262626262626262626` - Unblocks a user by ID.
        - `[p]localblocklist remove "Bad Apples"` - Unblocks a role with a space in the name without mentioning.

        **Arguments:**
        - `<users_or_roles...>` - The users or roles to remove from the local blocklist.
        """
        ctx = await commands.Context.from_interaction(interaction)
        users_or_roles = await self._people_or_ids(ctx, users_or_roles, roles=True)
        if users_or_roles is None:
            return
        await self.bot.remove_from_blacklist(users_or_roles, guild=ctx.guild)

        if len(users_or_roles) > 1:
            await ctx.send(_("Users and/or roles have been removed from the server blocklist."))
        else:
            await ctx.send(_("User or role has been removed from the server blocklist."))

    @blocklist_server.command(name="clear", description="Empty the blocklist in this server.")
    @app_commands.guild_only()
    @app_checks.admin_or_permissions(administrator=True)
    async def localblocklist_clear(
        self,
        interaction: discord.Interaction,
    ):
        """
        Clears the server blocklist.

        This disables the server blocklist and clears all entries.

        **Example:**
        - `[p]blocklist clear`
        """
        ctx = await commands.Context.from_interaction(interaction)
        await self.bot.clear_blacklist(ctx.guild)
        await ctx.send(_("Server blocklist has been cleared."))


    # These were four commands - enable, disable, defaultenable, defaultdisable -
    # which is the same two verbs twice over. The scope is an option now.
    SCOPES = [
        app_commands.Choice(name="This server", value="guild"),
        app_commands.Choice(name="Servers I newly join", value="default"),
    ]

    async def _scope_guild(self, ctx, scope: str):
        """The guild a 'this server' change applies to, or None if there isn't one."""
        if scope == "guild" and ctx.guild is None:
            await ctx.send(
                _("Run that in the server you mean, or choose the other scope."),
                ephemeral=True,
            )
            return None
        return ctx.guild

    @cog.command(name="disable", description="Turn a cog off for a server. It stays loaded.")
    @app_checks.is_owner()
    @app_commands.describe(cog="Which cog.", scope="Where this applies.")
    @app_commands.choices(scope=SCOPES)
    async def cog_disable(
        self, interaction: discord.Interaction, cog: str, scope: str = "guild"
    ):
        """Stop a cog being usable, without unloading it."""
        ctx = await commands.Context.from_interaction(interaction)
        found = await self._one_cog(ctx, cog)
        if found is None:
            return
        cogname = found.qualified_name
        if isinstance(found, commands.commands._RuleDropper):
            return await ctx.send(
                _("You can't disable this cog by default.")
                if scope == "default"
                else _("You can't disable this cog as you would lock yourself out.")
            )
        if scope == "default":
            await self.bot._disabled_cog_cache.default_disable(cogname)
            return await ctx.send(
                _("{cogname} will be off in servers I newly join.").format(cogname=cogname)
            )
        if (guild := await self._scope_guild(ctx, scope)) is None:
            return
        if await self.bot._disabled_cog_cache.disable_cog_in_guild(cogname, guild.id):
            await ctx.send(_("{cogname} has been disabled in this guild.").format(cogname=cogname))
        else:
            await ctx.send(
                _("{cogname} was already disabled (nothing to do).").format(cogname=cogname)
            )

    @cog.command(name="enable", description="Turn a cog back on for a server.")
    @app_checks.is_owner()
    @app_commands.describe(cog="Which cog.", scope="Where this applies.")
    @app_commands.choices(scope=SCOPES)
    async def cog_enable(
        self, interaction: discord.Interaction, cog: str, scope: str = "guild"
    ):
        """Let a cog be used again."""
        ctx = await commands.Context.from_interaction(interaction)
        if scope == "default":
            found = await self._one_cog(ctx, cog)
            if found is None:
                return
            cogname = found.qualified_name
            await self.bot._disabled_cog_cache.default_enable(cogname)
            return await ctx.send(
                _("{cogname} will be on in servers I newly join.").format(cogname=cogname)
            )
        if (guild := await self._scope_guild(ctx, scope)) is None:
            return
        # Deliberately not resolved first: a cog that was disabled and then
        # unloaded still has to be re-enableable, and it is not in bot.cogs.
        if await self.bot._disabled_cog_cache.enable_cog_in_guild(cog, guild.id):
            await ctx.send(_("{cogname} has been enabled in this guild.").format(cogname=cog))
        elif self.bot.get_cog(cog) is None:
            await ctx.send(_('Cog "{arg}" not found.').format(arg=cog))
        else:
            await ctx.send(_("{cogname} was not disabled (nothing to do).").format(cogname=cog))

    @cog_disable.autocomplete("cog")
    @cog_enable.autocomplete("cog")
    async def _cog_autocomplete(self, interaction, current: str) -> list:
        """The cogs this bot has loaded."""
        current = current.lower()
        return [
            app_commands.Choice(name=name[:100], value=name)
            for name in sorted(interaction.client.cogs)
            if current in name.lower()
        ][:25]

    @cog.command(name="disabled", description="List the cogs turned off in this server.")
    @app_commands.guild_only()
    async def cog_disabled(self, interaction: discord.Interaction):
        """The cogs that are loaded but turned off here."""
        ctx = await commands.Context.from_interaction(interaction)
        disabled = [
            cog.qualified_name
            for cog in self.bot.cogs.values()
            if await self.bot._disabled_cog_cache.cog_disabled_in_guild(
                cog.qualified_name, ctx.guild.id
            )
        ]
        if disabled:
            output = _("The following cogs are disabled in this guild:\n")
            output += humanize_list(disabled)

            for page in pagify(output):
                await ctx.send(page)
        else:
            await ctx.send(_("There are no disabled cogs in this guild."))


    async def _one_destination(self, ctx, channel, channel_id: str):
        """Exactly one of a channel or a raw ID, or None once refused.

        The prefix version took ``Union[..., int]`` in one argument, so a
        destination whose channel the bot can no longer see could still be
        removed by ID. An option cannot be a union of a channel and an int, so
        there are two and this insists on one.
        """
        if (channel is None) == (channel_id is None):
            await ctx.send(_("Give me either a channel or an ID, not both and not neither."))
            return None
        if channel is not None:
            return channel
        try:
            return int(channel_id.strip())
        except ValueError:
            await ctx.send(_("`{value}` is not an ID.").format(value=channel_id[:100]))
            return None

    async def _one_cog(self, ctx, name: str):
        """A loaded cog by name, or None once refused."""
        cog = ctx.bot.get_cog(name.strip())
        if cog is None:
            await ctx.send(_("`{name}` is not a loaded cog.").format(name=name[:100]))
            return None
        return cog

    async def _one_command(self, ctx, name: str):
        """A command by name from either tree, or None once refused."""
        found = ctx.bot.get_command(name.strip())
        if found is None:
            parts = name.strip().lstrip("/").split()
            node = ctx.bot.tree.get_command(parts[0]) if parts else None
            for part in parts[1:]:
                if node is None:
                    break
                node = getattr(node, "get_command", lambda _n: None)(part)
            found = node
        if found is None:
            await ctx.send(_("`{name}` is not a command.").format(name=name[:100]))
            return None
        return found

    @autoimmune.command(name="list", description="List who is exempt from automatic moderation.")
    @app_checks.guildowner_or_permissions(manage_guild=True)
    async def autoimmune_list(
        self,
        interaction: discord.Interaction,
    ):
        """
        Gets the current members and roles configured for automatic moderation action immunity.

        **Example:**
        - `[p]autoimmune list`
        """
        ctx = await commands.Context.from_interaction(interaction)
        ai_ids = await ctx.bot._config.guild(ctx.guild).autoimmune_ids()

        roles = {r.name for r in ctx.guild.roles if r.id in ai_ids}
        members = {str(m) for m in ctx.guild.members if m.id in ai_ids}

        output = ""
        if roles:
            output += _("Roles immune from automated moderation actions:\n")
            output += ", ".join(roles)
        if members:
            if roles:
                output += "\n"
            output += _("Members immune from automated moderation actions:\n")
            output += ", ".join(members)

        if not output:
            output = _("No immunity settings here.")

        for page in pagify(output):
            await ctx.send(page)

    @autoimmune.command(name="add", description="Exempt a member or role from automatic moderation.")
    @app_checks.guildowner_or_permissions(manage_guild=True)
    @app_commands.describe(user_or_role="Who or which role to exempt.")
    async def autoimmune_add(
        self,
        interaction: discord.Interaction,
        user_or_role: Union[discord.Member, discord.Role],
    ):
        """
        Makes a user or role immune from automated moderation actions.

        **Examples:**
        - `[p]autoimmune add @Twentysix` - Adds a user.
        - `[p]autoimmune add @Mods` - Adds a role.

        **Arguments:**
        - `<user_or_role>` - The user or role to add immunity to.
        """
        ctx = await commands.Context.from_interaction(interaction)
        async with ctx.bot._config.guild(ctx.guild).autoimmune_ids() as ai_ids:
            if user_or_role.id in ai_ids:
                return await ctx.send(_("Already added."))
            ai_ids.append(user_or_role.id)
        await ctx.tick()

    @autoimmune.command(name="remove", description="Stop exempting a member or role.")
    @app_checks.guildowner_or_permissions(manage_guild=True)
    @app_commands.describe(user_or_role="Who or which role to stop exempting.")
    async def autoimmune_remove(
        self,
        interaction: discord.Interaction,
        user_or_role: Union[discord.Member, discord.Role],
    ):
        """
        Remove a user or role from being immune to automated moderation actions.

        **Examples:**
        - `[p]autoimmune remove @Twentysix` - Removes a user.
        - `[p]autoimmune remove @Mods` - Removes a role.

        **Arguments:**
        - `<user_or_role>` - The user or role to remove immunity from.
        """
        ctx = await commands.Context.from_interaction(interaction)
        async with ctx.bot._config.guild(ctx.guild).autoimmune_ids() as ai_ids:
            if user_or_role.id not in ai_ids:
                return await ctx.send(_("Not in list."))
            ai_ids.remove(user_or_role.id)
        await ctx.tick()

    @autoimmune.command(name="isimmune", description="Check whether someone is exempt.")
    @app_checks.guildowner_or_permissions(manage_guild=True)
    @app_commands.describe(user_or_role="Who or which role to check.")
    async def autoimmune_checkimmune(
        self,
        interaction: discord.Interaction,
        user_or_role: Union[discord.Member, discord.Role],
    ):
        """
        Checks if a user or role would be considered immune from automated actions.

        **Examples:**
        - `[p]autoimmune isimmune @Twentysix`
        - `[p]autoimmune isimmune @Mods`

        **Arguments:**
        - `<user_or_role>` - The user or role to check the immunity of.
        """
        ctx = await commands.Context.from_interaction(interaction)

        if await ctx.bot.is_automod_immune(user_or_role):
            await ctx.send(_("They are immune."))
        else:
            await ctx.send(_("They are not immune."))

    # RPC handlers
    async def rpc_load(self, request):
        cog_name = request.params[0]

        spec = await self.bot._cog_mgr.find_cog(cog_name)
        if spec is None:
            raise LookupError("No such cog found.")

        self._cleanup_and_refresh_modules(spec.name)

        await self.bot.load_extension(spec)

    async def rpc_unload(self, request):
        cog_name = request.params[0]

        await self.bot.unload_extension(cog_name)

    async def rpc_reload(self, request):
        await self.rpc_unload(request)
        await self.rpc_load(request)


    async def count_ignored(self, ctx: commands.Context):
        category_channels: List[discord.CategoryChannel] = []
        channels: List[
            Union[
                discord.TextChannel,
                discord.VoiceChannel,
                discord.StageChannel,
                discord.ForumChannel,
            ]
        ] = []
        threads: List[discord.Thread] = []
        if await self.bot._ignored_cache.get_ignored_guild(ctx.guild):
            return _("This server is currently being ignored.")
        for channel in itertools.chain(
            ctx.guild.text_channels,
            ctx.guild.voice_channels,
            ctx.guild.stage_channels,
            ctx.guild.forums,
        ):
            if channel.category and channel.category not in category_channels:
                if await self.bot._ignored_cache.get_ignored_channel(channel.category):
                    category_channels.append(channel.category)
            if await self.bot._ignored_cache.get_ignored_channel(channel, check_category=False):
                channels.append(channel)
        for thread in ctx.guild.threads:
            if await self.bot._ignored_cache.get_ignored_channel(thread, check_category=False):
                threads.append(thread)

        cat_str = (
            humanize_list([c.name for c in category_channels]) if category_channels else _("None")
        )
        chan_str = humanize_list([c.mention for c in channels]) if channels else _("None")
        thread_str = humanize_list([c.mention for c in threads]) if threads else _("None")
        msg = _(
            "Currently ignored categories: {categories}\n"
            "Channels: {channels}\n"
            "Threads (excluding archived):{threads}"
        ).format(categories=cat_str, channels=chan_str, threads=thread_str)
        return msg

    # Removing this command from forks is a violation of the GPLv3 under which it is licensed.
    # Otherwise interfering with the ability for this command to be accessible is also a violation.
    @botinfo.command(
        name="licenseinfo",
        description="Show my licence.",
        extras={"red_force_enable": True},
    )
    @app_commands.checks.cooldown(1, 180)
    async def license_info_command(
        self,
        interaction: discord.Interaction,
    ):
        """
        Get info about Red's licenses.
        """
        ctx = await commands.Context.from_interaction(interaction)

        message = (
            "This bot is an instance of Red-DiscordBot (hereinafter referred to as Red).\n"
            "Red is a free and open source application made available to the public and "
            "licensed under the GNU GPLv3. The full text of this license is available to you at "
            "<https://github.com/Cog-Creators/Red-DiscordBot/blob/V3/develop/LICENSE>.\n\n"
            "This instance runs CUBIX, a modified fork of Red maintained by 1Example. "
            "Those modifications are covered by the same licence, and their source is "
            "available to you at <https://github.com/1Example/DiscordBot>."
        )
        await ctx.send(message)
        # We need a link which contains a thank you to other projects which we use at some point.
