from __future__ import annotations

import logging
import typing as t
from random import choice

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
    member_options,
)

log = logging.getLogger("red.insult.dashboard")


class DashboardIntegration:
    """``[p]insult`` without Discord.

    Rolls an insult for a chosen member, and can post it to a channel so the
    command never has to be typed in chat.
    """

    bot: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Insult as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Roll an insult, or send one to a channel.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_insult_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        staff = await is_staff(self.bot, user, member, guild)

        notifications: list[dict] = []
        preview = ""
        if kwargs.get("method") == "POST":
            notifications, preview = await self._insult_handle_post(member, guild, staff, kwargs)

        from .insult import insults

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": INSULT_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_staff": staff,
                "preview": preview,
                "count": len(insults),
                "member_options": member_options(guild, humans_only=True),
                "channel_options": channel_options(guild, require_send=True),
            },
        }

    async def _insult_handle_post(
        self, member: discord.Member, guild: discord.Guild, staff: bool, kwargs: dict
    ) -> tuple[list[dict], str]:
        from .insult import insults

        field = form_reader(kwargs)
        action = field("action")
        target = guild.get_member(field.integer("member_id", 0) or 0)
        mention = target.mention if target else member.mention
        text = f"{mention} {choice(insults)}"

        try:
            if action == "roll":
                return [], text

            if action == "send":
                if not staff:
                    return (
                        [
                            {
                                "message": "Only server administrators can post messages "
                                "from the dashboard.",
                                "category": "danger",
                            }
                        ],
                        "",
                    )
                channel = guild.get_channel(field.integer("channel_id", 0) or 0)
                if channel is None:
                    return [{"message": "Pick a channel.", "category": "warning"}], ""
                if not channel.permissions_for(guild.me).send_messages:
                    return (
                        [
                            {
                                "message": f"I cannot post in #{channel.name}.",
                                "category": "warning",
                            }
                        ],
                        "",
                    )
                await channel.send(text)
                return (
                    [{"message": f"Sent to #{channel.name}.", "category": "success"}],
                    text,
                )
        except discord.Forbidden:
            return [{"message": "Discord refused that message.", "category": "danger"}], ""
        except Exception as exc:  # noqa: BLE001
            log.exception("Insult dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}], ""

        return [{"message": f"Unknown action: {action}", "category": "warning"}], ""


INSULT_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-bolt"></i> Insult</h4>
    <p>{{ count }} lines in the pool. Roll one for a member, then post it if you like.</p>
  </div>

  {% if preview %}
    <div class="dz-panel">
      <h5><i class="fa fa-quote-left"></i> Result</h5>
      <div class="dz-text">{{ preview }}</div>
    </div>
  {% endif %}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-user"></i> Pick a target</h5>
      <p class="dz-hint">Leave the member empty to insult yourself, same as the command.</p>
      <div class="dz-grid two">
        <div>
          <label class="dz-label">Member</label>
          {{ picker('member_id', member_options, false, 8, 'Search members...',
                    true, 'me') }}
        </div>
        <div>
          <label class="dz-label">Channel to post in</label>
          {{ picker('channel_id', channel_options, false, 8, 'Search channels...',
                    true, 'preview only') }}
        </div>
      </div>
      <div class="dz-row dz-save">
        <button class="dz-btn primary" name="action" value="roll">
          <i class="fa fa-random"></i> Roll one
        </button>
        {% if is_staff %}
          <button class="dz-btn" name="action" value="send">
            <i class="fa fa-paper-plane"></i> Roll and post
          </button>
        {% endif %}
      </div>
    </div>
  </form>
</div>
"""
)
