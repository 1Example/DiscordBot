from __future__ import annotations

import logging
import typing as t
from collections import defaultdict

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    channel_options,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
    role_options,
)

log = logging.getLogger("red.streams.dashboard")

# The steps [p]streamset twitchtoken / youtubekey / kicktoken used to print.
# Each service's own docs are linked rather than restated, since they move.
# `fields` are the key names to enter under Admin -> API Keys, which is
# where credentials are set now that `[p]set api` is gone.
CREDENTIAL_HELP = (
    {
        "key": "twitch",
        "label": "Twitch",
        "docs": "https://dev.twitch.tv/dashboard/apps",
        "steps": [
            "Open the Twitch developer console linked above.",
            "Click <b>Register Your Application</b>.",
            "Enter a name, set the OAuth Redirect URI to <code>http://localhost</code>,"
            " and pick any application category.",
            "Click <b>Register</b>, then copy the client ID and the client secret.",
        ],
        "fields": ("client_id", "client_secret"),
    },
    {
        "key": "youtube",
        "label": "YouTube",
        "docs": "https://support.google.com/googleapi/answer/6251787",
        "steps": [
            "Create a Google API project - the link above is Google's guide.",
            "Enable the YouTube Data API v3 for that project"
            " (<a href=\"https://support.google.com/googleapi/answer/6158841\""
            ' target="_blank" rel="noopener">how</a>).',
            "Create an API key"
            " (<a href=\"https://support.google.com/googleapi/answer/6158862\""
            ' target="_blank" rel="noopener">how</a>).',
        ],
        "fields": ("api_key",),
    },
    {
        "key": "kick",
        "label": "Kick",
        "docs": "https://kick.com/settings/developer",
        "steps": [
            "Open the Kick developer settings linked above.",
            "Click <b>Create new</b>.",
            "Fill in a name and description, and set the redirection URL to"
            " <code>http://localhost</code>.",
            "Click <b>Create Application</b>, then copy the client ID and client secret.",
        ],
        "fields": ("client_id", "client_secret"),
    },
)

# Platform key -> (stream class name, label, token service, hint)
PLATFORMS = {
    "twitch": ("TwitchStream", "Twitch", "twitch", "Channel name, e.g. someone"),
    "youtube": (
        "YoutubeStream",
        "YouTube",
        "youtube",
        "Channel name or UC... channel ID",
    ),
    "picarto": ("PicartoStream", "Picarto", "picarto", "Channel name"),
    "kick": ("KickStream", "Kick", "kick", "Channel name"),
}

ALERT_CHANNEL_KINDS = ("text", "voice", "stage")


