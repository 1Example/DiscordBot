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

log = logging.getLogger("red.customcom.dashboard")

COOLDOWN_SCOPES = ("member", "channel", "guild")


class DashboardIntegration:
    """Custom command management from the dashboard.

    Mirrors ``[p]customcom`` in full: create simple and randomised commands,
    edit responses, set per-scope cooldowns, and delete.
    """

    bot: t.Any
    config: t.Any
    commandobj: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering CustomCommands as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Create and manage custom text commands.",
        methods=("GET", "POST"),
        context_ids=["guild_id", "user_id"],
    )
    async def dashboard_customcom_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = guild_member(user, guild)
        if error:
            return error
        staff = await is_staff(self.bot, user, member, guild)

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            if not staff:
                return {
                    "status": 1,
                    "error_title": "Forbidden",
                    "error_message": "Only server administrators can manage custom commands.",
                }
            notifications = await self._cc_handle_post(member, guild, kwargs)

        stored = await self.config.guild(guild).commands()
        rows = [
            self._cc_row(name, info, guild)
            for name, info in sorted(stored.items())
            if info
        ]

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": CUSTOMCOM_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "guild_name": guild.name,
                "is_staff": staff,
                "prefix": (await self.bot.get_valid_prefixes(guild))[0],
                "commands": rows,
                "randomised": sum(1 for r in rows if r["is_random"]),
                "with_cooldown": sum(1 for r in rows if r["cooldowns"]),
                "scopes": COOLDOWN_SCOPES,
            },
        }

    def _cc_row(self, name: str, info: dict, guild: discord.Guild) -> dict:
        response = info.get("response")
        responses = response if isinstance(response, list) else [response or ""]
        author_id = (info.get("author") or {}).get("id", 0)
        author = guild.get_member(author_id) or self.bot.get_user(author_id)
        return {
            "name": name,
            "responses": responses,
            "response_text": "\n".join(responses),
            "is_random": isinstance(response, list),
            "author": getattr(author, "display_name", None)
            or (info.get("author") or {}).get("name")
            or "Unknown",
            "created_at": (info.get("created_at") or "")[:16].replace("T", " "),
            "edited_at": (info.get("edited_at") or "")[:16].replace("T", " "),
            "editors": len(info.get("editors") or []),
            "cooldowns": info.get("cooldowns") or {},
            # `{0}`/`{author}` style markers mean the response is templated.
            "templated": any("{" in r for r in responses),
        }

    async def _cc_handle_post(
        self, member: discord.Member, guild: discord.Guild, kwargs: dict
    ) -> list[dict]:
        from .customcom import AlreadyExists, ArgParseError, NotFound, ResponseTooLong

        field = form_reader(kwargs)
        action = field("action")
        name = (field("cc_name") or "").strip().lower()
        raw = field("cc_response") or ""

        # `CommandObj` only reads `ctx.guild`, `ctx.message.author` and `ctx.cog`,
        # so a namespace carrying those is enough to reuse it from here.
        ctx = types.SimpleNamespace(
            guild=guild,
            cog=self,
            bot=self.bot,
            message=types.SimpleNamespace(author=member, guild=guild),
        )

        # A randomised command stores a list; one response per non-empty line.
        random = field.checked("cc_random")
        responses: t.Any
        if random:
            responses = [line.strip() for line in raw.splitlines() if line.strip()]
        else:
            responses = raw.strip()

        try:
            if action == "create":
                if not name:
                    return [{"message": "A command name is required.", "category": "warning"}]
                if any(char.isspace() for char in name):
                    return [
                        {
                            "message": "Command names cannot contain spaces.",
                            "category": "warning",
                        }
                    ]
                if not responses:
                    return [{"message": "A response is required.", "category": "warning"}]
                if name in (*self.bot.all_commands, *commands.RESERVED_COMMAND_NAMES):
                    return [
                        {
                            "message": f"`{name}` is already a bot command.",
                            "category": "warning",
                        }
                    ]
                await self.commandobj.create(ctx=ctx, command=name, response=responses)
                return [{"message": f"Custom command `{name}` created.", "category": "success"}]

            if action == "edit":
                if not responses:
                    return [{"message": "A response is required.", "category": "warning"}]
                await self.commandobj.edit(
                    ctx=ctx, command=name, response=responses, ask_for=False
                )
                return [{"message": f"Custom command `{name}` updated.", "category": "success"}]

            if action == "cooldown":
                scope = field("cc_scope") or "member"
                if scope not in COOLDOWN_SCOPES:
                    return [{"message": f"Unknown scope `{scope}`.", "category": "warning"}]
                seconds = field.integer("cc_seconds", 0) or 0
                # `edit` drops any cooldown that is set to zero or less.
                await self.commandobj.edit(
                    ctx=ctx, command=name, cooldowns={scope: seconds}, ask_for=False
                )
                if seconds > 0:
                    return [
                        {
                            "message": f"`{name}` is now limited to once every "
                            f"{seconds}s per {scope}.",
                            "category": "success",
                        }
                    ]
                return [
                    {"message": f"Cooldown per {scope} removed from `{name}`.",
                     "category": "success"}
                ]

            if action == "delete":
                await self.commandobj.delete(ctx=ctx, command=name)
                return [{"message": f"Custom command `{name}` deleted.", "category": "success"}]
        except AlreadyExists:
            return [{"message": f"`{name}` already exists.", "category": "warning"}]
        except NotFound:
            return [{"message": f"`{name}` does not exist.", "category": "warning"}]
        except ResponseTooLong:
            return [
                {"message": "A response cannot be longer than 2000 characters.",
                 "category": "warning"}
            ]
        except ArgParseError as exc:
            return [{"message": str(exc), "category": "warning"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("CustomCommands dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]


CUSTOMCOM_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-comment"></i> Custom commands in {{ guild_name }}</h4>
    <p>Text responses anyone can trigger. Use <code>&#123;0&#125;</code> for the first
       argument, <code>&#123;author.mention&#125;</code>, <code>&#123;server.name&#125;</code>
       and similar markers inside a response.</p>
  </div>

  {{ stats([('Commands', commands|length),
            ('Randomised', randomised),
            ('With cooldowns', with_cooldown)]) }}

  {% if is_staff %}
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <div class="dz-panel">
        <h5><i class="fa fa-plus"></i> New custom command</h5>
        <p class="dz-hint">Names are always lowercase and cannot clash with a bot command.</p>
        <div class="dz-grid two">
          <div>
            <label class="dz-label">Command name</label>
            <input class="dz-input" type="text" name="cc_name" placeholder="rules" required />
            <label class="dz-toggle">
              <input type="checkbox" name="cc_random" />
              <span>Randomised &mdash; pick one line at random</span>
            </label>
          </div>
          <div>
            <label class="dz-label">Response</label>
            <textarea class="dz-area" name="cc_response"
                      placeholder="Read the rules in #rules"></textarea>
          </div>
        </div>
        <div class="dz-save">
          <button class="dz-btn primary" name="action" value="create">
            <i class="fa fa-plus"></i> Create command
          </button>
        </div>
      </div>
    </form>
  {% endif %}

  {% if commands %}
    {% for c in commands %}
      <div class="dz-panel">
        <h5>
          <code>{{ prefix }}{{ c.name }}</code>
          {% if c.is_random %}<span class="dz-tag">random &times;{{ c.responses|length }}</span>{% endif %}
          {% if c.templated %}<span class="dz-tag">templated</span>{% endif %}
          {% for scope, rate in c.cooldowns.items() %}
            <span class="dz-tag warn">{{ rate }}s / {{ scope }}</span>
          {% endfor %}
        </h5>
        <p class="dz-hint">
          Created by {{ c.author }}{% if c.created_at %} on {{ c.created_at }}{% endif %}
          {%- if c.edited_at %}, last edited {{ c.edited_at }}{% endif %}
          {%- if c.editors %} &middot; {{ c.editors }} editor(s){% endif %}
        </p>

        {% if is_staff %}
          <form method="POST">
            <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
            <input type="hidden" name="cc_name" value="{{ c.name }}" />
            <label class="dz-label">Response{% if c.is_random %} (one per line){% endif %}</label>
            <textarea class="dz-area" name="cc_response">{{ c.response_text }}</textarea>
            <label class="dz-toggle">
              <input type="checkbox" name="cc_random" {% if c.is_random %}checked{% endif %} />
              <span>Randomised &mdash; pick one line at random</span>
            </label>
            <div class="dz-row" style="margin-top:10px;">
              <button class="dz-btn primary" name="action" value="edit">
                <i class="fa fa-save"></i> Save response
              </button>
            </div>
          </form>

          <form method="POST" style="margin-top:12px;">
            <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
            <input type="hidden" name="cc_name" value="{{ c.name }}" />
            <label class="dz-label">Cooldown</label>
            <div class="dz-row">
              <input class="dz-input" type="number" min="0" name="cc_seconds"
                     placeholder="seconds (0 removes)" style="max-width:190px;" />
              <select class="dz-select" name="cc_scope" style="max-width:170px;">
                {% for s in scopes %}
                  <option value="{{ s }}">per {{ s }}</option>
                {% endfor %}
              </select>
              <button class="dz-btn" name="action" value="cooldown">
                <i class="fa fa-clock-o"></i> Apply
              </button>
              {{ confirm('Delete command', 'delete',
                         'Delete the custom command ' ~ c.name ~ '?') }}
            </div>
          </form>
        {% else %}
          <div class="dz-text">{{ c.response_text }}</div>
        {% endif %}
      </div>
    {% endfor %}
  {% else %}
    <div class="dz-panel"><p class="dz-empty">No custom commands yet.</p></div>
  {% endif %}
</div>
"""
)
