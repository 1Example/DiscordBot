from __future__ import annotations

import logging
import types
import typing as t

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    dashboard_page,
    form_reader,
    guild_member,
    is_staff,
)

log = logging.getLogger("red.alias.dashboard")


class DashboardIntegration:
    """Full alias management from the dashboard.

    Covers everything ``[p]alias`` and ``[p]alias global`` can do: create, edit,
    delete and inspect both guild and global aliases.
    """

    bot: t.Any
    config: t.Any
    _aliases: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Alias as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Create, edit and remove command aliases.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_alias_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        staff = await is_staff(self.bot, user, member, guild)
        owner = await self.bot.is_owner(user)

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            if not staff:
                return {
                    "status": 1,
                    "error_title": "Forbidden",
                    "error_message": "Only server administrators can manage aliases.",
                }
            notifications = await self._alias_handle_post(member, guild, owner, kwargs)

        guild_aliases = await self._aliases.get_guild_aliases(guild)
        global_aliases = await self._aliases.get_global_aliases()

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": ALIAS_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_staff": staff,
                "is_owner": owner,
                "prefix": (await self.bot.get_valid_prefixes(guild))[0],
                "guild_aliases": [
                    self._alias_row(a, guild) for a in sorted(guild_aliases, key=lambda a: a.name)
                ],
                "global_aliases": [
                    self._alias_row(a, guild) for a in sorted(global_aliases, key=lambda a: a.name)
                ],
            },
        }

    def _alias_row(self, alias, guild: discord.Guild) -> dict:
        creator = guild.get_member(alias.creator) or self.bot.get_user(alias.creator)
        command = alias.command
        if not isinstance(command, str):
            command = " ".join(command)
        return {
            "name": alias.name,
            "command": command,
            "uses": alias.uses,
            "creator": getattr(creator, "display_name", None) or f"ID {alias.creator}",
            # An alias containing `{0}`-style markers forwards positional arguments.
            "takes_args": "{" in command,
        }

    async def _alias_handle_post(
        self, member: discord.Member, guild: discord.Guild, owner: bool, kwargs: dict
    ) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")
        name = (field("alias_name") or "").strip()
        command = (field("alias_command") or "").strip()
        # Global aliases are bot-wide, so only the owner may touch them.
        global_ = field("scope") == "global"

        if global_ and not owner:
            return [
                {"message": "Only the bot owner can manage global aliases.", "category": "danger"}
            ]

        # AliasCache only ever reads `ctx.author.id` and `ctx.guild`, so a namespace
        # carrying those attributes is enough to reuse it from here.
        ctx = types.SimpleNamespace(author=member, guild=guild, bot=self.bot)

        try:
            if action == "add":
                if not name or not command:
                    return [
                        {
                            "message": "A name and a command are both required.",
                            "category": "warning",
                        }
                    ]
                if not self.is_valid_alias_name(name):
                    return [
                        {
                            "message": "Alias names cannot contain spaces or unprintable "
                            "characters.",
                            "category": "warning",
                        }
                    ]
                if self.is_command(name):
                    return [
                        {"message": f"`{name}` is already a bot command.", "category": "warning"}
                    ]
                if await self._aliases.get_alias(None if global_ else guild, name):
                    return [{"message": f"`{name}` already exists.", "category": "warning"}]
                given = command.split(" ")[0]
                if self.bot.get_command(given) is None:
                    return [
                        {
                            "message": f"`{given}` is not an existing command.",
                            "category": "warning",
                        }
                    ]
                await self._aliases.add_alias(ctx, name, command, global_=global_)
                return [{"message": f"Alias `{name}` created.", "category": "success"}]

            if action == "edit":
                if not command:
                    return [{"message": "A command is required.", "category": "warning"}]
                given = command.split(" ")[0]
                if self.bot.get_command(given) is None:
                    return [
                        {
                            "message": f"`{given}` is not an existing command.",
                            "category": "warning",
                        }
                    ]
                if await self._aliases.edit_alias(ctx, name, command, global_=global_):
                    return [{"message": f"Alias `{name}` updated.", "category": "success"}]
                return [{"message": f"`{name}` does not exist.", "category": "warning"}]

            if action == "delete":
                if await self._aliases.delete_alias(ctx, name, global_=global_):
                    return [{"message": f"Alias `{name}` deleted.", "category": "success"}]
                return [{"message": f"`{name}` does not exist.", "category": "warning"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("Alias dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]


ALIAS_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-terminal"></i> Aliases in {{ guild_name }}</h4>
    <p>Shortcuts for commands. Use <code>&#123;0&#125;</code>, <code>&#123;1&#125;</code>&hellip;
       inside the command to capture arguments typed after the alias.</p>
  </div>

  {{ stats([('Server aliases', guild_aliases|length),
            ('Global aliases', global_aliases|length),
            ('Total uses', guild_aliases|sum(attribute='uses')
                           + global_aliases|sum(attribute='uses'))]) }}

  {% if is_staff %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-plus"></i> New alias</h5>
        <p class="dz-hint">The command runs exactly as if it had been typed in chat.</p>
        <div class="dz-grid two">
          <div>
            <label class="dz-label">Alias name</label>
            <input class="dz-input" type="text" name="alias_name" placeholder="mute5" required />
          </div>
          <div>
            <label class="dz-label">Command</label>
            <input class="dz-input" type="text" name="alias_command"
                   placeholder="mute &#123;0&#125; 5m Spamming" required />
          </div>
        </div>
        <div class="dz-row" style="margin-top:12px;">
          <select class="dz-select" name="scope" style="max-width:220px;">
            <option value="guild">This server only</option>
            {% if is_owner %}<option value="global">Global (all servers)</option>{% endif %}
          </select>
          <button class="dz-btn primary" name="action" value="add">
            <i class="fa fa-plus"></i> Create alias
          </button>
        </div>
      </div>
    </form>
  {% endif %}

  <div class="dz-panel">
    <h5><i class="fa fa-list"></i> Server aliases</h5>
    <p class="dz-hint">Only usable in {{ guild_name }}.</p>
    {% if guild_aliases %}
      <table class="dz-t">
        <tr><th>Alias</th><th>Command</th><th>Uses</th><th>Created by</th>
            {% if is_staff %}<th></th>{% endif %}</tr>
        {% for a in guild_aliases %}
          <tr>
            <td><code>{{ prefix }}{{ a.name }}</code>
                {% if a.takes_args %}<span class="dz-tag">args</span>{% endif %}</td>
            <td>
              {% if is_staff %}
                <form method="POST" class="dz-row" style="gap:6px;">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="alias_name" value="{{ a.name }}" />
                  <input type="hidden" name="scope" value="guild" />
                  <input class="dz-input" type="text" name="alias_command"
                         value="{{ a.command }}" style="flex:1 1 220px;" />
                  <button class="dz-btn" name="action" value="edit" title="Save">
                    <i class="fa fa-save"></i>
                  </button>
                </form>
              {% else %}<code>{{ a.command }}</code>{% endif %}
            </td>
            <td>{{ a.uses }}</td>
            <td>{{ a.creator }}</td>
            {% if is_staff %}
              <td>
                <form method="POST">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="alias_name" value="{{ a.name }}" />
                  <input type="hidden" name="scope" value="guild" />
                  {{ confirm('', 'delete', 'Delete the alias ' ~ a.name ~ '?') }}
                </form>
              </td>
            {% endif %}
          </tr>
        {% endfor %}
      </table>
    {% else %}
      <p class="dz-empty">No server aliases yet.</p>
    {% endif %}
  </div>

  <div class="dz-panel">
    <h5><i class="fa fa-globe"></i> Global aliases</h5>
    <p class="dz-hint">Available in every server.
      {% if not is_owner %}Only the bot owner can change these.{% endif %}</p>
    {% if global_aliases %}
      <table class="dz-t">
        <tr><th>Alias</th><th>Command</th><th>Uses</th>{% if is_owner %}<th></th>{% endif %}</tr>
        {% for a in global_aliases %}
          <tr>
            <td><code>{{ prefix }}{{ a.name }}</code></td>
            <td>
              {% if is_owner %}
                <form method="POST" class="dz-row" style="gap:6px;">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="alias_name" value="{{ a.name }}" />
                  <input type="hidden" name="scope" value="global" />
                  <input class="dz-input" type="text" name="alias_command"
                         value="{{ a.command }}" style="flex:1 1 220px;" />
                  <button class="dz-btn" name="action" value="edit" title="Save">
                    <i class="fa fa-save"></i>
                  </button>
                </form>
              {% else %}<code>{{ a.command }}</code>{% endif %}
            </td>
            <td>{{ a.uses }}</td>
            {% if is_owner %}
              <td>
                <form method="POST">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="alias_name" value="{{ a.name }}" />
                  <input type="hidden" name="scope" value="global" />
                  {{ confirm('', 'delete', 'Delete the global alias ' ~ a.name ~ '?') }}
                </form>
              </td>
            {% endif %}
          </tr>
        {% endfor %}
      </table>
    {% else %}
      <p class="dz-empty">No global aliases yet.</p>
    {% endif %}
  </div>
</div>
"""
)
