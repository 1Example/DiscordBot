from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    channel_options,
    role_options,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
)

log = logging.getLogger("red.infochannel.dashboard")

# Human-readable blurb for each counter the cog supports.
COUNTER_HELP = {
    "members": "Everyone, humans and bots.",
    "humans": "Members that are not bots.",
    "boosters": "Members currently boosting.",
    "bots": "Bot accounts.",
    "roles": "Number of roles in the server.",
    "channels": "Number of channels in the server.",
    "online": "Members showing as online.",
    "offline": "Members showing as offline.",
}


class DashboardIntegration:
    """Toggle counter channels and edit their name templates."""

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering InfoChannel as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Configure the server counter channels.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_infochannel_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        if not await is_staff(self.bot, user, member, guild):
            return {
                "status": 1,
                "error_title": "Forbidden",
                "error_message": "Only server administrators can change counter channels.",
            }

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._ic_handle_post(guild, kwargs)

        data = await self.config.guild(guild).all()
        names = data.get("channel_names") or {}
        enabled = data.get("enabled_channels") or {}
        ids = data.get("channel_ids") or {}

        counters = []
        for key in self.default_channel_names:
            channel = guild.get_channel(ids.get(key)) if ids.get(key) else None
            counters.append(
                {
                    "key": key,
                    "label": key.capitalize(),
                    "help": COUNTER_HELP.get(key, ""),
                    "enabled": bool(enabled.get(key)),
                    "template": names.get(key) or self.default_channel_names[key],
                    "channel": channel.name if channel else None,
                    # An enabled counter with no live channel means the channel
                    # was deleted manually in Discord.
                    "orphaned": bool(enabled.get(key)) and channel is None,
                }
            )

        category = guild.get_channel(data.get("category_id")) if data.get("category_id") else None

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": INFOCHANNEL_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "counters": counters,
                "category": category.name if category else None,
                "active": sum(1 for c in counters if c["enabled"]),
                "category_options": channel_options(
                    guild, kinds=("category",), selected=data.get("category_id")
                ),
                "category_missing": bool(data.get("category_id")) and category is None,
                "role_counters": await self._ic_role_counters(guild),
                "role_options": role_options(guild),
                "default_role_name": self.default_role["name"],
            },
        }

    async def _ic_role_counters(self, guild: discord.Guild) -> list[dict]:
        """Roles that have their own member-count channel."""
        rows = []
        for role_id, data in (await self.config.all_roles()).items():
            role = guild.get_role(role_id)
            if role is None or not (data or {}).get("enabled"):
                continue
            channel = guild.get_channel(data.get("channel_id") or 0)
            rows.append(
                {
                    "id": str(role.id),
                    "name": role.name,
                    "members": len(role.members),
                    "template": data.get("name") or self.default_role["name"],
                    "channel": getattr(channel, "name", ""),
                    # An enabled counter with no channel means it was deleted
                    # manually in Discord.
                    "orphaned": channel is None,
                }
            )
        rows.sort(key=lambda r: r["name"].lower())
        return rows

    async def _ic_handle_post(self, guild: discord.Guild, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

        if action in ("role_enable", "role_disable"):
            role = guild.get_role(field.integer("role_id", 0) or 0)
            if role is None:
                return [{"message": "Pick a role.", "category": "warning"}]
            enable = action == "role_enable"
            if enable:
                template = (field("role_name") or "").strip()
                if template:
                    if "{count}" not in template:
                        return [
                            {
                                "message": "The name template must contain {count}.",
                                "category": "warning",
                            }
                        ]
                    await self.config.role(role).name.set(template[:100])
            await self.config.role(role).enabled.set(enable)
            try:
                await self.make_infochannel(guild, channel_role=role)
            except Exception as exc:  # noqa: BLE001
                log.exception("InfoChannel role counter update failed")
                return [
                    {"message": f"Saved, but the channel could not be updated: {exc}",
                     "category": "warning"}
                ]
            verb = "enabled" if enable else "disabled"
            return [
                {"message": f"Counter for {role.name} {verb}.", "category": "success"}
            ]

        if action == "rebuild":
            try:
                await self.make_infochannel(guild)
                await self.update_infochannel(guild)
            except discord.Forbidden:
                return [
                    {"message": "I need Manage Channels to rebuild the counters.",
                     "category": "danger"}
                ]
            return [
                {"message": "Every enabled counter channel was rebuilt.",
                 "category": "success"}
            ]

        if action == "delete_all":
            try:
                await self.delete_all_infochannels(guild)
            except discord.Forbidden:
                return [
                    {"message": "I need Manage Channels to remove the counters.",
                     "category": "danger"}
                ]
            return [
                {"message": "All counter channels were deleted.", "category": "success"}
            ]

        if action != "save":
            return [{"message": "Unknown action.", "category": "warning"}]

        errors: list[dict] = []
        conf = self.config.guild(guild)

        async with conf.channel_names() as names:
            for key in self.default_channel_names:
                template = (field(f"n_{key}") or "").strip()
                if not template:
                    continue
                if "{count}" not in template:
                    errors.append(
                        {
                            "message": f"{key}: template must contain {{count}} - left unchanged.",
                            "category": "warning",
                        }
                    )
                    continue
                # Discord truncates channel names at 100 characters.
                names[key] = template[:100]

        # Where the counter channels live. Clearing this lets the cog create its
        # own "Server Stats" category the next time it builds them.
        raw_category = (field("category_id") or "").strip()
        chosen_category = int(raw_category) if raw_category.isdigit() else None
        if chosen_category is not None and guild.get_channel(chosen_category) is None:
            chosen_category = None
        previous_category = await conf.category_id()
        if chosen_category != previous_category:
            await conf.category_id.set(chosen_category)

        # Flipping the stored flag is not enough: the channel itself has to be
        # created or deleted, which is what `make_infochannel` does. Only the
        # counters that actually changed are rebuilt, because each rebuild is a
        # channel create or delete against the Discord API.
        changed: list[str] = []
        async with conf.enabled_channels() as enabled:
            for key in self.default_channel_names:
                wanted = field.checked(f"t_{key}")
                channel_id = await conf.channel_ids.get_raw(key, default=None)
                # A counter that is on but whose channel is gone needs rebuilding
                # too, otherwise it stays stuck showing "channel missing".
                orphaned = wanted and (
                    channel_id is None or guild.get_channel(channel_id) is None
                )
                if enabled.get(key) != wanted or orphaned:
                    changed.append(key)
                enabled[key] = wanted

        # Moving the category means every existing counter has to be recreated
        # inside the new one.
        if chosen_category != previous_category:
            changed = list(self.default_channel_names)

        try:
            for key in changed:
                await self.make_infochannel(guild, channel_type=key)
            await self.update_infochannel(guild)
        except discord.Forbidden:
            errors.append(
                {
                    "message": "Saved, but I need Manage Channels to create or remove "
                    "the counter channels.",
                    "category": "warning",
                }
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("InfoChannel update failed after a dashboard save")
            errors.append(
                {"message": f"Saved, but refreshing the channels failed: {exc}", "category": "warning"}
            )

        summary = (
            f"Counter settings saved; {len(changed)} channel(s) rebuilt."
            if changed
            else "Counter settings saved."
        )
        return errors + [{"message": summary, "category": "success"}]


INFOCHANNEL_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-bar-chart"></i> Counter channels in {{ guild_name }}</h4>
    <p>
      {{ active }} active{% if category %} &middot; category: <b>{{ category }}</b>{% endif %}.
      Discord rate-limits channel renames, so counters refresh at most every 5 minutes.
    </p>
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />

    <div class="dz-panel">
      <h5><i class="fa fa-folder"></i> Where the counters live</h5>
      <p class="dz-hint">
        Pick an existing category, or leave this empty and I will create one
        called "Server Stats". Changing it recreates every counter channel.
        {% if category_missing %}
          <b>The category that was set has been deleted.</b>
        {% endif %}
      </p>
      {{ picker('category_id', category_options, false, 8, 'Search categories...',
                true, 'create Server Stats for me') }}
    </div>

    <div class="dz-grid two">
      {% for c in counters %}
        <div class="dz-panel">
          <div style="display:flex; align-items:center; justify-content:space-between; gap:10px;">
            <label class="dz-toggle" style="padding:0;">
              <input type="checkbox" name="t_{{ c.key }}" {% if c.enabled %}checked{% endif %} />
              <b>{{ c.label }}</b>
            </label>
            {% if c.orphaned %}
              <span class="dz-tag" style="color:#f0aa3c;">channel missing</span>
            {% elif c.channel %}
              <span class="dz-tag">{{ c.channel }}</span>
            {% endif %}
          </div>
          <p class="dz-hint" style="margin:7px 0 8px;">{{ c.help }}</p>
          <input class="dz-input" type="text" name="n_{{ c.key }}" value="{{ c.template }}"
                 maxlength="100" />
          <div style="font-size:.72rem; opacity:.45; margin-top:5px;">
            Must include <code>{count}</code>
          </div>
        </div>
      {% endfor %}
    </div>

    <div class="dz-row dz-save">
      <button class="dz-btn primary" name="action" value="save">
        <i class="fa fa-save"></i> Save counters
      </button>
      {{ confirm('Rebuild channels', 'rebuild',
                 'Recreate every enabled counter channel? Existing ones are deleted and made again.',
                 '', 'fa-refresh') }}
      {{ confirm('Delete all counters', 'delete_all',
                 'Delete every counter channel and the category holding them?') }}
    </div>
  </form>

  <div class="dz-panel">
    <h5><i class="fa fa-users"></i> Role counters</h5>
    <p class="dz-hint">A channel that shows how many members hold a role.
       Use <code>&#123;count&#125;</code> and <code>&#123;role&#125;</code> in the name.</p>
    {% if role_counters %}
      <table class="dz-t">
        <tr><th>Role</th><th>Members</th><th>Channel</th><th>Name template</th><th></th></tr>
        {% for r in role_counters %}
          <tr>
            <td>{{ r.name }}</td>
            <td>{{ r.members }}</td>
            <td>
              {% if r.orphaned %}<span class="dz-tag warn">channel missing</span>
              {% else %}{{ r.channel }}{% endif %}
            </td>
            <td><code>{{ r.template }}</code></td>
            <td>
              <form method="POST">
                <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                <input type="hidden" name="role_id" value="{{ r.id }}" />
                {{ confirm('', 'role_disable',
                           'Remove the counter channel for ' ~ r.name ~ '?') }}
              </form>
            </td>
          </tr>
        {% endfor %}
      </table>
    {% else %}
      <p class="dz-empty">No role counters yet.</p>
    {% endif %}

    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-grid two" style="margin-top:12px;">
        <div>
          <label class="dz-label">Role</label>
          {{ picker('role_id', role_options, false, 6, 'Search roles...') }}
        </div>
        <div>
          <label class="dz-label">Channel name template</label>
          <input class="dz-input" type="text" name="role_name"
                 placeholder="{{ default_role_name }}" />
        </div>
      </div>
      <div class="dz-save">
        <button class="dz-btn primary" name="action" value="role_enable">
          <i class="fa fa-plus"></i> Add role counter
        </button>
      </div>
    </form>
  </div>
</div>
"""
)
