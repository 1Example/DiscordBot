from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands

from ...dashboard_integration import audio_pages
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
    ("reverb", "Reverb", "Adds a sense of space."),
)


# Bass boost presets. This is the only place they live now that /fx is gone,
# so the order here is the order of the dropdown - strongest first, ending
# with the two that take bass away rather than add it.
BASS_PRESETS = {
    "Maximum": [{"band": 0, "gain": 1.0}, {"band": 1, "gain": 1.0}],
    "Insane": [{"band": 0, "gain": 1.0}, {"band": 1, "gain": 0.75}],
    "Extreme": [{"band": 0, "gain": 1.0}, {"band": 1, "gain": 0.5}],
    "Very High": [{"band": 0, "gain": 0.75}, {"band": 1, "gain": 0.5}],
    "High": [{"band": 0, "gain": 0.75}, {"band": 1, "gain": 0.25}],
    "Medium": [{"band": 0, "gain": 0.5}, {"band": 1, "gain": 0.25}],
    "Fined Tuned": [{"band": 0, "gain": 0.25}, {"band": 1, "gain": 0.15}],
    "Cut-off": [{"band": 0, "gain": -0.25}, {"band": 1, "gain": -0.25}],
    # A lift you notice rather than one you feel in the desk - well under
    # Fine Tuned, for when the track already has enough low end.
    "Light": [{"band": 0, "gain": 0.15}, {"band": 1, "gain": 0.08}],
}

# Every filter parameter: the form field, the constructor keyword it maps to,
# a label, and min/max/step/default for its slider. PyLav bounds only vibrato
# and tremolo, so the rest are Lavalink's semantics and documented defaults.
FILTER_CONTROLS = (
    ("vibrato", "Vibrato", (
        ("vibrato_frequency", "frequency", "Frequency", 0.1, 14.0, 0.1, 2.0),
        ("vibrato_depth", "depth", "Depth", 0.05, 1.0, 0.05, 0.5),
    )),
    ("tremolo", "Tremolo", (
        ("tremolo_frequency", "frequency", "Frequency", 0.1, 14.0, 0.1, 2.0),
        ("tremolo_depth", "depth", "Depth", 0.05, 1.0, 0.05, 0.5),
    )),
    ("timescale", "Timescale", (
        ("timescale_speed", "speed", "Speed", 0.25, 3.0, 0.05, 1.0),
        ("timescale_pitch", "pitch", "Pitch", 0.25, 3.0, 0.05, 1.0),
        ("timescale_rate", "rate", "Rate", 0.25, 3.0, 0.05, 1.0),
    )),
    ("rotation", "Rotation", (
        ("rotation_hertz", "hertz", "Rotation Hz", 0.0, 5.0, 0.05, 0.2),
    )),
    ("low_pass", "Low pass", (
        ("low_pass_smoothing", "smoothing", "Smoothing", 1.0, 100.0, 1.0, 20.0),
    )),
    ("karaoke", "Karaoke", (
        ("karaoke_level", "level", "Level", 0.0, 1.0, 0.05, 1.0),
        ("karaoke_mono_level", "mono_level", "Mono level", 0.0, 1.0, 0.05, 1.0),
        ("karaoke_filter_band", "filter_band", "Filter band (Hz)", 0.0, 1000.0, 5.0, 220.0),
        ("karaoke_filter_width", "filter_width", "Filter width", 0.0, 500.0, 5.0, 100.0),
    )),
    ("channel_mix", "Channel mix", (
        ("mix_ll", "left_to_left", "Left to left", 0.0, 1.0, 0.05, 1.0),
        ("mix_lr", "left_to_right", "Left to right", 0.0, 1.0, 0.05, 0.0),
        ("mix_rl", "right_to_left", "Right to left", 0.0, 1.0, 0.05, 0.0),
        ("mix_rr", "right_to_right", "Right to right", 0.0, 1.0, 0.05, 1.0),
    )),
    ("distortion", "Distortion", (
        ("dist_sin_offset", "sin_offset", "Sin offset", -1.0, 1.0, 0.05, 0.0),
        ("dist_sin_scale", "sin_scale", "Sin scale", 0.0, 4.0, 0.05, 1.0),
        ("dist_cos_offset", "cos_offset", "Cos offset", -1.0, 1.0, 0.05, 0.0),
        ("dist_cos_scale", "cos_scale", "Cos scale", 0.0, 4.0, 0.05, 1.0),
        ("dist_tan_offset", "tan_offset", "Tan offset", -1.0, 1.0, 0.05, 0.0),
        ("dist_tan_scale", "tan_scale", "Tan scale", 0.0, 4.0, 0.05, 1.0),
        ("dist_offset", "offset", "Offset", -1.0, 1.0, 0.05, 0.0),
        ("dist_scale", "scale", "Scale", 0.0, 4.0, 0.05, 1.0),
    )),
    ("echo", "Echo", (
        ("echo_delay", "delay", "Delay (seconds)", 0.0, 10.0, 0.1, 1.0),
        ("echo_decay", "decay", "Decay", 0.0, 1.0, 0.05, 0.5),
    )),
)


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


