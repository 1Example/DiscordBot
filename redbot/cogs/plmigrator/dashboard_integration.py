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

log = logging.getLogger("red.plmigrator.dashboard")


class _DashboardContext:
    """Stand-in for the ``PyLavContext`` the migration helpers expect.

    They only ever read ``guild`` and call ``send``; here the sends are collected
    so anything the migration reports can be shown on the page instead.
    """

    def __init__(self, bot, guild: discord.Guild | None) -> None:
        self.bot = bot
        self.guild = guild
        self.author = getattr(guild, "me", None) or bot.user
        self.messages: list[str] = []

    async def send(self, *args: t.Any, **kwargs: t.Any) -> None:
        embed = kwargs.get("embed")
        if embed is not None and getattr(embed, "description", None):
            self.messages.append(str(embed.description))
        elif kwargs.get("content"):
            self.messages.append(str(kwargs["content"]))
        elif args:
            self.messages.append(str(args[0]))

    async def send_help(self, *args: t.Any, **kwargs: t.Any) -> None:
        return None


class DashboardIntegration:
    """The Audio-to-PyLav migration, from the dashboard.

    Covers ``[p]plmigrate``, ``[p]plm-playlists`` and ``[p]plmigrate-revert``
    without the confirmation argument, replaced by an explicit confirm dialog.
    """

    bot: t.Any
    pylav: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering PyLavMigrator as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Copy Red Audio settings and playlists into PyLav.",
        methods=("GET", "POST"),
        is_owner=True,
    )
    async def dashboard_plmigrator_page(
        self, user: discord.User, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        notifications: list[dict] = []
        report: list[str] = []
        if kwargs.get("method") == "POST":
            notifications, report = await self._plmigrate_handle_post(kwargs)

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": PLMIGRATOR_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "report": report,
                "audio_loaded": self.bot.get_cog("Audio") is not None,
            },
        }

    async def _plmigrate_handle_post(self, kwargs: dict) -> tuple[list[dict], list[str]]:
        field = form_reader(kwargs)
        action = field("action")
        # A guild is only needed so the playlist migration has a requester;
        # any guild the bot is in will do.
        context = _DashboardContext(self.bot, next(iter(self.bot.guilds), None))

        try:
            if action == "migrate_all":
                audio_config, playlist_api = await self._init_audio_cog_dependencies()
                await self._process_global_settings(audio_config, context)
                for guild_id, guild_config in (await audio_config.all_guilds()).items():
                    await self._process_server_settings(guild_id, guild_config)
                await self._process_playlists(playlist_api, context)
                return (
                    [
                        {
                            "message": "Audio settings migrated to PyLav. Restart the bot "
                            "for them to take effect.",
                            "category": "success",
                        }
                    ],
                    context.messages,
                )

            if action == "migrate_playlists":
                _audio_config, playlist_api = await self._init_audio_cog_dependencies()
                await self._process_playlists(playlist_api, context)
                return (
                    [{"message": "Playlists migrated to PyLav.", "category": "success"}],
                    context.messages,
                )

            if action == "revert_playlists":
                audio_config, _playlist_api = await self._init_audio_cog_dependencies()
                changed = 0
                for guild_id, guild_config in (await audio_config.all_guilds()).items():
                    player_config = self.pylav.player_config_manager.get_config(guild_id)
                    await player_config.update_auto_play_playlist_id(1)
                    await player_config.update_auto_play(
                        bool(guild_config.get("autoplaylist", {}).get("enabled", False))
                    )
                    changed += 1
                return (
                    [
                        {
                            "message": f"Autoplay reset to the bundled playlist in "
                            f"{changed} server(s).",
                            "category": "success",
                        }
                    ],
                    context.messages,
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("PyLavMigrator dashboard action %r failed", action)
            return [{"message": f"Migration failed: {exc}", "category": "danger"}], []

        return [{"message": f"Unknown action: {action}", "category": "warning"}], []


PLMIGRATOR_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-exchange"></i> Audio to PyLav migration</h4>
    <p>Copies the old Audio cog's settings and playlists into PyLav.
       This overwrites what PyLav already has, and is meant to be run once.</p>
  </div>

  {% if report %}
    <div class="dz-panel">
      <h5><i class="fa fa-file-text-o"></i> Migration report</h5>
      {% for line in report %}
        <div class="dz-text" style="margin-bottom:6px;">{{ line }}</div>
      {% endfor %}
    </div>
  {% endif %}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-play"></i> Run a migration</h5>
      <p class="dz-hint">
        Restart the bot afterwards so the migrated settings take effect. Once you
        are done, this cog can be unloaded and uninstalled.
      </p>
      <div class="dz-row">
        {{ confirm('Migrate everything', 'migrate_all',
                   'Copy every Audio setting and playlist into PyLav, replacing what is there now?',
                   'primary', 'fa-exchange') }}
        {{ confirm('Migrate playlists only', 'migrate_playlists',
                   'Copy the Audio playlists into PyLav?',
                   '', 'fa-list') }}
        {{ confirm('Revert playlist migration', 'revert_playlists',
                   'Reset every server autoplay back to the bundled playlist?') }}
      </div>
      <p class="dz-hint" style="margin-top:12px;">
        Reverting is for when a migrated autoplaylist ends up broken; it points
        autoplay back at PyLav's bundled playlist.
      </p>
    </div>
  </form>
</div>
"""
)
