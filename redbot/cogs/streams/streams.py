import discord
from redbot.core.bot import Red
from redbot.core import app_commands, commands, Config
from redbot.core.i18n import cog_i18n, Translator, set_contextual_locales_from_guild
from redbot.core.utils._internal_utils import send_to_owners_with_prefix_replaced
from redbot.core.utils.chat_formatting import escape, inline

from .streamtypes import (
    KickStream,
    PicartoStream,
    Stream,
    TwitchStream,
    YoutubeStream,
)
from .errors import (
    APIError,
    InvalidKickCredentials,
    InvalidTwitchCredentials,
    InvalidYoutubeCredentials,
    OfflineStream,
    StreamNotFound,
    StreamsError,
    YoutubeQuotaExceeded,
)
from . import streamtypes as _streamtypes

import re
import logging
import asyncio
import aiohttp
import contextlib
from datetime import datetime
from typing import Optional, List, Tuple, Union, Dict

MAX_RETRY_COUNT = 10

SET_CREDENTIALS = (
    "The {service} credentials are missing or invalid. The bot owner can set "
    "them from the Streams page of the dashboard, which explains how to get them."
)

from .dashboard_integration import DashboardIntegration

_ = Translator("Streams", __file__)
log = logging.getLogger("red.core.cogs.Streams")


