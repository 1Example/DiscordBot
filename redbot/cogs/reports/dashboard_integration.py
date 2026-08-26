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

log = logging.getLogger("red.reports.dashboard")


class DashboardIntegration:
    """Report intake: output channel, on/off, and ticket numbering."""

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Reports as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Where member reports are delivered.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_reports_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        if not await is_staff(self.bot, user, member, guild):
            return {
                "status": 1,
                "error_title": "Forbidden",
                "error_message": "Only server administrators can configure reports.",
            }

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._rep_handle_post(guild, kwargs)

        settings = await self.config.guild(guild).all()
        channel_id = settings.get("output_channel")
        channel = guild.get_channel(channel_id) if channel_id else None

        can_post = bool(channel and channel.permissions_for(guild.me).send_messages)

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": REPORTS_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "active": bool(settings.get("active")),
                "next_ticket": settings.get("next_ticket", 1),
                "channels": channel_options(guild, selected=channel_id),
                "channel_name": f"#{channel.name}" if channel else None,
                "channel_missing": bool(channel_id) and channel is None,
                "can_post": can_post,
                "tickets": await self._rep_tickets(guild),
            },
        }

    async def _rep_tickets(self, guild: discord.Guild, limit: int = 50) -> list[dict]:
        """Every stored report for this server, newest ticket first."""
        try:
            stored = await self.config.custom("REPORT", guild.id).all()
        except Exception:  # noqa: BLE001
            log.exception("Could not read stored reports")
            return []
        rows = []
        for ticket_number, data in stored.items():
            report = (data or {}).get("report") or {}
            if not report:
                continue
            author_id = report.get("user_id")
            author = guild.get_member(author_id or 0)
            rows.append(
                {
                    "number": int(ticket_number) if str(ticket_number).isdigit() else 0,
                    "author": getattr(author, "display_name", None)
                    or f"ID {author_id}",
                    "author_id": str(author_id or ""),
                    "present": author is not None,
                    "text": report.get("report") or "",
                }
            )
        rows.sort(key=lambda r: -r["number"])
        return rows[:limit]

    async def _rep_reply(self, guild: discord.Guild, field) -> list[dict]:
        """DM the reporter, which is what `[p]report interact` is used for."""
        ticket = field.integer("ticket_number", 0) or 0
        message = (field("reply") or "").strip()
        if not message:
            return [{"message": "Write a reply first.", "category": "warning"}]
        report = await self.config.custom("REPORT", guild.id, ticket).report()
        if not report:
            return [{"message": f"Ticket #{ticket} does not exist.", "category": "warning"}]
        target = guild.get_member(report.get("user_id") or 0)
        if target is None:
            return [
                {"message": "That reporter is no longer in the server.",
                 "category": "warning"}
            ]
        embed = discord.Embed(
            title=f"About your report #{ticket} in {guild.name}",
            description=message,
            colour=await self.bot.get_embed_colour(guild.text_channels[0])
            if guild.text_channels
            else discord.Colour.blurple(),
        )
        try:
            await target.send(embed=embed)
        except discord.Forbidden:
            return [
                {"message": f"{target.display_name} has DMs disabled.",
                 "category": "warning"}
            ]
        return [
            {"message": f"Reply sent to {target.display_name}.", "category": "success"}
        ]

    async def _rep_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

        if action == "reply":
            try:
                return await self._rep_reply(guild, field)
            except Exception as exc:  # noqa: BLE001
                log.exception("Reports dashboard reply failed")
                return [{"message": f"Action failed: {exc}", "category": "danger"}]

        if action != "save":
            return [{"message": "Unknown action.", "category": "warning"}]

        conf = self.config.guild(guild)
        warnings: list[dict] = []

        raw = field("output_channel") or ""
        channel_id = int(raw) if raw.isdigit() else None
        active = field.checked("active")

        # Reports with nowhere to go are silently dropped, so refuse the
        # combination rather than accepting it.
        if active and channel_id is None:
            return [
                {
                    "message": "Pick an output channel before enabling reports, "
                    "otherwise submissions go nowhere.",
                    "category": "warning",
                }
            ]

        if channel_id is not None:
            channel = guild.get_channel(channel_id)
            if channel is None:
                return [{"message": "That channel no longer exists.", "category": "danger"}]
            if not channel.permissions_for(guild.me).send_messages:
                warnings.append(
                    {
                        "message": f"I cannot send messages in #{channel.name}, "
                        f"so reports will not be delivered.",
                        "category": "warning",
                    }
                )

        await conf.output_channel.set(channel_id)
        await conf.active.set(active)

        raw_ticket = (field("next_ticket") or "").strip()
        if raw_ticket:
            try:
                ticket = int(raw_ticket)
            except ValueError:
                warnings.append(
                    {"message": f"'{raw_ticket}' is not a number.", "category": "danger"}
                )
            else:
                if ticket < 1:
                    warnings.append(
                        {"message": "Ticket numbers start at 1.", "category": "danger"}
                    )
                else:
                    await conf.next_ticket.set(ticket)

        return warnings + [
            {
                "message": f"Reports {'enabled' if active else 'disabled'}.",
                "category": "success",
            }
        ]


