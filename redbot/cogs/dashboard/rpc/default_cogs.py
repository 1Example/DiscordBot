import typing


from redbot.cogs.customcom.customcom import ArgParseError
from redbot.core import commands
from redbot.core.i18n import Translator

from redbot.core.utils import synthetic_context

from .utils import rpc_check

if typing.TYPE_CHECKING:
    from redbot.core.bot import Red

_: Translator = Translator("Dashboard", __file__)


class DashboardRPC_DefaultCogs:
    def __init__(self, cog: commands.Cog) -> None:
        self.bot: Red = cog.bot
        self.cog: commands.Cog = cog

        self.bot.register_rpc_handler(self.get_aliases)
        self.bot.register_rpc_handler(self.set_aliases)
        self.bot.register_rpc_handler(self.get_custom_commands)
        self.bot.register_rpc_handler(self.set_custom_commands)

    def unload(self) -> None:
        self.bot.unregister_rpc_handler(self.get_aliases)
        self.bot.unregister_rpc_handler(self.set_aliases)
        self.bot.unregister_rpc_handler(self.get_custom_commands)
        self.bot.unregister_rpc_handler(self.set_custom_commands)


    @rpc_check()
    async def get_custom_commands(self, user_id: int, guild_id: int):
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return {"status": 1}
        member = guild.get_member(user_id)
        if user_id not in self.bot.owner_ids and (
            member is None
            or not (await self.bot.is_mod(member) or member.guild_permissions.administrator)
        ):
            return {"status": 1}
        CustomCommands = self.bot.get_cog("CustomCommands")
        if CustomCommands is None:
            return {"status": 2}
        custom_commands = (
            await CustomCommands.commandobj.get_commands(CustomCommands.config.guild(guild))
        ).values()
        return {
            "status": 0,
            "custom_commands": {
                custom_command["command"]: custom_command["response"]
                for custom_command in sorted(
                    custom_commands,
                    key=lambda custom_command: custom_command["command"],
                )
            },
        }

    @rpc_check()
    async def set_custom_commands(
        self,
        user_id: int,
        guild_id: int,
        custom_commands: dict[str, str],
    ):
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return {"status": 1}
        member = guild.get_member(user_id)
        if user_id not in self.bot.owner_ids and (
            member is None
            or not (await self.bot.is_mod(member) or member.guild_permissions.administrator)
        ):
            return {"status": 1}
        CustomCommands = self.bot.get_cog("CustomCommands")
        if CustomCommands is None:
            return {"status": 2}
        ctx = await synthetic_context(
            bot=self.bot,
            author=member,
            channel=guild.text_channels[0],
        )
        existing_custom_commands = await CustomCommands.commandobj.get_commands(
            CustomCommands.config.guild(guild),
        )
        errors = []
        for command, responses in custom_commands.items():
            if command not in existing_custom_commands:
                try:
                    await CustomCommands.commandobj.create(ctx, command, response=responses)
                except ArgParseError as e:
                    errors.append(_("`{command}`: ").format(command=command) + e.args[0])
            elif responses != existing_custom_commands[command]["response"]:
                await CustomCommands.commandobj.edit(
                    ctx,
                    command,
                    response=responses,
                    ask_for=False,
                )
        for command in existing_custom_commands:
            if command not in custom_commands:
                await CustomCommands.commandobj.delete(ctx, command)
        if errors:
            return {"status": 1, "errors": errors}
        return {"status": 0}
