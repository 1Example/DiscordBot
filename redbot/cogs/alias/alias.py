import asyncio
import logging
from copy import copy
from re import search
from string import Formatter
from typing import Literal

import discord
from redbot.core import Config, commands
from redbot.core.i18n import Translator, cog_i18n

from redbot.core.bot import Red
from .alias_entry import AliasEntry, AliasCache
from .dashboard_integration import DashboardIntegration

_ = Translator("Alias", __file__)

log = logging.getLogger("red.cogs.alias")


class _TrackingFormatter(Formatter):
    def __init__(self):
        super().__init__()
        self.max = -1

    def get_value(self, key, args, kwargs):
        if isinstance(key, int):
            self.max = max((key, self.max))
        return super().get_value(key, args, kwargs)


@cog_i18n(_)
class Alias(DashboardIntegration, commands.Cog):
    """Create aliases for commands.

    Aliases are alternative names/shortcuts for commands. They
    can act as both a lambda (storing arguments for repeated use)
    or as simply a shortcut to saying "x y z".

    When run, aliases will accept any additional arguments
    and append them to the stored alias.
    """

    def __init__(self, bot: Red):
        super().__init__()
        self.bot = bot
        self.config = Config.get_conf(self, 8927348724)

        self.config.register_global(entries=[], handled_string_creator=False)
        self.config.register_guild(entries=[])
        self._aliases: AliasCache = AliasCache(config=self.config, cache_enabled=True)

    async def cog_load(self) -> None:
        await self._maybe_handle_string_keys()

        if not self._aliases._loaded:
            await self._aliases.load_aliases()

    async def red_delete_data_for_user(
        self,
        *,
        requester: Literal["discord_deleted_user", "owner", "user", "user_strict"],
        user_id: int,
    ):
        if requester != "discord_deleted_user":
            return

        await self._aliases.anonymize_aliases(user_id)

    async def _maybe_handle_string_keys(self):
        # This isn't a normal schema migration because it's being added
        # after the fact for GH-3788
        if await self.config.handled_string_creator():
            return

        async with self.config.entries() as alias_list:
            bad_aliases = []
            for a in alias_list:
                for keyname in ("creator", "guild"):
                    if isinstance((val := a.get(keyname)), str):
                        try:
                            a[keyname] = int(val)
                        except ValueError:
                            # Because migrations weren't created as changes were made,
                            # and the prior form was a string of an ID,
                            # if this fails, there's nothing to go back to
                            bad_aliases.append(a)
                            break

            for a in bad_aliases:
                alias_list.remove(a)

        # if this was using a custom group of (guild_id, aliasname) it would be better but...
        all_guild_aliases = await self.config.all_guilds()

        for guild_id, guild_data in all_guild_aliases.items():
            to_set = []
            modified = False

            for a in guild_data.get("entries", []):
                for keyname in ("creator", "guild"):
                    if isinstance((val := a.get(keyname)), str):
                        try:
                            a[keyname] = int(val)
                        except ValueError:
                            break
                        finally:
                            modified = True
                else:
                    to_set.append(a)

            if modified:
                await self.config.guild_from_id(guild_id).entries.set(to_set)

            await asyncio.sleep(0)
            # control yielded per loop since this is most likely to happen
            # at bot startup, where this is most likely to have a performance
            # hit.

        await self.config.handled_string_creator.set(True)

    def is_command(self, alias_name: str) -> bool:
        """
        The logic here is that if this returns true, the name should not be used for an alias
        The function name can be changed when alias is reworked
        """
        command = self.bot.get_command(alias_name)
        return command is not None or alias_name in commands.RESERVED_COMMAND_NAMES

    @staticmethod
    def is_valid_alias_name(alias_name: str) -> bool:
        return not bool(search(r"\s", alias_name)) and alias_name.isprintable()

    async def get_prefix(self, message: discord.Message) -> str:
        """
        Tries to determine what prefix is used in a message object.
            Looks to identify from longest prefix to smallest.

            Will raise ValueError if no prefix is found.
        :param message: Message object
        :return:
        """
        content = message.content
        prefix_list = await self.bot.command_prefix(self.bot, message)
        prefixes = sorted(prefix_list, key=lambda pfx: len(pfx), reverse=True)
        for p in prefixes:
            if content.startswith(p):
                return p
        raise ValueError("No prefix found.")

    async def call_alias(self, message: discord.Message, prefix: str, alias: AliasEntry):
        new_message = self.translate_alias_message(message, prefix, alias)
        await self.bot.process_commands(new_message)

    def translate_alias_message(self, message: discord.Message, prefix: str, alias: AliasEntry):
        """
        Translates a discord message using an alias
        for a command to a discord message using the
        alias' base command.
        """
        new_message = copy(message)
        try:
            args = alias.get_extra_args_from_alias(message, prefix)
        except commands.BadArgument:
            return

        trackform = _TrackingFormatter()
        command = trackform.format(alias.command, *args)

        # noinspection PyDunderSlots
        new_message.content = "{}{} {}".format(
            prefix, command, " ".join(args[trackform.max + 1 :])
        ).strip()

        return new_message

    @commands.Cog.listener()
    async def on_message_without_command(self, message: discord.Message):
        if message.guild is not None:
            if await self.bot.cog_disabled_in_guild(self, message.guild):
                return

        try:
            prefix = await self.get_prefix(message)
        except ValueError:
            return

        try:
            potential_alias = message.content[len(prefix) :].split(" ")[0]
        except IndexError:
            return

        alias = await self._aliases.get_alias(message.guild, potential_alias)

        if alias:
            await self.call_alias(message, prefix, alias)
