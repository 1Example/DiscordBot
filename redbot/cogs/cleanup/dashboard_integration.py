from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands
from redbot.core.utils.mod import mass_purge

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    channel_options,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
    member_options,
    message_preview,
)

log = logging.getLogger("red.cleanup.dashboard")

# Hard ceiling on a single dashboard operation. The commands allow more, but an
# unbounded purge triggered from a web form is a bad idea.
MAX_SCAN = 500
MAX_PREVIEW = 60

MODES = (
    ("recent", "Recent messages", "The last N messages in the channel."),
    ("user", "From a member", "Messages posted by one member."),
    ("text", "Containing text", "Messages containing a phrase."),
    ("bot", "Bot messages", "Messages from bots, including command invocations."),
    ("self", "My messages", "Only messages sent by this bot."),
    ("duplicates", "Duplicates", "Repeated identical messages."),
)


class DashboardIntegration:
    """Preview and run message cleanup, with the channel and filter chosen here."""

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Cleanup as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Preview and delete messages.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_cleanup_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        if not await is_staff(self.bot, user, member, guild):
            return {
                "status": 1,
                "error_title": "Forbidden",
                "error_message": "Only server administrators can run cleanup.",
            }

        notifications: list[dict] = []
        preview: list[dict] = []
        state = {
            "mode": "recent",
            "channel": None,
            "number": 25,
            "text": "",
            "target": None,
            "keep_pinned": True,
        }

        if kwargs.get("method") == "POST":
            notifications, preview, state = await self._cu_handle_post(guild, member, kwargs, state)

        selected_id = int(state["channel"]) if str(state["channel"] or "").isdigit() else None

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": CLEANUP_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "notify": bool(await self.config.guild(guild).notify()),
                "modes": [{"key": k, "label": lbl, "help": h} for k, lbl, h in MODES],
                "state": state,
                "channels": channel_options(
                    guild, kinds=("text", "voice"), selected=selected_id
                ),
                "members": member_options(guild),
                "preview": preview,
                "preview_count": len(preview),
                "deletable": sum(1 for m in preview if not m["old"]),
                "too_old": sum(1 for m in preview if m["old"]),
                "pinned": sum(1 for m in preview if m["pinned"]),
                "max_scan": MAX_SCAN,
                "can_manage": bool(guild.me and guild.me.guild_permissions.manage_messages),
            },
        }

    # ------------------------------------------------------------------ logic

    def _cu_read_state(self, field) -> dict:
        return {
            "mode": field("mode") or "recent",
            "channel": field("channel") or None,
            "number": max(1, min(MAX_SCAN, field.integer("number", 25) or 25)),
            "text": (field("text") or "").strip(),
            "target": field("target") or None,
            "keep_pinned": field.checked("keep_pinned"),
            # Message IDs bounding the scan, covering `[p]cleanup before`,
            # `after` and `between` in one form.
            "before_id": (field("before_id") or "").strip(),
            "after_id": (field("after_id") or "").strip(),
        }

    def _cu_check(self, guild: discord.Guild, state: dict):
        """Build the per-message predicate for the chosen mode."""
        mode = state["mode"]
        if mode == "user":
            target_id = int(state["target"]) if str(state["target"] or "").isdigit() else 0
            return lambda m: m.author.id == target_id
        if mode == "text":
            needle = state["text"].lower()
            return lambda m: needle in (m.content or "").lower()
        if mode == "bot":
            prefixes = tuple()
            return lambda m: m.author.bot
        if mode == "self":
            return lambda m: m.author.id == self.bot.user.id
        if mode == "duplicates":
            seen: set[tuple] = set()

            def check(m):
                key = (m.author.id, (m.content or "").strip())
                if not key[1]:
                    return False
                if key in seen:
                    return True
                seen.add(key)
                return False

            return check
        return lambda m: True

    async def _cu_collect(self, channel, state: dict) -> list[discord.Message]:
        check = self._cu_check(channel.guild, state)
        before = self._cu_bound(state["before_id"])
        after = self._cu_bound(state["after_id"])
        return await self.get_messages_for_deletion(
            channel=channel,
            # With an explicit range the count is a cap, not a target; passing
            # `number` alongside `after` would cut the range short.
            number=None if after else state["number"],
            check=check,
            limit=MAX_SCAN,
            before=before,
            after=after,
            delete_pinned=not state["keep_pinned"],
        )

    @staticmethod
    def _cu_bound(raw: str):
        """Turn a message ID into the timestamp `get_messages_for_deletion` wants."""
        if not raw or not raw.isdigit():
            return None
        return discord.utils.snowflake_time(int(raw))

    async def _cu_handle_post(self, guild, actor, kwargs: dict, state: dict):
        field = form_reader(kwargs)
        action = field("action")

        if action == "save_notify":
            await self.config.guild(guild).notify.set(field.checked("notify"))
            return [{"message": "Saved.", "category": "success"}], [], state

        state = self._cu_read_state(field)

        raw = state["channel"] or ""
        channel = guild.get_channel(int(raw)) if str(raw).isdigit() else None
        if channel is None:
            return (
                [{"message": "Pick a channel first.", "category": "warning"}],
                [],
                state,
            )

        perms = channel.permissions_for(guild.me)
        if not perms.read_message_history:
            return (
                [{"message": f"I cannot read history in #{channel.name}.", "category": "danger"}],
                [],
                state,
            )
        if state["mode"] == "user" and not str(state["target"] or "").isdigit():
            return ([{"message": "Pick a member.", "category": "warning"}], [], state)
        if state["mode"] == "text" and not state["text"]:
            return ([{"message": "Enter some text to match.", "category": "warning"}], [], state)

        try:
            messages = await self._cu_collect(channel, state)
        except discord.Forbidden:
            return (
                [{"message": "Discord refused to let me read that channel.", "category": "danger"}],
                [],
                state,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Cleanup preview failed")
            return ([{"message": f"Could not scan: {exc}", "category": "danger"}], [], state)

        if action == "preview":
            preview = [message_preview(m) for m in messages[:MAX_PREVIEW]]
            note = {
                "message": f"{len(messages)} message(s) match in #{channel.name}."
                + (f" Showing the first {MAX_PREVIEW}." if len(messages) > MAX_PREVIEW else ""),
                "category": "info" if messages else "warning",
            }
            return [note], preview, state

        if action == "delete":
            if not perms.manage_messages:
                return (
                    [{"message": f"I need Manage Messages in #{channel.name}.", "category": "danger"}],
                    [],
                    state,
                )
            if not messages:
                return ([{"message": "Nothing matched, nothing deleted.", "category": "info"}], [], state)

            reason = f"Cleanup ({state['mode']}) from the dashboard by {actor} ({actor.id})"
            try:
                await mass_purge(messages, channel, reason=reason)
            except discord.Forbidden:
                return (
                    [{"message": "Discord refused the deletion.", "category": "danger"}],
                    [],
                    state,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("Cleanup deletion failed")
                return ([{"message": f"Deletion failed: {exc}", "category": "danger"}], [], state)

            # The page has a Notify toggle; honour it here, or the setting
            # does nothing once the dashboard is the only way to clean up.
            try:
                await self.send_optional_notification(len(messages), channel)
            except discord.HTTPException:
                log.debug("Could not post the cleanup notification.", exc_info=True)
            log.info("Dashboard cleanup: %s removed %s messages from #%s (%s)",
                     actor, len(messages), channel.name, state["mode"])
            return (
                [
                    {
                        "message": f"Deleted {len(messages)} message(s) from #{channel.name}.",
                        "category": "success",
                    }
                ],
                [],
                state,
            )

        return ([{"message": f"Unknown action: {action}", "category": "warning"}], [], state)


CLEANUP_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-eraser"></i> Cleanup in {{ guild_name }}</h4>
    <p>Choose a channel and filter, preview exactly what matches, then delete.</p>
  </div>

  {% if not can_manage %}
    <div class="dz-panel" style="border-color:rgba(255,90,90,.35);">
      <p style="margin:0; color:#ff8b8b;">
        <i class="fa fa-exclamation-circle"></i>
        I do not have <b>Manage Messages</b> in this server, so deletion will fail.
      </p>
    </div>
  {% endif %}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-filter"></i> What to remove</h5>

      <div class="dz-grid two">
        <div>
          <div class="dz-label">Channel</div>
          {{ picker('channel', channels, allow_none=true, none_label='pick a channel',
                    placeholder='Search channels...') }}

          <div class="dz-label" style="margin-top:10px;">Filter</div>
          <select class="dz-select" name="mode"
                  onchange="var v=this.value;
                            document.getElementById('cuUser').style.display = v==='user'?'block':'none';
                            document.getElementById('cuText').style.display = v==='text'?'block':'none';">
            {% for m in modes %}
              <option value="{{ m.key }}" {% if state.mode == m.key %}selected{% endif %}>{{ m.label }}</option>
            {% endfor %}
          </select>
          <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
            {% for m in modes %}{% if m.key == state.mode %}{{ m.help }}{% endif %}{% endfor %}
          </div>
        </div>

        <div>
          <div class="dz-label">How many to remove</div>
          <input class="dz-input" type="number" min="1" max="{{ max_scan }}" name="number"
                 value="{{ state.number }}" />
          <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
            Up to {{ max_scan }} per run from the dashboard.
          </div>

          <div id="cuUser" style="margin-top:10px; display:{% if state.mode == 'user' %}block{% else %}none{% endif %};">
            <div class="dz-label">Member</div>
            {{ picker('target', members, allow_none=true, none_label='pick a member',
                      placeholder='Search members...') }}
          </div>

          <div id="cuText" style="margin-top:10px; display:{% if state.mode == 'text' %}block{% else %}none{% endif %};">
            <div class="dz-label">Text to match</div>
            <input class="dz-input" type="text" name="text" value="{{ state.text }}"
                   placeholder="case-insensitive" />
          </div>

          <label class="dz-toggle" style="margin-top:10px;">
            <input type="checkbox" name="keep_pinned" {% if state.keep_pinned %}checked{% endif %} />
            <span>Keep pinned messages</span>
          </label>

          <div style="margin-top:10px;">
            <div class="dz-label">Only messages in a range</div>
            <div class="dz-row">
              <input class="dz-input" type="text" name="after_id"
                     value="{{ state.after_id }}" placeholder="after this message ID"
                     style="flex:1 1 160px;" />
              <input class="dz-input" type="text" name="before_id"
                     value="{{ state.before_id }}" placeholder="before this message ID"
                     style="flex:1 1 160px;" />
            </div>
            <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
              Fill one for before or after, both for a window. Copy a message ID in
              Discord with developer mode on. Discord still refuses to bulk delete
              anything older than 14 days.
            </div>
          </div>
        </div>
      </div>

      <div class="dz-row" style="margin-top:13px;">
        <button class="dz-btn primary" name="action" value="preview">
          <i class="fa fa-search"></i> Preview matches
        </button>
        {% if preview_count %}
          <button class="dz-btn danger" name="action" value="delete"
                  onclick="return confirm('Permanently delete {{ deletable }} message(s)? This cannot be undone.');">
            <i class="fa fa-trash-o"></i> Delete {{ deletable }} message(s)
          </button>
        {% endif %}
      </div>
    </div>
  </form>

  {% if preview_count %}
    <div class="dz-panel">
      <h5><i class="fa fa-eye"></i> Preview</h5>
      {{ stats([('Matched', preview_count), ('Deletable', deletable),
                ('Too old', too_old), ('Pinned', pinned)]) }}
      {% if too_old %}
        <p class="dz-hint" style="margin-top:10px;">
          Discord will not bulk-delete messages older than 14 days; those are
          greyed out and will be skipped.
        </p>
      {% endif %}
      <div style="margin-top:11px;">{{ msglist(preview) }}</div>
    </div>
  {% endif %}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-cog"></i> Setting</h5>
      <label class="dz-toggle">
        <input type="checkbox" name="notify" {% if notify %}checked{% endif %} />
        <span>Post a summary in the channel after a cleanup</span>
      </label>
      <div style="margin-top:11px;">
        <button class="dz-btn primary" name="action" value="save_notify">
          <i class="fa fa-save"></i> Save
        </button>
      </div>
    </div>
  </form>
</div>
"""
)
