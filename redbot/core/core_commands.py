import asyncio
import datetime
import importlib
import itertools
import keyword
import logging
import io
import random
import markdown
import re
import sys
import platform
import traceback
from pathlib import Path
from redbot.core import app_commands
from redbot.core.app_commands import checks as app_checks
from redbot.core.utils.menus import menu
from redbot.core.utils.views import SetApiView
from redbot.core.commands import GuildConverter
from string import ascii_letters, digits
from typing import TYPE_CHECKING, Union, List, Optional, Iterable, Sequence, Dict, Set, Literal

import aiohttp
import discord
from packaging.version import Version

from . import __version__, version_info as red_version_info, commands, errors, i18n, modlog, _downloader
from ._diagnoser import IssueDiagnoser
from .utils import AsyncIter, can_user_send_messages_in
from .utils._internal_utils import fetch_latest_red_version
from .utils.predicates import MessagePredicate
from .utils.chat_formatting import box, humanize_list, humanize_timedelta, inline, pagify, warning
from .commands import CommandConverter, CogConverter

_entities = {
    "*": "&midast;",
    "\\": "&bsol;",
    "`": "&grave;",
    "!": "&excl;",
    "{": "&lcub;",
    "[": "&lsqb;",
    "_": "&UnderBar;",
    "(": "&lpar;",
    "#": "&num;",
    ".": "&period;",
    "+": "&plus;",
    "}": "&rcub;",
    "]": "&rsqb;",
    ")": "&rpar;",
}

PRETTY_HTML_HEAD = """
<!DOCTYPE html>
<html>
<head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>3rd Party Data Statements</title>
<style type="text/css">
body{margin:2em auto;max-width:800px;line-height:1.4;font-size:16px;
background-color=#EEEEEE;color:#454545;padding:1em;text-align:justify}
h1,h2,h3{line-height:1.2}
</style></head><body>
"""  # This ends up being a small bit extra that really makes a difference.

HTML_CLOSING = "</body></html>"