REPORTS_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-flag"></i> Reports in {{ guild_name }}</h4>
    <p>
      {% if active %}Accepting reports{% if channel_name %} into {{ channel_name }}{% endif %}.
      {% else %}Reports are currently disabled.{% endif %}
      &middot; next ticket #{{ next_ticket }}
    </p>
  </div>

  {% if channel_missing %}
    <div class="dz-panel" style="border-color:rgba(255,90,90,.35);">
      <p style="margin:0; color:#ff8b8b;">
        <i class="fa fa-exclamation-circle"></i>
        The configured output channel no longer exists. Pick another one.
      </p>
    </div>
  {% elif active and not can_post %}
    <div class="dz-panel" style="border-color:rgba(240,170,60,.35);">
      <p style="margin:0; color:#f0aa3c;">
        <i class="fa fa-exclamation-triangle"></i>
        I cannot send messages in the output channel, so reports will not arrive.
      </p>
    </div>
  {% endif %}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-inbox"></i> Intake</h5>

      <label class="dz-toggle">
        <input type="checkbox" name="active" {% if active %}checked{% endif %} />
        <span>Accept reports from members</span>
      </label>

      <div class="dz-label" style="margin-top:11px;">Output channel</div>
      <select class="dz-select" name="output_channel">
        <option value="">&mdash; none &mdash;</option>
        {% for c in channels %}
          <option value="{{ c.id }}" {% if c.selected %}selected{% endif %}>{{ c.name }}</option>
        {% endfor %}
      </select>
      <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
        Members submit with the report command; each one lands here as a ticket.
      </div>

      <div class="dz-label" style="margin-top:11px;">Next ticket number</div>
      <input class="dz-input" type="number" min="1" name="next_ticket" value="{{ next_ticket }}" />
      <div style="font-size:.72rem; opacity:.45; margin-top:4px;">
        Change this only if you want to restart or skip the numbering.
      </div>

      <div style="margin-top:13px;">
        <button class="dz-btn primary" name="action" value="save">
          <i class="fa fa-save"></i> Save
        </button>
      </div>
    </div>
  </form>

  <div class="dz-panel">
    <h5><i class="fa fa-inbox"></i> Submitted reports</h5>
    <p class="dz-hint">Reply here to DM the reporter, the way
       <code>[p]report interact</code> opens a conversation in Discord.</p>
    {% if tickets %}
      {% for t in tickets %}
        <div style="padding:11px 0; border-bottom:1px solid rgba(255,255,255,.06);">
          <div class="dz-row">
            <b>#{{ t.number }}</b>
            <span class="dz-tag">{{ t.author }}</span>
            {% if not t.present %}<span class="dz-tag warn">left the server</span>{% endif %}
          </div>
          <div class="dz-text" style="margin:6px 0;">{{ t.text }}</div>
          {% if t.present %}
            <form method="POST" class="dz-row" style="gap:6px;">
              <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
              <input type="hidden" name="ticket_number" value="{{ t.number }}" />
              <input class="dz-input" type="text" name="reply"
                     placeholder="reply, sent to them by DM" style="flex:1 1 240px;" />
              <button class="dz-btn primary" name="action" value="reply">
                <i class="fa fa-paper-plane"></i> Send
              </button>
            </form>
          {% endif %}
        </div>
      {% endfor %}
    {% else %}
      <p class="dz-empty">No reports submitted yet.</p>
    {% endif %}
  </div>
</div>
"""
)
