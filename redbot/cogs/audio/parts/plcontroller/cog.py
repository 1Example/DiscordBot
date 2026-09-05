from __future__ import annotations

import asyncio
import contextlib
import datetime
from collections import defaultdict
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Literal

import discord
from apscheduler.jobstores.base import JobLookupError
from redbot.core import Config, commands
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.antispam import AntiSpam
from redbot.core.utils.chat_formatting import humanize_number

from .dashboard_integration import ControllerDashboard
from .view import PersistentControllerView
from pylav import logging
from pylav.core.context import PyLavContext
from pylav.events.player import PlayerPausedEvent, PlayerResumedEvent, PlayerStoppedEvent
from pylav.events.queue import QueueEndEvent
from pylav.events.track import TrackStartEvent
from pylav.helpers.time import get_now_utc
from pylav.players.player import Player
from pylav.players.query.obj import Query
from pylav.type_hints.bot import DISCORD_COG_TYPE_MIXIN

_ = Translator("PyLavController", Path(__file__))

LOGGER = logging.getLogger("red.PyLav.cog.Controller")


@cog_i18n(_)
class PyLavController(
    ControllerDashboard,
    DISCORD_COG_TYPE_MIXIN,
):
    """Set a channel to listens and control the music player."""

    __version__ = "1.0.0"

    def _controller_init(self) -> None:
        self._controller_config = Config.get_conf(
            None, identifier=208903205982044161, cog_name="PyLavController"
        )
        self.__lock: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.__defaults = dict(
            channel=None,
            list_for_requests=False,
            list_for_searches=False,
            persistent_view_message_id=None,
            enable_antispam=True,
            use_slow_mode=True,
            # Per-button appearance, set from the dashboard's settings page.
            # Empty means "use the built-in default" - see BUTTONS in view.py.
            button_emojis={},
            button_labels={},
            button_styles={},
            # Emoji this cog uploaded itself, so replacing a picture can delete
            # the old one instead of leaving it in the bot's emoji list.
            owned_emojis={},
            # "guild" puts an uploaded picture in this server's emoji list, which
            # is known to work on a button but costs a slot. "bot" puts it in the
            # bot's own application emoji, which costs no slot - Discord has been
            # seen refusing those on a component, so any refusal is recorded in
            # rejected_emojis rather than silently losing the icon.
            upload_target="guild",
            rejected_emojis={},
            # Per-action credit costs for the web dashboard. Staff are never
            # charged. "Play now" jumps the queue so it costs more than queueing.
            # Tune with [p]plcontrollerset cost <action> <amount>, 0 = free.
            dashboard_action_costs={
                "play": 25,
                "play_now": 100,
                "fav_play": 100,
                "fav_queue": 25,
                "fav_add": 10,
                "search": 5,
                "skip": 50,
                "previous": 50,
                "pause": 10,
                "resume": 10,
                "shuffle": 30,
                "seek": 20,
                "volume_set": 10,
                "volume_up": 10,
                "volume_down": 10,
            },
            # Master switch - flip off to make the dashboard player free again.
            dashboard_economy_enabled=True,
        )
        self._controller_config.register_guild(**self.__defaults)
        self._controller_config.register_global(
            listen_to_any_message=False,
        )
        self._channel_cache: dict[int, int] = {}
        self._list_for_search_cache: dict[int, bool] = defaultdict(lambda: self.__defaults["list_for_searches"])
        self._list_for_command_cache: dict[int, bool] = defaultdict(lambda: self.__defaults["list_for_requests"])
        self._enable_antispam_cache: dict[int, bool] = defaultdict(lambda: self.__defaults["enable_antispam"])
        self._use_slow_mode_cache: dict[int, bool] = defaultdict(lambda: self.__defaults["use_slow_mode"])
        self._view_cache: dict[int, PersistentControllerView] = {}
        self.__failed_messages_to_delete: dict[int, set[discord.Message]] = defaultdict(set)
        self.__success_messages_to_delete: dict[int, set[discord.Message]] = defaultdict(set)
        self._greedy_cache = False
        self.__ready = asyncio.Event()
        intervals = [
            (timedelta(minutes=1), 5),
            (timedelta(hours=1), 50),
        ]

        self.antispam: dict[int, dict[int, AntiSpam]] = defaultdict(lambda: defaultdict(partial(AntiSpam, intervals)))

    async def _controller_ready(self, context: PyLavContext) -> bool:
        return self.__ready.is_set()

    async def _controller_unload(self) -> None:
        self.bot.remove_listener(self.on_message)
        self.bot.remove_listener(self.on_message_without_command)
        for view in self._view_cache.values():
            view.stop()
        self._view_cache.clear()
        with contextlib.suppress(JobLookupError):
            self.pylav.scheduler.remove_job(f"{self.__class__.__name__}-{self.bot.user.id}-delete_failed_messages")
        with contextlib.suppress(JobLookupError):
            self.pylav.scheduler.remove_job(f"{self.__class__.__name__}-{self.bot.user.id}-delete_successful_messages")

    async def _controller_initialize(self):
        await self.pylav.wait_until_ready()

        guild_data = await self._controller_config.all_guilds()

        for guild_id, data in guild_data.items():
            if channel_id := data["channel"]:
                self._channel_cache[guild_id] = channel_id
            self._list_for_command_cache[guild_id] = data["list_for_requests"]
            self._list_for_search_cache[guild_id] = data["list_for_searches"]
            self._enable_antispam_cache[guild_id] = data["enable_antispam"]
            self._use_slow_mode_cache[guild_id] = data["use_slow_mode"]
            if data["persistent_view_message_id"]:
                if channel := self.bot.get_channel(channel_id):
                    await self.prepare_channel(channel)
        self.__ready.set()
        self.pylav.scheduler.add_job(
            self.delete_failed_messages,
            trigger="interval",
            seconds=5,
            max_instances=1,
            id=f"{self.__class__.__name__}-{self.bot.user.id}-delete_failed_messages",
            replace_existing=True,
            coalesce=True,
            next_run_time=get_now_utc() + datetime.timedelta(seconds=5),
        )
        self.pylav.scheduler.add_job(
            self.delete_successful_messages,
            trigger="interval",
            seconds=5,
            max_instances=1,
            id=f"{self.__class__.__name__}-{self.bot.user.id}-delete_successful_messages",
            replace_existing=True,
            coalesce=True,
            next_run_time=get_now_utc() + datetime.timedelta(seconds=5),
        )
        if await self._controller_config.listen_to_any_message():
            self.bot.add_listener(self.on_message)
        else:
            self.bot.add_listener(self.on_message_without_command)


    async def volume(self, context: PyLavContext, change_by: int):
        if isinstance(context, discord.Interaction):
            context = await self.bot.get_context(context)
        if context.interaction and not context.interaction.response.is_done():
            await context.defer(ephemeral=True)
        if not context.player:
            await context.send(
                embed=await context.pylav.construct_embed(
                    description=_("I am not connected to a voice channel."), messageable=context
                ),
                ephemeral=True,
            )
            return
        max_volume = await self.pylav.player_config_manager.get_max_volume(context.guild.id)
        new_vol = context.player.volume + change_by
        if new_vol > max_volume:
            await context.player.set_volume(max_volume, requester=context.author)
            await context.send(
                embed=await context.pylav.construct_embed(
                    description=_(
                        "Volume limit reached, player volume set to {volume_variable_do_not_translate}%."
                    ).format(volume_variable_do_not_translate=humanize_number(context.player.volume)),
                    messageable=context,
                ),
                ephemeral=True,
            )
        elif new_vol < 0:
            await context.player.set_volume(0, requester=context.author)
            await context.send(
                embed=await context.pylav.construct_embed(
                    description=_("Minimum volume reached, player volume set to 0%."), messageable=context
                ),
                ephemeral=True,
            )
        else:
            await context.player.set_volume(new_vol, requester=context.author)
            await context.send(
                embed=await context.pylav.construct_embed(
                    description=_("Player volume set to {volume_variable_do_not_translate}%").format(
                        volume_variable_do_not_translate=new_vol
                    ),
                    messageable=context,
                ),
                ephemeral=True,
            )

    async def repeat(self, context: PyLavContext, queue: bool | None = None):
        """Set whether to repeat the current song or queue.

        If no argument is given, the current repeat mode will be toggled between the current track and off.
        """
        if isinstance(context, discord.Interaction):
            context = await self.bot.get_context(context)
        if context.interaction and not context.interaction.response.is_done():
            await context.defer(ephemeral=True)
        if not context.player:
            await context.send(
                embed=await context.pylav.construct_embed(
                    description=_("I am not currently playing anything on this server."), messageable=context
                ),
                ephemeral=True,
            )
            return

        if queue:
            await context.player.set_repeat("queue", True, context.author)
            msg = _("From now on, I will now repeat the entire queue.")
        elif await context.player.is_repeating():
            await context.player.set_repeat("disable", False, context.author)
            msg = _("From now on, I will no longer repeat any tracks.")
        else:
            await context.player.set_repeat("current", True, context.author)
            if context.player.current:
                msg = _("From now on, I will now repeat {track_name_variable_do_not_translate}.").format(
                    track_name_variable_do_not_translate=await context.player.current.get_track_display_name(
                        with_url=True
                    )
                )
            else:
                msg = _("From now on, I will now repeat the current track.")
        await context.send(
            embed=await context.pylav.construct_embed(description=msg, messageable=context), ephemeral=True
        )

    async def shuffle(self, context: PyLavContext):
        """Shuffles the current queue."""
        if isinstance(context, discord.Interaction):
            context = await self.bot.get_context(context)
        if context.interaction and not context.interaction.response.is_done():
            await context.defer(ephemeral=True)
        if not context.player:
            await context.send(
                embed=await context.pylav.construct_embed(
                    description=_("I am not currently playing anything on this server."), messageable=context
                ),
                ephemeral=True,
            )
            return
        if context.player.queue.empty():
            await context.send(
                embed=await context.pylav.construct_embed(
                    description=_("The server queue is currently empty."), messageable=context
                ),
                ephemeral=True,
            )
            return
        if (await self.pylav.player_config_manager.get_shuffle(context.guild.id)) is False:
            await context.send(
                embed=await context.pylav.construct_embed(
                    description=_("You are not allowed to shuffle the queue."), messageable=context
                ),
                ephemeral=True,
            )
            return
        await context.player.shuffle_queue(context.author.id)
        await context.send(
            embed=await context.pylav.construct_embed(
                description=_("{queue_size_variable_do_not_translate} tracks shuffled.").format(
                    queue_size_variable_do_not_translate=context.player.queue.size()
                ),
                messageable=context,
            ),
            ephemeral=True,
        )

    async def skip(self, context: PyLavContext):
        """Skips the current track."""
        if isinstance(context, discord.Interaction):
            context = await self.bot.get_context(context)
        if context.interaction and not context.interaction.response.is_done():
            await context.defer(ephemeral=True)

        if not context.player or (not context.player.current and not context.player.autoplay_enabled):
            await context.send(
                embed=await context.pylav.construct_embed(
                    description=_("I am not currently playing anything on this server."), messageable=context
                ),
                ephemeral=True,
            )
            return
        if context.player.current:
            await context.send(
                embed=await context.pylav.construct_embed(
                    description=_("I have skipped {track_name_variable_do_not_translate} as requested.").format(
                        track_name_variable_do_not_translate=await context.player.current.get_track_display_name(
                            with_url=True
                        )
                    ),
                    thumbnail=await context.player.current.artworkUrl(),
                    messageable=context,
                ),
                ephemeral=True,
                file=await context.player.current.get_embedded_artwork(),
            )
        await context.player.skip(requester=context.author)

    async def resume(self, context: PyLavContext):
        """Resume the player"""
        if isinstance(context, discord.Interaction):
            context = await self.bot.get_context(context)
        if context.interaction and not context.interaction.response.is_done():
            await context.defer(ephemeral=True)
        if not context.player:
            await context.send(
                embed=await context.pylav.construct_embed(
                    description=_("I am not currently playing anything on this server."), messageable=context
                ),
                ephemeral=True,
            )
            return
        if not context.player.paused:
            description = _("The player already resumed")
            await context.send(
                embed=await context.pylav.construct_embed(description=description, messageable=context),
                ephemeral=True,
            )
            return

        await context.player.set_pause(False, context.author)
        await context.send(
            embed=await context.pylav.construct_embed(
                description=_("I have now resumed the player as requested."), messageable=context
            ),
            ephemeral=True,
        )

    async def pause(self, context: PyLavContext):
        """Pause the player"""
        if isinstance(context, discord.Interaction):
            context = await self.bot.get_context(context)
        if context.interaction and not context.interaction.response.is_done():
            await context.defer(ephemeral=True)
        if not context.player:
            await context.send(
                embed=await context.pylav.construct_embed(
                    description=_("I am not currently playing anything on this server."), messageable=context
                ),
                ephemeral=True,
            )
            return
        if context.player.paused:
            description = _("The player is already paused.")
            await context.send(
                embed=await context.pylav.construct_embed(description=description, messageable=context),
                ephemeral=True,
            )
            return

        await context.player.set_pause(True, requester=context.author)
        await context.send(
            embed=await context.pylav.construct_embed(
                description=_("I have now paused the player as requested."), messageable=context
            ),
            ephemeral=True,
        )

    async def stop(self, context: PyLavContext):
        """Stops the player and clears the queue."""
        if isinstance(context, discord.Interaction):
            context = await self.bot.get_context(context)
        if context.interaction and not context.interaction.response.is_done():
            await context.defer(ephemeral=True)
        if not context.player or not context.player.current:
            await context.send(
                embed=await context.pylav.construct_embed(
                    description=_("I am not currently playing anything on this server."), messageable=context
                ),
                ephemeral=True,
            )
            return
        await context.player.stop(context.author)
        await context.send(
            embed=await context.pylav.construct_embed(
                description=_("I have stopped the playback and cleared the queue as requested."), messageable=context
            ),
            ephemeral=True,
        )

    async def previous(self, context: PyLavContext):
        """Play previously played tracks.

        A history of the last 100 tracks played is kept.
        """
        if isinstance(context, discord.Interaction):
            context = await self.bot.get_context(context)
        if context.interaction and not context.interaction.response.is_done():
            await context.defer(ephemeral=True)

        if not context.player:
            await context.send(
                embed=await context.pylav.construct_embed(
                    description=_("I am not currently playing anything on this server."), messageable=context
                ),
                ephemeral=True,
            )
            return

        if context.player.history.empty():
            await context.send(
                embed=await context.pylav.construct_embed(
                    description=_("The history of tracks is currently empty."), messageable=context
                ),
                ephemeral=True,
            )
            return
        await context.player.previous(requester=context.author)
        await context.send(
            embed=await context.pylav.construct_embed(
                description=_("Playing previous track: {track_name_variable_do_not_translate}.").format(
                    track_name_variable_do_not_translate=await context.player.current.get_track_display_name(
                        with_url=True
                    )
                ),
                thumbnail=await context.player.current.artworkUrl(),
                messageable=context,
            ),
            ephemeral=True,
            file=await context.player.current.get_embedded_artwork(),
        )

    async def prepare_channel(self, channel: discord.TextChannel | discord.Thread | discord.VoiceChannel):
        permissions = channel.permissions_for(channel.guild.me)
        if not all(
            [
                permissions.read_messages,
                permissions.manage_channels,
                permissions.manage_roles,
                permissions.send_messages,
                permissions.embed_links,
                permissions.add_reactions,
                permissions.external_emojis,
                permissions.manage_messages,
                permissions.manage_threads,
                permissions.read_message_history,
            ]
        ):
            await channel.send(
                embed=await self.pylav.construct_embed(
                    title=_("I do not have the required permissions in this channel."),
                    description=_(
                        "Please make sure I have the following permissions: "
                        "`View Channel`, `Manage Channel`, `Manage Permissions`, "
                        "`Send Messages`, `Embed Links`, `Add Reactions`, "
                        "`Use External Emojis`, `Manage Messages`, `Manage Threads` and `Read Message History`. "
                        "Once you give me these permissions, set the controller channel "
                        "again under Audio on the dashboard."
                    ),
                    messageable=channel,
                )
            )
            return
        existing_view_id = await self._controller_config.guild(channel.guild).persistent_view_message_id()
        if existing_view_id:
            with contextlib.suppress(discord.NotFound):
                existing_view = await channel.fetch_message(existing_view_id)
                self._view_cache[channel.id] = PersistentControllerView(
                    cog=self, channel=channel, message=existing_view
                )
                await self._view_cache[channel.id].set_permissions()
                await self._view_cache[channel.id].prepare()
                if channel.guild.id in self._list_for_search_cache and self._list_for_search_cache[channel.guild.id]:
                    self._view_cache[channel.id].enable_show_help()
                self.bot.add_view(self._view_cache[channel.id], message_id=existing_view_id)
                if self._use_slow_mode_cache[channel.guild.id]:
                    await self._view_cache[channel.id].enable_slow_mode()
                else:
                    await self._view_cache[channel.id].disable_slow_mode()
                return
        self._view_cache[channel.id] = PersistentControllerView(cog=self, channel=channel)
        await self._view_cache[channel.id].prepare()
        await self._view_cache[channel.id].set_permissions()
        if channel.guild.id in self._list_for_search_cache and self._list_for_search_cache[channel.guild.id]:
            self._view_cache[channel.id].enable_show_help()
        message = await self.send_channel_view(channel)
        await self._controller_config.guild(channel.guild).persistent_view_message_id.set(message.id)
        self._view_cache[channel.id].set_message(message)
        self.bot.add_view(self._view_cache[channel.id], message_id=message.id)
        if self._use_slow_mode_cache[channel.guild.id]:
            await self._view_cache[channel.id].enable_slow_mode()
        else:
            await self._view_cache[channel.id].disable_slow_mode()

    async def send_channel_view(
        self, channel: discord.TextChannel | discord.Thread | discord.VoiceChannel
    ) -> discord.Message:
        return await channel.send(
            view=self._view_cache[channel.id], **(await self._view_cache[channel.id].get_now_playing_embed())
        )

    async def on_message(self, message: discord.Message):
        guild = message.guild
        if guild is None:
            return

        if message.author.bot:
            return

        if guild.id not in self._channel_cache:
            return

        if message.channel.id != self._channel_cache[guild.id]:
            return

        channel = self.bot.get_channel(self._channel_cache[guild.id])
        if channel is None:
            return

        if channel.id not in self._view_cache:
            return

        if await self.bot.cog_disabled_in_guild(self, guild):
            return

        if (await self.bot.get_context(message)).valid:
            await self.__add_failed_message_to_delete(message)
            return

        async with self.__lock[message.guild.id]:
            player = await self._view_cache[channel.id].get_player(message)
        if player is None:
            await self.add_failure_reaction(message)
            return

        await self.process_potential_query(message, player)

    async def on_message_without_command(self, message: discord.Message):
        guild = message.guild
        if guild is None:
            return

        if message.author.bot:
            return

        if guild.id not in self._channel_cache:
            return

        if message.channel.id != self._channel_cache[guild.id]:
            return

        channel = self.bot.get_channel(self._channel_cache[guild.id])
        if channel is None:
            return

        if channel.id not in self._view_cache:
            return

        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        async with self.__lock[message.guild.id]:
            player = await self._view_cache[channel.id].get_player(message)
        if player is None:
            await self.add_failure_reaction(message)
            return

        await self.process_potential_query(message, player)

    async def process_potential_query(self, message: discord.Message, player: Player):
        if (message.guild.id not in self._list_for_command_cache) or (
            self._list_for_command_cache[message.guild.id] is False
        ):
            await self.add_failure_reaction(message)
            return

        if message.guild.id in self._enable_antispam_cache and self._enable_antispam_cache[message.guild.id]:
            if self.antispam[message.guild.id][message.author.id].spammy:
                await self.add_failure_reaction(message)
                return
            self.antispam[message.guild.id][message.author.id].stamp()

        query = await Query.from_string(
            message.clean_content, dont_search=not self._list_for_search_cache[message.guild.id]
        )
        if query.invalid:
            await self.add_failure_reaction(message)
            return
        if query.is_search and not self._list_for_search_cache[message.guild.id]:
            await self.add_failure_reaction(message)
            return

        successful, count, failed = await self.pylav.get_all_tracks_for_queries(
            query, player=player, requester=message.author
        )

        if successful:
            if query.is_search:
                successful = [successful[0]]
            await player.bulk_add(tracks_and_queries=successful, requester=message.author.id)
            if (not player.is_active) and player.queue.size() > 0:
                await player.next(requester=message.author)
            await self.add_success_reaction(message)
        else:
            await self.add_failure_reaction(message)

    async def _controller_delete_user_data(
        self,
        *,
        requester: Literal["discord_deleted_user", "owner", "user", "user_strict"],
        user_id: int,
    ) -> None:
        await self._controller_config.user_from_id(user_id).clear()

    @commands.Cog.listener("on_pylav_track_start_event")
    async def _controller_on_pylav_track_start_event(self, event: TrackStartEvent) -> None:
        await self.process_event(event)

    @commands.Cog.listener("on_pylav_queue_end_event")
    async def _controller_on_pylav_queue_end_event(self, event: QueueEndEvent) -> None:
        await self.process_event(event)

    @commands.Cog.listener("on_pylav_player_stopped_event")
    async def _controller_on_pylav_player_stopped_event(self, event: PlayerStoppedEvent) -> None:
        await self.process_event(event)

    @commands.Cog.listener("on_pylav_player_paused_event")
    async def _controller_on_pylav_player_paused_event(self, event: PlayerPausedEvent) -> None:
        await self.process_event(event)

    @commands.Cog.listener("on_pylav_player_resumed_event")
    async def _controller_on_pylav_player_resumed_event(self, event: PlayerResumedEvent) -> None:
        await self.process_event(event)

    async def process_event(
        self, event: TrackStartEvent | QueueEndEvent | PlayerStoppedEvent | PlayerPausedEvent | PlayerResumedEvent
    ) -> None:
        await asyncio.sleep(1)
        guild = event.player.guild
        if guild.id not in self._channel_cache:
            return
        channel = self.bot.get_channel(self._channel_cache[guild.id])
        if channel is None:
            return
        if channel.id not in self._view_cache:
            return
        if await self.bot.cog_disabled_in_guild(self, channel.guild):
            return
        await self._view_cache[channel.id].update_view()

    async def __add_failed_message_to_delete(self, message: discord.Message) -> None:
        async with self.__lock[message.guild.id]:
            self.__failed_messages_to_delete[message.guild.id].add(message)

    async def __copy_failed_message_to_delete(self, guild_id: int) -> set[discord.Message]:
        async with self.__lock[guild_id]:
            now = get_now_utc()
            r = {m for m in self.__failed_messages_to_delete[guild_id] if m.created_at + timedelta(seconds=10) < now}
            remaining = self.__failed_messages_to_delete[guild_id] - r
            self.__failed_messages_to_delete[guild_id] = remaining
            return r

    async def delete_failed_messages(self) -> None:
        await self.__ready.wait()
        for guild_id in self.__failed_messages_to_delete:
            if self.bot.get_guild(guild_id) is None:
                self.__failed_messages_to_delete.pop(guild_id, None)
                continue
            channel = self.bot.get_channel(self._channel_cache[guild_id])
            if channel is None:
                return
            messages = list(await self.__copy_failed_message_to_delete(guild_id))
            for chunk in [messages[i : i + 100] for i in range(0, len(messages), 100)]:
                await channel.delete_messages(chunk, reason=_("PyLavController: Deleting failed messages is channel"))

    async def add_failure_reaction(self, message: discord.Message) -> None:
        await self.__add_failed_message_to_delete(message)
        with contextlib.suppress(discord.HTTPException):
            await message.add_reaction("\N{CROSS MARK}")

    async def __add_successful_message_to_delete(self, message: discord.Message) -> None:
        async with self.__lock[message.guild.id]:
            self.__success_messages_to_delete[message.guild.id].add(message)

    async def __copy_success_messages_to_delete(self, guild_id: int) -> set[discord.Message]:
        async with self.__lock[guild_id]:
            now = get_now_utc()
            r = {m for m in self.__success_messages_to_delete[guild_id] if m.created_at + timedelta(seconds=30) < now}
            remaining = self.__success_messages_to_delete[guild_id] - r
            self.__success_messages_to_delete[guild_id] = remaining
            return r

    async def delete_successful_messages(self) -> None:
        await self.__ready.wait()
        for guild_id in self.__success_messages_to_delete:
            if self.bot.get_guild(guild_id) is None:
                self.__success_messages_to_delete.pop(guild_id, None)
                continue
            channel = self.bot.get_channel(self._channel_cache[guild_id])
            if channel is None:
                return
            messages = list(await self.__copy_success_messages_to_delete(guild_id))
            for chunk in [messages[i : i + 100] for i in range(0, len(messages), 100)]:
                await channel.delete_messages(
                    chunk, reason=_("PyLavController: Deleting successful messages is channel")
                )

    async def add_success_reaction(self, message: discord.Message) -> None:
        await self.__add_successful_message_to_delete(message)
        with contextlib.suppress(discord.HTTPException):
            await message.add_reaction("\N{WHITE HEAVY CHECK MARK}")