def entity_transformer(statement: str) -> str:
    return "".join(_entities.get(c, c) for c in statement)


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

    @app_commands.command(
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

    @app_commands.command(
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
                "This bot is an instance of [Red, an open source Discord bot]({}) "
                "created by [Twentysix]({}) and [improved by many]({}).\n\n"
                "Red is backed by a passionate community who contributes and "
                "creates content for everyone to enjoy. [Join us today]({}) "
                "and help us improve!\n\n"
                "(c) Cog Creators"
            ).format(red_repo, author_repo, contributors_url, support_server_url)

            embed = discord.Embed(color=(await ctx.embed_colour()))
            embed.add_field(
                name=_("Instance owned by team") if app_info.team else _("Instance owned by"),
                value=str(owner),
            )
            embed.add_field(name="Python", value=python_version)
            embed.add_field(name="discord.py", value=dpy_version)
            embed.add_field(name=_("Red version"), value=red_version)
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
            embed.add_field(name=_("About Red"), value=about, inline=False)

            embed.set_footer(
                text=_("Bringing joy since 02 Jan 2016 (over {} days ago!)").format(days_since)
            )
            await ctx.send(embed=embed)
        else:
            python_version = "{}.{}.{}".format(*sys.version_info[:3])
            dpy_version = "{}".format(discord.__version__)
            red_version = "{}".format(__version__)

            about = _(
                "This bot is an instance of Red, an open source Discord bot (1) "
                "created by Twentysix (2) and improved by many (3).\n\n"
                "Red is backed by a passionate community who contributes and "
                "creates content for everyone to enjoy. Join us today (4) "
                "and help us improve!\n\n"
                "(c) Cog Creators"
            )
            about = box(about)

            if app_info.team:
                extras = _(
                    "Instance owned by team: [{owner}]\n"
                    "Python:                 [{python_version}] (5)\n"
                    "discord.py:             [{dpy_version}] (6)\n"
                    "Red version:            [{red_version}] (7)\n"
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
                    "Red version:       [{red_version}] (7)\n"
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
            ).format(
                red_repo,
                author_repo,
                contributors_url,
                support_server_url,
                python_url,
                dpy_repo,
                red_pypi,
            )
            await ctx.send(refs)

    @app_commands.command(
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

    @commands.group(cls=commands.commands._AlwaysAvailableGroup)
    async def mydata(self, ctx: commands.Context):
        """
        Commands which interact with the data [botname] has about you.

        More information can be found in the [End User Data Documentation.](https://docs.discord.red/en/stable/red_core_data_statement.html)
        """

    # 1/10 minutes. It's a static response, but the inability to lock
    # will annoy people if it's spammable
    @commands.cooldown(1, 600, commands.BucketType.user)
    @mydata.command(cls=commands.commands._AlwaysAvailableCommand, name="whatdata")
    async def mydata_whatdata(self, ctx: commands.Context):
        """
        Find out what type of data [botname] stores and why.

        **Example:**
        - `[p]mydata whatdata`
        """

        ver = "latest" if red_version_info.dev_release else "stable"
        link = f"https://docs.discord.red/en/{ver}/red_core_data_statement.html"
        await ctx.send(
            _(
                "This bot stores some data about users as necessary to function. "
                "This is mostly the ID your user is assigned by Discord, linked to "
                "a handful of things depending on what you interact with in the bot. "
                "There are a few commands which store it to keep track of who created "
                "something. (such as playlists) "
                "For full details about this as well as more in depth details of what "
                "is stored and why, see {link}.\n\n"
                "Additionally, 3rd party addons loaded by the bot's owner may or "
                "may not store additional things. "
                "You can use `{prefix}mydata 3rdparty` "
                "to view the statements provided by each 3rd-party addition."
            ).format(link=link, prefix=ctx.clean_prefix)
        )

    # 1/30 minutes. It's not likely to change much and uploads a standalone webpage.
    @commands.cooldown(1, 1800, commands.BucketType.user)
    @mydata.command(cls=commands.commands._AlwaysAvailableCommand, name="3rdparty")
    async def mydata_3rd_party(self, ctx: commands.Context):
        """View the End User Data statements of each 3rd-party module.

        This will send an attachment with the End User Data statements of all loaded 3rd party cogs.

        **Example:**
        - `[p]mydata 3rdparty`
        """

        # Can't check this as a command check, and want to prompt DMs as an option.
        if not ctx.bot_permissions.attach_files:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(_("I need to be able to attach files (try in DMs?)."))

        statements = {
            ext_name: getattr(ext, "__red_end_user_data_statement__", None)
            for ext_name, ext in ctx.bot.extensions.items()
            if not (ext.__package__ and ext.__package__.startswith("redbot."))
        }

        if not statements:
            return await ctx.send(
                _("This instance does not appear to have any 3rd-party extensions loaded.")
            )

        parts = []

        formatted_statements = []

        no_statements = []

        for ext_name, statement in sorted(statements.items()):
            if not statement:
                no_statements.append(ext_name)
            else:
                formatted_statements.append(
                    f"### {entity_transformer(ext_name)}\n\n{entity_transformer(statement)}"
                )

        if formatted_statements:
            parts.append(
                "## "
                + _("3rd party End User Data statements")
                + "\n\n"
                + _("The following are statements provided by 3rd-party extensions.")
            )
            parts.extend(formatted_statements)

        if no_statements:
            parts.append("## " + _("3rd-party extensions without statements\n"))
            for ext in no_statements:
                parts.append(f"\n - {entity_transformer(ext)}")

        generated = markdown.markdown("\n".join(parts), output_format="html")

        html = "\n".join((PRETTY_HTML_HEAD, generated, HTML_CLOSING))

        fp = io.BytesIO(html.encode())

        await ctx.send(
            _("Here's a generated page with the statements provided by 3rd-party extensions."),
            file=discord.File(fp, filename="3rd-party.html"),
        )

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
    @commands.cooldown(1, 86400, commands.BucketType.user)
    @mydata.command(cls=commands.commands._ForgetMeSpecialCommand, name="forgetme")
    async def mydata_forgetme(self, ctx: commands.Context):
        """
        Have [botname] forget what it knows about you.

        This may not remove all data about you, data needed for operation,
        such as command cooldowns will be kept until no longer necessary.

        Further interactions with [botname] may cause it to learn about you again.

        **Example:**
        - `[p]mydata forgetme`
        """
        if ctx.assume_yes:
            # lol, no, we're not letting users schedule deletions every day to thrash the bot.
            ctx.command.reset_cooldown(ctx)  # We will however not let that lock them out either.
            return await ctx.send(
                _("This command ({command}) does not support non-interactive usage.").format(
                    command=ctx.command.qualified_name
                )
            )

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
            ctx.command.reset_cooldown(ctx)
            return
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

    # The cooldown of this should be longer once actually implemented
    # This is a couple hours, and lets people occasionally check status, I guess.
    @commands.cooldown(1, 7200, commands.BucketType.user)
    @mydata.command(cls=commands.commands._AlwaysAvailableCommand, name="getmydata")
    async def mydata_getdata(self, ctx: commands.Context):
        """[Coming Soon] Get what data [botname] has about you."""
        await ctx.send(
            _(
                "This command doesn't do anything yet, "
                "but we're working on adding support for this."
            )
        )

    @commands.is_owner()
    @mydata.group(name="ownermanagement")
    async def mydata_owner_management(self, ctx: commands.Context):
        """
        Commands for more complete data handling.
        """

    @mydata_owner_management.command(name="allowuserdeletions")
    async def mydata_owner_allow_user_deletions(self, ctx):
        """
        Set the bot to allow users to request a data deletion.

        This is on by default.
        Opposite of `[p]mydata ownermanagement disallowuserdeletions`

        **Example:**
        - `[p]mydata ownermanagement allowuserdeletions`
        """
        await ctx.bot._config.datarequests.allow_user_requests.set(True)
        await ctx.send(
            _(
                "User can delete their own data. "
                "This will not include operational data such as blocked users."
            )
        )

    @mydata_owner_management.command(name="disallowuserdeletions")
    async def mydata_owner_disallow_user_deletions(self, ctx):
        """
        Set the bot to not allow users to request a data deletion.

        Opposite of `[p]mydata ownermanagement allowuserdeletions`

        **Example:**
        - `[p]mydata ownermanagement disallowuserdeletions`
        """
        await ctx.bot._config.datarequests.allow_user_requests.set(False)
        await ctx.send(_("User can not delete their own data."))

    @mydata_owner_management.command(name="setuserdeletionlevel")
    async def mydata_owner_user_deletion_level(self, ctx, level: int):
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
            await ctx.send_help()

    @mydata_owner_management.command(name="processdiscordrequest")
    async def mydata_discord_deletion_request(self, ctx, user_id: int):
        """
        Handle a deletion request from Discord.

        This will cause the bot to get rid of or disassociate all data from the specified user ID.
        You should not use this unless Discord has specifically requested this with regard to a deleted user.
        This will remove the user from various anti-abuse measures.
        If you are processing a manual request from a user, you may want `[p]mydata ownermanagement deleteforuser` instead.

        **Arguments:**
        - `<user_id>` - The id of the user whose data would be deleted.
        """

        if not await self.get_serious_confirmation(
            ctx,
            _(
                "This will cause the bot to get rid of or disassociate all data "
                "from the specified user ID. You should not use this unless "
                "Discord has specifically requested this with regard to a deleted user. "
                "This will remove the user from various anti-abuse measures. "
                "If you are processing a manual request from a user, you may want "
                "`{prefix}{command_name}` instead."
                "\n\nIf you are sure this is what you intend to do "
                "please respond with the following:"
            ).format(prefix=ctx.clean_prefix, command_name="mydata ownermanagement deleteforuser"),
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

    @mydata_owner_management.command(name="deleteforuser")
    async def mydata_user_deletion_request_by_owner(self, ctx, user_id: int):
        """Delete data [botname] has about a user for a user.

        This will cause the bot to get rid of or disassociate a lot of non-operational data from the specified user.
        Users have access to a different command for this unless they can't interact with the bot at all.
        This is a mostly safe operation, but you should not use it unless processing a request from this user as it may impact their usage of the bot.

        **Arguments:**
        - `<user_id>` - The id of the user whose data would be deleted.
        """
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

    @mydata_owner_management.command(name="deleteuserasowner")
    async def mydata_user_deletion_by_owner(self, ctx, user_id: int):
        """Delete data [botname] has about a user.

        This will cause the bot to get rid of or disassociate a lot of data about the specified user.
        This may include more than just end user data, including anti abuse records.

        **Arguments:**
        - `<user_id>` - The id of the user whose data would be deleted.
        """
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

    @commands.group()
    async def embedset(self, ctx: commands.Context):
        """
        Commands for toggling embeds on or off.

        This setting determines whether or not to use embeds as a response to a command (for commands that support it).
        The default is to use embeds.

        The embed settings are checked until the first True/False in this order:

        - In guild context:
          1. Channel override - `[p]embedset channel`
          2. Server command override - `[p]embedset command server`
          3. Server override - `[p]embedset server`
          4. Global command override - `[p]embedset command global`
          5. Global setting  -`[p]embedset global`

        - In DM context:
          1. User override - `[p]embedset user`
          2. Global command override - `[p]embedset command global`
          3. Global setting - `[p]embedset global`
        """


    @commands.guildowner_or_permissions(administrator=True)
    @embedset.group(name="command", invoke_without_command=True)
    async def embedset_command(
        self, ctx: commands.Context, command: CommandConverter, enabled: bool = None
    ) -> None:
        """
        Sets a command's embed setting.

        If you're the bot owner, this will try to change the command's embed setting globally by default.
        Otherwise, this will try to change embed settings on the current server.

        If enabled is left blank, the setting will be unset.

        To see full evaluation order of embed settings, run `[p]help embedset`.

        **Examples:**
        - `[p]embedset command info` - Clears command specific embed settings for 'info'.
        - `[p]embedset command info False` - Disables embeds for 'info'.
        - `[p]embedset command "ignore list" True` - Quotes are needed for subcommands.

        **Arguments:**
        - `[enabled]` - Whether to use embeds for this command. Leave blank to reset to default.
        """
        # Select the scope based on the author's privileges
        if await ctx.bot.is_owner(ctx.author):
            await self.embedset_command_global(ctx, command, enabled)
        else:
            await self.embedset_command_guild(ctx, command, enabled)

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

    @commands.is_owner()
    @embedset_command.command(name="global")
    async def embedset_command_global(
        self, ctx: commands.Context, command: CommandConverter, enabled: bool = None
    ):
        """
        Sets a command's embed setting globally.

        If set, this is used instead of the global default to determine whether or not to use embeds.

        If enabled is left blank, the setting will be unset.

        To see full evaluation order of embed settings, run `[p]help embedset`.

        **Examples:**
        - `[p]embedset command global info` - Clears command specific embed settings for 'info'.
        - `[p]embedset command global info False` - Disables embeds for 'info'.
        - `[p]embedset command global "ignore list" True` - Quotes are needed for subcommands.

        **Arguments:**
        - `[enabled]` - Whether to use embeds for this command. Leave blank to reset to default.
        """
        self._check_if_command_requires_embed_links(command)
        # qualified name might be different if alias was passed to this command
        command_name = command.qualified_name

        if enabled is None:
            await self.bot._config.custom("COMMAND", command_name, 0).embeds.clear()
            await ctx.send(_("Embeds will now fall back to the global setting."))
            return

        await self.bot._config.custom("COMMAND", command_name, 0).embeds.set(enabled)
        if enabled:
            await ctx.send(
                _("Embeds are now enabled for {command_name} command.").format(
                    command_name=inline(command_name)
                )
            )
        else:
            await ctx.send(
                _("Embeds are now disabled for {command_name} command.").format(
                    command_name=inline(command_name)
                )
            )

    @commands.guild_only()
    @embedset_command.command(name="server", aliases=["guild"])
    async def embedset_command_guild(
        self, ctx: commands.GuildContext, command: CommandConverter, enabled: bool = None
    ):
        """
        Sets a command's embed setting for the current server.

        If set, this is used instead of the server default to determine whether or not to use embeds.

        If enabled is left blank, the setting will be unset and the server default will be used instead.

        To see full evaluation order of embed settings, run `[p]help embedset`.

        **Examples:**
        - `[p]embedset command server info` - Clears command specific embed settings for 'info'.
        - `[p]embedset command server info False` - Disables embeds for 'info'.
        - `[p]embedset command server "ignore list" True` - Quotes are needed for subcommands.

        **Arguments:**
        - `[enabled]` - Whether to use embeds for this command. Leave blank to reset to default.
        """
        self._check_if_command_requires_embed_links(command)
        # qualified name might be different if alias was passed to this command
        command_name = command.qualified_name

        if enabled is None:
            await self.bot._config.custom("COMMAND", command_name, ctx.guild.id).embeds.clear()
            await ctx.send(_("Embeds will now fall back to the server setting."))
            return

        await self.bot._config.custom("COMMAND", command_name, ctx.guild.id).embeds.set(enabled)
        if enabled:
            await ctx.send(
                _("Embeds are now enabled for {command_name} command.").format(
                    command_name=inline(command_name)
                )
            )
        else:
            await ctx.send(
                _("Embeds are now disabled for {command_name} command.").format(
                    command_name=inline(command_name)
                )
            )

    @embedset.command(name="channel")
    @commands.guildowner_or_permissions(administrator=True)
    @commands.guild_only()
    async def embedset_channel(
        self,
        ctx: commands.Context,
        channel: Union[
            discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel
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

    @embedset.command(name="user")
    async def embedset_user(self, ctx: commands.Context, enabled: bool = None):
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

    @app_commands.command(
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

    @app_commands.command(
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


    @app_commands.command(
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

    @app_commands.command(
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


    @commands.group(name="set")
    async def _set(self, ctx: commands.Context):
        """Commands for changing [botname]'s settings."""

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
            await ctx.send_help()
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

    @_set.group(name="api", invoke_without_command=True)
    @commands.is_owner()
    async def _set_api(
        self,
        ctx: commands.Context,
        service: Optional[str] = None,
        *,
        tokens: Optional[TokenConverter] = None,
    ):
        """
        Commands to set, list or remove various external API tokens.

        This setting will be asked for by some 3rd party cogs and some core cogs.

        If passed without the `<service>` or `<tokens>` arguments it will allow you to open a modal to set your API keys securely.

        To add the keys provide the service name and the tokens as a comma separated
        list of key,values as described by the cog requesting this command.

        Note: API tokens are sensitive, so this command should only be used in a private channel or in DM with the bot.

        **Examples:**
        - `[p]set api`
        - `[p]set api spotify`
        - `[p]set api spotify redirect_uri localhost`
        - `[p]set api github client_id,whoops client_secret,whoops`

        **Arguments:**
        - `<service>` - The service you're adding tokens to.
        - `<tokens>` - Pairs of token keys and values. The key and value should be separated by one of ` `, `,`, or `;`.
        """
        if service is None or tokens is None:
            view = SetApiView(default_service=service)
            msg = await ctx.send(_("Click the button below to set your keys."), view=view)
            await view.wait()
            await msg.edit(content=_("This API keys setup message has expired."), view=None)
        else:
            if ctx.bot_permissions.manage_messages:
                await ctx.message.delete()

            angle_bracket_warning = None

            for token_name, token in tokens.items():
                if token.startswith("<") and token.endswith(">"):
                    angle_bracket_warning = _(
                        "You may have failed to properly format your `{token_name}`. If you were told to enter a token"
                        " with an example such as `[p]set api {service} {token_name} <your_{token_name}_here>`, and your {token_name}"
                        " was `HREDFGWE`, make sure to run `[p]set api {service} {token_name} HREDFGWE` and not "
                        "`[p]set api {service} {token_name} <HREDFGWE>`."
                    ).format(token_name=token_name, service=service)
                    break

            await ctx.bot.set_shared_api_tokens(service, **tokens)

            message = _("`{service}` API tokens have been set.").format(service=service)
            if angle_bracket_warning:
                message += "\n\n" + _("**Warning:** ") + angle_bracket_warning

            await ctx.send(message)

    @_set_api.command(name="list")
    async def _set_api_list(self, ctx: commands.Context):
        """
        Show all external API services along with their keys that have been set.

        Secrets are not shown.

        **Example:**
        - `[p]set api list`
        """

        services: dict = await ctx.bot.get_shared_api_tokens()
        if not services:
            await ctx.send(_("No API services have been set yet."))
            return

        sorted_services = sorted(services.keys(), key=str.lower)

        joined = _("Set API services:\n") if len(services) > 1 else _("Set API service:\n")
        for service_name in sorted_services:
            joined += "+ {}\n".format(service_name)
            for key_name in services[service_name].keys():
                joined += "  - {}\n".format(key_name)
        for page in pagify(joined, ["\n"], shorten_by=16):
            await ctx.send(box(page.lstrip(" "), lang="diff"))

    @_set_api.command(name="remove", require_var_positional=True)
    async def _set_api_remove(self, ctx: commands.Context, *services: str):
        """
        Remove the given services with all their keys and tokens.

        **Examples:**
        - `[p]set api remove spotify`
        - `[p]set api remove github youtube`

        **Arguments:**
        - `<services...>` - The services to remove."""
        bot_services = (await ctx.bot.get_shared_api_tokens()).keys()
        services = [s for s in services if s in bot_services]

        if services:
            await self.bot.remove_shared_api_services(*services)
            if len(services) > 1:
                msg = _("Services deleted successfully:\n{services_list}").format(
                    services_list=humanize_list(services)
                )
            else:
                msg = _("Service deleted successfully: {service_name}").format(
                    service_name=services[0]
                )
            await ctx.send(msg)
        else:
            await ctx.send(_("None of the services you provided had any keys set."))

    # -- End Set Api Commands -- ###
    # -- Set Ownernotifications Commands -- ###

    @commands.is_owner()
    @_set.group(name="ownernotifications")
    async def _set_ownernotifications(self, ctx: commands.Context):
        """
        Commands for configuring owner notifications.

        Owner notifications include usage of `[p]contact` and available Red updates.
        """
        pass

    @_set_ownernotifications.command(name="optin")
    async def _set_ownernotifications_optin(self, ctx: commands.Context):
        """
        Opt-in on receiving owner notifications.

        This is the default state.

        Note: This will only resume sending owner notifications to your DMs.
            Additional owners and destinations will not be affected.

        **Example:**
        - `[p]set ownernotifications optin`
        """
        async with ctx.bot._config.owner_opt_out_list() as opt_outs:
            if ctx.author.id in opt_outs:
                opt_outs.remove(ctx.author.id)

        await ctx.tick()

    @_set_ownernotifications.command(name="optout")
    async def _set_ownernotifications_optout(self, ctx: commands.Context):
        """
        Opt-out of receiving owner notifications.

        Note: This will only stop sending owner notifications to your DMs.
            Additional owners and destinations will still receive notifications.

        **Example:**
        - `[p]set ownernotifications optout`
        """
        async with ctx.bot._config.owner_opt_out_list() as opt_outs:
            if ctx.author.id not in opt_outs:
                opt_outs.append(ctx.author.id)

        await ctx.tick()

    @_set_ownernotifications.command(name="adddestination")
    async def _set_ownernotifications_adddestination(
        self,
        ctx: commands.Context,
        *,
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
        async with ctx.bot._config.extra_owner_destinations() as extras:
            if channel.id not in extras:
                extras.append(channel.id)

        await ctx.tick()

    @_set_ownernotifications.command(
        name="removedestination", aliases=["remdestination", "deletedestination", "deldestination"]
    )
    async def _set_ownernotifications_removedestination(
        self,
        ctx: commands.Context,
        *,
        channel: Union[discord.TextChannel, discord.VoiceChannel, discord.StageChannel, int],
    ):
        """
        Removes a destination text channel from receiving owner notifications.

        **Examples:**
        - `[p]set ownernotifications removedestination #owner-notifications`
        - `[p]set ownernotifications deletedestination 168091848718417920` - Accepts channel IDs.

        **Arguments:**
        - `<channel>` - The channel to stop sending owner notifications to.
        """

        try:
            channel_id = channel.id
        except AttributeError:
            channel_id = channel

        async with ctx.bot._config.extra_owner_destinations() as extras:
            if channel_id in extras:
                extras.remove(channel_id)

        await ctx.tick()

    @_set_ownernotifications.command(name="listdestinations")
    async def _set_ownernotifications_listdestinations(self, ctx: commands.Context):
        """
        Lists the configured extra destinations for owner notifications.

        **Example:**
        - `[p]set ownernotifications listdestinations`
        """

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


    @app_commands.command(
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

    @app_commands.command(
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

    @app_commands.command(
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

    @app_commands.command(
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
    @app_commands.command(
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

    @commands.group(aliases=["whitelist"])
    @commands.is_owner()
    async def allowlist(self, ctx: commands.Context):
        """
        Commands to manage the allowlist.

        Warning: When the allowlist is in use, the bot will ignore commands from everyone not on the list.

        Use `[p]allowlist clear` to disable the allowlist
        """
        pass

    @allowlist.command(name="add", require_var_positional=True)
    async def allowlist_add(self, ctx: commands.Context, *users: Union[discord.Member, int]):
        """
        Adds users to the allowlist.

        **Examples:**
        - `[p]allowlist add @26 @Will` - Adds two users to the allowlist.
        - `[p]allowlist add 262626262626262626` - Adds a user by ID.

        **Arguments:**
        - `<users...>` - The user or users to add to the allowlist.
        """
        await self.bot.add_to_whitelist(users)
        if len(users) > 1:
            await ctx.send(_("Users have been added to the allowlist."))
        else:
            await ctx.send(_("User has been added to the allowlist."))

    @allowlist.command(name="list")
    async def allowlist_list(self, ctx: commands.Context):
        """
        Lists users on the allowlist.

        **Example:**
        - `[p]allowlist list`
        """
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

    @allowlist.command(name="remove", require_var_positional=True)
    async def allowlist_remove(self, ctx: commands.Context, *users: Union[discord.Member, int]):
        """
        Removes users from the allowlist.

        The allowlist will be disabled if all users are removed.

        **Examples:**
        - `[p]allowlist remove @26 @Will` - Removes two users from the allowlist.
        - `[p]allowlist remove 262626262626262626` - Removes a user by ID.

        **Arguments:**
        - `<users...>` - The user or users to remove from the allowlist.
        """
        await self.bot.remove_from_whitelist(users)
        if len(users) > 1:
            await ctx.send(_("Users have been removed from the allowlist."))
        else:
            await ctx.send(_("User has been removed from the allowlist."))

    @allowlist.command(name="clear")
    async def allowlist_clear(self, ctx: commands.Context):
        """
        Clears the allowlist.

        This disables the allowlist.

        **Example:**
        - `[p]allowlist clear`
        """
        await self.bot.clear_whitelist()
        await ctx.send(_("Allowlist has been cleared."))

    @commands.group(aliases=["blacklist", "denylist"])
    @commands.is_owner()
    async def blocklist(self, ctx: commands.Context):
        """
        Commands to manage the blocklist.

        Use `[p]blocklist clear` to disable the blocklist
        """
        pass

    @blocklist.command(name="add", require_var_positional=True)
    async def blocklist_add(self, ctx: commands.Context, *users: Union[discord.Member, int]):
        """
        Adds users to the blocklist.

        **Examples:**
        - `[p]blocklist add @26 @Will` - Adds two users to the blocklist.
        - `[p]blocklist add 262626262626262626` - Blocks a user by ID.

        **Arguments:**
        - `<users...>` - The user or users to add to the blocklist.
        """
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

    @blocklist.command(name="list")
    async def blocklist_list(self, ctx: commands.Context):
        """
        Lists users on the blocklist.

        **Example:**
        - `[p]blocklist list`
        """
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

    @blocklist.command(name="remove", require_var_positional=True)
    async def blocklist_remove(self, ctx: commands.Context, *users: Union[discord.Member, int]):
        """
        Removes users from the blocklist.

        **Examples:**
        - `[p]blocklist remove @26 @Will` - Removes two users from the blocklist.
        - `[p]blocklist remove 262626262626262626` - Removes a user by ID.

        **Arguments:**
        - `<users...>` - The user or users to remove from the blocklist.
        """
        await self.bot.remove_from_blacklist(users)
        if len(users) > 1:
            await ctx.send(_("Users have been removed from the blocklist."))
        else:
            await ctx.send(_("User has been removed from the blocklist."))

    @blocklist.command(name="clear")
    async def blocklist_clear(self, ctx: commands.Context):
        """
        Clears the blocklist.

        **Example:**
        - `[p]blocklist clear`
        """
        await self.bot.clear_blacklist()
        await ctx.send(_("Blocklist has been cleared."))

    @commands.group(aliases=["localwhitelist"])
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    async def localallowlist(self, ctx: commands.Context):
        """
        Commands to manage the server specific allowlist.

        Warning: When the allowlist is in use, the bot will ignore commands from everyone not on the list in the server.

        Use `[p]localallowlist clear` to disable the allowlist
        """
        pass

    @localallowlist.command(name="add", require_var_positional=True)
    async def localallowlist_add(
        self, ctx: commands.Context, *users_or_roles: Union[discord.Member, discord.Role, int]
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

    @localallowlist.command(name="list")
    async def localallowlist_list(self, ctx: commands.Context):
        """
        Lists users and roles on the server allowlist.

        **Example:**
        - `[p]localallowlist list`
        """
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

    @localallowlist.command(name="remove", require_var_positional=True)
    async def localallowlist_remove(
        self, ctx: commands.Context, *users_or_roles: Union[discord.Member, discord.Role, int]
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

    @localallowlist.command(name="clear")
    async def localallowlist_clear(self, ctx: commands.Context):
        """
        Clears the allowlist.

        This disables the local allowlist and clears all entries.

        **Example:**
        - `[p]localallowlist clear`
        """
        await self.bot.clear_whitelist(ctx.guild)
        await ctx.send(_("Server allowlist has been cleared."))

    @commands.group(aliases=["localblacklist"])
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    async def localblocklist(self, ctx: commands.Context):
        """
        Commands to manage the server specific blocklist.

        Use `[p]localblocklist clear` to disable the blocklist
        """
        pass

    @localblocklist.command(name="add", require_var_positional=True)
    async def localblocklist_add(
        self, ctx: commands.Context, *users_or_roles: Union[discord.Member, discord.Role, int]
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

    @localblocklist.command(name="list")
    async def localblocklist_list(self, ctx: commands.Context):
        """
        Lists users and roles on the server blocklist.

        **Example:**
        - `[p]localblocklist list`
        """
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

    @localblocklist.command(name="remove", require_var_positional=True)
    async def localblocklist_remove(
        self, ctx: commands.Context, *users_or_roles: Union[discord.Member, discord.Role, int]
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
        await self.bot.remove_from_blacklist(users_or_roles, guild=ctx.guild)

        if len(users_or_roles) > 1:
            await ctx.send(_("Users and/or roles have been removed from the server blocklist."))
        else:
            await ctx.send(_("User or role has been removed from the server blocklist."))

    @localblocklist.command(name="clear")
    async def localblocklist_clear(self, ctx: commands.Context):
        """
        Clears the server blocklist.

        This disables the server blocklist and clears all entries.

        **Example:**
        - `[p]blocklist clear`
        """
        await self.bot.clear_blacklist(ctx.guild)
        await ctx.send(_("Server blocklist has been cleared."))

    @commands.guildowner_or_permissions(administrator=True)
    @commands.group(name="command")
    async def command_manager(self, ctx: commands.Context):
        """Commands to enable and disable commands and cogs."""
        pass

    @commands.is_owner()
    @command_manager.command(name="defaultdisablecog")
    async def command_default_disable_cog(self, ctx: commands.Context, *, cog: CogConverter):
        """Set the default state for a cog as disabled.

        This will disable the cog for all servers by default.
        To override it, use `[p]command enablecog` on the servers you want to allow usage.

        Note: This will only work on loaded cogs, and must reference the title-case cog name.

        **Examples:**
        - `[p]command defaultdisablecog Economy`
        - `[p]command defaultdisablecog ModLog`

        **Arguments:**
        - `<cog>` - The name of the cog to make disabled by default. Must be title-case.
        """
        cogname = cog.qualified_name
        if isinstance(cog, commands.commands._RuleDropper):
            return await ctx.send(_("You can't disable this cog by default."))
        await self.bot._disabled_cog_cache.default_disable(cogname)
        await ctx.send(_("{cogname} has been set as disabled by default.").format(cogname=cogname))

    @commands.is_owner()
    @command_manager.command(name="defaultenablecog")
    async def command_default_enable_cog(self, ctx: commands.Context, *, cog: CogConverter):
        """Set the default state for a cog as enabled.

        This will re-enable the cog for all servers by default.
        To override it, use `[p]command disablecog` on the servers you want to disallow usage.

        Note: This will only work on loaded cogs, and must reference the title-case cog name.

        **Examples:**
        - `[p]command defaultenablecog Economy`
        - `[p]command defaultenablecog ModLog`

        **Arguments:**
        - `<cog>` - The name of the cog to make enabled by default. Must be title-case.
        """
        cogname = cog.qualified_name
        await self.bot._disabled_cog_cache.default_enable(cogname)
        await ctx.send(_("{cogname} has been set as enabled by default.").format(cogname=cogname))

    @commands.guild_only()
    @command_manager.command(name="disablecog")
    async def command_disable_cog(self, ctx: commands.Context, *, cog: CogConverter):
        """Disable a cog in this server.

        Note: This will only work on loaded cogs, and must reference the title-case cog name.

        **Examples:**
        - `[p]command disablecog Economy`
        - `[p]command disablecog ModLog`

        **Arguments:**
        - `<cog>` - The name of the cog to disable on this server. Must be title-case.
        """
        cogname = cog.qualified_name
        if isinstance(cog, commands.commands._RuleDropper):
            return await ctx.send(_("You can't disable this cog as you would lock yourself out."))
        if await self.bot._disabled_cog_cache.disable_cog_in_guild(cogname, ctx.guild.id):
            await ctx.send(_("{cogname} has been disabled in this guild.").format(cogname=cogname))
        else:
            await ctx.send(
                _("{cogname} was already disabled (nothing to do).").format(cogname=cogname)
            )

    @commands.guild_only()
    @command_manager.command(name="enablecog", usage="<cog>")
    async def command_enable_cog(self, ctx: commands.Context, *, cogname: str):
        """Enable a cog in this server.

        Note: This will only work on loaded cogs, and must reference the title-case cog name.

        **Examples:**
        - `[p]command enablecog Economy`
        - `[p]command enablecog ModLog`

        **Arguments:**
        - `<cog>` - The name of the cog to enable on this server. Must be title-case.
        """
        if await self.bot._disabled_cog_cache.enable_cog_in_guild(cogname, ctx.guild.id):
            await ctx.send(_("{cogname} has been enabled in this guild.").format(cogname=cogname))
        else:
            # putting this here allows enabling a cog that isn't loaded but was disabled.
            cog = self.bot.get_cog(cogname)
            if not cog:
                return await ctx.send(_('Cog "{arg}" not found.').format(arg=cogname))

            await ctx.send(
                _("{cogname} was not disabled (nothing to do).").format(cogname=cogname)
            )

    @commands.guild_only()
    @command_manager.command(name="listdisabledcogs")
    async def command_list_disabled_cogs(self, ctx: commands.Context):
        """List the cogs which are disabled in this server.

        **Example:**
        - `[p]command listdisabledcogs`
        """
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


    @commands.guild_only()
    @commands.guildowner_or_permissions(manage_guild=True)
    @commands.group(name="autoimmune")
    async def autoimmune_group(self, ctx: commands.Context):
        """
        Commands to manage server settings for immunity from automated actions.

        This includes duplicate message deletion and mention spam from the Mod cog, and filters from the Filter cog.
        """
        pass

    @autoimmune_group.command(name="list")
    async def autoimmune_list(self, ctx: commands.Context):
        """
        Gets the current members and roles configured for automatic moderation action immunity.

        **Example:**
        - `[p]autoimmune list`
        """
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

    @autoimmune_group.command(name="add")
    async def autoimmune_add(
        self, ctx: commands.Context, *, user_or_role: Union[discord.Member, discord.Role]
    ):
        """
        Makes a user or role immune from automated moderation actions.

        **Examples:**
        - `[p]autoimmune add @Twentysix` - Adds a user.
        - `[p]autoimmune add @Mods` - Adds a role.

        **Arguments:**
        - `<user_or_role>` - The user or role to add immunity to.
        """
        async with ctx.bot._config.guild(ctx.guild).autoimmune_ids() as ai_ids:
            if user_or_role.id in ai_ids:
                return await ctx.send(_("Already added."))
            ai_ids.append(user_or_role.id)
        await ctx.tick()

    @autoimmune_group.command(name="remove")
    async def autoimmune_remove(
        self, ctx: commands.Context, *, user_or_role: Union[discord.Member, discord.Role]
    ):
        """
        Remove a user or role from being immune to automated moderation actions.

        **Examples:**
        - `[p]autoimmune remove @Twentysix` - Removes a user.
        - `[p]autoimmune remove @Mods` - Removes a role.

        **Arguments:**
        - `<user_or_role>` - The user or role to remove immunity from.
        """
        async with ctx.bot._config.guild(ctx.guild).autoimmune_ids() as ai_ids:
            if user_or_role.id not in ai_ids:
                return await ctx.send(_("Not in list."))
            ai_ids.remove(user_or_role.id)
        await ctx.tick()

    @autoimmune_group.command(name="isimmune")
    async def autoimmune_checkimmune(
        self, ctx: commands.Context, *, user_or_role: Union[discord.Member, discord.Role]
    ):
        """
        Checks if a user or role would be considered immune from automated actions.

        **Examples:**
        - `[p]autoimmune isimmune @Twentysix`
        - `[p]autoimmune isimmune @Mods`

        **Arguments:**
        - `<user_or_role>` - The user or role to check the immunity of.
        """

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
    @app_commands.command(
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
            "<https://github.com/Cog-Creators/Red-DiscordBot/blob/V3/develop/LICENSE>."
        )
        await ctx.send(message)
        # We need a link which contains a thank you to other projects which we use at some point.