class EffectsDashboard:
    """Audio effects: apply them, clear them, and choose what persists.

    Covers the ``/fx`` presets (nightcore, vaporwave, piano, rock, bass boost),
    the tunable filters (vibrato, tremolo, timescale, rotation, low pass,
    karaoke, channel mix, distortion, echo, reverb), a custom 15-band equalizer,
    ``/fx show`` and ``/fx reset``.
    """

    bot: t.Any
    _config: t.Any
    pylav: t.Any

    @dashboard_page(
        name="effects",
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

        settings = await self._effects_config.guild(guild).all()
        player = self.pylav.get_player(guild)
        gains = self._eq_gains(player)

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": EFFECTS_TEMPLATE,
                "audio_pages": audio_pages(await self.bot.is_owner(user)),
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
                "current_bass": self._current_bass(player),
                "fx_controls": FILTER_CONTROLS,
                "fx_values": self._fx_slider_values(player),
                "fx_enabled": self._fx_enabled(player),
                "eq_presets": [(key, value[0]) for key, value in EQ_PRESETS.items()],
                # (band, label, gain) rather than a dict keyed by band:
                # this crosses JSON-RPC, and JSON turns an integer key
                # into a string, so band_gains[0] came back undefined.
                "bands": [
                    (band, label, gains.get(band, 0.0))
                    for band, label in EQ_BAND_LABELS.items()
                ],
            },
        }

    def _fx_slider_values(self, player) -> dict[str, float]:
        """What each slider should open on: the player's value, or the default.

        A filter that is off reports None for its parameters, and a slider
        parked at zero would misrepresent that, so the documented default is
        shown instead and the enable box is what says it is off.
        """
        values: dict[str, float] = {}
        for key, _label, controls in FILTER_CONTROLS:
            obj = getattr(player, key, None) if player is not None else None
            for name, attr, _lbl, _mn, _mx, _st, default in controls:
                current = getattr(obj, attr, None) if obj is not None else None
                values[name] = default if current is None else current
        return values

    def _fx_enabled(self, player) -> dict[str, bool]:
        """Which filters are doing something right now."""
        keys = [key for key, _l, _c in FILTER_CONTROLS] + ["reverb"]
        if player is None:
            return {key: False for key in keys}
        return {
            key: bool(getattr(getattr(player, key, None), "changed", False))
            for key in keys
        }

    @staticmethod
    def _eq_gains(player) -> dict[int, float]:
        """The gain on each band, so the equalizer sliders start where it is."""
        equalizer = getattr(player, "equalizer", None) if player is not None else None
        if equalizer is None:
            return {band: 0.0 for band in EQ_BAND_LABELS}
        return {band: equalizer.get_gain(band) for band in EQ_BAND_LABELS}

    @staticmethod
    def _current_bass(player) -> str:
        """Which bass preset is on, read off the equalizer's name.

        The handler names it "Bass boost - <level>" when it applies one, which
        is a good deal steadier than comparing gain lists and finding two
        presets that happen to share a shape.
        """
        equalizer = getattr(player, "equalizer", None) if player is not None else None
        name = getattr(equalizer, "name", "") or ""
        prefix = "Bass boost - "
        if name.startswith(prefix):
            level = name[len(prefix):]
            if level in BASS_PRESETS:
                return level
        return "Off"

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

    @staticmethod
    def _fx_floats(field, key: str):
        """A comma separated list of numbers, as reverb's delays and gains are.

        None when the box is empty or holds anything that is not a number, so
        an unreadable value leaves that half of the filter alone rather than
        applying a partial list.
        """
        raw = (field(key) or "").strip()
        if not raw:
            return None
        try:
            return [float(part) for part in raw.replace(";", ",").split(",") if part.strip()]
        except ValueError:
            return None

    async def _fx_apply(self, guild: discord.Guild, field, action: str) -> list[dict]:
        """Apply one of the `/fx` presets or filters to the live player."""
        from pylav.exceptions.node import NodeHasNoFiltersException
        from pylav.players.filters import (
            ChannelMix,
            Distortion,
            Echo,
            Equalizer,
            Karaoke,
            LowPass,
            Reverb,
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
            if await self._effects_config.guild(guild).persist_eq():
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
                classes = {
                    "vibrato": Vibrato,
                    "tremolo": Tremolo,
                    "timescale": Timescale,
                    "rotation": Rotation,
                    "low_pass": LowPass,
                    "karaoke": Karaoke,
                    "channel_mix": ChannelMix,
                    "distortion": Distortion,
                    "echo": Echo,
                }
                filters: dict[str, t.Any] = {}
                for key, _label, controls in FILTER_CONTROLS:
                    cls = classes[key]
                    if not field.checked(f"enable_{key}"):
                        # A filter built with nothing set is what PyLav treats
                        # as off, so unticking is how you turn one off.
                        filters[key] = cls()
                        continue
                    filters[key] = cls(
                        **{
                            attr: self._fx_float(field, name, default)
                            for name, attr, _lbl, _mn, _mx, _st, default in controls
                        }
                    )
                # Reverb takes two lists, so it keeps its text boxes.
                filters["reverb"] = (
                    Reverb(
                        delays=self._fx_floats(field, "reverb_delays"),
                        gains=self._fx_floats(field, "reverb_gains"),
                    )
                    if field.checked("enable_reverb")
                    else Reverb()
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
                await self._effects_config.guild(guild).persist_fx.set(field.checked("persist_fx"))
                await self._effects_config.guild(guild).persist_eq.set(field.checked("persist_eq"))
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
    + '<style>\n  /* An effects rack reads as rows of one control, not a grid of boxes: the\n     label, the travel, and the number it is currently on. */\n  .fx-slider { display:flex; align-items:center; gap:10px; margin:6px 0; }\n  .fx-slider > label { flex:0 0 118px; font-size:.79rem; opacity:.75;\n                       white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }\n  .fx-slider input[type=range] { flex:1 1 auto; min-width:70px; accent-color:#6ea8fe;\n                                 background:transparent; }\n  .fx-slider output { flex:0 0 46px; text-align:right; font-size:.79rem;\n                      font-variant-numeric:tabular-nums; opacity:.9; }\n  .fx-group { padding:10px 12px; margin:8px 0; border-radius:11px;\n              border:1px solid rgba(255,255,255,.08); background:rgba(0,0,0,.16); }\n  .fx-head { display:flex; align-items:center; gap:9px; font-weight:600;\n             font-size:.87rem; margin-bottom:4px; cursor:pointer; }\n  .fx-head input { accent-color:#6ea8fe; }\n  /* Unticked reads as off without hiding where its sliders sit. */\n  .fx-group:has(.fx-head input:not(:checked)) .fx-slider { opacity:.45; }\n</style>\n'
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

  {{ subnav(name, audio_pages, 'effects', guild) }}

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
            <option value="Off" {% if current_bass == "Off" %}selected{% endif %}>Off</option>
            {% for level in bass_levels %}
              <option value="{{ level }}"
                      {% if current_bass == level %}selected{% endif %}>{{ level }}</option>
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
        <p class="dz-hint">Gain per band, from -0.25 to 1.0. The sliders start
           where the player is.</p>
        <div class="dz-grid three">
          {% for band, label, gain in bands %}
            <div class="fx-slider">
              <label for="band_{{ band }}">{{ label }}</label>
              <input type="range" id="band_{{ band }}" name="band_{{ band }}"
                     min="-0.25" max="1" step="0.05"
                     value="{{ gain }}"
                     oninput="this.nextElementSibling.value = (+this.value).toFixed(2);" />
              <output>{{ "%.2f"|format(gain) }}</output>
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
        <p class="dz-hint">Tick a filter to apply its sliders; untick it to turn
           it off. Everything here is applied together.</p>
        {% for key, label, controls in fx_controls %}
          <div class="fx-group">
            <label class="fx-head">
              <input type="checkbox" name="enable_{{ key }}"
                     {% if fx_enabled[key] %}checked{% endif %} />
              <span>{{ label }}</span>
            </label>
            {% for name, attr, ctl_label, lo, hi, step, default in controls %}
              <div class="fx-slider">
                <label for="{{ name }}">{{ ctl_label }}</label>
                <input type="range" id="{{ name }}" name="{{ name }}"
                       min="{{ lo }}" max="{{ hi }}" step="{{ step }}"
                       value="{{ fx_values[name] }}"
                       oninput="this.nextElementSibling.value = (+this.value).toFixed(2);" />
                <output>{{ "%.2f"|format(fx_values[name]) }}</output>
              </div>
            {% endfor %}
          </div>
        {% endfor %}
        <div class="fx-group">
          <label class="fx-head">
            <input type="checkbox" name="enable_reverb"
                   {% if fx_enabled["reverb"] %}checked{% endif %} />
            <span>Reverb</span>
          </label>
          <p class="dz-hint" style="margin:2px 0 8px;">Two lists of numbers, so
             these stay as text.</p>
          <div class="dz-grid two">
            <input class="dz-input" name="reverb_delays"
                   placeholder="delays, comma separated" />
            <input class="dz-input" name="reverb_gains"
                   placeholder="gains, comma separated" />
          </div>
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
