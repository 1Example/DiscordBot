import asyncio
import re
import random
from datetime import datetime, timedelta
from typing import Iterable, List, Mapping, Tuple, Dict, Set, Literal, Union
from urllib.parse import quote_plus

import discord

from redbot.core import Config, commands
from redbot.core.commands import Parameter
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils import AsyncIter
from redbot.core.utils.chat_formatting import escape, humanize_list
from redbot.core.utils.predicates import MessagePredicate

from .dashboard_integration import DashboardIntegration

_ = Translator("CustomCommands", __file__)


class CCError(Exception):
    pass


class AlreadyExists(CCError):
    pass


class ArgParseError(CCError):
    pass


class NotFound(CCError):
    pass


class OnCooldown(CCError):
    pass


class CommandNotEdited(CCError):
    pass


class ResponseTooLong(CCError):
    pass


class CommandObj:
    def __init__(self, **kwargs):
        self.config = kwargs.get("config")
        self.bot = kwargs.get("bot")
        self.db = self.config.guild

    @staticmethod
    async def get_commands(config) -> dict:
        _commands = await config.commands()
        return {k: v for k, v in _commands.items() if _commands[k]}

    async def redact_author_ids(self, user_id: int):
        all_guilds = await self.config.all_guilds()

        for guild_id in all_guilds.keys():
            await asyncio.sleep(0)
            async with self.config.guild_from_id(guild_id).commands() as all_commands:
                async for com_name, com_info in AsyncIter(all_commands.items(), steps=100):
                    if not com_info:
                        continue

                    if com_info.get("author", {}).get("id", 0) == user_id:
                        com_info["author"]["id"] = 0xDE1
                        com_info["author"]["name"] = "Deleted User"

                    if editors := com_info.get("editors", None):
                        for index, editor_id in enumerate(editors):
                            if editor_id == user_id:
                                editors[index] = 0xDE1

    async def get_responses(self, ctx):
        intro = _(
            "Welcome to the interactive random {cc} maker!\n"
            "Every message you send will be added as one of the random "
            "responses to choose from once this {cc} is "
            "triggered. To exit this interactive menu, type `{quit}`"
        ).format(cc="customcommand", quit="exit()")
        await ctx.send(intro)

        responses = []
        args = None
        while True:
            await ctx.send(_("Add a random response:"))
            msg = await self.bot.wait_for("message", check=MessagePredicate.same_context(ctx))

            if msg.content.lower() == "exit()":
                break
            elif len(msg.content) > 2000:
                await ctx.send(
                    _(
                        "The text response you're trying to create has more than 2000 characters.\n"
                        "I cannot send messages that are longer than 2000 characters, please try again."
                    )
                )
                continue
            else:
                try:
                    this_args = ctx.cog.prepare_args(msg.content)
                except ArgParseError as e:
                    await ctx.send(e.args[0])
                    continue
                if args and args != this_args:
                    await ctx.send(_("Random responses must take the same arguments!"))
                    continue
                args = args or this_args
                responses.append(msg.content)
        return responses

    @staticmethod
    def get_now() -> str:
        # Get current time as a string, for 'created_at' and 'edited_at' fields
        # in the ccinfo dict
        return "{:%d/%m/%Y %H:%M:%S}".format(datetime.utcnow())

    async def get(self, message: discord.Message, command: str) -> Tuple[str, Dict]:
        if not command:
            raise NotFound()
        ccinfo = await self.db(message.guild).commands.get_raw(command, default=None)
        if not ccinfo:
            raise NotFound()
        else:
            return ccinfo["response"], ccinfo.get("cooldowns", {})

    async def get_full(self, message: discord.Message, command: str) -> Dict:
        ccinfo = await self.db(message.guild).commands.get_raw(command, default=None)
        if ccinfo:
            return ccinfo
        else:
            raise NotFound()

    async def create(
        self, ctx: commands.Context, command: str, *, response: Union[str, List[str]]
    ):
        """Create a custom command"""
        # Check if this command is already registered as a customcommand
        if await self.db(ctx.guild).commands.get_raw(command, default=None):
            raise AlreadyExists()
        # Check against those pesky nitro users!
        if isinstance(response, str) and len(response) > 2000:
            raise ResponseTooLong()
        elif isinstance(response, list) and any([len(i) > 2000 for i in response]):
            raise ResponseTooLong()
        # test to raise
        ctx.cog.prepare_args(response if isinstance(response, str) else response[0])
        author = ctx.message.author
        ccinfo = {
            "author": {"id": author.id, "name": str(author)},
            "command": command,
            "cooldowns": {},
            "created_at": self.get_now(),
            "editors": [],
            "response": response,
        }
        await self.db(ctx.guild).commands.set_raw(command, value=ccinfo)

    async def edit(
        self,
        ctx: commands.Context,
        command: str,
        *,
        response=None,
        cooldowns: Mapping[str, int] = None,
        ask_for: bool = True,
    ):
        """Edit an already existing custom command"""
        ccinfo = await self.db(ctx.guild).commands.get_raw(command, default=None)

        # Check if this command is registered
        if not ccinfo:
            raise NotFound()

        author = ctx.message.author

        if ask_for and not response:
            await ctx.send(_("Do you want to create a 'randomized' custom command?") + " (yes/no)")

            pred = MessagePredicate.yes_or_no(ctx)
            try:
                await self.bot.wait_for("message", check=pred, timeout=30)
            except asyncio.TimeoutError:
                await ctx.send(_("Response timed out, please try again later."))
                raise CommandNotEdited()
            if pred.result is True:
                response = await self.get_responses(ctx=ctx)
            else:
                await ctx.send(_("What response do you want?"))
                try:
                    resp = await self.bot.wait_for(
                        "message", check=MessagePredicate.same_context(ctx), timeout=180
                    )
                except asyncio.TimeoutError:
                    await ctx.send(_("Response timed out, please try again later."))
                    raise CommandNotEdited()
                response = resp.content

        if response:
            # test to raise
            if len(response) > 2000:
                raise ResponseTooLong()
            ctx.cog.prepare_args(response if isinstance(response, str) else response[0])
            ccinfo["response"] = response

        if cooldowns:
            ccinfo.setdefault("cooldowns", {}).update(cooldowns)
            for key, value in ccinfo["cooldowns"].copy().items():
                if value <= 0:
                    del ccinfo["cooldowns"][key]

        if author.id not in ccinfo["editors"]:
            # Add the person who invoked the `edit` coroutine to the list of
            # editors, if the person is not yet in there
            ccinfo["editors"].append(author.id)

        ccinfo["edited_at"] = self.get_now()

        await self.db(ctx.guild).commands.set_raw(command, value=ccinfo)

    async def delete(self, ctx: commands.Context, command: str):
        """Delete an already existing custom command"""
        # Check if this command is registered
        if not await self.db(ctx.guild).commands.get_raw(command, default=None):
            raise NotFound()
        await self.db(ctx.guild).commands.set_raw(command, value=None)


