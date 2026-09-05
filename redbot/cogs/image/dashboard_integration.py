from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import (
    BASE_CSS,
    MACROS,
    dashboard_page,
    form_reader,
)

log = logging.getLogger("red.image.dashboard")

# What [p]imgurcreds and [p]giphycreds printed. Kept as steps rather than a
# link because two of the Imgur ones are easy to get wrong.
CREDENTIAL_STEPS = (
    {
        "key": "imgur",
        "label": "Imgur",
        "docs": "https://api.imgur.com/oauth2/addclient",
        "steps": [
            "Sign in to an Imgur account, then open the link above.",
            "Enter a name for the application.",
            "Choose <b>Anonymous usage without user authorization</b> as the auth type.",
            "Set the authorization callback URL to <code>https://localhost</code>.",
            "Leave the app website blank.",
            "Enter a valid email address and a description.",
            "Complete the captcha. The client ID is on the next page.",
        ],
        "command": "[p]set api imgur client_id &lt;your_client_id&gt;",
    },
    {
        "key": "giphy",
        "label": "Giphy",
        "docs": "https://developers.giphy.com/dashboard",
        "steps": [
            "Sign in to (or create) a Giphy account, then open the link above.",
            "Press <b>Create an App</b>.",
            "Click <b>Select API</b>, then <b>Next Step</b>.",
            "Give it a name and a description, then create it.",
            "Copy the API key it shows you.",
        ],
        "command": "[p]set api giphy api_key &lt;your_api_key&gt;",
    },
)


