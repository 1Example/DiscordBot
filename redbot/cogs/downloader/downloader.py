import asyncio
import contextlib
from typing import Collection, Tuple

import discord
from redbot import __version__
from redbot.core import _downloader, commands
from redbot.core._downloader.installable import InstalledModule
from redbot.core.bot import Red
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils import can_user_react_in
from redbot.core.utils.chat_formatting import pagify, humanize_list, inline
from redbot.core.utils.menus import start_adding_reactions
from redbot.core.utils.predicates import MessagePredicate, ReactionPredicate
from .converters import Repo
from .dashboard_integration import DashboardIntegration

_ = Translator("Downloader", __file__)


DEPRECATION_NOTICE = _(
    "\n**WARNING:** The following repos are using shared libraries"
    " which are marked for removal in the future: {repo_list}.\n"
    " You should inform maintainers of these repos about this message."
)


@cog_i18n(_)
class Downloader(DashboardIntegration, commands.Cog):
    """Install community cogs from third-party repositories.

    Community cogs are not part of the default install. They come in
    repos, which you add before installing anything from them.
    """

    def __init__(self, bot: Red):
        super().__init__()
        self.bot = bot
        self.already_agreed = False

    async def red_delete_data_for_user(self, **kwargs):
        """Nothing to delete"""
        return

    # This is a compatibility shim for people using Downloader internal pre-3.5.25.
    # It will probably get removed in Red 3.6.
    @property
    def _repo_manager(self):
        return _downloader._repo_manager

    # This is a compatibility shim for people using Downloader internal pre-3.5.25.
    # It will probably get removed in Red 3.6.
    async def installed_cogs(self) -> Tuple[InstalledModule, ...]:
        return await _downloader.installed_cogs()

    @staticmethod
    async def send_pagified(target: discord.abc.Messageable, content: str) -> None:
        for page in pagify(content):
            await target.send(page)


    def _format_invalid_cogs(
        self, repo: Repo, install_result: _downloader.CogInstallResult
    ) -> str:
        message = ""
        if install_result.unavailable_cogs:
            message = (
                _("\nCouldn't find these cogs in {repo.name}: ")
                if len(install_result.unavailable_cogs) > 1
                else _("\nCouldn't find this cog in {repo.name}: ")
            ).format(repo=repo) + humanize_list(install_result.unavailable_cogs)
        if install_result.already_installed:
            message += (
                _("\nThese cogs were already installed: ")
                if len(install_result.already_installed) > 1
                else _("\nThis cog was already installed: ")
            ) + humanize_list([cog.name for cog in install_result.already_installed])
        if install_result.name_already_used:
            message += (
                _("\nSome cogs with these names are already installed from different repos: ")
                if len(install_result.name_already_used) > 1
                else _("\nCog with this name is already installed from a different repo: ")
            ) + humanize_list([cog.name for cog in install_result.name_already_used])
        # TODO: resolve typing issue
        add_to_message = self._format_incompatible_cogs(install_result)
        if add_to_message:
            return f"{message}{add_to_message}"
        return message

    def _format_incompatible_cogs(
        self, update_check_result: _downloader.CogUpdateCheckResult
    ) -> str:
        message = ""
        if update_check_result.incompatible_python_version:
            message += (
                _("\nThese cogs require higher python version than you have: ")
                if len(update_check_result.incompatible_python_version)
                else _("\nThis cog requires higher python version than you have: ")
            ) + humanize_list(
                [
                    inline(cog.name)
                    + _(" (Minimum: {min_version})").format(min_version=cog.min_python_version)
                    for cog in update_check_result.incompatible_python_version
                ]
            )
        if update_check_result.incompatible_bot_version:
            message += (
                _(
                    "\nThese cogs require different Red version"
                    " than you currently have ({current_version}): "
                )
                if len(update_check_result.incompatible_bot_version) > 1
                else _(
                    "\nThis cog requires different Red version than you currently "
                    "have ({current_version}): "
                )
            ).format(current_version=__version__) + humanize_list(
                [
                    inline(cog.name)
                    + _(" (Minimum: {min_version}").format(min_version=cog.min_bot_version)
                    + (
                        ""
                        if cog.min_bot_version > cog.max_bot_version
                        else _(", at most: {max_version}").format(max_version=cog.max_bot_version)
                    )
                    + ")"
                    for cog in update_check_result.incompatible_bot_version
                ]
            )

        return message

    async def _format_cog_update_result(
        self, ctx: commands.Context, update_result: _downloader.CogUpdateResult
    ) -> str:
        current_cog_versions_map = {cog.name: cog for cog in update_result.checked_cogs}
        if update_result.failed_reqs:
            return (
                _("Failed to install requirements: ")
                if len(update_result.failed_reqs) > 1
                else _("Failed to install the requirement: ")
            ) + humanize_list(tuple(map(inline, update_result.failed_reqs)))

        message = _("Cog update completed successfully.")

        if update_result.updated_cogs:
            cogs_with_changed_eud_statement = set()
            for cog in update_result.updated_cogs:
                current_eud_statement = current_cog_versions_map[cog.name].end_user_data_statement
                if current_eud_statement != cog.end_user_data_statement:
                    cogs_with_changed_eud_statement.add(cog.name)
            message += _("\nUpdated: ") + humanize_list(
                [inline(cog.name) for cog in update_result.updated_cogs]
            )
            if cogs_with_changed_eud_statement:
                if len(cogs_with_changed_eud_statement) > 1:
                    message += (
                        _("\nEnd user data statements of these cogs have changed: ")
                        + humanize_list(tuple(map(inline, cogs_with_changed_eud_statement)))
                        + _("\nYou can use {command} to see the updated statements.\n").format(
                            command=inline(f"{ctx.clean_prefix}cog info <repo> <cog>")
                        )
                    )
                else:
                    message += (
                        _("\nEnd user data statement of this cog has changed:")
                        + inline(next(iter(cogs_with_changed_eud_statement)))
                        + _("\nYou can use {command} to see the updated statement.\n").format(
                            command=inline(f"{ctx.clean_prefix}cog info <repo> <cog>")
                        )
                    )
        if update_result.failed_cogs:
            cognames = [cog.name for cog in update_result.failed_cogs]
            message += (
                _("\nFailed to update cogs: ")
                if len(update_result.failed_cogs) > 1
                else _("\nFailed to update cog: ")
            ) + humanize_list(tuple(map(inline, cognames)))
        if not update_result.outdated_cogs:
            message = _("No cogs were updated.")
        if update_result.updated_libs:
            message += (
                _(
                    "\nSome shared libraries were updated, you should restart the bot "
                    "to bring the changes into effect."
                )
                if len(update_result.updated_libs) > 1
                else _(
                    "\nA shared library was updated, you should restart the "
                    "bot to bring the changes into effect."
                )
            )
        if update_result.failed_libs:
            libnames = [lib.name for lib in update_result.failed_libs]
            message += (
                _("\nFailed to install shared libraries: ")
                if len(update_result.failed_libs) > 1
                else _("\nFailed to install shared library: ")
            ) + humanize_list(tuple(map(inline, libnames)))
        return message

    async def _ask_for_cog_reload(
        self, ctx: commands.Context, updated_cogs: Tuple[InstalledModule, ...]
    ) -> None:
        updated_cognames = {cog.name for cog in updated_cogs}
        updated_cognames &= ctx.bot.extensions.keys()  # only reload loaded cogs
        if not updated_cognames:
            await ctx.send(_("None of the updated cogs were previously loaded. Update complete."))
            return

        if not ctx.assume_yes:
            message = (
                _("Would you like to reload the updated cogs?")
                if len(updated_cognames) > 1
                else _("Would you like to reload the updated cog?")
            )
            can_react = can_user_react_in(ctx.me, ctx.channel)
            if not can_react:
                message += " (yes/no)"
            query: discord.Message = await ctx.send(message)
            if can_react:
                # noinspection PyAsyncCall
                start_adding_reactions(query, ReactionPredicate.YES_OR_NO_EMOJIS)
                pred = ReactionPredicate.yes_or_no(query, ctx.author)
                event = "reaction_add"
            else:
                pred = MessagePredicate.yes_or_no(ctx)
                event = "message"
            try:
                await ctx.bot.wait_for(event, check=pred, timeout=30)
            except asyncio.TimeoutError:
                with contextlib.suppress(discord.NotFound):
                    await query.delete()
                return

            if not pred.result:
                if can_react:
                    with contextlib.suppress(discord.NotFound):
                        await query.delete()
                else:
                    await ctx.send(_("OK then."))
                return
            else:
                if can_react:
                    with contextlib.suppress(discord.Forbidden):
                        await query.clear_reactions()

        await ctx.invoke(ctx.bot.get_cog("Core").reload, *updated_cognames)

    def cog_name_from_instance(self, instance: object) -> str:
        """Determines the cog name that Downloader knows from the cog instance.

        Probably.

        Parameters
        ----------
        instance : object
            The cog instance.

        Returns
        -------
        str
            The name of the cog according to Downloader..

        """
        splitted = instance.__module__.split(".")
        return splitted[0]

    def _find_command(self, name: str):
        """A command by name, from either tree.

        Looks in the application commands first: this bot has been moving off
        prefix commands, so `bot.all_commands` is empty for most cogs now. A
        space-separated name reaches a subcommand, which is how slash commands
        are usually written down.
        """
        parts = name.split()
        node = self.bot.tree.get_command(parts[0])
        for part in parts[1:]:
            if node is None:
                break
            node = getattr(node, "get_command", lambda _n: None)(part)
        if node is not None:
            return node
        return self.bot.all_commands.get(parts[0])


    @staticmethod
    def format_failed_repos(failed: Collection[str]) -> str:
        """Format collection of ``Repo.name``'s into failed message.

        Parameters
        ----------
        failed : Collection
            Collection of ``Repo.name``

        Returns
        -------
        str
            formatted message
        """

        message = (
            _("Failed to update the following repositories:")
            if len(failed) > 1
            else _("Failed to update the following repository:")
        )
        message += " " + humanize_list(tuple(map(inline, failed))) + "\n"
        message += _(
            "The repository's branch might have been removed or"
            " the repository is no longer accessible at set url."
            " See logs for more information."
        )
        return message
