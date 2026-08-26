from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
)

log = logging.getLogger("red.pleffects.dashboard")

# Filters PyLav exposes on a player, with a friendly label and the attribute
# that indicates the filter is doing something.
FILTERS = (
    ("nightcore", "Nightcore", "Speeds up and raises the pitch."),
    ("vibrato", "Vibrato", "Wobbles the pitch."),
    ("tremolo", "Tremolo", "Wobbles the volume."),
    ("karaoke", "Karaoke", "Attempts to remove vocals."),
    ("timescale", "Timescale", "Speed, pitch and rate."),
    ("distortion", "Distortion", "Adds harmonic distortion."),
    ("low_pass", "Low pass", "Muffles the high end."),
    ("rotation", "Rotation", "Rotates audio between channels."),
    ("channel_mix", "Channel mix", "Blends left and right."),
    ("echo", "Echo", "Repeats the signal."),
)


# Bass boost presets, matching the levels `[p]fx bassboost` accepts.
BASS_PRESETS = {
    "Maximum": [{"band": 0, "gain": 1.0}, {"band": 1, "gain": 1.0}],
    "Insane": [{"band": 0, "gain": 1.0}, {"band": 1, "gain": 0.75}],
    "Extreme": [{"band": 0, "gain": 1.0}, {"band": 1, "gain": 0.5}],
    "High": [{"band": 0, "gain": 0.75}, {"band": 1, "gain": 0.5}],
    "Very High": [{"band": 0, "gain": 0.75}, {"band": 1, "gain": 0.25}],
    "Medium": [{"band": 0, "gain": 0.5}, {"band": 1, "gain": 0.25}],
    "Fined Tuned": [{"band": 0, "gain": 0.25}, {"band": 1, "gain": 0.15}],
    "Cut-off": [{"band": 0, "gain": -0.25}, {"band": 1, "gain": -0.25}],
}

# Band index to the frequency it controls, matching PyLav's EQ_BAND_MAPPING.
EQ_BAND_LABELS = {
    0: "20Hz",
    1: "40Hz",
    2: "63Hz",
    3: "100Hz",
    4: "160Hz",
    5: "250Hz",
    6: "400Hz",
    7: "630Hz",
    8: "1kHz",
    9: "1.6kHz",
    10: "2.5kHz",
    11: "4kHz",
    12: "6.3kHz",
    13: "10kHz",
    14: "16kHz",
}

# Named equalizer presets available as one-click buttons, copied from the cog.
EQ_PRESETS = {
    "piano": (
        "Piano",
        [
            {"band": 0, "gain": -0.25},
            {"band": 1, "gain": -0.25},
            {"band": 2, "gain": -0.125},
            {"band": 4, "gain": 0.25},
            {"band": 5, "gain": 0.25},
            {"band": 7, "gain": -0.25},
            {"band": 8, "gain": -0.25},
            {"band": 11, "gain": 0.5},
            {"band": 12, "gain": 0.25},
            {"band": 13, "gain": -0.025},
        ],
    ),
    "rock": (
        "Metal",
        [
            {"band": 1, "gain": 0.1},
            {"band": 2, "gain": 0.1},
            {"band": 3, "gain": 0.15},
            {"band": 4, "gain": 0.13},
            {"band": 5, "gain": 0.1},
            {"band": 7, "gain": 0.125},
            {"band": 8, "gain": 0.175},
            {"band": 9, "gain": 0.175},
            {"band": 10, "gain": 0.125},
            {"band": 11, "gain": 0.125},
            {"band": 12, "gain": 0.1},
            {"band": 13, "gain": 0.075},
        ],
    ),
}


