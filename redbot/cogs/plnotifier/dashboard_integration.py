from __future__ import annotations

import logging
import typing as t

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
)

log = logging.getLogger("red.plnotifier.dashboard")

# 58 flat toggles is unusable, so events are grouped for presentation only.
# Any key not listed here still appears, under "Other".
EVENT_GROUPS = (
    (
        "Playback",
        (
            "track_start", "track_end", "track_skipped", "track_seek", "track_replaced",
            "track_resumed", "track_autoplay", "track_previous_requested", "tracks_requested",
        ),
    ),
    (
        "Problems",
        ("track_stuck", "track_exception", "websocket_closed"),
    ),
    (
        "Queue",
        (
            "queue_shuffled", "queue_end", "queue_track_position_changed",
            "queue_tracks_removed",
        ),
    ),
    (
        "Player",
        (
            "player_paused", "player_stopped", "player_resumed", "player_moved",
            "player_disconnected", "player_connected", "player_restored", "volume_changed",
            "player_repeat", "filters_applied",
        ),
    ),
    (
        "Automatic actions",
        (
            "player_auto_paused", "player_auto_resumed", "player_auto_disconnected",
            "player_auto_disconnected_empty_queue",
        ),
    ),
    (
        "Nodes",
        ("node_connected", "node_disconnected", "node_changed"),
    ),
    (
        "SponsorBlock",
        ("segment_skipped", "segments_loaded"),
    ),
)

# Every track_start_* variant, collapsed into one section.
SOURCE_PREFIX = "track_start_"

# What "just the basics" means: the handful of events worth announcing, used by
# the reset button and matching the cog's registered defaults.
BASIC_EVENTS = frozenset(
    {"tracks_requested", "track_skipped", "track_start", "queue_end"}
)

PRETTY_SOURCES = {
    "youtube_music": "YouTube Music",
    "apple_music": "Apple Music",
    "localfile": "Local files",
    "http": "Direct HTTP",
    "gctts": "Google TTS",
    "flowery_tts": "Flowery TTS",
    "ocrmix": "OverClocked ReMix",
    "clypit": "Clyp.it",
    "getyarn": "getyarn.io",
}


