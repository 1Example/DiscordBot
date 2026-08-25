from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
)

log = logging.getLogger("red.cleanup.dashboard")

# Documented here rather than exposed as controls: bulk deletion from a web
# form with no preview is a good way to lose messages by accident.
COMMANDS = (
    ("cleanup messages", "Delete a number of recent messages."),
    ("cleanup user", "Delete recent messages from one member."),
    ("cleanup text", "Delete messages containing given text."),
    ("cleanup bot", "Delete the bot's own messages and command invocations."),
    ("cleanup self", "Delete only the bot's messages."),
    ("cleanup before", "Delete messages sent before a given message."),
    ("cleanup after", "Delete messages sent after a given message."),
    ("cleanup between", "Delete messages between two messages."),
    ("cleanup duplicates", "Remove repeated messages."),
    ("cleanup spam", "Remove messages from members posting too fast."),
)


class DashboardIntegration:
    """Cleanup notification setting and a reference for the commands."""

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Cleanup as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Message cleanup settings.",
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
                "error_message": "Only server administrators can change cleanup settings.",
            }

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._cl_handle_post(guild, kwargs)

        notify = await self.config.guild(guild).notify()
        me = guild.me

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": CLEANUP_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "notify": bool(notify),
                "commands": COMMANDS,
                # Without this permission every cleanup command fails.
                "can_manage": bool(me and me.guild_permissions.manage_messages),
            },
        }

    async def _cl_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        if field("action") != "save":
            return [{"message": "Unknown action.", "category": "warning"}]
        await self.config.guild(guild).notify.set(field.checked("notify"))
        return [{"message": "Saved.", "category": "success"}]


CLEANUP_TEMPLATE = (
    BASE_CSS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-eraser"></i> Cleanup in {{ guild_name }}</h4>
    <p>Bulk message deletion is run from Discord; this page holds the setting.</p>
  </div>

  {% if not can_manage %}
    <div class="dz-panel" style="border-color:rgba(255,90,90,.35);">
      <p style="margin:0; color:#ff8b8b;">
        <i class="fa fa-exclamation-circle"></i>
        I do not have <b>Manage Messages</b> in this server, so every cleanup
        command will fail.
      </p>
    </div>
  {% endif %}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-cog"></i> Setting</h5>
      <label class="dz-toggle">
        <input type="checkbox" name="notify" {% if notify %}checked{% endif %} />
        <span>Post a summary after a cleanup</span>
      </label>
      <div style="font-size:.72rem; opacity:.45; margin-left:26px;">
        Sends a short message saying how many messages were removed. The message
        deletes itself shortly afterwards.
      </div>
      <div style="margin-top:13px;">
        <button class="dz-btn primary" name="action" value="save">
          <i class="fa fa-save"></i> Save
        </button>
      </div>
    </div>
  </form>

  <div class="dz-panel">
    <h5><i class="fa fa-terminal"></i> Commands</h5>
    <p class="dz-hint">
      Deletion is deliberately not exposed here &mdash; there is no preview or
      undo, so it stays where you can see the channel you are clearing.
    </p>
    <table class="dz-t">
      <thead><tr><th>Command</th><th>What it does</th></tr></thead>
      <tbody>
        {% for name, blurb in commands %}
          <tr>
            <td><code style="font-size:.78rem;">{{ name }}</code></td>
            <td style="opacity:.75;">{{ blurb }}</td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
    <p class="dz-hint" style="margin-top:9px;">
      Discord will not bulk-delete messages older than 14 days.
    </p>
  </div>
</div>
"""
)