class DashboardIntegration:
    """Audio effects: apply them, clear them, and choose what persists.

    Covers the ``/fx`` presets (nightcore, vaporwave, piano, rock, bass boost),
    the tunable filters (vibrato, tremolo, timescale, rotation, low pass,
    karaoke, channel mix, distortion, echo, reverb), a custom 15-band equalizer,
    ``/fx show`` and ``/fx reset``.
    """

    bot: t.Any
    _config: t.Any
    pylav: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering PyLavEffects as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Audio effects and whether they persist.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_pleffects_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        staff = await is_staff(self.bot, user, member, guild)

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            if not staff:
                return {
                    "status": 1,
                    "error_title": "Forbidden",
                    "error_message": "Only server administrators can change effect settings.",
                }
            notifications = await self._fx_handle_post(guild, kwargs)

        settings = await self._config.guild(guild).all()
        player = self.pylav.get_player(guild)

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": EFFECTS_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_staff": staff,
                "persist_fx": bool(settings.get("persist_fx")),
                "persist_eq": bool(settings.get("persist_eq")),
                "connected": player is not None,
                "filters": self._fx_state(player),
                "volume": int(getattr(player, "volume", 0) or 0) if player else None,
                "active_filters": sum(1 for f in self._fx_state(player) if f["active"]),
                "current_eq": getattr(getattr(player, "equalizer", None), "name", "Flat") if player else "Flat",
                "bass_levels": list(BASS_PRESETS),
                "eq_presets": [(key, value[0]) for key, value in EQ_PRESETS.items()],
                "bands": list(EQ_BAND_LABELS.items()),
            },
        }

    def _fx_state(self, player) -> list[dict]:
        """Which filters are currently active on the live player."""
        rows = []
        for key, label, blurb in FILTERS:
            active = False
            if player is not None:
                obj = getattr(player, key, None)
                # PyLav filter objects expose .changed when they differ from default.
                if obj is not None:
                    active = bool(getattr(obj, "changed", False))
            rows.append({"key": key, "label": label, "help": blurb, "active": active})
        return rows

    @staticmethod
    def _fx_float(field, key: str, default=None):
        raw = (field(key) or "").strip()
        if raw == "":
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    async def _fx_apply(self, guild: discord.Guild, field, action: str) -> list[dict]:
        """Apply one of the `/fx` presets or filters to the live player."""
        from pylav.exceptions.node import NodeHasNoFiltersException
        from pylav.players.filters import (
            ChannelMix,
            Distortion,
            Equalizer,
            Karaoke,
            LowPass,
            Rotation,
            Timescale,
            Tremolo,
            Vibrato,
        )

        player = self.pylav.get_player(guild)
        if player is None:
            return [
                {"message": "I am not connected to a voice channel.", "category": "warning"}
            ]
        requester = guild.me

        async def store_eq(equalizer) -> None:
            """Mirror what the commands do when equalizer persistence is on."""
            if await self._config.guild(guild).persist_eq():
                effects = await player.config.fetch_effects()
                effects["equalizer"] = equalizer.to_dict() if equalizer else []
                await player.config.update_effects(effects)

        try:
            if action == "preset_nightcore":
                if player.equalizer.name == "Nightcore":
                    await player.remove_nightcore(requester=requester)
                    return [{"message": "Nightcore disabled.", "category": "success"}]
                await player.apply_nightcore(requester=requester)
                return [{"message": "Nightcore enabled.", "category": "success"}]

            if action == "preset_vaporwave":
                if player.equalizer.name == "Vaporwave":
                    await player.remove_vaporwave(requester=requester)
                    return [{"message": "Vaporwave disabled.", "category": "success"}]
                await player.apply_vaporwave(requester=requester)
                return [{"message": "Vaporwave enabled.", "category": "success"}]

            if action == "preset_eq":
                key = field("preset")
                if key not in EQ_PRESETS:
                    return [{"message": "Unknown preset.", "category": "warning"}]
                name, levels = EQ_PRESETS[key]
                equalizer = Equalizer(levels=levels, name=name)
                await player.set_equalizer(requester=requester, equalizer=equalizer)
                await store_eq(equalizer)
                return [
                    {"message": f"{name} equalizer preset applied.", "category": "success"}
                ]

            if action == "bassboost":
                level = field("bass_level") or "Off"
                if level == "Off":
                    equalizer = Equalizer.default()
                    await player.set_equalizer(requester=requester, equalizer=equalizer)
                    await store_eq(None)
                    return [
                        {"message": "Bass boost disabled.", "category": "success"}
                    ]
                if level not in BASS_PRESETS:
                    return [{"message": "Unknown bass boost level.", "category": "warning"}]
                equalizer = Equalizer(
                    levels=BASS_PRESETS[level], name=f"Bass boost - {level}"
                )
                await player.set_equalizer(requester=requester, equalizer=equalizer)
                await store_eq(equalizer)
                return [
                    {"message": f"Bass boost set to {level}.", "category": "success"}
                ]

            if action == "custom_eq":
                levels = []
                for band in range(15):
                    gain = self._fx_float(field, f"band_{band}")
                    if gain is None:
                        continue
                    if not -0.25 <= gain <= 1.0:
                        return [
                            {
                                "message": f"Band {band}: gain must be between "
                                "-0.25 and 1.0.",
                                "category": "warning",
                            }
                        ]
                    levels.append({"band": band, "gain": gain})
                equalizer = Equalizer(levels=levels, name="Custom")
                await player.set_equalizer(requester=requester, equalizer=equalizer)
                await store_eq(equalizer)
                return [
                    {"message": "Custom equalizer applied.", "category": "success"}
                ]

            if action == "filters":
                filters: dict[str, t.Any] = {}
                filters["vibrato"] = Vibrato(
                    frequency=self._fx_float(field, "vibrato_frequency"),
                    depth=self._fx_float(field, "vibrato_depth"),
                )
                filters["tremolo"] = Tremolo(
                    frequency=self._fx_float(field, "tremolo_frequency"),
                    depth=self._fx_float(field, "tremolo_depth"),
                )
                filters["rotation"] = Rotation(
                    hertz=self._fx_float(field, "rotation_hertz")
                )
                filters["low_pass"] = LowPass(
                    smoothing=self._fx_float(field, "low_pass_smoothing")
                )
                filters["timescale"] = Timescale(
                    speed=self._fx_float(field, "timescale_speed"),
                    pitch=self._fx_float(field, "timescale_pitch"),
                    rate=self._fx_float(field, "timescale_rate"),
                )
                filters["karaoke"] = Karaoke(
                    level=self._fx_float(field, "karaoke_level"),
                    mono_level=self._fx_float(field, "karaoke_mono_level"),
                    filter_band=self._fx_float(field, "karaoke_filter_band"),
                    filter_width=self._fx_float(field, "karaoke_filter_width"),
                )
                filters["channel_mix"] = ChannelMix(
                    left_to_left=self._fx_float(field, "mix_ll"),
                    left_to_right=self._fx_float(field, "mix_lr"),
                    right_to_left=self._fx_float(field, "mix_rl"),
                    right_to_right=self._fx_float(field, "mix_rr"),
                )
                filters["distortion"] = Distortion(
                    sin_offset=self._fx_float(field, "dist_sin_offset"),
                    sin_scale=self._fx_float(field, "dist_sin_scale"),
                    cos_offset=self._fx_float(field, "dist_cos_offset"),
                    cos_scale=self._fx_float(field, "dist_cos_scale"),
                    tan_offset=self._fx_float(field, "dist_tan_offset"),
                    tan_scale=self._fx_float(field, "dist_tan_scale"),
                    offset=self._fx_float(field, "dist_offset"),
                    scale=self._fx_float(field, "dist_scale"),
                )
                await player.set_filters(requester=requester, **filters)
                return [
                    {"message": "Filters applied.", "category": "success"}
                ]
        except NodeHasNoFiltersException as exc:
            return [{"message": str(exc), "category": "warning"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    async def _fx_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

        try:
            if action.startswith(("preset_", "bassboost", "custom_eq", "filters")):
                return await self._fx_apply(guild, field, action)

            if action == "save":
                await self._config.guild(guild).persist_fx.set(field.checked("persist_fx"))
                await self._config.guild(guild).persist_eq.set(field.checked("persist_eq"))
                return [{"message": "Persistence settings saved.", "category": "success"}]

            if action == "reset":
                player = self.pylav.get_player(guild)
                if player is None:
                    return [{"message": "I am not connected to a voice channel.", "category": "warning"}]
                # set_filters with no arguments and reset=True clears everything.
                await player.set_filters(requester=self.bot.user, reset_not_set=True)
                return [{"message": "All effects cleared.", "category": "success"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("PyLavEffects dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]


EFFECTS_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-sliders"></i> Audio effects in {{ guild_name }}</h4>
    <p>
      {% if connected %}Player connected{% if volume is not none %} at {{ volume }}% volume{% endif %}.
      {% else %}No active player.{% endif %}
    </p>
  </div>

  {% if is_staff %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-thumb-tack"></i> Persistence</h5>
        <p class="dz-hint">Whether settings survive the player disconnecting.</p>
        <label class="dz-toggle">
          <input type="checkbox" name="persist_fx" {% if persist_fx %}checked{% endif %} />
          <span>Keep effects between sessions</span>
        </label>
        <label class="dz-toggle">
          <input type="checkbox" name="persist_eq" {% if persist_eq %}checked{% endif %} />
          <span>Keep the equaliser between sessions</span>
        </label>
        <div class="dz-row" style="margin-top:12px;">
          <button class="dz-btn primary" name="action" value="save">
            <i class="fa fa-save"></i> Save
          </button>
          {% if connected %}
            <button class="dz-btn danger" name="action" value="reset"
                    onclick="return confirm('Clear every active effect?');">
              <i class="fa fa-undo"></i> Clear all effects
            </button>
          {% endif %}
        </div>
      </div>
    </form>
  {% endif %}


  <div class="dz-panel">
    <h5><i class="fa fa-chart-line"></i> Player Status</h5>
    <div class="dz-grid two">
      <div><strong>Volume</strong><br>{{ volume if volume is not none else 0 }}%</div>
      <div><strong>Equalizer</strong><br>{{ current_eq }}</div>
      <div><strong>Active Filters</strong><br>{{ active_filters }}</div>
      <div><strong>Connection</strong><br>{% if connected %}Connected{% else %}Offline{% endif %}</div>
    </div>
  </div>

  {% if is_staff %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-magic"></i> Presets</h5>
        <p class="dz-hint">
          {% if connected %}One click each; nightcore and vaporwave toggle off
            when they are already on.
          {% else %}Connect a player first &mdash; these act on the live player.
          {% endif %}
        </p>
        <div class="dz-row">
          <button class="dz-btn" name="action" value="preset_nightcore">
            <i class="fa fa-bolt"></i> Nightcore
          </button>
          <button class="dz-btn" name="action" value="preset_vaporwave">
            <i class="fa fa-tint"></i> Vaporwave
          </button>
          {% for key, label in eq_presets %}
            <button class="dz-btn" name="action" value="preset_eq"
                    onclick="this.form.preset.value='{{ key }}';">
              <i class="fa fa-music"></i> {{ label }}
            </button>
          {% endfor %}
          <input type="hidden" name="preset" value="" />
        </div>
        <div class="dz-row" style="margin-top:12px;">
          <label class="dz-label" style="margin:0;">Bass boost</label>
          <select class="dz-select" name="bass_level" style="max-width:200px;">
            <option value="Off">Off</option>
            {% for level in bass_levels %}
              <option value="{{ level }}">{{ level }}</option>
            {% endfor %}
          </select>
          <button class="dz-btn primary" name="action" value="bassboost">
            <i class="fa fa-volume-up"></i> Apply
          </button>
        </div>
      </div>
    </form>

    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-sliders"></i> Custom equalizer</h5>
        <p class="dz-hint">Gain per band, from -0.25 to 1.0. Leave a band empty
           to keep it flat.</p>
        <div class="dz-grid three">
          {% for band, label in bands %}
            <div>
              <label class="dz-label">{{ label }}</label>
              <input class="dz-input" type="number" step="0.05" min="-0.25" max="1"
                     name="band_{{ band }}" placeholder="0" />
            </div>
          {% endfor %}
        </div>
        <div class="dz-save">
          <button class="dz-btn primary" name="action" value="custom_eq">
            <i class="fa fa-save"></i> Apply equalizer
          </button>
        </div>
      </div>
    </form>

    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-flask"></i> Filters</h5>
        <p class="dz-hint">Everything is applied together; leave a field empty to
           leave that part of the filter alone.</p>
        <div class="dz-grid three">
          <div>
            <label class="dz-label">Vibrato</label>
            <input class="dz-input" type="number" step="0.1" name="vibrato_frequency"
                   placeholder="frequency (0-14)" />
            <input class="dz-input" type="number" step="0.1" name="vibrato_depth"
                   placeholder="depth (0-1)" style="margin-top:6px;" />
          </div>
          <div>
            <label class="dz-label">Tremolo</label>
            <input class="dz-input" type="number" step="0.1" name="tremolo_frequency"
                   placeholder="frequency" />
            <input class="dz-input" type="number" step="0.1" name="tremolo_depth"
                   placeholder="depth (0-1)" style="margin-top:6px;" />
          </div>
          <div>
            <label class="dz-label">Rotation and low pass</label>
            <input class="dz-input" type="number" step="0.1" name="rotation_hertz"
                   placeholder="rotation hertz" />
            <input class="dz-input" type="number" step="0.1" name="low_pass_smoothing"
                   placeholder="low pass smoothing" style="margin-top:6px;" />
          </div>
        </div>
        <div class="dz-grid three" style="margin-top:10px;">
          <div>
            <label class="dz-label">Timescale</label>
            <input class="dz-input" type="number" step="0.05" name="timescale_speed"
                   placeholder="speed" />
            <input class="dz-input" type="number" step="0.05" name="timescale_pitch"
                   placeholder="pitch" style="margin-top:6px;" />
            <input class="dz-input" type="number" step="0.05" name="timescale_rate"
                   placeholder="rate" style="margin-top:6px;" />
          </div>
          <div>
            <label class="dz-label">Karaoke</label>
            <input class="dz-input" type="number" step="0.05" name="karaoke_level"
                   placeholder="level" />
            <input class="dz-input" type="number" step="0.05" name="karaoke_mono_level"
                   placeholder="mono level" style="margin-top:6px;" />
            <input class="dz-input" type="number" step="1" name="karaoke_filter_band"
                   placeholder="filter band" style="margin-top:6px;" />
            <input class="dz-input" type="number" step="1" name="karaoke_filter_width"
                   placeholder="filter width" style="margin-top:6px;" />
          </div>
          <div>
            <label class="dz-label">Channel mix</label>
            <input class="dz-input" type="number" step="0.05" name="mix_ll"
                   placeholder="left to left" />
            <input class="dz-input" type="number" step="0.05" name="mix_lr"
                   placeholder="left to right" style="margin-top:6px;" />
            <input class="dz-input" type="number" step="0.05" name="mix_rl"
                   placeholder="right to left" style="margin-top:6px;" />
            <input class="dz-input" type="number" step="0.05" name="mix_rr"
                   placeholder="right to right" style="margin-top:6px;" />
          </div>
        </div>
        <label class="dz-label" style="margin-top:12px;">Distortion</label>
        <div class="dz-grid three">
          <input class="dz-input" type="number" step="0.05" name="dist_sin_offset"
                 placeholder="sin offset" />
          <input class="dz-input" type="number" step="0.05" name="dist_sin_scale"
                 placeholder="sin scale" />
          <input class="dz-input" type="number" step="0.05" name="dist_cos_offset"
                 placeholder="cos offset" />
          <input class="dz-input" type="number" step="0.05" name="dist_cos_scale"
                 placeholder="cos scale" />
          <input class="dz-input" type="number" step="0.05" name="dist_tan_offset"
                 placeholder="tan offset" />
          <input class="dz-input" type="number" step="0.05" name="dist_tan_scale"
                 placeholder="tan scale" />
          <input class="dz-input" type="number" step="0.05" name="dist_offset"
                 placeholder="offset" />
          <input class="dz-input" type="number" step="0.05" name="dist_scale"
                 placeholder="scale" />
        </div>
        <div class="dz-save">
          <button class="dz-btn primary" name="action" value="filters">
            <i class="fa fa-check"></i> Apply filters
          </button>
        </div>
      </div>
    </form>
  {% endif %}

  <div class="dz-panel">
    <h5><i class="fa fa-magic"></i> Current filters</h5>
    <p class="dz-hint">
      {% if connected %}Applied with the effects commands in Discord.
      {% else %}Connect a player to see live state.{% endif %}
    </p>
    <div class="dz-grid two">
      {% for f in filters %}
        <div style="display:flex; align-items:center; gap:10px; padding:7px 0;
                    border-bottom:1px solid rgba(255,255,255,.05);">
          <span style="width:9px; height:9px; border-radius:50%; flex:0 0 auto;
                       background:{% if f.active %}#3ba55d{% else %}rgba(255,255,255,.16){% endif %};"></span>
          <div style="min-width:0;">
            <div style="font-size:.87rem; font-weight:600;">
              {{ f.label }}
              {% if f.active %}<span class="dz-tag" style="margin-left:5px;">on</span>{% endif %}
            </div>
            <div style="font-size:.72rem; opacity:.45;">{{ f.help }}</div>
          </div>
        </div>
      {% endfor %}
    </div>
  </div>
</div>
"""
)