class DashboardIntegration:
    """Stream alerts and settings from the dashboard.

    The only place these can be changed. ``[p]streamalert`` and
    ``[p]streamset`` are gone, so this page has to keep covering all of it:
    adding, removing and listing alerts, stopping them per channel or per
    server, the live messages with and without a mention, everyone/here/role
    mentions, auto-delete, rerun and schedule filtering, buttons, and the
    owner-only refresh timer plus how to obtain each service's credentials.

    ``/stream twitch|youtube|picarto|kick`` is all that stayed in Discord.
    """

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Streams as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Stream alerts, alert messages and API credentials.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_streams_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        staff = await is_staff(self.bot, user, member, guild)
        is_mod = staff or await self.bot.is_mod(member)
        owner = await self.bot.is_owner(user)

        notifications: list[dict] = []
        live: dict = {}
        if kwargs.get("method") == "POST":
            if not is_mod:
                return {
                    "status": 1,
                    "error_title": "Forbidden",
                    "error_message": "Only server moderators can manage stream alerts.",
                }
            notifications, live = await self._streams_handle_post(
                guild, staff, owner, kwargs
            )

        settings = await self.config.guild(guild).all()

        guild_channel_ids = {c.id for c in guild.channels}
        grouped: dict[int, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for stream in self.streams:
            for channel_id in stream.channels:
                if channel_id in guild_channel_ids:
                    grouped[channel_id][stream.type].append(stream.name)

        alerts = []
        for channel_id, by_type in grouped.items():
            channel = guild.get_channel(channel_id)
            for stream_type, names in by_type.items():
                platform = next(
                    (k for k, v in PLATFORMS.items() if v[0] == stream_type), stream_type
                )
                for name in sorted(names):
                    alerts.append(
                        {
                            "channel": getattr(channel, "name", str(channel_id)),
                            "channel_id": str(channel_id),
                            "platform": platform,
                            "platform_label": PLATFORMS.get(platform, (None, platform))[1],
                            "name": name,
                        }
                    )
        alerts.sort(key=lambda a: (a["channel"], a["platform"], a["name"].lower()))

        mention_roles = []
        for role in guild.roles:
            if role.is_default():
                continue
            if await self.config.role(role).mention():
                mention_roles.append(role.name)

        tokens = {}
        if owner:
            for service in ("twitch", "youtube", "kick"):
                stored = await self.bot.get_shared_api_tokens(service)
                tokens[service] = bool(stored)

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": STREAMS_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_mod": is_mod,
                "is_admin": staff,
                "is_owner": owner,
                "alerts": alerts,
                "platforms": [
                    {"key": key, "label": value[1], "hint": value[3]}
                    for key, value in PLATFORMS.items()
                ],
                "channel_options": channel_options(
                    guild, kinds=ALERT_CHANNEL_KINDS, require_send=True
                ),
                "role_options": role_options(guild),
                "mention_roles": mention_roles,
                "live_message_mention": settings.get("live_message_mention") or "",
                "live_message_nomention": settings.get("live_message_nomention") or "",
                "mention_everyone": bool(settings.get("mention_everyone")),
                "mention_here": bool(settings.get("mention_here")),
                "autodelete": bool(settings.get("autodelete")),
                "ignore_reruns": bool(settings.get("ignore_reruns")),
                "ignore_schedule": bool(settings.get("ignore_schedule")),
                "use_buttons": bool(settings.get("use_buttons")),
                "refresh_timer": await self.config.refresh_timer(),
                "tokens": tokens,
                "credentials": [
                    {**service, "set": tokens.get(service["key"], False)}
                    for service in CREDENTIAL_HELP
                ]
                if owner
                else [],
                "live": live,
            },
        }

    async def _streams_build(self, platform: str, name: str):
        """Build a Stream object the same way the commands do."""
        from . import streamtypes as _streamtypes

        class_name = PLATFORMS[platform][0]
        stream_class = getattr(_streamtypes, class_name)
        token = await self.bot.get_shared_api_tokens(stream_class.token_name)

        if class_name == "TwitchStream":
            await self.maybe_renew_twitch_bearer_token()
            return stream_class(
                _bot=self.bot,
                name=name,
                token=token.get("client_id"),
                bearer=self.ttv_bearer_cache.get("access_token", None),
            )
        if class_name == "KickStream":
            await self.maybe_renew_kick_token()
            return stream_class(
                _bot=self.bot, name=name, token=self.kick_bearer_cache.get("access_token")
            )
        if class_name == "YoutubeStream":
            # A UC... string is a channel ID rather than a name.
            if self.check_name_or_id(name):
                return stream_class(
                    _bot=self.bot, name=name, token=token, config=self.config
                )
            return stream_class(_bot=self.bot, id=name, token=token, config=self.config)
        return stream_class(_bot=self.bot, name=name, token=token)

    async def _streams_handle_post(
        self, guild: discord.Guild, staff: bool, owner: bool, kwargs: dict
    ) -> tuple[list[dict], dict]:
        from .errors import (
            APIError,
            InvalidKickCredentials,
            InvalidTwitchCredentials,
            InvalidYoutubeCredentials,
            OfflineStream,
            StreamNotFound,
            YoutubeQuotaExceeded,
        )

        field = form_reader(kwargs)
        action = field("action")
        platform = (field("platform") or "").lower()
        name = (field("stream_name") or "").strip()

        try:
            if action in ("add_alert", "check"):
                if platform not in PLATFORMS:
                    return [{"message": "Pick a platform.", "category": "warning"}], {}
                if not name:
                    return [{"message": "Enter a channel name.", "category": "warning"}], {}

            if action == "check":
                stream = await self._streams_build(platform, name)
                try:
                    info = await stream.is_online()
                except OfflineStream:
                    return [
                        {"message": f"{name} is offline.", "category": "info"}
                    ], {}
                except StreamNotFound:
                    return [
                        {"message": f"{name} does not seem to exist.", "category": "warning"}
                    ], {}
                embed = info[0] if isinstance(info, tuple) else info
                is_rerun = info[1] if isinstance(info, tuple) else False
                return [], {
                    "name": name,
                    "platform": PLATFORMS[platform][1],
                    "title": embed.title or "",
                    "url": embed.url or "",
                    "description": (embed.description or "")[:400],
                    "thumbnail": embed.thumbnail.url if embed.thumbnail else "",
                    "image": embed.image.url if embed.image else "",
                    "rerun": bool(is_rerun),
                    "fields": [
                        {"name": f.name, "value": f.value} for f in embed.fields[:6]
                    ],
                }

            if action == "add_alert":
                channel = guild.get_channel(field.integer("channel_id", 0) or 0)
                if channel is None:
                    return [{"message": "Pick a Discord channel.", "category": "warning"}], {}
                if isinstance(channel, discord.Thread):
                    return [
                        {"message": "Stream alerts cannot be set up in threads.",
                         "category": "warning"}
                    ], {}

                from . import streamtypes as _streamtypes

                stream_class = getattr(_streamtypes, PLATFORMS[platform][0])
                stream = self.get_stream(stream_class, name)
                if stream is None:
                    stream = await self._streams_build(platform, name)
                    if not await self.check_exists(stream):
                        return [
                            {"message": f"{name} does not seem to exist.",
                             "category": "warning"}
                        ], {}

                if channel.id in stream.channels:
                    return [
                        {
                            "message": f"{stream.name} already alerts in #{channel.name}.",
                            "category": "info",
                        }
                    ], {}
                stream.channels.append(channel.id)
                if stream not in self.streams:
                    self.streams.append(stream)
                await self.save_streams()
                return [
                    {
                        "message": f"I will announce {stream.name} in #{channel.name}.",
                        "category": "success",
                    }
                ], {}

            if action == "remove_alert":
                channel_id = field.integer("channel_id", 0) or 0
                wanted_type = PLATFORMS.get(platform, (platform,))[0]
                for stream in self.streams.copy():
                    if stream.type != wanted_type or stream.name.lower() != name.lower():
                        continue
                    if channel_id in stream.channels:
                        stream.channels.remove(channel_id)
                    if not stream.channels:
                        self.streams.remove(stream)
                    await self.save_streams()
                    return [
                        {"message": f"Alert for {name} removed.", "category": "success"}
                    ], {}
                return [{"message": "That alert no longer exists.", "category": "info"}], {}

            if action in ("stop_channel", "stop_guild"):
                guild_channel_ids = {c.id for c in guild.channels}
                targets = (
                    guild_channel_ids
                    if action == "stop_guild"
                    else {field.integer("channel_id", 0) or 0}
                )
                removed = 0
                for stream in self.streams.copy():
                    for channel_id in list(stream.channels):
                        if channel_id in targets:
                            stream.channels.remove(channel_id)
                            removed += 1
                    if not stream.channels:
                        self.streams.remove(stream)
                await self.save_streams()
                where = (
                    "this server" if action == "stop_guild" else "that channel"
                )
                return [
                    {"message": f"{removed} alert(s) removed from {where}.",
                     "category": "success"}
                ], {}

            if action == "save_messages":
                await self.config.guild(guild).live_message_mention.set(
                    (field("live_message_mention") or "").strip()
                )
                await self.config.guild(guild).live_message_nomention.set(
                    (field("live_message_nomention") or "").strip()
                )
                return [{"message": "Alert messages saved.", "category": "success"}], {}

            if action == "save_behaviour":
                await self.config.guild(guild).autodelete.set(field.checked("autodelete"))
                await self.config.guild(guild).ignore_reruns.set(
                    field.checked("ignore_reruns")
                )
                await self.config.guild(guild).ignore_schedule.set(
                    field.checked("ignore_schedule")
                )
                await self.config.guild(guild).use_buttons.set(field.checked("use_buttons"))
                return [{"message": "Alert behaviour saved.", "category": "success"}], {}

            if action == "save_mentions":
                if not staff:
                    return [
                        {"message": "Only server administrators can change mentions.",
                         "category": "danger"}
                    ], {}
                await self.config.guild(guild).mention_everyone.set(
                    field.checked("mention_everyone")
                )
                await self.config.guild(guild).mention_here.set(field.checked("mention_here"))
                wanted = set(field.many("mention_roles"))
                changed = 0
                for role in guild.roles:
                    if role.is_default():
                        continue
                    want = str(role.id) in wanted
                    if await self.config.role(role).mention() != want:
                        await self.config.role(role).mention.set(want)
                        changed += 1
                return [
                    {"message": f"Mention settings saved ({changed} role change(s)).",
                     "category": "success"}
                ], {}

            if action == "save_refresh":
                if not owner:
                    return [
                        {"message": "Only the bot owner can change the refresh timer.",
                         "category": "danger"}
                    ], {}
                seconds = field.integer("refresh_timer", 0) or 0
                if seconds < 60:
                    return [
                        {"message": "The refresh timer cannot be under 60 seconds.",
                         "category": "warning"}
                    ], {}
                await self.config.refresh_timer.set(seconds)
                return [
                    {"message": f"Streams are now checked every {seconds} seconds.",
                     "category": "success"}
                ], {}
        except InvalidTwitchCredentials:
            return [
                {"message": "The Twitch token is missing or invalid.", "category": "danger"}
            ], {}
        except InvalidYoutubeCredentials:
            return [
                {"message": "The YouTube API key is missing or invalid.",
                 "category": "danger"}
            ], {}
        except InvalidKickCredentials:
            return [
                {"message": "The Kick token is missing or invalid.", "category": "danger"}
            ], {}
        except YoutubeQuotaExceeded:
            return [
                {"message": "The YouTube quota has been exceeded; try again later.",
                 "category": "danger"}
            ], {}
        except APIError:
            log.exception("Streams dashboard action %r hit an API error", action)
            return [
                {"message": "The stream service's API could not be reached.",
                 "category": "danger"}
            ], {}
        except Exception as exc:  # noqa: BLE001
            log.exception("Streams dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}], {}

        return [{"message": f"Unknown action: {action}", "category": "warning"}], {}


STREAMS_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-video-camera"></i> Stream alerts in {{ guild_name }}</h4>
    <p>Announce when a Twitch, YouTube, Picarto or Kick channel goes live.</p>
  </div>

  {{ stats([('Alerts', alerts|length),
            ('Channels', alerts|map(attribute='channel')|unique|list|length),
            ('Mention roles', mention_roles|length),
            ('Check every', refresh_timer ~ 's')]) }}

  {% if not is_mod %}
    <div class="dz-panel">
      <p class="dz-empty">You need moderator permissions to manage stream alerts.</p>
    </div>
  {% else %}

  {% if live %}
    <div class="dz-panel">
      <h5><i class="fa fa-circle" style="color:#f04747;"></i>
          {{ live.name }} is live on {{ live.platform }}</h5>
      {% if live.rerun %}<span class="dz-tag warn">rerun</span>{% endif %}
      <div class="dz-embed" style="max-width:none;">
        {% if live.title %}
          <div class="et">
            {% if live.url %}<a href="{{ live.url }}" target="_blank" rel="noopener">{{ live.title }}</a>
            {% else %}{{ live.title }}{% endif %}
          </div>
        {% endif %}
        {% if live.description %}<div class="ed">{{ live.description }}</div>{% endif %}
        {% for f in live.fields %}
          <div class="efield"><b>{{ f.name }}</b>{{ f.value }}</div>
        {% endfor %}
      </div>
      {% if live.image %}
        <img src="{{ live.image }}" alt=""
             style="max-width:340px; border-radius:10px; margin-top:10px;" />
      {% endif %}
    </div>
  {% endif %}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-bell"></i> Add an alert</h5>
      <p class="dz-hint">The same form also checks whether a channel is live right now.</p>
      <div class="dz-grid three">
        <div>
          <label class="dz-label">Platform</label>
          <select class="dz-select" name="platform">
            {% for p in platforms %}
              <option value="{{ p.key }}">{{ p.label }}</option>
            {% endfor %}
          </select>
          <p class="dz-hint" style="margin-top:6px;">
            YouTube accepts a name or a <code>UC...</code> channel ID.
          </p>
        </div>
        <div>
          <label class="dz-label">Streamer</label>
          <input class="dz-input" type="text" name="stream_name" placeholder="channel name" />
        </div>
        <div>
          <label class="dz-label">Announce in</label>
          {{ picker('channel_id', channel_options, false, 6, 'Search channels...') }}
        </div>
      </div>
      <div class="dz-row dz-save">
        <button class="dz-btn primary" name="action" value="add_alert">
          <i class="fa fa-plus"></i> Add alert
        </button>
        <button class="dz-btn" name="action" value="check">
          <i class="fa fa-search"></i> Is it live?
        </button>
        {{ confirm('Remove all alerts here', 'stop_guild',
                   'Remove every stream alert in this server?') }}
      </div>
    </div>
  </form>

  <div class="dz-panel">
    <h5><i class="fa fa-list"></i> Active alerts</h5>
    {% if alerts %}
      <table class="dz-t">
        <tr><th>Streamer</th><th>Platform</th><th>Channel</th><th></th></tr>
        {% for a in alerts %}
          <tr>
            <td>{{ a.name }}</td>
            <td>{{ a.platform_label }}</td>
            <td>#{{ a.channel }}</td>
            <td>
              <form method="POST">
                <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                <input type="hidden" name="platform" value="{{ a.platform }}" />
                <input type="hidden" name="stream_name" value="{{ a.name }}" />
                <input type="hidden" name="channel_id" value="{{ a.channel_id }}" />
                {{ confirm('', 'remove_alert',
                           'Stop announcing ' ~ a.name ~ ' in #' ~ a.channel ~ '?') }}
              </form>
            </td>
          </tr>
        {% endfor %}
      </table>
    {% else %}
      <p class="dz-empty">No alerts set up yet.</p>
    {% endif %}
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-comment"></i> Alert messages</h5>
      <p class="dz-hint">
        Use <code>&#123;stream&#125;</code> for the streamer and
        <code>&#123;stream.display_name&#125;</code> for their display name
        (on Twitch the two can differ). <code>&#123;mention&#125;</code> inserts
        the mentions chosen below, and works in the first box only.
        Leave a box empty to use the default.
      </p>
      <label class="dz-label">Message when a mention is used</label>
      <textarea class="dz-area" name="live_message_mention"
                placeholder="&#123;mention&#125;, &#123;stream&#125; is live!">{{ live_message_mention }}</textarea>
      <label class="dz-label">Message with no mention</label>
      <textarea class="dz-area" name="live_message_nomention"
                placeholder="&#123;stream&#125; is live!">{{ live_message_nomention }}</textarea>
      <div class="dz-save">
        <button class="dz-btn primary" name="action" value="save_messages">
          <i class="fa fa-save"></i> Save messages
        </button>
      </div>
    </div>
  </form>

  <div class="dz-grid two">
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-sliders"></i> Behaviour</h5>
        <label class="dz-toggle">
          <input type="checkbox" name="autodelete" {% if autodelete %}checked{% endif %} />
          <span>Delete the alert when the stream ends</span>
        </label>
        <label class="dz-toggle">
          <input type="checkbox" name="ignore_reruns" {% if ignore_reruns %}checked{% endif %} />
          <span>Do not alert for reruns</span>
        </label>
        <label class="dz-toggle">
          <input type="checkbox" name="ignore_schedule"
                 {% if ignore_schedule %}checked{% endif %} />
          <span>Do not alert for scheduled YouTube streams</span>
        </label>
        <label class="dz-toggle">
          <input type="checkbox" name="use_buttons" {% if use_buttons %}checked{% endif %} />
          <span>Add a "watch the stream" button</span>
        </label>
        <div class="dz-save">
          <button class="dz-btn primary" name="action" value="save_behaviour">
            <i class="fa fa-save"></i> Save
          </button>
        </div>
      </div>
    </form>

    {% if is_admin %}
      <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <div class="dz-panel">
          <h5><i class="fa fa-at"></i> Mentions</h5>
          <label class="dz-toggle">
            <input type="checkbox" name="mention_everyone"
                   {% if mention_everyone %}checked{% endif %} />
            <span>Mention @everyone</span>
          </label>
          <label class="dz-toggle">
            <input type="checkbox" name="mention_here" {% if mention_here %}checked{% endif %} />
            <span>Mention @here</span>
          </label>
          <label class="dz-label" style="margin-top:10px;">Roles to mention</label>
          {{ picker('mention_roles', role_options, true, 8, 'Search roles...') }}
          <div class="dz-save">
            <button class="dz-btn primary" name="action" value="save_mentions">
              <i class="fa fa-save"></i> Save mentions
            </button>
          </div>
        </div>
      </form>
    {% endif %}
  </div>

  {% if is_owner %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-key"></i> Bot-wide settings</h5>
        <p class="dz-hint">
          Credentials: Twitch {{ 'set' if tokens.twitch else 'missing' }} &middot;
          YouTube {{ 'set' if tokens.youtube else 'missing' }} &middot;
          Kick {{ 'set' if tokens.kick else 'missing' }}.
          They are set under Admin &rarr; API Keys and are never shown here.
        </p>

        {% for c in credentials %}
          <details style="margin:8px 0; padding:9px 11px;
                          border:1px solid rgba(255,255,255,.09); border-radius:9px;">
            <summary style="cursor:pointer;">
              How to get {{ c.label }} credentials &mdash;
              <b>{{ 'already set' if c.set else 'not set' }}</b>
            </summary>
            <p class="dz-hint" style="margin-top:8px;">
              <a href="{{ c.docs }}" target="_blank" rel="noopener">{{ c.docs }}</a>
            </p>
            <ol class="dz-hint" style="margin:0 0 8px 18px; padding:0;">
              {% for step in c.steps %}<li style="margin:3px 0;">{{ step|safe }}</li>{% endfor %}
            </ol>
            <p class="dz-hint" style="margin:0;">
              Then add them under <a href="{{ url_for('base_blueprint.admin', page='api') }}">Admin &rarr; API Keys</a>, under the service
              <code>{{ c.key }}</code>:
            </p>
            <ul class="dz-hint" style="margin:5px 0 0 18px; padding:0;">
              {% for field in c.fields %}<li><code>{{ field }}</code></li>{% endfor %}
            </ul>
          </details>
        {% endfor %}
        <p class="dz-hint">
          Only the bot owner can see or change those keys.
        </p>
        <label class="dz-label">Seconds between checks</label>
        <div class="dz-row">
          <input class="dz-input" type="number" min="60" name="refresh_timer"
                 value="{{ refresh_timer }}" style="max-width:160px;" />
          <button class="dz-btn primary" name="action" value="save_refresh">
            <i class="fa fa-save"></i> Save
          </button>
        </div>
      </div>
    </form>
  {% endif %}

  {% endif %}
</div>
"""
)
