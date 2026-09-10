"""Every starboard setting, on one page.

Upstream had twenty prefix commands for these: one to set a threshold, one for
a colour, one each to add and remove from the allow and block lists. This fork
puts settings on the dashboard, and a starboard is mostly settings - so all of
it lives here, and the cog keeps three slash commands for what a member does.
"""
from __future__ import annotations

import logging
import typing as t

import discord

from redbot.core import commands
from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    EMOJI_UPLOAD_ASSETS,
    MACROS,
    channel_options,
    dashboard_page,
    decode_emoji_image,
    create_guild_emoji,
    emoji_cdn_url,
    emoji_problem,
    form_reader,
    guild_member,
    is_staff,
    role_options,
)

log = logging.getLogger("red.starboard.dashboard")

# What an embed's colour follows. Anything else stored is a raw colour int.
COLOUR_MODES = (
    ("user", "The starred person's colour"),
    ("bot", "My colour"),
    ("custom", "A colour I pick"),
)


class DashboardIntegration:
    """Starboards, end to end."""

    bot: t.Any
    config: t.Any
    starboards: dict

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Set up the starboards for this server.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_starboard_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        staff = await is_staff(self.bot, user, member, guild)
        if not staff:
            return {
                "status": 0,
                "web_content": {
                    "source": "<p>You do not have permission to manage starboards here.</p>",
                },
            }

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._sb_handle_post(guild, kwargs)

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": STARBOARD_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "boards": self._sb_rows(guild),
                "channel_options": channel_options(guild, require_send=True),
                "role_options": role_options(guild),
                "colour_modes": COLOUR_MODES,
            },
        }

    # ---------------- reading ----------------

    def _sb_rows(self, guild: discord.Guild) -> list[dict]:
        rows = []
        for name, board in sorted(self.starboards.get(guild.id, {}).items()):
            channel = guild.get_channel(board.channel)
            colour = board.colour
            if colour in ("user", "member", "author"):
                mode, custom = "user", ""
            elif colour == "bot":
                mode, custom = "bot", ""
            else:
                try:
                    mode, custom = "custom", f"#{int(colour):06x}"
                except (TypeError, ValueError):
                    mode, custom = "user", ""
            rows.append(
                {
                    "name": name,
                    "channel_id": str(board.channel or ""),
                    "channel_name": channel.name if channel else "",
                    "channel_missing": channel is None,
                    "emoji": str(board.emoji),
                    "emoji_image": emoji_cdn_url(str(board.emoji)),
                    "threshold": board.threshold,
                    "enabled": bool(board.enabled),
                    "selfstar": bool(board.selfstar),
                    "autostar": bool(board.autostar),
                    "inherit": bool(board.inherit),
                    "colour_mode": mode,
                    "colour_custom": custom,
                    "allow": [str(i) for i in board.whitelist],
                    "block": [str(i) for i in board.blacklist],
                    "starred": board.starred_messages,
                    "stars": board.stars_added,
                    "channels": channel_options(guild, require_send=False),
                    "roles": role_options(guild),
                }
            )
        return rows

    # ---------------- writing ----------------

    async def _sb_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")
        try:
            if action == "create":
                return await self._sb_create(guild, field)
            if action == "save":
                return await self._sb_save(guild, field)
            if action == "delete":
                return await self._sb_delete(guild, field)
            if action == "cleanup":
                return await self._sb_cleanup(guild)
        except Exception as e:  # noqa: BLE001
            log.exception("Starboard dashboard action %s failed in %s", action, guild.id)
            return [{"message": f"That did not work: {e}", "category": "danger"}]
        return []

    async def _sb_create(self, guild: discord.Guild, field) -> list[dict]:
        from .starboard_entry import StarboardEntry

        name = (field("name") or "").strip().lower()
        if not name:
            return [{"message": "Give the starboard a name.", "category": "warning"}]
        if len(name) > 32 or not name.replace("-", "").replace("_", "").isalnum():
            return [
                {
                    "message": "Names are up to 32 letters, numbers, dashes or underscores.",
                    "category": "warning",
                }
            ]
        self.starboards.setdefault(guild.id, {})
        if name in self.starboards[guild.id]:
            return [{"message": f"There is already a starboard called {name}.",
                     "category": "warning"}]

        channel = guild.get_channel(field.integer("channel", 0) or 0)
        if channel is None:
            return [{"message": "Pick a channel for it.", "category": "warning"}]
        permissions = channel.permissions_for(guild.me)
        if not (permissions.send_messages and permissions.embed_links):
            return [
                {
                    "message": f"I need Send Messages and Embed Links in #{channel.name}.",
                    "category": "warning",
                }
            ]

        emoji, problem = await self._sb_read_emoji(guild, field, "\N{WHITE MEDIUM STAR}")
        if problem:
            return [{"message": problem, "category": "warning"}]

        entry = StarboardEntry(name=name, guild=guild.id, channel=channel.id, emoji=emoji)
        self.starboards[guild.id][name] = entry
        await self._save_starboards(guild)
        return [
            {
                "message": f"Starboard {name} posts to #{channel.name} at {emoji}.",
                "category": "success",
            }
        ]

    async def _sb_read_emoji(
        self, guild: discord.Guild, field, fallback: str, key: str = ""
    ) -> tuple[str, str]:
        """The emoji from an upload, a picker, or a typed one. (emoji, problem)."""
        suffix = f"_{key}" if key else ""
        uploaded = field(f"img_emoji{suffix}") or ""
        if uploaded:
            raw, why = decode_emoji_image(uploaded)
            if raw is None:
                return "", f"That picture could not be used: {why}."
            token, why = await create_guild_emoji(
                guild, f"sb_{key or 'new'}", raw, reason="Starboard emoji"
            )
            if token is None:
                return "", why
            return token, ""
        picked = (field(f"emoji_pick{suffix}") or "").strip()
        typed = (field(f"emoji{suffix}") or "").strip()
        chosen = picked or typed or fallback
        if chosen == fallback:
            return fallback, ""
        partial = discord.PartialEmoji.from_str(chosen)
        if partial.id is None and len(chosen) > 8:
            return "", f"{chosen!r} is not an emoji: {emoji_problem(chosen)}."
        return chosen, ""

    async def _sb_save(self, guild: discord.Guild, field) -> list[dict]:
        name = (field("name") or "").strip().lower()
        board = self.starboards.get(guild.id, {}).get(name)
        if board is None:
            return [{"message": "That starboard no longer exists.", "category": "warning"}]

        notes = []
        channel = guild.get_channel(field.integer("channel", 0) or 0)
        if channel is not None:
            permissions = channel.permissions_for(guild.me)
            if not (permissions.send_messages and permissions.embed_links):
                notes.append(
                    {
                        "message": f"I need Send Messages and Embed Links in #{channel.name},"
                        " so the channel was left alone.",
                        "category": "warning",
                    }
                )
            else:
                board.channel = channel.id

        emoji, problem = await self._sb_read_emoji(guild, field, str(board.emoji), key=name)
        if problem:
            notes.append({"message": problem, "category": "warning"})
        else:
            board.emoji = discord.PartialEmoji.from_str(emoji)

        threshold = field.integer("threshold", board.threshold) or 1
        board.threshold = max(1, min(threshold, 10_000))
        board.enabled = field.checked("enabled")
        board.selfstar = field.checked("selfstar")
        board.autostar = field.checked("autostar")
        board.inherit = field.checked("inherit")

        mode = (field("colour_mode") or "user").strip()
        if mode == "custom":
            raw = (field("colour_custom") or "").strip().lstrip("#")
            try:
                board.colour = int(raw, 16)
            except ValueError:
                notes.append(
                    {"message": f"{raw!r} is not a colour, so it was left alone.",
                     "category": "warning"}
                )
        elif mode == "bot":
            board.colour = "bot"
        else:
            board.colour = "user"

        board.whitelist = [int(i) for i in field.many("allow") if str(i).isdigit()]
        board.blacklist = [int(i) for i in field.many("block") if str(i).isdigit()]

        await self._save_starboards(guild)
        return notes + [{"message": f"Saved {name}.", "category": "success"}]

    async def _sb_delete(self, guild: discord.Guild, field) -> list[dict]:
        name = (field("name") or "").strip().lower()
        if name not in self.starboards.get(guild.id, {}):
            return [{"message": "That starboard no longer exists.", "category": "warning"}]
        del self.starboards[guild.id][name]
        # to_json only writes what is in the cache, so the stored copy has to
        # go explicitly or a deleted starboard comes back on the next save.
        async with self.config.guild(guild).starboards() as stored:
            stored.pop(name, None)
        return [{"message": f"Deleted {name}.", "category": "success"}]

    async def _sb_cleanup(self, guild: discord.Guild) -> list[dict]:
        """Drop starboards whose channel is gone, and dead ids from the lists."""
        boards = self.starboards.get(guild.id, {})
        if not boards:
            return [{"message": "There are no starboards here.", "category": "info"}]
        removed_boards = []
        removed_ids = 0
        for name in list(boards):
            board = boards[name]
            if guild.get_channel(board.channel) is None:
                del boards[name]
                async with self.config.guild(guild).starboards() as stored:
                    stored.pop(name, None)
                removed_boards.append(name)
                continue
            for attr in ("whitelist", "blacklist"):
                kept = [
                    i
                    for i in getattr(board, attr)
                    if guild.get_channel(i) is not None or guild.get_role(i) is not None
                ]
                removed_ids += len(getattr(board, attr)) - len(kept)
                setattr(board, attr, kept)
        await self._save_starboards(guild)
        parts = []
        if removed_boards:
            parts.append(f"dropped {', '.join(removed_boards)} (channel gone)")
        if removed_ids:
            parts.append(f"cleared {removed_ids} deleted roles or channels")
        return [
            {
                "message": "Cleanup: " + ("; ".join(parts) if parts else "nothing to do"),
                "category": "success" if parts else "info",
            }
        ]


