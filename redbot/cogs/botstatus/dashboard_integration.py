from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import BASE_CSS, dashboard_page, form_reader

log = logging.getLogger("red.botstatus.dashboard")

# Config stores a 3-tuple: (activity type, presence status, text).
ACTIVITY_TYPES = {
    "playing": "Playing",
    "listening": "Listening to",
    "watching": "Watching",
    "competing": "Competing in",
    "streaming": "Streaming",
}

PRESENCE_STATES = {
    "online": "Online",
    "idle": "Idle",
    "dnd": "Do not disturb",
    "invisible": "Invisible",
}

MAX_TEXT = 128


class DashboardIntegration:
    """Owner-only editor for the bot's persistent presence."""

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Botstatus as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Set the bot's status and activity.",
        methods=("GET", "POST"),
        is_owner=True,
    )
    async def dashboard_botstatus_page(self, user: discord.User, **kwargs: t.Any) -> dict[str, t.Any]:
        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._bs_handle_post(kwargs)

        stored = await self.config.status()
        # Older versions saved "game"; the cog still maps it to "playing".
        activity = "playing" if stored[0] == "game" else (stored[0] or "playing")
        state = stored[1] or "online"
        text = stored[2] or ""

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": BOTSTATUS_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "activity_types": [
                    {"key": k, "label": v, "selected": k == activity}
                    for k, v in ACTIVITY_TYPES.items()
                ],
                "presence_states": [
                    {"key": k, "label": v, "selected": k == state} for k, v in PRESENCE_STATES.items()
                ],
                "text": text,
                "is_streaming": activity == "streaming",
                "stream_url": stored[1] if activity == "streaming" else "",
                "configured": bool(stored[0] and stored[1] and stored[2]),
                "max_text": MAX_TEXT,
                "bot_name": self.bot.user.name if self.bot.user else "the bot",
            },
        }

    async def _bs_handle_post(self, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

        if action == "clear":
            await self.config.status.set((None, None, None))
            try:
                await self.bot.change_presence(activity=None, status=discord.Status.online)
            except Exception as exc:  # noqa: BLE001
                log.exception("Could not clear presence")
                return [{"message": f"Cleared, but Discord rejected it: {exc}", "category": "warning"}]
            return [{"message": "Status cleared.", "category": "success"}]

        if action != "save":
            return [{"message": f"Unknown action: {action}", "category": "warning"}]

        activity = field("activity_type") or "playing"
        text = (field("text") or "").strip()

        if activity not in ACTIVITY_TYPES:
            return [{"message": "Unknown activity type.", "category": "warning"}]
        if not text:
            return [{"message": "Enter some status text.", "category": "warning"}]
        if len(text) > MAX_TEXT:
            return [
                {"message": f"Status text is limited to {MAX_TEXT} characters.", "category": "warning"}
            ]

        # Streaming is the odd one out: setfunc() reads the second tuple slot as
        # the stream URL rather than a presence state.
        if activity == "streaming":
            url = (field("stream_url") or "").strip()
            if not url.startswith(("https://twitch.tv/", "https://www.twitch.tv/",
                                   "https://youtube.com/", "https://www.youtube.com/")):
                return [
                    {
                        "message": "Streaming needs a Twitch or YouTube URL, "
                        "otherwise Discord ignores it.",
                        "category": "warning",
                    }
                ]
            second = url
        else:
            second = field("presence_state") or "online"
            if second not in PRESENCE_STATES:
                return [{"message": "Unknown presence state.", "category": "warning"}]

        await self.config.status.set((activity, second, text))
        try:
            await self.setfunc(activity, second, text)
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not apply presence")
            return [{"message": f"Saved, but Discord rejected it: {exc}", "category": "warning"}]

        return [{"message": "Status updated and saved across restarts.", "category": "success"}]


BOTSTATUS_TEMPLATE = (
    BASE_CSS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-circle"></i> Presence for {{ bot_name }}</h4>
    <p>
      {% if configured %}A status is set and reapplied on every restart.
      {% else %}No persistent status is set.{% endif %}
    </p>
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />

    <div class="dz-panel">
      <div class="dz-grid two">
        <div>
          <div class="dz-label">Activity</div>
          <select class="dz-select" name="activity_type" id="bsActivity"
                  onchange="document.getElementById('bsStream').style.display =
                            this.value === 'streaming' ? 'block' : 'none';
                            document.getElementById('bsPresence').style.display =
                            this.value === 'streaming' ? 'none' : 'block';">
            {% for a in activity_types %}
              <option value="{{ a.key }}" {% if a.selected %}selected{% endif %}>{{ a.label }}</option>
            {% endfor %}
          </select>
        </div>

        <div id="bsPresence" style="display:{% if is_streaming %}none{% else %}block{% endif %};">
          <div class="dz-label">Presence</div>
          <select class="dz-select" name="presence_state">
            {% for s in presence_states %}
              <option value="{{ s.key }}" {% if s.selected %}selected{% endif %}>{{ s.label }}</option>
            {% endfor %}
          </select>
        </div>
      </div>

      <div class="dz-label" style="margin-top:12px;">Status text</div>
      <input class="dz-input" type="text" name="text" value="{{ text }}"
             maxlength="{{ max_text }}" placeholder="with {{ bot_name }}" />
      <div style="font-size:.72rem; opacity:.45; margin-top:5px;">
        Up to {{ max_text }} characters.
      </div>

      <div id="bsStream" style="display:{% if is_streaming %}block{% else %}none{% endif %}; margin-top:12px;">
        <div class="dz-label">Stream URL</div>
        <input class="dz-input" type="text" name="stream_url" value="{{ stream_url }}"
               placeholder="https://twitch.tv/yourchannel" />
        <div style="font-size:.72rem; opacity:.45; margin-top:5px;">
          Discord only shows the streaming badge for Twitch and YouTube links.
        </div>
      </div>

      <div class="dz-row" style="margin-top:14px;">
        <button class="dz-btn primary" name="action" value="save">
          <i class="fa fa-check"></i> Apply status
        </button>
        <button class="dz-btn danger" name="action" value="clear"
                onclick="return confirm('Clear the saved status?');">
          <i class="fa fa-times"></i> Clear
        </button>
      </div>
    </div>
  </form>
</div>
"""
)
