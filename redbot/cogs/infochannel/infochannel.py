import asyncio
import logging
from collections import defaultdict
from typing import Dict, Optional

import discord
from redbot.core import Config
from redbot.core.bot import Red
from redbot.core.commands import Cog
from .dashboard_integration import DashboardIntegration

# 10 minutes. Rate limit is 2 per 10, so 1 per 6 is safe.
RATE_LIMIT_DELAY = 60 * 6  # If you're willing to risk rate limiting, you can decrease the delay

log = logging.getLogger("red.yamicogs.infochannel")


async def get_channel_counts(category, guild):
    # Gets count of bots
    bot_num = len([m for m in guild.members if m.bot])
    # Gets count of roles in the server
    roles_num = len(guild.roles) - 1
    # Gets count of channels in the server
    # <number of total channels> - <number of channels in the stats category> - <categories>
    channels_num = len(guild.channels) - len(category.voice_channels) - len(guild.categories)
    # Gets all counts of members
    members = guild.member_count
    offline_num = len(list(filter(lambda m: m.status is discord.Status.offline, guild.members)))
    online_num = members - offline_num
    # Gets count of actual users
    human_num = members - bot_num
    # count amount of premium subs/nitro subs.
    boosters = guild.premium_subscription_count
    return {
        "members": members,
        "humans": human_num,
        "boosters": boosters,
        "bots": bot_num,
        "roles": roles_num,
        "channels": channels_num,
        "online": online_num,
        "offline": offline_num,
    }