class DashboardIntegration:
    """Image searches and the Imgur credentials, owner only.

    Covers ``[p]gif``, ``[p]gifr``, ``[p]imgur search``, ``[p]imgur subreddit``
    and the stored Imgur client ID.
    """

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Image as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Image searches and the Imgur and Giphy credentials.",
        methods=("GET", "POST"),
        is_owner=True,
    )
    async def dashboard_image_page(self, user: discord.User, **kwargs: t.Any) -> dict[str, t.Any]:
        notifications: list[dict] = []
        results: dict = {}
        if kwargs.get("method") == "POST":
            notifications, results = await self._img_handle_post(kwargs)

        client_id = await self.config.imgur_client_id()

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": IMAGE_TEMPLATE,
                "credentials": [
                    {
                        **service,
                        "set": bool(
                            await self.bot.get_shared_api_tokens(service["key"])
                        ),
                    }
                    for service in CREDENTIAL_STEPS
                ],
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "configured": bool(client_id),
                # Never send the credential itself to the browser; a length and
                # a masked tail is enough to confirm which one is stored.
                "masked": f"{'*' * 8}{client_id[-4:]}" if client_id and len(client_id) > 4 else "",
                "giphy_set": bool(
                    (await self.bot.get_shared_api_tokens("GIPHY")).get("api_key")
                ),
                "imgur_shared": bool(
                    (await self.bot.get_shared_api_tokens("imgur")).get("client_id")
                ),
                "results": results,
            },
        }

    async def _img_handle_post(self, kwargs: dict) -> tuple[list[dict], dict]:
        field = form_reader(kwargs)
        action = field("action")

        try:
            if action in ("giphy", "giphy_random", "imgur_search", "imgur_subreddit"):
                return await self._img_search(action, field)

            if action == "clear":
                await self.config.imgur_client_id.set(None)
                return [
                    {"message": "Imgur client ID removed.", "category": "success"}
                ], {}

            if action == "save":
                value = (field("client_id") or "").strip()
                if not value:
                    return [{"message": "Enter a client ID.", "category": "warning"}], {}
                if len(value) < 8:
                    return [
                        {"message": "That does not look like an Imgur client ID.",
                         "category": "warning"}
                    ], {}
                await self.config.imgur_client_id.set(value)
                return [
                    {"message": "Imgur client ID saved.", "category": "success"}
                ], {}
        except Exception as exc:  # noqa: BLE001
            log.exception("Image dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}], {}

        return [{"message": f"Unknown action: {action}", "category": "warning"}], {}

    async def _img_search(self, action: str, field) -> tuple[list[dict], dict]:
        """Run the same lookups `[p]gif`, `[p]gifr` and `[p]imgur` do."""
        from random import shuffle

        terms = (field("terms") or "").strip()
        count = max(1, min(field.integer("count", 1) or 1, 5))

        if action in ("giphy", "giphy_random"):
            api_key = (await self.bot.get_shared_api_tokens("GIPHY")).get("api_key")
            if not api_key:
                return [
                    {
                        "message": "No Giphy API key is set. Add one with "
                        "`[p]set api GIPHY api_key <key>`.",
                        "category": "warning",
                    }
                ], {}
            if not terms:
                return [{"message": "Enter some keywords.", "category": "warning"}], {}
            if action == "giphy":
                url = "http://api.giphy.com/v1/gifs/search"
                params = {"api_key": api_key, "q": terms, "limit": count}
            else:
                url = "http://api.giphy.com/v1/gifs/random"
                params = {"api_key": api_key, "tag": terms}
            async with self.session.get(url, params=params) as response:
                if response.status != 200:
                    return [
                        {"message": "Giphy could not be reached.", "category": "danger"}
                    ], {}
                payload = await response.json()
            data = payload.get("data")
            if not data:
                return [{"message": "No results found.", "category": "info"}], {}
            if isinstance(data, dict):
                data = [data]
            return [], {
                "title": f"Giphy: {terms}",
                "links": [item.get("url") for item in data[:count] if item.get("url")],
            }

        client_id = (await self.bot.get_shared_api_tokens("imgur")).get("client_id")
        if not client_id:
            return [
                {
                    "message": "No Imgur client ID is set. Save one below first.",
                    "category": "warning",
                }
            ], {}
        headers = {"Authorization": f"Client-ID {client_id}"}

        if action == "imgur_search":
            if not terms:
                return [{"message": "Enter a search term.", "category": "warning"}], {}
            url = self.imgur_base_url + "gallery/search/time/all/0"
            params = {"q": terms}
        else:
            subreddit = (field("subreddit") or "").strip()
            if not subreddit:
                return [{"message": "Enter a subreddit.", "category": "warning"}], {}
            sort = "time" if (field("sort") or "top") == "new" else "top"
            window = field("window") or "day"
            url = (
                self.imgur_base_url
                + f"gallery/r/{subreddit}/{sort}/{window}/0"
            )
            params = {}
            terms = f"r/{subreddit}"

        async with self.session.get(url, headers=headers, params=params) as response:
            payload = await response.json()
        if not payload.get("success"):
            return [
                {
                    "message": f"Imgur returned an error (code "
                    f"{payload.get('status', '?')}).",
                    "category": "danger",
                }
            ], {}
        results = payload.get("data") or []
        if not results:
            return [{"message": "No results found.", "category": "info"}], {}
        shuffle(results)
        return [], {
            "title": f"Imgur: {terms}",
            "links": [
                item.get("gifv") or item.get("link")
                for item in results[:count]
                if item.get("gifv") or item.get("link")
            ],
        }


IMAGE_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-picture-o"></i> Image sources</h4>
    <p>
      {% if configured %}Imgur configured ({{ masked }}).
      {% else %}No Imgur client ID set &mdash; the imgur commands will not work.{% endif %}
    </p>
  </div>

  {% if is_owner %}
    <div class="dz-panel">
      <h5><i class="fa fa-key"></i> API credentials</h5>
      {% for c in credentials %}
        <details style="margin:8px 0; padding:9px 11px;
                        border:1px solid rgba(255,255,255,.09); border-radius:9px;">
          <summary style="cursor:pointer;">
            How to get a {{ c.label }} key &mdash;
            <b>{{ 'already set' if c.set else 'not set' }}</b>
          </summary>
          <p class="dz-hint" style="margin-top:8px;">
            <a href="{{ c.docs }}" target="_blank" rel="noopener">{{ c.docs }}</a>
          </p>
          <ol class="dz-hint" style="margin:0 0 8px 18px; padding:0;">
            {% for step in c.steps %}<li style="margin:3px 0;">{{ step|safe }}</li>{% endfor %}
          </ol>
          <p class="dz-hint" style="margin:0;">Then send the bot, in a DM:</p>
          <code style="display:block; margin-top:5px; word-break:break-all;"
            >{{ c.command|safe }}</code>
        </details>
      {% endfor %}
      <p class="dz-hint">These are secrets: send that in a DM, not a server channel.</p>
    </div>
  {% endif %}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-key"></i> Imgur client ID</h5>
      <p class="dz-hint">
        Register an application at api.imgur.com to get one. Only the client ID
        is needed, not the secret.
      </p>
      <input class="dz-input" type="password" name="client_id"
             autocomplete="new-password"
             placeholder="{% if configured %}leave blank to keep the current one{% else %}client ID{% endif %}" />
      <div class="dz-row" style="margin-top:12px;">
        <button class="dz-btn primary" name="action" value="save">
          <i class="fa fa-save"></i> Save
        </button>
        {% if configured %}
          <button class="dz-btn danger" name="action" value="clear"
                  onclick="return confirm('Remove the stored Imgur client ID?');">
            <i class="fa fa-times"></i> Remove
          </button>
        {% endif %}
      </div>
      <p class="dz-hint" style="margin-top:11px;">
        The stored value is never sent back to this page.
      </p>
    </div>
  </form>

  {% if results %}
    <div class="dz-panel">
      <h5><i class="fa fa-picture-o"></i> {{ results.title }}</h5>
      <div class="dz-grid two">
        {% for link in results.links %}
          <div>
            <img src="{{ link }}" alt="" style="max-width:100%; border-radius:10px;" />
            <p class="dz-hint"><a href="{{ link }}" target="_blank" rel="noopener">{{ link }}</a></p>
          </div>
        {% endfor %}
      </div>
    </div>
  {% endif %}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-search"></i> Find an image</h5>
      <p class="dz-hint">
        Giphy {{ 'is configured' if giphy_set else 'needs an API key' }} &middot;
        Imgur {{ 'is configured' if imgur_shared else 'needs a client ID' }}.
      </p>
      <div class="dz-row">
        <input class="dz-input" type="text" name="terms"
               placeholder="keywords" style="flex:1 1 220px;" />
        <input class="dz-input" type="number" min="1" max="5" name="count" value="1"
               style="max-width:110px;" title="How many results" />
        <button class="dz-btn primary" name="action" value="giphy">
          <i class="fa fa-film"></i> Giphy search
        </button>
        <button class="dz-btn" name="action" value="giphy_random">
          <i class="fa fa-random"></i> Random GIF
        </button>
        <button class="dz-btn" name="action" value="imgur_search">
          <i class="fa fa-image"></i> Imgur search
        </button>
      </div>
      <label class="dz-label" style="margin-top:14px;">Imgur subreddit</label>
      <div class="dz-row">
        <input class="dz-input" type="text" name="subreddit"
               placeholder="aww" style="max-width:220px;" />
        <select class="dz-select" name="sort" style="max-width:130px;">
          <option value="top">Top</option>
          <option value="new">New</option>
        </select>
        <select class="dz-select" name="window" style="max-width:150px;">
          <option value="day">Past day</option>
          <option value="week">Past week</option>
          <option value="month">Past month</option>
          <option value="year">Past year</option>
          <option value="all">All time</option>
        </select>
        <button class="dz-btn" name="action" value="imgur_subreddit">
          <i class="fa fa-reddit"></i> Fetch
        </button>
      </div>
    </div>
  </form>
</div>
"""
)