@cog_i18n(_)
class Streams(DashboardIntegration, commands.Cog):
    """Check whether a Twitch, YouTube, Picarto or Kick stream is live.

    Who gets announced where, the alert messages, the mentions and the
    API credentials all live on this cog's page.
    """

    stream_lookup = app_commands.Group(
        name="stream",
        description="Check whether a channel is live right now.",
        guild_only=True,
        extras={"red_force_enable": True},
    )

    global_defaults = {
        "refresh_timer": 300,
        "tokens": {},
        "streams": [],
        "notified_owner_missing_twitch_secret": False,
        "notified_owner_missing_kick_secret": False,
    }

    guild_defaults = {
        "autodelete": False,
        "mention_everyone": False,
        "mention_here": False,
        "live_message_mention": False,
        "live_message_nomention": False,
        "ignore_reruns": False,
        "ignore_schedule": False,
        "use_buttons": False,
    }

    role_defaults = {"mention": False}

    def __init__(self, bot: Red):
        super().__init__()
        self.config: Config = Config.get_conf(self, 26262626)
        self.ttv_bearer_cache: dict = {}
        self.kick_bearer_cache: dict = {}
        self.config.register_global(**self.global_defaults)
        self.config.register_guild(**self.guild_defaults)
        self.config.register_role(**self.role_defaults)

        self.bot: Red = bot

        self.streams: List[Stream] = []
        self.task: Optional[asyncio.Task] = None

        self.yt_cid_pattern = re.compile("^UC[-_A-Za-z0-9]{21}[AQgw]$")

    async def red_delete_data_for_user(self, **kwargs):
        """Nothing to delete"""
        return

    def check_name_or_id(self, data: str) -> bool:
        matched = self.yt_cid_pattern.fullmatch(data)
        if matched is None:
            return True
        return False

    async def cog_load(self) -> None:
        """Should be called straight after cog instantiation."""
        try:
            await self.move_api_keys()
            await self.get_twitch_bearer_token()
            self.streams = await self.load_streams()
            self.task = asyncio.create_task(self._stream_alerts())
        except Exception as error:
            log.exception("Failed to initialize Streams cog:", exc_info=error)

    @commands.Cog.listener()
    async def on_red_api_tokens_update(self, service_name, api_tokens):
        if service_name == "twitch":
            await self.get_twitch_bearer_token(api_tokens)
        elif service_name == "kick":
            await self.get_kick_bearer_token(api_tokens)

    async def move_api_keys(self) -> None:
        """Move the API keys from cog stored config to core bot config if they exist."""
        tokens = await self.config.tokens()
        youtube = await self.bot.get_shared_api_tokens("youtube")
        twitch = await self.bot.get_shared_api_tokens("twitch")
        for token_type, token in tokens.items():
            if token_type == "YoutubeStream" and "api_key" not in youtube:
                await self.bot.set_shared_api_tokens("youtube", api_key=token)
            if token_type == "TwitchStream" and "client_id" not in twitch:
                # Don't need to check Community since they're set the same
                await self.bot.set_shared_api_tokens("twitch", client_id=token)
        await self.config.tokens.clear()

    async def _notify_owner_about_missing_twitch_secret(self) -> None:
        message = _(
            "You need a client secret key if you want to use the Twitch API on this cog.\n"
            "Follow these steps:\n"
            "1. Go to this page: {link}.\n"
            '2. Click "Manage" on your application.\n'
            '3. Click on "New secret".\n'
            "4. Copy your client ID and your client secret into:\n"
            "{command}"
            "\n\n"
            "Note: These tokens are sensitive and should only be used in a private channel "
            "or in DM with the bot."
        ).format(
            link="https://dev.twitch.tv/console/apps",
            command=inline(
                "[p]set api twitch client_id {} client_secret {}".format(
                    _("<your_client_id_here>"), _("<your_client_secret_here>")
                )
            ),
        )
        await send_to_owners_with_prefix_replaced(self.bot, message)
        await self.config.notified_owner_missing_twitch_secret.set(True)

    async def _notify_owner_about_missing_kick_secret(self) -> None:
        message = _(
            "You need a client secret key if you want to use the Kick API on this cog.\n"
            "Follow these steps:\n"
            "1. Go to this page: {link}.\n"
            '2. Click "Manage" on your application.\n'
            "3. Copy your client ID and your client secret into:\n"
            "{command}"
            "\n\n"
            "Note: These tokens are sensitive and should only be used in a private channel "
            "or in DM with the bot."
        ).format(
            link="https://kick.com/settings/developer",
            command=inline(
                "[p]set api kick client_id {} client_secret {}".format(
                    _("<your_client_id_here>"), _("<your_client_secret_here>")
                )
            ),
        )
        await send_to_owners_with_prefix_replaced(self.bot, message)
        await self.config.notified_owner_missing_kick_secret.set(True)

    async def get_twitch_bearer_token(self, api_tokens: Optional[Dict] = None) -> None:
        tokens = (
            await self.bot.get_shared_api_tokens("twitch") if api_tokens is None else api_tokens
        )
        if tokens.get("client_id"):
            notified_owner_missing_twitch_secret = (
                await self.config.notified_owner_missing_twitch_secret()
            )
            try:
                tokens["client_secret"]
                if notified_owner_missing_twitch_secret is True:
                    await self.config.notified_owner_missing_twitch_secret.set(False)
            except KeyError:
                if notified_owner_missing_twitch_secret is False:
                    asyncio.create_task(self._notify_owner_about_missing_twitch_secret())
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://id.twitch.tv/oauth2/token",
                params={
                    "client_id": tokens.get("client_id", ""),
                    "client_secret": tokens.get("client_secret", ""),
                    "grant_type": "client_credentials",
                },
            ) as req:
                try:
                    data = await req.json()
                except aiohttp.ContentTypeError:
                    data = {}

                if req.status == 200:
                    pass
                elif req.status == 400 and data.get("message") == "invalid client":
                    log.error(
                        "Twitch API request failed authentication: set Client ID is invalid."
                    )
                elif req.status == 403 and data.get("message") == "invalid client secret":
                    log.error(
                        "Twitch API request failed authentication: set Client Secret is invalid."
                    )
                elif "message" in data:
                    log.error(
                        "Twitch OAuth2 API request failed with status code %s"
                        " and error message: %s",
                        req.status,
                        data["message"],
                    )
                else:
                    log.error("Twitch OAuth2 API request failed with status code %s", req.status)

                if req.status != 200:
                    return

        self.ttv_bearer_cache = data
        self.ttv_bearer_cache["expires_at"] = datetime.now().timestamp() + data.get("expires_in")

    async def maybe_renew_twitch_bearer_token(self) -> None:
        if (
            self.ttv_bearer_cache
            and self.ttv_bearer_cache["expires_at"] - datetime.now().timestamp() <= 60
        ):
            await self.get_twitch_bearer_token()

    async def get_kick_bearer_token(self, api_tokens: Optional[Dict] = None) -> None:
        tokens = await self.bot.get_shared_api_tokens("kick") if api_tokens is None else api_tokens
        if tokens.get("client_id"):
            notified_owner_missing_kick_secret = (
                await self.config.notified_owner_missing_kick_secret()
            )
            try:
                tokens["client_secret"]
                if notified_owner_missing_kick_secret is True:
                    await self.config.notified_owner_missing_kick_secret.set(False)
            except KeyError:
                if notified_owner_missing_kick_secret is False:
                    asyncio.create_task(self._notify_owner_about_missing_kick_secret())
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://id.kick.com/oauth/token",
                params={
                    "client_id": tokens.get("client_id", ""),
                    "client_secret": tokens.get("client_secret", ""),
                    "grant_type": "client_credentials",
                },
            ) as req:
                try:
                    data = await req.json()
                except aiohttp.ContentTypeError:
                    data = {}

                if req.status == 200:
                    pass
                elif req.status == 401 and data.get("error") == "invalid_client":
                    log.error("Kick API request failed authentication: set Client ID is invalid.")
                elif "error" in data:
                    log.error(
                        "Kick OAuth2 API request failed with status code %s and error message: %s",
                        req.status,
                        data["error"],
                    )
                else:
                    log.error("Kick OAuth2 API request failed with status code %s", req.status)

                if req.status != 200:
                    return

        self.kick_bearer_cache = data
        self.kick_bearer_cache["expires_at"] = datetime.now().timestamp() + data.get("expires_in")

    async def maybe_renew_kick_token(self) -> None:
        if (
            self.kick_bearer_cache
            and self.kick_bearer_cache["expires_at"] - datetime.now().timestamp() <= 60
        ):
            await self.get_kick_bearer_token()

    @stream_lookup.command(name="twitch", description="Check if a Twitch channel is live.")
    @app_commands.describe(channel_name="The Twitch channel name.")
    async def twitchstream(self, interaction: discord.Interaction, channel_name: str):
        """Check if a Twitch channel is live."""
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        await self.maybe_renew_twitch_bearer_token()
        token = (await self.bot.get_shared_api_tokens("twitch")).get("client_id")
        stream = TwitchStream(
            _bot=self.bot,
            name=channel_name,
            token=token,
            bearer=self.ttv_bearer_cache.get("access_token", None),
        )
        await self.check_online(ctx, stream)

    @stream_lookup.command(
        name="youtube", description="Check if a YouTube channel is live."
    )
    @app_commands.describe(
        channel_id_or_name="The YouTube channel name, or its UC... channel ID."
    )
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.guild_id)
    async def youtubestream(
        self, interaction: discord.Interaction, channel_id_or_name: str
    ):
        """Check if a YouTube channel is live.

        Rate limited per server: each lookup spends YouTube API quota,
        which is shared by every server this bot is in.
        """
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        apikey = await self.bot.get_shared_api_tokens("youtube")
        is_name = self.check_name_or_id(channel_id_or_name)
        if is_name:
            stream = YoutubeStream(
                _bot=self.bot, name=channel_id_or_name, token=apikey, config=self.config
            )
        else:
            stream = YoutubeStream(
                _bot=self.bot, id=channel_id_or_name, token=apikey, config=self.config
            )
        await self.check_online(ctx, stream)

    @stream_lookup.command(
        name="picarto", description="Check if a Picarto channel is live."
    )
    @app_commands.describe(channel_name="The Picarto channel name.")
    async def picarto(self, interaction: discord.Interaction, channel_name: str):
        """Check if a Picarto channel is live."""
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        stream = PicartoStream(_bot=self.bot, name=channel_name)
        await self.check_online(ctx, stream)

    @stream_lookup.command(name="kick", description="Check if a Kick channel is live.")
    @app_commands.describe(channel_name="The Kick channel name.")
    async def kickstream(self, interaction: discord.Interaction, channel_name: str):
        """Check if a Kick channel is live."""
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.typing()
        await self.maybe_renew_kick_token()
        token = self.kick_bearer_cache.get("access_token")
        stream = _streamtypes.KickStream(_bot=self.bot, name=channel_name, token=token)
        await self.check_online(ctx, stream)

    async def check_online(
        self,
        ctx: commands.Context,
        stream: Union[PicartoStream, YoutubeStream, TwitchStream, KickStream],
    ):
        try:
            info = await stream.is_online()
        except OfflineStream:
            await ctx.send(_("That user is offline."))
        except StreamNotFound:
            await ctx.send(_("That user doesn't seem to exist."))
        # The [p]streamset *token commands that used to walk the owner
        # through this are gone; the Streams page carries those steps now.
        except InvalidTwitchCredentials:
            await ctx.send(_(SET_CREDENTIALS).format(service="Twitch"))
        except InvalidYoutubeCredentials:
            await ctx.send(_(SET_CREDENTIALS).format(service="YouTube"))
        except InvalidKickCredentials:
            await ctx.send(_(SET_CREDENTIALS).format(service="Kick"))
        except YoutubeQuotaExceeded:
            await ctx.send(
                _(
                    "YouTube quota has been exceeded."
                    " Try again later or contact the owner if this continues."
                )
            )
        except APIError as e:
            log.error(
                "Something went wrong whilst trying to contact the stream service's API.\n"
                "Raw response data:\n%r",
                e,
            )
            await ctx.send(
                _("Something went wrong whilst trying to contact the stream service's API.")
            )
        else:
            if isinstance(info, tuple):
                embed, is_rerun = info
                ignore_reruns = await self.config.guild(ctx.channel.guild).ignore_reruns()
                if ignore_reruns and is_rerun:
                    await ctx.send(_("That user is offline."))
                    return
            else:
                embed = info

            use_buttons: bool = await self.config.guild(ctx.channel.guild).use_buttons()
            view = None
            if use_buttons:
                stream_url = embed.url
                view = discord.ui.View()
                view.add_item(
                    discord.ui.Button(
                        label=_("Watch the stream"), style=discord.ButtonStyle.link, url=stream_url
                    )
                )
            await ctx.send(embed=embed, view=view)

    def get_stream(self, _class, name):
        for stream in self.streams:
            # if isinstance(stream, _class) and stream.name == name:
            #    return stream
            # Reloading this cog causes an issue with this check ^
            # isinstance will always return False
            # As a workaround, we'll compare the class' name instead.
            # Good enough.
            if _class.__name__ == "YoutubeStream" and stream.type == _class.__name__:
                # Because name could be a username or a channel id
                if self.check_name_or_id(name) and stream.name.lower() == name.lower():
                    return stream
                elif not self.check_name_or_id(name) and stream.id == name:
                    return stream
            elif stream.type == _class.__name__ and stream.name.lower() == name.lower():
                return stream

    @staticmethod
    async def check_exists(stream):
        try:
            await stream.is_online()
        except OfflineStream:
            pass
        except StreamNotFound:
            return False
        except StreamsError:
            raise
        return True

    async def _stream_alerts(self):
        await self.bot.wait_until_ready()
        while True:
            await self.check_streams()
            await asyncio.sleep(await self.config.refresh_timer())

    async def _send_stream_alert(
        self,
        stream,
        channel: Union[discord.TextChannel, discord.VoiceChannel, discord.StageChannel],
        embed: discord.Embed,
        content: str = None,
        *,
        is_schedule: bool = False,
    ):
        use_buttons: bool = await self.config.guild(channel.guild).use_buttons()
        view = None
        if use_buttons:
            stream_url = embed.url
            view = discord.ui.View()
            view.add_item(
                discord.ui.Button(
                    label=_("Watch the stream"), style=discord.ButtonStyle.link, url=stream_url
                )
            )
        m = await channel.send(
            content,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(roles=True, everyone=True),
            view=view,
        )
        message_data = {"guild": m.guild.id, "channel": m.channel.id, "message": m.id}
        if is_schedule:
            message_data["is_schedule"] = True
        stream.messages.append(message_data)

    def _has_stream_alert_perms(self, channel: discord.TextChannel) -> bool:
        perms = channel.permissions_for(channel.guild.me)
        return all((perms.send_messages, perms.embed_links))

    async def check_streams(self):
        to_remove = []
        for stream in self.streams:
            try:
                try:
                    is_rerun, is_schedule = False, False
                    if stream.__class__.__name__ == "TwitchStream":
                        await self.maybe_renew_twitch_bearer_token()
                        embed, is_rerun = await stream.is_online()

                    elif stream.__class__.__name__ == "YoutubeStream":
                        embed, is_schedule = await stream.is_online()

                    elif stream.__class__.__name__ == "KickStream":
                        await self.maybe_renew_kick_token()
                        embed = await stream.is_online()

                    else:
                        embed = await stream.is_online()
                except StreamNotFound:
                    if stream.retry_count > MAX_RETRY_COUNT:
                        log.info("Stream with name %s no longer exists. Removing...", stream.name)
                        to_remove.append(stream)
                    else:
                        log.info(
                            "Stream with name %s seems to not exist, will retry later", stream.name
                        )
                        stream.retry_count += 1
                    continue
                except OfflineStream:
                    if not stream.messages:
                        continue

                    for msg_data in stream.iter_messages():
                        partial_msg = msg_data["partial_message"]
                        if partial_msg is None:
                            continue
                        if await self.bot.cog_disabled_in_guild(self, partial_msg.guild):
                            continue
                        if not await self.config.guild(partial_msg.guild).autodelete():
                            continue

                        with contextlib.suppress(discord.NotFound):
                            await partial_msg.delete()

                    stream.messages.clear()
                    await self.save_streams()
                except APIError as e:
                    log.error(
                        "Something went wrong whilst trying to contact the stream service's API.\n"
                        "Raw response data:\n%r",
                        e,
                    )
                    continue
                else:
                    if stream.messages:
                        continue
                    for channel_id in stream.channels:
                        channel = self.bot.get_channel(channel_id)
                        if not channel:
                            continue
                        if await self.bot.cog_disabled_in_guild(self, channel.guild):
                            continue

                        guild_data = await self.config.guild(channel.guild).all()
                        if guild_data["ignore_reruns"] and is_rerun:
                            continue
                        if guild_data["ignore_schedule"] and is_schedule:
                            continue
                        if is_schedule:
                            if not self._has_stream_alert_perms(channel):
                                continue
                            # skip messages and mentions
                            await self._send_stream_alert(stream, channel, embed, is_schedule=True)
                            await self.save_streams()
                            continue
                        await set_contextual_locales_from_guild(self.bot, channel.guild)

                        mention_str, edited_roles = await self._get_mention_str(
                            channel.guild, channel, guild_data
                        )

                        if mention_str:
                            if guild_data["live_message_mention"]:
                                # Stop bad things from happening here...
                                content = guild_data["live_message_mention"]
                                content = content.replace(
                                    "{stream.name}", str(stream.name)
                                )  # Backwards compatibility
                                content = content.replace(
                                    "{stream.display_name}", str(stream.display_name)
                                )
                                content = content.replace("{stream}", str(stream.name))
                                content = content.replace("{mention}", mention_str)
                            else:
                                content = _("{mention}, {display_name} is live!").format(
                                    mention=mention_str,
                                    display_name=escape(
                                        str(stream.display_name),
                                        mass_mentions=True,
                                        formatting=True,
                                    ),
                                )
                        else:
                            if guild_data["live_message_nomention"]:
                                # Stop bad things from happening here...
                                content = guild_data["live_message_nomention"]
                                content = content.replace(
                                    "{stream.name}", str(stream.name)
                                )  # Backwards compatibility
                                content = content.replace(
                                    "{stream.display_name}", str(stream.display_name)
                                )
                                content = content.replace("{stream}", str(stream.name))
                            else:
                                content = _("{display_name} is live!").format(
                                    display_name=escape(
                                        str(stream.display_name),
                                        mass_mentions=True,
                                        formatting=True,
                                    )
                                )

                        if self._has_stream_alert_perms(channel):
                            await self._send_stream_alert(stream, channel, embed, content)
                            if edited_roles:
                                for role in edited_roles:
                                    await role.edit(mentionable=False)
                            await self.save_streams()
            except Exception as e:
                log.error("An error has occurred with Streams. Please report it.", exc_info=e)

        if to_remove:
            for stream in to_remove:
                self.streams.remove(stream)
            await self.save_streams()

    async def _get_mention_str(
        self,
        guild: discord.Guild,
        channel: Union[discord.TextChannel, discord.VoiceChannel, discord.StageChannel],
        guild_data: dict,
    ) -> Tuple[str, List[discord.Role]]:
        """Returns a 2-tuple with the string containing the mentions, and a list of
        all roles which need to have their `mentionable` property set back to False.
        """
        mentions = []
        edited_roles = []
        if guild_data["mention_everyone"]:
            mentions.append("@everyone")
        if guild_data["mention_here"]:
            mentions.append("@here")
        can_manage_roles = guild.me.guild_permissions.manage_roles
        can_mention_everyone = channel.permissions_for(guild.me).mention_everyone
        for role in guild.roles:
            if await self.config.role(role).mention():
                if not can_mention_everyone and can_manage_roles and not role.mentionable:
                    try:
                        await role.edit(mentionable=True)
                    except discord.Forbidden:
                        # Might still be unable to edit role based on hierarchy
                        pass
                    else:
                        edited_roles.append(role)
                mentions.append(role.mention)
        return " ".join(mentions), edited_roles

    async def filter_streams(
        self,
        streams: list,
        channel: Union[discord.TextChannel, discord.VoiceChannel, discord.StageChannel],
    ) -> list:
        filtered = []
        for stream in streams:
            tw_id = str(stream["channel"]["_id"])
            for alert in self.streams:
                if isinstance(alert, TwitchStream) and alert.id == tw_id:
                    if channel.id in alert.channels:
                        break
            else:
                filtered.append(stream)
        return filtered

    async def load_streams(self):
        streams = []
        for raw_stream in await self.config.streams():
            _class = getattr(_streamtypes, raw_stream["type"], None)
            if not _class:
                continue
            token = await self.bot.get_shared_api_tokens(_class.token_name)
            if token:
                if _class.__name__ == "TwitchStream":
                    raw_stream["token"] = token.get("client_id")
                    raw_stream["bearer"] = self.ttv_bearer_cache.get("access_token", None)
                elif _class.__name__ == "KickStream":
                    raw_stream["token"] = self.kick_bearer_cache.get("access_token", None)
                else:
                    if _class.__name__ == "YoutubeStream":
                        raw_stream["config"] = self.config
                    raw_stream["token"] = token
            raw_stream["_bot"] = self.bot
            streams.append(_class(**raw_stream))

        return streams

    async def save_streams(self):
        raw_streams = []
        for stream in self.streams:
            raw_streams.append(stream.export())

        await self.config.streams.set(raw_streams)

    def cog_unload(self):
        if self.task:
            self.task.cancel()