class DashboardIntegration:
    """Which player events produce a notification, and where."""

    bot: t.Any
    _config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering PyLavNotifier as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Choose which music events are announced.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_plnotifier_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        if not await is_staff(self.bot, user, member, guild):
            return {
                "status": 1,
                "error_title": "Forbidden",
                "error_message": "Only server administrators can change notifications.",
            }

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._pln_handle_post(guild, kwargs)

        settings = await self._config.guild(guild).all()
        grouped, seen = [], set()

        for title, keys in EVENT_GROUPS:
            events = []
            for key in keys:
                data = settings.get(key)
                if not isinstance(data, dict):
                    continue
                seen.add(key)
                events.append(self._pln_event(key, data, key.replace("_", " ").capitalize()))
            if events:
                grouped.append({"title": title, "events": events})

        sources = []
        for key, data in settings.items():
            if not key.startswith(SOURCE_PREFIX) or not isinstance(data, dict):
                continue
            seen.add(key)
            raw = key[len(SOURCE_PREFIX):]
            sources.append(self._pln_event(key, data, PRETTY_SOURCES.get(raw, raw.capitalize())))
        if sources:
            grouped.append(
                {"title": "Track start by source", "events": sorted(sources, key=lambda e: e["label"])}
            )

        other = [
            self._pln_event(k, v, k.replace("_", " ").capitalize())
            for k, v in settings.items()
            if k not in seen and isinstance(v, dict) and "enabled" in v
        ]
        if other:
            grouped.append({"title": "Other", "events": sorted(other, key=lambda e: e["label"])})

        total = sum(len(g["events"]) for g in grouped)
        active = sum(1 for g in grouped for e in g["events"] if e["enabled"])

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": PLNOTIFIER_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "groups": grouped,
                "total": total,
                "active": active,
                "channels": channel_options(guild, selected=settings.get("notify_channel_id")),
                "auto_delete_after": settings.get("auto_delete_after", 0) or 0,
                "colour_coded": bool(settings.get("colour_coded", True)),
                "members_only": bool(settings.get("members_only", True)),
                "channel_name": (
                    f"#{guild.get_channel(settings['notify_channel_id']).name}"
                    if settings.get("notify_channel_id")
                    and guild.get_channel(settings["notify_channel_id"])
                    else ""
                ),
                # Mirrors PyLavNotifier._COLOURS so the legend matches reality.
                "swatches": (
                    ("Now playing", "#3ba55d"),
                    ("Queue", "#5865f2"),
                    ("Skipped", "#e67e22"),
                    ("Problems", "#ed4245"),
                    ("Charges", "#f1c40f"),
                    ("Player", "#95a5a6"),
                ),
            },
        }

    @staticmethod
    def _pln_event(key: str, data: dict, label: str) -> dict:
        return {
            "key": key,
            "label": label,
            "enabled": bool(data.get("enabled")),
            "mention": bool(data.get("mention")),
        }

    async def _pln_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")
        conf = self._config.guild(guild)

        try:
            if action == "save_group":
                keys = [k for k in (field("keys") or "").split(",") if k]
                if not keys:
                    return [{"message": "Nothing to save.", "category": "warning"}]
                for key in keys:
                    # Each event is a dict; write the whole mapping so a missing
                    # sub-key never leaves a half-updated entry behind.
                    await conf.get_attr(key).set(
                        {
                            "enabled": field.checked(f"{key}__enabled"),
                            "mention": field.checked(f"{key}__mention"),
                        }
                    )
                return [{"message": f"Saved {len(keys)} event(s).", "category": "success"}]

            if action == "reset_basics":
                changed = 0
                async with self._config.guild(guild).all() as stored:
                    for key, value in stored.items():
                        if not isinstance(value, dict) or "enabled" not in value:
                            continue
                        wanted = key in BASIC_EVENTS
                        if value["enabled"] != wanted:
                            value["enabled"] = wanted
                            changed += 1
                return [
                    {
                        "message": f"Reset to the basic notifications; "
                        f"{changed} event(s) changed.",
                        "category": "success",
                    }
                ]

            if action == "save_setup":
                notes: list[dict] = []

                seconds = field.integer("auto_delete_after", 0) or 0
                if not 0 <= seconds <= 3600:
                    return [
                        {"message": "Choose between 0 and 3600 seconds.",
                         "category": "warning"}
                    ]
                await conf.auto_delete_after.set(seconds)
                await conf.colour_coded.set(field.checked("colour_coded"))
                await conf.members_only.set(field.checked("members_only"))

                raw = field("notify_channel_id") or ""
                channel_id = int(raw) if raw.isdigit() else None
                if channel_id is not None:
                    channel = guild.get_channel(channel_id)
                    if channel is None:
                        channel_id = None
                    elif not channel.permissions_for(guild.me).send_messages:
                        notes.append(
                            {
                                "message": f"I cannot send messages in #{channel.name}, "
                                "so nothing will appear there.",
                                "category": "warning",
                            }
                        )
                await conf.notify_channel_id.set(channel_id)

                where = (
                    f"#{guild.get_channel(channel_id).name}"
                    if channel_id
                    else "the player's own channel"
                )
                lifetime = (
                    f"cleared after {seconds}s" if seconds else "kept indefinitely"
                )
                return notes + [
                    {
                        "message": f"Announcing in {where}, {lifetime}.",
                        "category": "success",
                    }
                ]

            if action in ("enable_all", "disable_all", "mentions_off"):
                target = action == "enable_all"
                settings = await conf.all()
                changed = 0
                for key, data in settings.items():
                    if not isinstance(data, dict) or "enabled" not in data:
                        continue
                    if action == "mentions_off":
                        if not data.get("mention"):
                            continue
                        await conf.get_attr(key).set({**data, "mention": False})
                    else:
                        if data.get("enabled") is target:
                            continue
                        await conf.get_attr(key).set({**data, "enabled": target})
                    changed += 1
                verb = {"enable_all": "Enabled", "disable_all": "Disabled",
                        "mentions_off": "Removed mentions from"}[action]
                return [{"message": f"{verb} {changed} event(s).", "category": "success"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("PyLavNotifier dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]


PLNOTIFIER_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<style>
  .pn-ev { display:grid; grid-template-columns:1fr auto auto; align-items:center;
           gap:12px; padding:8px 10px; border-radius:9px; }
  .pn-ev:nth-child(odd) { background:rgba(255,255,255,.025); }
  .pn-ev .pn-name { font-size:.87rem; }
  .pn-col { width:74px; text-align:center; font-size:.68rem; opacity:.5;
            text-transform:uppercase; letter-spacing:.05em; }
  /* Grid rows share a height, so a short card leaves a hole beside a tall one.
     Multi-column packs each card directly under the previous one instead. */
  .pn-grid { column-gap:14px; }
  @media (min-width:1150px){ .pn-grid { column-count:2; } }
  .pn-grid > form { break-inside:avoid; page-break-inside:avoid; display:block;
                    margin:0 0 14px; }
  .pn-preview { display:flex; gap:12px; padding:12px 14px; border-radius:10px;
                background:rgba(0,0,0,.28); border-left:4px solid #5865f2; }
  .pn-preview .pn-av { width:38px; height:38px; border-radius:50%;
                       background:rgba(255,255,255,.12); flex:0 0 auto; }
  .pn-preview .pn-art { width:56px; height:56px; border-radius:6px;
                        background:rgba(255,255,255,.09); flex:0 0 auto; margin-left:auto; }
  .pn-swatch { display:inline-block; width:10px; height:10px; border-radius:3px;
               margin-right:6px; vertical-align:-1px; }
</style>

<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-bell"></i> Music notifications in {{ guild_name }}</h4>
    <p>
      {{ active }} of {{ total }} events announced
      &middot; posting to {{ channel_name or "the player's own channel" }}
      &middot; {% if auto_delete_after %}clearing after {{ auto_delete_after }}s
               {% else %}kept indefinitely{% endif %}
    </p>
  </div>

  <div class="dz-panel">
    <h5><i class="fa fa-eye"></i> What they look like</h5>
    <p class="dz-hint">
      The requester's avatar sits on the author line and the track's artwork on
      the right. Colour tells you the kind of event at a glance.
    </p>
    <div class="pn-preview">
      <div class="pn-av"></div>
      <div style="min-width:0;">
        <div style="font-size:.78rem; opacity:.65;">Someone</div>
        <div style="font-weight:600; font-size:.9rem;">Added to queue</div>
        <div style="font-size:.84rem; opacity:.85;">Artist &mdash; Track title</div>
      </div>
      <div class="pn-art"></div>
    </div>
    <p class="dz-hint" style="margin-top:10px;">
      {% for label, colour in swatches %}
        <span class="dz-tag"><span class="pn-swatch"
              style="background:{{ colour }};"></span>{{ label }}</span>
      {% endfor %}
    </p>
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-sliders"></i> Setup</h5>
      <div class="dz-grid two">
        <div>
          <label class="dz-label">Where to announce</label>
          <p class="dz-hint">Leave unset to post wherever the player was summoned.</p>
          {{ picker('notify_channel_id', channels, false, 8, 'Search channels...',
                    true, "the player's own channel") }}
        </div>
        <div>
          <label class="dz-label">Remove notifications after</label>
          <p class="dz-hint">Seconds. 0 keeps them in the channel for good.</p>
          <input class="dz-input" type="number" min="0" max="3600"
                 name="auto_delete_after" value="{{ auto_delete_after }}"
                 style="max-width:200px;" />
          <label class="dz-toggle" style="margin-top:12px;">
            <input type="checkbox" name="colour_coded"
                   {% if colour_coded %}checked{% endif %} />
            <span>Colour each notification by what happened</span>
          </label>
          <label class="dz-toggle" style="margin-top:8px;">
            <input type="checkbox" name="members_only"
                   {% if members_only %}checked{% endif %} />
            <span>Only announce what members did</span>
          </label>
          <p class="dz-hint" style="margin-top:4px;">
            Drops anything the bot did to itself &mdash; autoplay queueing a
            track, or an action it could not attribute to anyone. Notifications
            that name nobody at all, like the queue running out, still go out.
          </p>
        </div>
      </div>
      <div class="dz-save">
        <button class="dz-btn primary" name="action" value="save_setup">
          <i class="fa fa-save"></i> Save setup
        </button>
      </div>
    </div>
  </form>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-magic"></i> Presets</h5>
      <p class="dz-hint">
        Fifty-nine events is a lot to curate by hand. Start from one of these,
        then adjust below.
      </p>
      <div class="dz-row">
        {{ confirm('Just the basics', 'reset_basics',
                   'Announce only play, queue, skip, connect, problems and charges?',
                   'primary', 'fa-magic') }}
        {{ confirm('Announce everything', 'enable_all',
                   'Turn on all 59 events? Busy servers get very noisy.',
                   '', 'fa-check-square-o') }}
        {{ confirm('Silence everything', 'disable_all', 'Turn off every event?') }}
        <button class="dz-btn" name="action" value="mentions_off">
          <i class="fa fa-at"></i> Stop pinging requesters
        </button>
      </div>
    </div>
  </form>

  <div class="pn-grid">
    {% for g in groups %}
      <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <input type="hidden" name="keys"
               value="{% for e in g.events %}{{ e.key }}{% if not loop.last %},{% endif %}{% endfor %}" />
        <div class="dz-panel">
          <h5><i class="fa fa-music"></i> {{ g.title }}</h5>
          <p class="dz-hint">
            {{ g.events|selectattr('enabled')|list|length }} of
            {{ g.events|length }} on. &ldquo;Mention&rdquo; pings the requester.
          </p>
          <div class="pn-ev" style="background:none;">
            <span></span>
            <span class="pn-col">Announce</span>
            <span class="pn-col">Mention</span>
          </div>
          {% for e in g.events %}
            <label class="pn-ev">
              <span class="pn-name">{{ e.label }}</span>
              <span class="pn-col">
                <input type="checkbox" name="{{ e.key }}__enabled"
                       {% if e.enabled %}checked{% endif %} />
              </span>
              <span class="pn-col">
                <input type="checkbox" name="{{ e.key }}__mention"
                       {% if e.mention %}checked{% endif %} />
              </span>
            </label>
          {% endfor %}
          <div class="dz-save">
            <button class="dz-btn primary" name="action" value="save_group">
              <i class="fa fa-save"></i> Save {{ g.title|lower }}
            </button>
          </div>
        </div>
      </form>
    {% endfor %}
  </div>
</div>
"""
)