@cog_i18n(_)
class CustomCommands(DashboardIntegration, commands.Cog):
    """Commands you write yourself that reply with stored text.

    Useful for FAQ answers and invite links. Names are lowercase, and
    anyone can use them by default, so take care with pings.
    """

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.key = 414589031223512
        self.config = Config.get_conf(self, self.key)
        self.config.register_guild(commands={})
        self.commandobj = CommandObj(config=self.config, bot=self.bot)
        self.cooldowns = {}

    async def red_delete_data_for_user(
        self,
        *,
        requester: Literal["discord_deleted_user", "owner", "user", "user_strict"],
        user_id: int,
    ):
        if requester != "discord_deleted_user":
            return

        await self.commandobj.redact_author_ids(user_id)

    @commands.Cog.listener()
    async def on_message_without_command(self, message):
        is_private = message.guild is None

        # user_allowed check, will be replaced with self.bot.user_allowed or
        # something similar once it's added
        user_allowed = True

        if isinstance(message.channel, discord.PartialMessageable):
            return

        if len(message.content) < 2 or is_private or not user_allowed or message.author.bot:
            return

        if await self.bot.cog_disabled_in_guild(self, message.guild):
            return

        ctx = await self.bot.get_context(message)

        if ctx.prefix is None:
            return

        try:
            raw_response, cooldowns = await self.commandobj.get(
                message=message, command=ctx.invoked_with
            )
            if isinstance(raw_response, list):
                raw_response = random.choice(raw_response)
            elif isinstance(raw_response, str):
                pass
            else:
                raise NotFound()
            if cooldowns:
                self.test_cooldowns(ctx, ctx.invoked_with, cooldowns)
        except CCError:
            return

        # wrap the command here so it won't register with the bot
        fake_cc = commands.command(name=ctx.invoked_with)(self.cc_callback)
        fake_cc.params = self.prepare_args(raw_response)
        fake_cc.requires.ready_event.set()
        ctx.command = fake_cc

        await self.bot.invoke(ctx)
        if not ctx.command_failed:
            await self.cc_command(*ctx.args, **ctx.kwargs, raw_response=raw_response)

    async def cc_callback(self, *args, **kwargs) -> None:
        """
        Custom command.

        Created via the CustomCom cog. See `[p]customcom` for more details.
        """
        # fake command to take advantage of discord.py's parsing and events
        pass

    async def cc_command(self, ctx, *cc_args, raw_response, **cc_kwargs) -> None:
        cc_args = (*cc_args, *cc_kwargs.values())
        results = re.findall(r"{([^}]+)\}", raw_response)
        for result in results:
            param = self.transform_parameter(result, ctx.message)
            raw_response = raw_response.replace("{" + result + "}", param)
        results = re.findall(r"{((\d+)[^.}]*(\.[^:}]+)?[^}]*)\}", raw_response)
        if results:
            low = min(int(result[1]) for result in results)
            for result in results:
                index = int(result[1]) - low
                arg = self.transform_arg(result[0], result[2], cc_args[index])
                raw_response = raw_response.replace("{" + result[0] + "}", arg)
        await ctx.send(raw_response)

    @staticmethod
    def prepare_args(raw_response) -> Mapping[str, Parameter]:
        args = re.findall(r"{(\d+)[^:}]*(:[^.}]*)?[^}]*\}", raw_response)
        if not args:
            return {}
        allowed_builtins = {
            "bool": bool,
            "complex": complex,
            "float": float,
            "frozenset": frozenset,
            "int": int,
            "list": list,
            "set": set,
            "str": str,
            "tuple": tuple,
            "query": quote_plus,
        }
        indices = [int(a[0]) for a in args]
        low = min(indices)
        indices = [a - low for a in indices]
        high = max(indices)
        if high > 9:
            raise ArgParseError(_("Too many arguments!"))
        gaps = set(indices).symmetric_difference(range(high + 1))
        if gaps:
            raise ArgParseError(
                _("Arguments must be sequential. Missing arguments: ")
                + ", ".join(str(i + low) for i in gaps)
            )
        fin = [Parameter("_" + str(i), Parameter.POSITIONAL_OR_KEYWORD) for i in range(high + 1)]
        for arg in args:
            index = int(arg[0]) - low
            anno_raw = arg[1][1:]  # strip initial colon
            if anno_raw.lower().endswith("converter"):
                anno_raw = anno_raw[:-9]
            if not anno_raw or anno_raw.startswith("_"):  # public types only
                name = "{}_{}".format("text", index if index < high else "final")
                fin[index] = fin[index].replace(name=name)
                continue
            # allow type hinting only for discord.py and builtin types
            try:
                anno = getattr(discord, anno_raw)
                # force an AttributeError if there's no discord.py converter
                getattr(commands, anno.__name__ + "Converter")
            except AttributeError:
                anno = allowed_builtins.get(anno_raw.lower(), Parameter.empty)
            if (
                anno is not Parameter.empty
                and fin[index].annotation is not Parameter.empty
                and anno != fin[index].annotation
            ):
                raise ArgParseError(
                    _(
                        'Conflicting colon notation for argument {index}: "{name1}" and "{name2}".'
                    ).format(
                        index=index + low,
                        name1=fin[index].annotation.__name__,
                        name2=anno.__name__,
                    )
                )
            if anno is not Parameter.empty:
                fin[index] = fin[index].replace(annotation=anno)
        # consume rest
        fin[-1] = fin[-1].replace(kind=Parameter.KEYWORD_ONLY)
        # name the parameters for the help text
        for i, param in enumerate(fin):
            anno = param.annotation
            name = "{}_{}".format(
                "text" if anno is Parameter.empty else anno.__name__.lower(),
                i if i < high else "final",
            )
            fin[i] = fin[i].replace(name=name)
        return dict((p.name, p) for p in fin)

    def test_cooldowns(self, ctx, command, cooldowns):
        now = datetime.utcnow()
        new_cooldowns = {}
        for per, rate in cooldowns.items():
            if per == "guild":
                key = (command, ctx.guild)
            elif per == "channel":
                key = (command, ctx.guild, ctx.channel)
            elif per == "member":
                key = (command, ctx.guild, ctx.author)
            else:
                raise ValueError(per)
            cooldown = self.cooldowns.get(key)
            if cooldown:
                cooldown += timedelta(seconds=rate)
                if cooldown > now:
                    raise OnCooldown()
            new_cooldowns[key] = now
        # only update cooldowns if the command isn't on cooldown
        self.cooldowns.update(new_cooldowns)

    @classmethod
    def transform_arg(cls, result, attr, obj) -> str:
        attr = attr[1:]  # strip initial dot
        if not attr:
            return cls.maybe_humanize_list(obj)
        raw_result = "{" + result + "}"
        # forbid private members and nested attr lookups
        if attr.startswith("_") or "." in attr:
            return raw_result
        return cls.maybe_humanize_list(getattr(obj, attr, raw_result))

    @staticmethod
    def maybe_humanize_list(thing) -> str:
        if isinstance(thing, str):
            return thing
        try:
            return humanize_list(list(map(str, thing)))
        except TypeError:
            return str(thing)

    @staticmethod
    def transform_parameter(result, message) -> str:
        """
        For security reasons only specific objects are allowed
        Internals are ignored
        """
        raw_result = "{" + result + "}"
        objects = {
            "message": message,
            "author": message.author,
            "channel": message.channel,
            "guild": message.guild,
            "server": message.guild,
        }
        if result in objects:
            return str(objects[result])
        try:
            first, second = result.split(".")
        except ValueError:
            return raw_result
        if first in objects and not second.startswith("_"):
            first = objects[first]
        else:
            return raw_result
        return str(getattr(first, second, raw_result))

    async def get_command_names(self, guild: discord.Guild) -> Set[str]:
        """Get all custom command names in a guild.

        Returns
        --------
        Set[str]
            A set of all custom command names.

        """
        return set(await CommandObj.get_commands(self.config.guild(guild)))

    @staticmethod
    def prepare_command_list(
        ctx: commands.Context, command_list: Iterable[Tuple[str, dict]]
    ) -> List[Tuple[str, str]]:
        results = []
        for command, body in command_list:
            responses = body["response"]
            if isinstance(responses, list):
                result = ", ".join(responses)
            elif isinstance(responses, str):
                result = responses
            else:
                continue
            # Cut preview to 52 characters max
            if len(result) > 52:
                result = result[:49] + "..."
            # Replace newlines with spaces
            result = result.replace("\n", " ")
            # Escape markdown and mass mentions
            result = escape(result, formatting=True, mass_mentions=True)
            results.append((f"{ctx.clean_prefix}{command}", result))
        return results