class InfoChannel(DashboardIntegration, Cog):
    """Channels whose names carry live server counts.

    Renaming a channel is heavily rate limited, so these refresh at most
    once every five minutes.
    """

    def __init__(self, bot: Red):
        super().__init__()
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=731101021116710497110110101108, force_registration=True
        )

        # self. so I can get the keys from this later
        self.default_channel_names = {
            "members": "Members: {count}",
            "humans": "Humans: {count}",
            "boosters": "Boosters: {count}",
            "bots": "Bots: {count}",
            "roles": "Roles: {count}",
            "channels": "Channels: {count}",
            "online": "Online: {count}",
            "offline": "Offline: {count}",
        }

        default_channel_ids = {k: None for k in self.default_channel_names}
        # Only members is enabled by default
        default_enabled_counts = {k: k == "members" for k in self.default_channel_names}

        default_guild = {
            "category_id": None,
            "channel_ids": default_channel_ids,
            "enabled_channels": default_enabled_counts,
            "channel_names": self.default_channel_names,
        }

        self.config.register_guild(**default_guild)

        self.default_role = {
            "enabled": False,
            "channel_id": None,
            "name": "{role}: {count}",
        }

        self.config.register_role(**self.default_role)

        self._critical_section_wooah_ = 0

        self.channel_data = defaultdict(dict)

        self.edit_queue = defaultdict(lambda: defaultdict(lambda: asyncio.Queue(maxsize=2)))

        self._rate_limited_edits: Dict[int, Dict[str, Optional[asyncio.Task]]] = defaultdict(
            lambda: defaultdict(lambda: None)
        )

    async def red_delete_data_for_user(self, **kwargs):
        """Nothing to delete"""
        return

    async def cog_load(self):
        asyncio.create_task(self.initialize())

    async def initialize(self):
        await self.bot.wait_until_red_ready()
        for guild in self.bot.guilds:
            await self.update_infochannel(guild)

    def cog_unload(self):
        self.stop_all_queues()


    async def create_individual_channel(
        self, guild, category: discord.CategoryChannel, overwrites, channel_type, count
    ):
        # Delete the channel if it exists
        channel_id = await self.config.guild(guild).channel_ids.get_raw(channel_type)
        if channel_id is not None:
            channel: discord.VoiceChannel = guild.get_channel(channel_id)
            if channel:
                self.stop_queue(guild.id, channel_type)
                await channel.delete(reason="InfoChannel delete")

        # Only make the channel if it's enabled
        if await self.config.guild(guild).enabled_channels.get_raw(channel_type):
            name = await self.config.guild(guild).channel_names.get_raw(channel_type)
            name = name.format(count=count)
            channel = await category.create_voice_channel(
                name, reason="InfoChannel make", overwrites=overwrites
            )
            await self.config.guild(guild).channel_ids.set_raw(channel_type, value=channel.id)
            return channel
        return None

    async def create_role_channel(
        self, guild, category: discord.CategoryChannel, overwrites, role: discord.Role
    ):
        # Delete the channel if it exists
        channel_id = await self.config.role(role).channel_id()
        if channel_id is not None:
            channel: discord.VoiceChannel = guild.get_channel(channel_id)
            if channel:
                self.stop_queue(guild.id, role.id)
                await channel.delete(reason="InfoChannel delete")

        # Only make the channel if it's enabled
        if await self.config.role(role).enabled():
            count = len(role.members)
            name = await self.config.role(role).name()
            name = name.format(role=role.name, count=count)
            channel = await category.create_voice_channel(
                name, reason="InfoChannel make", overwrites=overwrites
            )
            await self.config.role(role).channel_id.set(channel.id)
            return channel
        return None

    async def make_infochannel(self, guild: discord.Guild, channel_type=None, channel_role=None):
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(connect=False),
            guild.me: discord.PermissionOverwrite(manage_channels=True, connect=True),
        }

        # Check for and create the Infochannel category
        category_id = await self.config.guild(guild).category_id()
        if category_id is not None:
            category: discord.CategoryChannel = guild.get_channel(category_id)
            if category is None:  # Category id is invalid, probably deleted.
                category_id = None
        if category_id is None:
            category: discord.CategoryChannel = await guild.create_category(
                "Server Stats", reason="InfoChannel Category make"
            )
            await self.config.guild(guild).category_id.set(category.id)
            await category.edit(position=0)
            category_id = category.id

        category: discord.CategoryChannel = guild.get_channel(category_id)

        channel_data = await get_channel_counts(category, guild)

        # Only update a single channel
        if channel_type is not None:
            await self.create_individual_channel(
                guild, category, overwrites, channel_type, channel_data[channel_type]
            )
            return
        if channel_role is not None:
            await self.create_role_channel(guild, category, overwrites, channel_role)
            return

        # Update all channels
        for channel_type in self.default_channel_names.keys():
            await self.create_individual_channel(
                guild, category, overwrites, channel_type, channel_data[channel_type]
            )

        for role in guild.roles:
            await self.create_role_channel(guild, category, overwrites, role)

        # await self.update_infochannel(guild)

    async def delete_all_infochannels(self, guild: discord.Guild):
        self.stop_guild_queues(guild.id)  # Stop processing edits

        # Delete regular channels
        for channel_type in self.default_channel_names.keys():
            channel_id = await self.config.guild(guild).channel_ids.get_raw(channel_type)
            if channel_id is not None:
                channel = guild.get_channel(channel_id)
                if channel is not None:
                    await channel.delete(reason="InfoChannel delete")
                await self.config.guild(guild).channel_ids.clear_raw(channel_type)

        # Delete role channels
        for role in guild.roles:
            channel_id = await self.config.role(role).channel_id()
            if channel_id is not None:
                channel = guild.get_channel(channel_id)
                if channel is not None:
                    await channel.delete(reason="InfoChannel delete")
                await self.config.role(role).channel_id.clear()

        # Delete the category last
        category_id = await self.config.guild(guild).category_id()
        if category_id is not None:
            category = guild.get_channel(category_id)
            if category is not None:
                await category.delete(reason="InfoChannel delete")

    async def add_to_queue(self, guild, channel, identifier, count, formatted_name):
        self.channel_data[guild.id][identifier] = (count, formatted_name, channel.id)
        if not self.edit_queue[guild.id][identifier].full():
            try:
                self.edit_queue[guild.id][identifier].put_nowait(identifier)
            except asyncio.QueueFull:
                pass  # If queue is full, disregard

        if self._rate_limited_edits[guild.id][identifier] is None:
            await self.start_queue(guild.id, identifier)

    async def update_individual_channel(self, guild, channel_type, count, guild_data):
        name = guild_data["channel_names"][channel_type]
        name = name.format(count=count)
        channel = guild.get_channel(guild_data["channel_ids"][channel_type])
        if channel is None:
            return  # abort
        await self.add_to_queue(guild, channel, channel_type, count, name)

    async def update_role_channel(self, guild, role: discord.Role, role_data):
        if not role_data["enabled"]:
            return  # Not enabled
        count = len(role.members)
        name = role_data["name"]
        name = name.format(role=role.name, count=count)
        channel = guild.get_channel(role_data["channel_id"])
        if channel is None:
            return  # abort
        await self.add_to_queue(guild, channel, role.id, count, name)

    async def update_infochannel(self, guild: discord.Guild, channel_type=None, channel_role=None):
        if channel_type is None and channel_role is None:
            return await self.trigger_updates_for(
                guild,
                members=True,
                humans=True,
                boosters=True,
                bots=True,
                roles=True,
                channels=True,
                online=True,
                offline=True,
                extra_roles=set(guild.roles),
            )

        if channel_type is not None:
            return await self.trigger_updates_for(guild, **{channel_type: True})

        return await self.trigger_updates_for(guild, extra_roles={channel_role})

    async def start_queue(self, guild_id, identifier):
        self._rate_limited_edits[guild_id][identifier] = asyncio.create_task(
            self._process_queue(guild_id, identifier)
        )

    def stop_queue(self, guild_id, identifier):
        if self._rate_limited_edits[guild_id][identifier] is not None:
            self._rate_limited_edits[guild_id][identifier].cancel()

    def stop_guild_queues(self, guild_id):
        for identifier in self._rate_limited_edits[guild_id].keys():
            self.stop_queue(guild_id, identifier)

    def stop_all_queues(self):
        for guild_id in self._rate_limited_edits.keys():
            self.stop_guild_queues(guild_id)

    async def _process_queue(self, guild_id, identifier):
        while True:
            identifier = await self.edit_queue[guild_id][identifier].get()  # Waits forever

            count, formatted_name, channel_id = self.channel_data[guild_id][identifier]
            channel: discord.VoiceChannel = self.bot.get_channel(channel_id)

            if channel.name == formatted_name:
                continue  # Nothing to process

            log.debug(f"Processing guild_id: {guild_id} - identifier: {identifier}")

            try:
                await channel.edit(reason="InfoChannel update", name=formatted_name)
            except (discord.Forbidden, discord.HTTPException):
                pass  # Don't bother figuring it out
            except discord.InvalidArgument:
                log.exception(f"Invalid formatted infochannel: {formatted_name}")
            else:
                await asyncio.sleep(RATE_LIMIT_DELAY)  # Wait a reasonable amount of time

    async def trigger_updates_for(self, guild, **kwargs):
        extra_roles: Optional[set] = kwargs.pop("extra_roles", False)
        guild_data = await self.config.guild(guild).all()

        to_update = kwargs.keys() & [
            key for key, value in guild_data["enabled_channels"].items() if value
        ]  # Value in kwargs doesn't matter

        if to_update or extra_roles:
            log.debug(f"{to_update=}\n" f"{extra_roles=}")

            category = guild.get_channel(guild_data["category_id"])
            if category is None:
                log.debug("Channel category is missing, updating must be off")
                return  # Nothing to update, must be off

            channel_data = await get_channel_counts(category, guild)
            if to_update:
                for channel_type in to_update:
                    await self.update_individual_channel(
                        guild, channel_type, channel_data[channel_type], guild_data
                    )
            if extra_roles:
                role_data = await self.config.all_roles()
                for channel_role in extra_roles:
                    if channel_role.id in role_data:
                        await self.update_role_channel(
                            guild, channel_role, role_data[channel_role.id]
                        )

    @Cog.listener(name="on_member_join")
    @Cog.listener(name="on_member_remove")
    async def on_member_join_remove(self, member: discord.Member):
        if await self.bot.cog_disabled_in_guild(self, member.guild):
            return

        if member.bot:
            await self.trigger_updates_for(
                member.guild, members=True, bots=True, online=True, offline=True
            )
        else:
            await self.trigger_updates_for(
                member.guild, members=True, humans=True, online=True, offline=True
            )

    @Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if await self.bot.cog_disabled_in_guild(self, after.guild):
            return

        # XOR
        c = set(after.roles) ^ set(before.roles)

        if c:
            await self.trigger_updates_for(after.guild, extra_roles=c)

    @Cog.listener()
    async def on_presence_update(self, before, after):
        if await self.bot.cog_disabled_in_guild(self, after.guild):
            return

        if before.status != after.status:
            return await self.trigger_updates_for(after.guild, online=True, offline=True)

    @Cog.listener("on_guild_channel_create")
    @Cog.listener("on_guild_channel_delete")
    async def on_guild_channel_create_delete(self, channel: discord.TextChannel):
        if await self.bot.cog_disabled_in_guild(self, channel.guild):
            return
        await self.trigger_updates_for(channel.guild, channels=True)

    @Cog.listener()
    async def on_guild_role_create(self, role):
        if await self.bot.cog_disabled_in_guild(self, role.guild):
            return
        await self.trigger_updates_for(role.guild, roles=True)

    @Cog.listener()
    async def on_guild_role_delete(self, role):
        if await self.bot.cog_disabled_in_guild(self, role.guild):
            return
        await self.trigger_updates_for(role.guild, roles=True)

        role_channel_id = await self.config.role(role).channel_id()
        if role_channel_id is not None:
            rolechannel: discord.VoiceChannel = role.guild.get_channel(role_channel_id)
            if rolechannel:
                await rolechannel.delete(reason="InfoChannel delete")

        await self.config.role(role).clear()
