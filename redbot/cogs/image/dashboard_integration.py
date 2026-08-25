from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands

from redbot.core.utils.dashboard_helpers import BASE_CSS, dashboard_page, form_reader

log = logging.getLogger("red.image.dashboard")


class DashboardIntegration:
    """Imgur credentials, owner only."""

    bot: t.Any
    config: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Image as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Imgur API credentials.",
        methods=("GET", "POST"),
        is_owner=True,
    )
    async def dashboard_image_page(self, user: discord.User, **kwargs: t.Any) -> dict[str, t.Any]:
        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._img_handle_post(kwargs)

        client_id = await self.config.imgur_client_id()

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": IMAGE_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "configured": bool(client_id),
                # Never send the credential itself to the browser; a length and
                # a masked tail is enough to confirm which one is stored.
                "masked": f"{'*' * 8}{client_id[-4:]}" if client_id and len(client_id) > 4 else "",
            },
        }

    async def _img_handle_post(self, kwargs: dict) -> list[dict]:
        field = form_reader(kwargs)
        action = field("action")

        try:
            if action == "clear":
                await self.config.imgur_client_id.set(None)
                return [{"message": "Imgur client ID removed.", "category": "success"}]

            if action == "save":
                value = (field("client_id") or "").strip()
                if not value:
                    return [{"message": "Enter a client ID.", "category": "warning"}]
                if len(value) < 8:
                    return [
                        {"message": "That does not look like an Imgur client ID.",
                         "category": "warning"}
                    ]
                await self.config.imgur_client_id.set(value)
                return [{"message": "Imgur client ID saved.", "category": "success"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("Image dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]


IMAGE_TEMPLATE = (
    BASE_CSS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-picture-o"></i> Image sources</h4>
    <p>
      {% if configured %}Imgur configured ({{ masked }}).
      {% else %}No Imgur client ID set &mdash; the imgur commands will not work.{% endif %}
    </p>
  </div>

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
</div>
"""
)
