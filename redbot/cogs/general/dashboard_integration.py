from __future__ import annotations

import logging
import typing as t
from datetime import datetime, timezone

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    dashboard_page,
    guild_member,
)

log = logging.getLogger("red.general.dashboard")

VERIFICATION_LABELS = {
    "none": "None",
    "low": "Low - verified email",
    "medium": "Medium - registered for 5 minutes",
    "high": "High - member for 10 minutes",
    "highest": "Highest - verified phone",
}


class DashboardIntegration:
    """Everything ``[p]serverinfo`` reports, including its detailed mode.

    The cog's other commands are chat toys - dice, coin flips, rock paper
    scissors, the magic 8-ball - which have no purpose on a dashboard, so they
    are deliberately not mirrored here.
    """

    bot: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering General as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Detailed information about this server.",
        methods=("GET",),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_general_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error

        return {
            "status": 0,
            "web_content": {
                "source": GENERAL_TEMPLATE,
                "guild_name": guild.name,
                "info": self._gen_server_info(guild),
            },
        }

    def _gen_server_info(self, guild: discord.Guild) -> dict:
        online = sum(
            1
            for m in guild.members
            if m.status is not discord.Status.offline and not m.bot
        )
        bots = sum(1 for m in guild.members if m.bot)
        created = guild.created_at or datetime.now(timezone.utc)
        age_days = (datetime.now(timezone.utc) - created).days

        by_status = {
            "online": 0,
            "idle": 0,
            "dnd": 0,
            "offline": 0,
        }
        for m in guild.members:
            by_status[str(m.status)] = by_status.get(str(m.status), 0) + 1

        return {
            "name": guild.name,
            "id": str(guild.id),
            "icon": str(guild.icon.url) if guild.icon else "",
            "banner": str(guild.banner.url) if guild.banner else "",
            "description": guild.description or "",
            "owner": str(guild.owner) if guild.owner else "Unknown",
            "created": created.strftime("%d %b %Y"),
            "age_days": age_days,
            "members": guild.member_count or len(guild.members),
            "humans": len(guild.members) - bots,
            "bots": bots,
            "online": online,
            "by_status": by_status,
            "text_channels": len(guild.text_channels),
            "voice_channels": len(guild.voice_channels),
            "stage_channels": len(getattr(guild, "stage_channels", [])),
            "forums": len(getattr(guild, "forums", [])),
            "categories": len(guild.categories),
            "roles": len(guild.roles) - 1,
            "emojis": len(guild.emojis),
            "emoji_limit": guild.emoji_limit,
            "stickers": len(getattr(guild, "stickers", [])),
            "boosts": guild.premium_subscription_count or 0,
            "boost_tier": guild.premium_tier,
            "boosters": len(guild.premium_subscribers),
            "verification": VERIFICATION_LABELS.get(
                str(guild.verification_level), str(guild.verification_level)
            ),
            "content_filter": str(guild.explicit_content_filter).replace("_", " "),
            "mfa": "Required" if guild.mfa_level else "Not required",
            "locale": str(guild.preferred_locale),
            "afk_channel": guild.afk_channel.name if guild.afk_channel else "None",
            "afk_timeout": (guild.afk_timeout or 0) // 60,
            "rules_channel": guild.rules_channel.name if guild.rules_channel else "None",
            "system_channel": guild.system_channel.name if guild.system_channel else "None",
            "features": sorted(f.replace("_", " ").title() for f in guild.features),
            "vanity": guild.vanity_url_code or "",
            "shard": guild.shard_id,
            "large": guild.large,
        }


GENERAL_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-info-circle"></i> {{ guild_name }}</h4>
    <p>Everything <code>[p]serverinfo</code> reports, in one place.</p>
  </div>

  {{ stats([('Members', info.members),
            ('Humans', info.humans),
            ('Bots', info.bots),
            ('Roles', info.roles),
            ('Boosts', info.boosts)]) }}

  <div class="dz-panel">
    <h5><i class="fa fa-server"></i> Overview</h5>
    <div class="dz-grid two">
      <div>
        <table class="dz-t">
          <tr><th>Owner</th><td>{{ info.owner }}</td></tr>
          <tr><th>Server ID</th><td><code>{{ info.id }}</code></td></tr>
          <tr><th>Created</th><td>{{ info.created }} ({{ info.age_days }} days ago)</td></tr>
          <tr><th>Shard</th><td>{{ info.shard }}</td></tr>
          <tr><th>Locale</th><td>{{ info.locale }}</td></tr>
          {% if info.vanity %}
            <tr><th>Vanity URL</th><td><code>{{ info.vanity }}</code></td></tr>
          {% endif %}
          {% if info.description %}
            <tr><th>Description</th><td>{{ info.description }}</td></tr>
          {% endif %}
        </table>
      </div>
      <div>
        <table class="dz-t">
          <tr><th>Text channels</th><td>{{ info.text_channels }}</td></tr>
          <tr><th>Voice channels</th><td>{{ info.voice_channels }}</td></tr>
          <tr><th>Stage channels</th><td>{{ info.stage_channels }}</td></tr>
          <tr><th>Forums</th><td>{{ info.forums }}</td></tr>
          <tr><th>Categories</th><td>{{ info.categories }}</td></tr>
          <tr><th>Emojis</th><td>{{ info.emojis }} / {{ info.emoji_limit }}</td></tr>
          <tr><th>Stickers</th><td>{{ info.stickers }}</td></tr>
        </table>
      </div>
    </div>
  </div>

  <div class="dz-panel">
    <h5><i class="fa fa-shield"></i> Moderation and presence</h5>
    <div class="dz-grid two">
      <div>
        <table class="dz-t">
          <tr><th>Verification</th><td>{{ info.verification }}</td></tr>
          <tr><th>Content filter</th><td>{{ info.content_filter }}</td></tr>
          <tr><th>2FA for mods</th><td>{{ info.mfa }}</td></tr>
          <tr><th>Rules channel</th><td>{{ info.rules_channel }}</td></tr>
          <tr><th>System channel</th><td>{{ info.system_channel }}</td></tr>
          <tr><th>AFK</th>
              <td>{{ info.afk_channel }} after {{ info.afk_timeout }} min</td></tr>
        </table>
      </div>
      <div>
        <table class="dz-t">
          <tr><th>Online</th><td>{{ info.by_status.online }}</td></tr>
          <tr><th>Idle</th><td>{{ info.by_status.idle }}</td></tr>
          <tr><th>Do not disturb</th><td>{{ info.by_status.dnd }}</td></tr>
          <tr><th>Offline</th><td>{{ info.by_status.offline }}</td></tr>
          <tr><th>Boost tier</th>
              <td>{{ info.boost_tier }} ({{ info.boosters }} boosters)</td></tr>
        </table>
      </div>
    </div>
    {% if info.features %}
      <p class="dz-hint" style="margin-top:10px;">
        {% for f in info.features %}<span class="dz-tag">{{ f }}</span> {% endfor %}
      </p>
    {% endif %}
  </div>

</div>
"""
)
