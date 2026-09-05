import logging
import typing as t

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import pagify
from .dashboard_integration import DashboardIntegration

log = logging.getLogger("red.vrt.commandlock")


class CogCommandConverter(t.NamedTuple):
    cog_or_command: commands.Command | commands.Cog
    is_cog: bool

    @classmethod
    async def convert(cls, ctx: commands.Context, argument: str) -> "CogCommandConverter":
        if cog := ctx.bot.get_cog(argument):
            return cls(cog, True)
        if command := ctx.bot.get_command(argument):
            return cls(command, False)
        raise commands.BadArgument(f"Cog or command '{argument}' not found.")


class CommandLock(DashboardIntegration, commands.Cog):
    """
    Lock command or cog usage to specific channels and redirect users to the correct ones.
    """

    __author__ = "[vertyco](https://github.com/vertyco/vrt-cogs)"
    __version__ = "0.1.0"

    def __init__(self, bot: Red):
        super().__init__()
        self.bot: Red = bot
        self.config = Config.get_conf(self, 350053505815281665, force_registration=True)
        guild_config = {
            "cog_locks": {},  # {cog_name: [channel_ids]}
            "command_locks": {},  # {qualified_command_name: [channel_ids]}
            "whitelisted_roles": [],  # list[role_ids] that bypass all locks
            "delete_after": 30,  # seconds; 0 means never delete
            "threads_bypass": False,  # if True, threads bypass all locks
        }
        self.config.register_guild(**guild_config)

    def format_help_for_context(self, ctx: commands.Context):
        helpcmd = super().format_help_for_context(ctx)
        txt = "Version: {}\nAuthor: {}".format(self.__version__, self.__author__)
        return f"{helpcmd}\n\n{txt}"

    async def red_delete_data_for_user(self, *, requester: str, user_id: int):
        # Requester can be "discord_deleted_user", "owner", "user", or "user_strict"
        return

    async def red_get_data_for_user(self, *, requester: str, user_id: int):
        # Requester can be "discord_deleted_user", "owner", "user", or "user_strict"
        return

    async def cog_load(self) -> None:
        self.bot.before_invoke(self.before_invoke_hook)

    async def cog_unload(self) -> None:
        self.bot.remove_before_invoke_hook(self.before_invoke_hook)

    async def before_invoke_hook(self, ctx: commands.Context):
        if await self.is_immune(ctx):
            return True
        if isinstance(ctx.channel, discord.Thread) and await self.config.guild(ctx.guild).threads_bypass():
            return True
        allowed_channels = await self.get_allowed_channels(ctx)
        delete_after = await self.config.guild(ctx.guild).delete_after()
        kwargs = {"delete_after": delete_after} if delete_after else {}
        if not allowed_channels:
            # The allowed channels are channels they cannot access
            err = "There are no channels in which you have permission to use this command."
            await ctx.send(err, **kwargs)
            raise commands.UserFeedbackCheckFailure()
        channel = ctx.channel.parent if isinstance(ctx.channel, discord.Thread) else ctx.channel
        if channel in allowed_channels:
            return True
        mentions = []
        for channel in allowed_channels:
            if isinstance(channel, discord.CategoryChannel):
                for inner_channel in channel.channels:
                    mentions.append(inner_channel.mention)
            else:
                mentions.append(channel.mention)
        mentions = ", ".join(mentions)
        msg = f"{ctx.author.mention}, you can only use this command in the following channels: {mentions}"
        for p in pagify(msg, page_length=1900, delims=[",", "\n"]):
            await ctx.send(p, **kwargs)
        raise commands.UserFeedbackCheckFailure()

    async def is_immune(self, ctx: commands.Context) -> bool:
        if (
            isinstance(ctx.command, commands.commands._AlwaysAvailableCommand)
            or ctx.guild is None
            or ctx.guild.owner_id == ctx.author.id
            or await self.bot.is_owner(ctx.author)
            or not isinstance(ctx.author, discord.Member)
            or await self.bot.is_admin(ctx.author)
        ):
            log.debug("User %s is immune to command locks.", ctx.author)
            return True
        # Whitelisted roles bypass all locks
        whitelist = await self.config.guild(ctx.guild).whitelisted_roles()
        if whitelist and any(role.id in whitelist for role in ctx.author.roles):
            log.debug("User %s has a whitelisted role and is immune to command locks.", ctx.author)
            return True
        return False

    async def get_allowed_channels(
        self,
        ctx: commands.Context,
    ) -> set[discord.abc.GuildChannel]:
        command = ctx.command
        assert command.cog_name is not None, "Command has no cog_name in allowed_channels"
        allowed_channels: set[discord.abc.GuildChannel] = set()
        command_name = command.qualified_name
        cog_name = command.cog_name
        log.debug(f"Checking locks for command '{command_name}' in cog '{cog_name}'")
        conf = await self.config.guild(ctx.guild).all()
        if command_name in conf["command_locks"]:
            channel_ids = conf["command_locks"][command_name]
            for channel_id in channel_ids:
                channel = ctx.guild.get_channel_or_thread(channel_id)
                if channel:
                    allowed_channels.add(channel)
                    if isinstance(channel, discord.CategoryChannel):
                        for inner_channel in channel.channels:
                            allowed_channels.add(inner_channel)
        elif cog_name in conf["cog_locks"]:
            channel_ids = conf["cog_locks"][cog_name]
            for channel_id in channel_ids:
                channel = ctx.guild.get_channel_or_thread(channel_id)
                if channel:
                    allowed_channels.add(channel)
                    if isinstance(channel, discord.CategoryChannel):
                        for inner_channel in channel.channels:
                            allowed_channels.add(inner_channel)
        else:
            # No locks set, allow all channels
            allowed_channels = set(ctx.guild.channels).union(set(ctx.guild.threads))
        return set(
            c
            for c in allowed_channels
            if c.permissions_for(ctx.author).view_channel and c.permissions_for(ctx.author).send_messages
        )
