from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    channel_options,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
    role_options,
)

log = logging.getLogger("red.commandlock.dashboard")


class DashboardIntegration:
    """Restrict cogs and commands to specific channels."""

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering CommandLock as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Lock cogs and commands to chosen channels.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_commandlock_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        if not await is_staff(self.bot, user, member, guild):
            return {
                "status": 1,
                "error_title": "Forbidden",
                "error_message": "Only server administrators can change command locks.",
            }

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._cl_handle_post(guild, kwargs)

        data = await self.config.guild(guild).all()
        cog_locks = data.get("cog_locks") or {}
        command_locks = data.get("command_locks") or {}

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": COMMANDLOCK_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "cog_locks": self._cl_rows(guild, cog_locks),
                "command_locks": self._cl_rows(guild, command_locks),
                "all_cogs": sorted(self.bot.cogs),
                "all_commands": sorted(c.qualified_name for c in self.bot.walk_commands())[:400],
                "channels": channel_options(guild),
                "roles": role_options(guild),
                "whitelisted": [str(i) for i in (data.get("whitelisted_roles") or [])],
                "delete_after": data.get("delete_after", 30),
                "threads_bypass": bool(data.get("threads_bypass")),
            },
        }

    def _cl_rows(self, guild: discord.Guild, locks: dict) -> list[dict]:
        rows = []
        for name, channel_ids in sorted(locks.items()):
            channels = []
            for channel_id in channel_ids or []:
                channel = guild.get_channel(channel_id)
                channels.append(
                    {"name": f"#{channel.name}" if channel else f"(deleted {channel_id})"}
                )
            rows.append({"name": name, "channels": channels, "count": len(channels)})
        return rows

    async def _cl_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")
        conf = self.config.guild(guild)

        try:
            if action in ("lock_cog", "lock_command"):
                key = "cog_locks" if action == "lock_cog" else "command_locks"
                name = (field("name") or "").strip()
                channel_ids = [int(x) for x in field.many("channels") if str(x).isdigit()]
                if not name:
                    return [{"message": "Pick something to lock.", "category": "warning"}]
                if not channel_ids:
                    return [
                        {
                            "message": "Pick at least one channel, or use Unlock to remove it.",
                            "category": "warning",
                        }
                    ]
                # Verify the target still exists, otherwise the lock is dead weight.
                if action == "lock_cog" and self.bot.get_cog(name) is None:
                    return [{"message": f"No loaded cog named '{name}'.", "category": "warning"}]
                if action == "lock_command" and self.bot.get_command(name) is None:
                    return [{"message": f"No command named '{name}'.", "category": "warning"}]

                async with conf.get_attr(key)() as locks:
                    locks[name] = channel_ids
                return [
                    {
                        "message": f"'{name}' is now limited to {len(channel_ids)} channel(s).",
                        "category": "success",
                    }
                ]

            if action in ("unlock_cog", "unlock_command"):
                key = "cog_locks" if action == "unlock_cog" else "command_locks"
                name = (field("name") or "").strip()
                async with conf.get_attr(key)() as locks:
                    if name not in locks:
                        return [{"message": f"'{name}' was not locked.", "category": "warning"}]
                    del locks[name]
                return [{"message": f"'{name}' unlocked.", "category": "success"}]

            if action == "save_options":
                raw = (field("delete_after") or "").strip()
                try:
                    seconds = int(raw) if raw else 0
                except ValueError:
                    return [{"message": f"'{raw}' is not a number.", "category": "danger"}]
                await conf.delete_after.set(max(0, seconds))
                await conf.threads_bypass.set(field.checked("threads_bypass"))
                role_ids = [int(x) for x in field.many("whitelisted_roles") if str(x).isdigit()]
                await conf.whitelisted_roles.set(role_ids)
                return [{"message": "Options saved.", "category": "success"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("CommandLock dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]


COMMANDLOCK_TEMPLATE = (
    BASE_CSS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-lock"></i> Command locks in {{ guild_name }}</h4>
    <p>
      {{ cog_locks|length }} cog lock(s), {{ command_locks|length }} command lock(s).
      A locked item only works in the channels you list.
    </p>
  </div>

  <div class="dz-grid two">
    <div class="dz-panel">
      <h5><i class="fa fa-cubes"></i> Cog locks</h5>
      {% if cog_locks %}
        <table class="dz-t">
          <thead><tr><th>Cog</th><th>Allowed in</th><th></th></tr></thead>
          <tbody>
            {% for row in cog_locks %}
              <tr>
                <td><b>{{ row.name }}</b></td>
                <td>
                  {% for c in row.channels %}<span class="dz-tag">{{ c.name }}</span> {% endfor %}
                </td>
                <td style="width:1%;">
                  <form method="POST">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                    <input type="hidden" name="name" value="{{ row.name }}" />
                    <button class="dz-btn round danger" name="action" value="unlock_cog"
                            title="Unlock"><i class="fa fa-unlock"></i></button>
                  </form>
                </td>
              </tr>
            {% endfor %}
          </tbody>
        </table>
      {% else %}
        <p class="dz-empty">No cogs locked.</p>
      {% endif %}

      <form method="POST" style="margin-top:11px;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <div class="dz-label">Cog</div>
        <select class="dz-select" name="name">
          {% for c in all_cogs %}<option value="{{ c }}">{{ c }}</option>{% endfor %}
        </select>
        <div class="dz-label" style="margin-top:9px;">Allowed channels</div>
        <select class="dz-select" name="channels" multiple size="6">
          {% for c in channels %}<option value="{{ c.id }}">{{ c.name }}</option>{% endfor %}
        </select>
        <button class="dz-btn primary" name="action" value="lock_cog" style="margin-top:9px;">
          <i class="fa fa-lock"></i> Lock cog
        </button>
      </form>
    </div>

    <div class="dz-panel">
      <h5><i class="fa fa-terminal"></i> Command locks</h5>
      {% if command_locks %}
        <table class="dz-t">
          <thead><tr><th>Command</th><th>Allowed in</th><th></th></tr></thead>
          <tbody>
            {% for row in command_locks %}
              <tr>
                <td><code>{{ row.name }}</code></td>
                <td>
                  {% for c in row.channels %}<span class="dz-tag">{{ c.name }}</span> {% endfor %}
                </td>
                <td style="width:1%;">
                  <form method="POST">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                    <input type="hidden" name="name" value="{{ row.name }}" />
                    <button class="dz-btn round danger" name="action" value="unlock_command"
                            title="Unlock"><i class="fa fa-unlock"></i></button>
                  </form>
                </td>
              </tr>
            {% endfor %}
          </tbody>
        </table>
      {% else %}
        <p class="dz-empty">No commands locked.</p>
      {% endif %}

      <form method="POST" style="margin-top:11px;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <div class="dz-label">Command</div>
        <select class="dz-select" name="name">
          {% for c in all_commands %}<option value="{{ c }}">{{ c }}</option>{% endfor %}
        </select>
        <div class="dz-label" style="margin-top:9px;">Allowed channels</div>
        <select class="dz-select" name="channels" multiple size="6">
          {% for c in channels %}<option value="{{ c.id }}">{{ c.name }}</option>{% endfor %}
        </select>
        <button class="dz-btn primary" name="action" value="lock_command" style="margin-top:9px;">
          <i class="fa fa-lock"></i> Lock command
        </button>
      </form>
    </div>
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-cog"></i> Options</h5>
      <div class="dz-grid two">
        <div>
          <div class="dz-label">Delete the warning after (seconds)</div>
          <input class="dz-input" type="number" min="0" name="delete_after"
                 value="{{ delete_after }}" />
          <div style="font-size:.72rem; opacity:.45; margin-top:4px;">0 keeps it forever.</div>
          <label class="dz-toggle" style="margin-top:9px;">
            <input type="checkbox" name="threads_bypass" {% if threads_bypass %}checked{% endif %} />
            <span>Threads ignore all locks</span>
          </label>
        </div>
        <div>
          <div class="dz-label">Roles that bypass every lock</div>
          <select class="dz-select" name="whitelisted_roles" multiple size="7">
            {% for r in roles %}
              <option value="{{ r.id }}" {% if r.id in whitelisted %}selected{% endif %}>
                {{ r.name }}
              </option>
            {% endfor %}
          </select>
        </div>
      </div>
      <div style="margin-top:12px;">
        <button class="dz-btn primary" name="action" value="save_options">
          <i class="fa fa-save"></i> Save options
        </button>
      </div>
    </div>
  </form>
</div>
"""
)
