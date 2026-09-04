from typing import Deque, Dict

import discord
from red_commons.logging import getLogger
from redbot.core import Config, commands, modlog
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.chat_formatting import humanize_list

from .eventmixin import EventMixin, MemberUpdateEnum
from .settings import inv_settings
from .dashboard_integration import DashboardIntegration

_ = Translator("ExtendedModLog", __file__)
logger = getLogger("red.trusty-cogs.ExtendedModLog")


def wrapped_additional_help():
    """
    This wrapper lets me add a common string to multiple commands via a decorator.

    Note: This must be the last decorator on the function for it to work.
    """
    added_doc = _(
        """
    - `[events...]` must be any of the following options (more than one event can be provided at once):
     - `channel_change` - Updates to channel name, etc.
     - `channel_create`
     - `channel_delete`
     - `commands_used`  - Bot command usage
     - `emoji_change`   - Emojis added or deleted
     - `guild_change`   - Server settings changed
     - `message_edit`
     - `message_delete`
     - `member_change`  - Member changes like roles added/removed, nicknames, etc.
     - `role_change`    - Role updates permissions, name, etc.
     - `role_create`
     - `role_delete`
     - `voice_change`   - Voice channel join/leave
     - `member_join`
     - `member_left`
     - `invite_created`
     - `invite_deleted`
     - `thread_create`
     - `thread_delete`
     - `thread_change`
     - `stickers_change`
    """
    )

    def decorator(func):
        old = func.__doc__ or ""
        setattr(func, "__doc__", old + added_doc)
        return func

    return decorator


@cog_i18n(_)
class ExtendedModLog(DashboardIntegration, EventMixin, commands.Cog):
    """
    Extended modlogs
    Works with core modlogset channel
    """

    __author__ = ["RePulsar", "TrustyJAID"]
    __version__ = "2.13.1"

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, 154457677895, force_registration=True)
        self.config.register_guild(**inv_settings)
        self.config.register_global(version="0.0.0")
        self.settings = {}
        self._ban_cache = {}
        self.invite_links_loop.start()
        self.allowed_mentions = discord.AllowedMentions(users=False, roles=False, everyone=False)
        self.audit_log: Dict[int, Deque[discord.AuditLogEntry]] = {}

    def format_help_for_context(self, ctx: commands.Context):
        """
        Thanks Sinbad!
        """
        pre_processed = super().format_help_for_context(ctx)
        return f"{pre_processed}\n\nCog Version: {self.__version__}"

    async def cog_unload(self):
        self.invite_links_loop.stop()

    async def red_delete_data_for_user(self, **kwargs):
        """
        Nothing to delete
        """
        return

    async def cog_load(self) -> None:
        if await self.config.version() < "2.8.5":
            await self.migrate_2_8_5_settings()
        for guild_id in await self.config.all_guilds():
            self.settings[int(guild_id)] = await self.config.guild_from_id(guild_id).all()

    async def migrate_2_8_5_settings(self):
        all_data = await self.config.all_guilds()
        for guild_id, data in all_data.items():
            for entry, default in inv_settings.items():
                if entry not in data:
                    all_data[guild_id][entry] = inv_settings[entry]
                if isinstance(default, dict):
                    for key, _default in inv_settings[entry].items():
                        if not isinstance(all_data[guild_id][entry], dict):
                            all_data[guild_id][entry] = default
                        try:
                            if key not in all_data[guild_id][entry]:
                                all_data[guild_id][entry][key] = _default
                        except TypeError:
                            # del all_data[guild_id][entry]
                            logger.error("Somehow your dict was invalid.")
                            continue
            logger.info("Saving guild %s data to new version type", guild_id)
            await self.config.guild_from_id(guild_id).set(all_data[guild_id])
        await self.config.version.set("2.8.5")

    async def modlog_settings(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            return
        guild = ctx.guild
        try:
            _modlog_channel = await modlog.get_modlog_channel(guild)
            modlog_channel = _modlog_channel.mention
        except Exception:
            modlog_channel = _("Not Set")
        cur_settings = {
            "message_edit": _("Message edits"),
            "message_delete": _("Message delete"),
            "user_change": _("Member changes"),
            "role_change": _("Role changes"),
            "role_create": _("Role created"),
            "role_delete": _("Role deleted"),
            "voice_change": _("Voice changes"),
            "user_join": _("Member join"),
            "user_left": _("Member left"),
            "channel_change": _("Channel changes"),
            "channel_create": _("Channel created"),
            "channel_delete": _("Channel deleted"),
            "guild_change": _("Guild changes"),
            "emoji_change": _("Emoji changes"),
            "stickers_change": _("Stickers changes"),
            "commands_used": _("Commands"),
            "invite_created": _("Invite created"),
            "invite_deleted": _("Invite deleted"),
            "thread_create": _("Thread created"),
            "thread_delete": _("Thread deleted"),
            "thread_change": _("Thread changed"),
        }
        msg = _("Setting for {guild}\n Modlog Channel {channel}\n\n").format(
            guild=guild.name, channel=modlog_channel
        )
        if guild.id not in self.settings:
            self.settings[guild.id] = await self.config.guild(guild).all()

        data = self.settings[guild.id]
        ign_chans = data["ignored_channels"]
        ignored_channels = []
        for c in ign_chans:
            chn = guild.get_channel(c)
            if chn is None:
                # a bit of automatic cleanup so things don't break
                data["ignored_channels"].remove(c)
            else:
                ignored_channels.append(chn)
        ignored_users = [f"<@{uid}>" for uid in data["ignored_users"]]
        ignored_mods = [f"<@{uid}>" for uid in data["ignored_mods"]]
        enabled = ""
        disabled = ""
        for settings, name in cur_settings.items():
            msg += f"{name}: **{data[settings]['enabled']}**"
            if settings == "commands_used":
                msg += "\n" + humanize_list(data[settings]["privs"])
            if data[settings]["channel"]:
                chn = guild.get_channel(data[settings]["channel"])
                if chn is None:
                    # a bit of automatic cleanup so things don't break
                    data[settings]["channel"] = None
                else:
                    msg += f" {chn.mention}\n"
            else:
                msg += "\n"

        if enabled == "":
            enabled = _("None  ")
        if disabled == "":
            disabled = _("None  ")
        if ignored_channels:
            chans = ", ".join(c.mention for c in ignored_channels)
            msg += _("Ignored Channels") + ": " + chans
        if ignored_users:
            msg += _("Ignored Users: ") + humanize_list(ignored_users)
        if ignored_mods:
            msg += _("Ignored Mods: ") + humanize_list(ignored_mods)
        await self.config.guild(ctx.guild).set(data)
        # save the data back to config incase we had some deleted channels
        if await ctx.embed_requested():
            em = discord.Embed(description=msg)
            await ctx.send(embed=em)
        else:
            await ctx.send(msg, allowed_mentions=self.allowed_mentions)


    async def _members_settings(self, ctx: commands.Context, msg: str = ""):
        guild = ctx.guild
        if guild is None:
            return
        msg += _("\n### Member logging Settings for {guild}\n").format(guild=guild.name)
        if guild.id not in self.settings:
            self.settings[guild.id] = inv_settings

        data = self.settings[guild.id]["user_change"]
        for update_type in MemberUpdateEnum:
            msg += f"{update_type.get_name()}: **{data[update_type.name]}**\n"
        await self.save(guild)
        # save the data back to config incase we had some deleted channels
        await ctx.maybe_send_embed(msg)


    # For whatever reason trying to toggle all these settings causes all of the guilds
    # config to reset and I have no clue why so this will be unsupported for now