STARBOARD_TEMPLATE = (
    BASE_CSS
    + MACROS
    + EMOJI_UPLOAD_ASSETS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-star"></i> Starboards in {{ guild_name }}</h4>
    <p>React to a message with a starboard's emoji and, once enough people have,
       it gets posted to that starboard's channel and kept.</p>
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-plus"></i> New starboard</h5>
      <div class="dz-grid three">
        <div>
          <label class="dz-label">Name</label>
          <input class="dz-input" name="name" placeholder="starboard" maxlength="32" />
        </div>
        <div>
          <label class="dz-label">Posts to</label>
          {{ picker('channel', channel_options, false, 8, 'Search channels...') }}
        </div>
        <div>
          <label class="dz-label">Emoji</label>
          <div class="dz-up-row">
            <input class="dz-input" name="emoji" placeholder="⭐ or <:name:id>" />
            {{ emoji_upload('emoji') }}
          </div>
        </div>
      </div>
      <div class="dz-save">
        <button class="dz-btn primary" name="action" value="create">
          <i class="fa fa-plus"></i> Create
        </button>
      </div>
    </div>
  </form>

  {% for b in boards %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <input type="hidden" name="name" value="{{ b.name }}" />
      <div class="dz-panel">
        <h5>
          {% if b.emoji_image %}<img class="dz-emoji" src="{{ b.emoji_image }}" alt="" />
          {% else %}{{ b.emoji }}{% endif %}
          {{ b.name }}
          {% if not b.enabled %}<span class="dz-tag">off</span>{% endif %}
          {% if b.channel_missing %}<span class="dz-tag bad">channel gone</span>{% endif %}
        </h5>
        <p class="dz-hint">
          {{ b.starred }} messages starred, {{ b.stars }} stars given.
        </p>

        <div class="dz-grid three">
          <div>
            <label class="dz-label">Posts to</label>
            {{ picker('channel', b.channels, false, 8, 'Search channels...') }}
          </div>
          <div>
            <label class="dz-label">Emoji</label>
            <div class="dz-up-row">
              <input class="dz-input" name="emoji_{{ b.name }}" value="{{ b.emoji }}" />
              {{ emoji_upload('emoji_' ~ b.name, b.emoji_image) }}
            </div>
          </div>
          <div>
            <label class="dz-label">Stars needed</label>
            <input class="dz-input" type="number" min="1" max="10000"
                   name="threshold" value="{{ b.threshold }}" />
          </div>
        </div>

        <div class="dz-grid two" style="margin-top:12px;">
          <div>
            <label class="dz-label">Roles and channels that may star</label>
            {{ picker('allow', b.roles + b.channels, true, 8, 'Leave empty for everyone') }}
          </div>
          <div>
            <label class="dz-label">Roles and channels that may not</label>
            {{ picker('block', b.roles + b.channels, true, 8, 'Leave empty for nobody') }}
          </div>
        </div>

        <div class="dz-grid two" style="margin-top:12px;">
          <div>
            <label class="dz-label">Embed colour</label>
            <select class="dz-select" name="colour_mode">
              {% for value, label in colour_modes %}
                <option value="{{ value }}"
                        {% if b.colour_mode == value %}selected{% endif %}>{{ label }}</option>
              {% endfor %}
            </select>
            <input class="dz-input" name="colour_custom" style="margin-top:6px;"
                   value="{{ b.colour_custom }}" placeholder="#ffcc00" />
          </div>
          <div>
            <label class="dz-toggle">
              <input type="checkbox" name="enabled" {% if b.enabled %}checked{% endif %} />
              <span>Switched on</span>
            </label>
            <label class="dz-toggle">
              <input type="checkbox" name="selfstar" {% if b.selfstar %}checked{% endif %} />
              <span>People may star their own messages</span>
            </label>
            <label class="dz-toggle">
              <input type="checkbox" name="autostar" {% if b.autostar %}checked{% endif %} />
              <span>React with the emoji myself when something is starred</span>
            </label>
            <label class="dz-toggle">
              <input type="checkbox" name="inherit" {% if b.inherit %}checked{% endif %} />
              <span>Use the source channel's allow and block lists as well</span>
            </label>
          </div>
        </div>

        <div class="dz-save">
          <button class="dz-btn primary" name="action" value="save">
            <i class="fa fa-save"></i> Save
          </button>
          {{ confirm('Delete', 'delete', 'Delete the ' ~ b.name ~ ' starboard? Messages already posted stay where they are.') }}
        </div>
      </div>
    </form>
  {% else %}
    <div class="dz-panel">
      <p class="dz-empty">No starboards yet.</p>
    </div>
  {% endfor %}

  {% if boards %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-broom"></i> Tidy up</h5>
        <p class="dz-hint">
          Drops starboards whose channel has been deleted, and clears roles and
          channels that no longer exist from the allow and block lists.
        </p>
        <div class="dz-save">
          <button class="dz-btn" name="action" value="cleanup">
            <i class="fa fa-broom"></i> Clean up
          </button>
        </div>
      </div>
    </form>
  {% endif %}
</div>
"""
)
