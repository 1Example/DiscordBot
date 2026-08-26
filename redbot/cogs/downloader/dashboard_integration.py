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

log = logging.getLogger("red.downloader.dashboard")


class DashboardIntegration:
    """Repo and cog management from the dashboard.

    Covers ``[p]repo`` and ``[p]cog`` end to end: add, update and remove repos;
    install, update, pin and uninstall cogs; check for updates; run pip installs.
    Owner only, since every action here changes what code the bot runs.
    """

    bot: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:  # noqa: D401
        log.info("Dashboard cog found, registering Downloader as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name=None,
        description="Repositories, installed cogs and updates.",
        methods=("GET", "POST"),
        is_owner=True,
    )
    async def dashboard_downloader_page(
        self, user: discord.User, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        from redbot.core import _downloader

        notifications: list[dict] = []
        if kwargs.get("method") == "POST":
            notifications = await self._dl_handle_post(kwargs)

        repos = sorted(_downloader._repo_manager.repos, key=lambda r: r.name.lower())
        installed = sorted(await _downloader.installed_cogs(), key=lambda c: c.name.lower())
        loaded = set(self.bot.extensions.keys())

        repo_rows = []
        for repo in repos:
            available = [c for c in repo.available_cogs if not c.hidden]
            repo_rows.append(
                {
                    "name": repo.name,
                    "url": repo.clean_url,
                    "branch": repo.branch or "",
                    "author": ", ".join(repo.author) if repo.author else "",
                    "short": repo.short or "",
                    "description": repo.description or "",
                    "commit": (repo.commit or "")[:7],
                    "available": [self._dl_cog_row(c, installed, loaded) for c in available],
                    "hidden_count": len(repo.available_cogs) - len(available),
                }
            )

        installed_rows = []
        for cog in installed:
            installed_rows.append(
                {
                    "name": cog.name,
                    "repo": cog.repo_name or "unknown",
                    "commit": (cog.commit or "")[:7],
                    "pinned": bool(getattr(cog, "pinned", False)),
                    "loaded": cog.name in loaded,
                    "short": cog.short or "",
                }
            )

        return {
            "status": 0,
            "notifications": notifications,
            "web_content": {
                "source": DOWNLOADER_TEMPLATE,
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "repos": repo_rows,
                "installed": installed_rows,
                "pinned_count": sum(1 for c in installed_rows if c["pinned"]),
                "loaded_count": sum(1 for c in installed_rows if c["loaded"]),
                "update_report": self._dl_last_report,
            },
        }

    # Result of the most recent "check for updates" run, so the outcome survives
    # the redirect back to a GET.
    _dl_last_report: str = ""

    def _dl_cog_row(self, cog, installed, loaded) -> dict:
        match = discord.utils.get(installed, name=cog.name)
        is_installed = match is not None
        return {
            # The `id`/`group`/`selected`/`warn` keys let this row feed the
            # `picker` macro directly.
            "id": cog.name,
            "name": cog.name if not is_installed else f"{cog.name} (installed)",
            "group": "Installed" if is_installed else "Available",
            "selected": False,
            "warn": bool(cog.disabled),
            "cog_name": cog.name,
            "short": cog.short or "",
            "installed": is_installed,
            "loaded": cog.name in loaded,
            "disabled": bool(cog.disabled),
            "tags": ", ".join(cog.tags),
            "requirements": ", ".join(cog.requirements),
            "min_bot_version": str(cog.min_bot_version),
        }

    async def _dl_handle_post(self, kwargs: dict) -> list[dict]:
        from redbot.core import _downloader
        from redbot.core._downloader import errors

        field = form_reader(kwargs)
        action = field("action")
        manager = _downloader._repo_manager

        try:
            if action == "repo_add":
                name = (field("repo_name") or "").strip()
                url = (field("repo_url") or "").strip()
                branch = (field("repo_branch") or "").strip() or None
                if not name or not url:
                    return [
                        {"message": "A name and a URL are both required.", "category": "warning"}
                    ]
                try:
                    name = manager.validate_and_normalize_repo_name(name)
                except errors.InvalidRepoName as exc:
                    return [{"message": str(exc), "category": "warning"}]
                if manager.does_repo_exist(name):
                    return [
                        {"message": f"A repo named `{name}` already exists.",
                         "category": "warning"}
                    ]
                repo = await manager.add_repo(name=name, url=url, branch=branch)
                extra = f" {repo.install_msg}" if repo.install_msg else ""
                return [{"message": f"Repo `{name}` added.{extra}", "category": "success"}]

            if action == "repo_delete":
                name = field("repo_name")
                await manager.delete_repo(name)
                return [{"message": f"Repo `{name}` removed.", "category": "success"}]

            if action == "repo_update":
                names = field.many("repo_names") or [r.name for r in manager.repos]
                repos = [r for r in (manager.get_repo(n) for n in names) if r is not None]
                if not repos:
                    return [{"message": "No repos to update.", "category": "warning"}]
                # Maps each repo that moved to (old_hash, new_hash); `failed` is names.
                updated, failed = await manager.update_repos(repos)
                messages = []
                if updated:
                    messages.append(
                        {
                            "message": "Updated: "
                            + ", ".join(sorted(r.name for r in updated)),
                            "category": "success",
                        }
                    )
                else:
                    messages.append(
                        {"message": "No repos had new commits.", "category": "info"}
                    )
                if failed:
                    messages.append(
                        {
                            "message": "Failed: " + ", ".join(sorted(failed)),
                            "category": "danger",
                        }
                    )
                return messages

            if action == "cog_install":
                repo = manager.get_repo(field("repo_name"))
                if repo is None:
                    return [{"message": "That repo no longer exists.", "category": "warning"}]
                names = field.many("cog_names")
                if not names:
                    return [{"message": "Select at least one cog.", "category": "warning"}]
                # A revision pins the install to that commit, same as `[p]cog installrev`.
                rev = (field("cog_rev") or "").strip() or None
                result = await _downloader.install_cogs(repo, rev, names)
                return self._dl_install_notifications(result, repo)

            if action == "cog_uninstall":
                names = field.many("cog_names") or [field("cog_name")]
                installed = await _downloader.installed_cogs()
                cogs = [c for c in installed if c.name in names]
                if not cogs:
                    return [{"message": "Nothing matched.", "category": "warning"}]
                uninstalled, failed = await _downloader.uninstall_cogs(*cogs)
                out = []
                if uninstalled:
                    out.append(
                        {
                            "message": "Uninstalled: " + ", ".join(sorted(uninstalled)),
                            "category": "success",
                        }
                    )
                if failed:
                    out.append(
                        {
                            "message": "Could not uninstall: " + ", ".join(sorted(failed)),
                            "category": "danger",
                        }
                    )
                return out

            if action in ("cog_pin", "cog_unpin"):
                names = field.many("cog_names") or [field("cog_name")]
                installed = await _downloader.installed_cogs()
                cogs = [c for c in installed if c.name in names]
                if not cogs:
                    return [{"message": "Nothing matched.", "category": "warning"}]
                if action == "cog_pin":
                    changed, unchanged = await _downloader.pin_cogs(*cogs)
                    verb, other = "pinned", "already pinned"
                else:
                    changed, unchanged = await _downloader.unpin_cogs(*cogs)
                    verb, other = "unpinned", "were not pinned"
                out = []
                if changed:
                    out.append(
                        {
                            "message": f"{verb.capitalize()}: "
                            + ", ".join(sorted(c.name for c in changed)),
                            "category": "success",
                        }
                    )
                if unchanged:
                    out.append(
                        {
                            "message": f"Skipped ({other}): "
                            + ", ".join(sorted(c.name for c in unchanged)),
                            "category": "info",
                        }
                    )
                return out

            if action == "check_updates":
                result = await _downloader.check_cog_updates()
                lines = []
                if result.updatable_cogs:
                    lines.append(
                        "Updates available: "
                        + ", ".join(sorted(c.name for c in result.updatable_cogs))
                    )
                elif result.outdated_libs:
                    lines.append("Shared library updates are available.")
                else:
                    lines.append("Everything is up to date.")
                for label, group in (
                    ("needs a newer Red", result.incompatible_bot_version),
                    ("needs a newer Python", result.incompatible_python_version),
                ):
                    if group:
                        lines.append(
                            f"Cannot update ({label}): "
                            + ", ".join(sorted(c.name for c in group))
                        )
                if result.failed_repos:
                    lines.append(
                        "Repos that failed to update: "
                        + ", ".join(sorted(result.failed_repos))
                    )
                self._dl_last_report = "\n".join(lines)
                return [
                    {
                        "message": lines[0],
                        "category": "success" if result.updates_installable else "info",
                    }
                ]

            if action == "cog_update":
                names = field.many("cog_names")
                installed = await _downloader.installed_cogs()
                if names:
                    cogs = [c for c in installed if c.name in names]
                    if not cogs:
                        return [{"message": "Nothing matched.", "category": "warning"}]
                    result = await _downloader.update_cogs(cogs=cogs)
                else:
                    result = await _downloader.update_cogs()
                updated = sorted(c.name for c in result.updated_cogs)
                if not updated:
                    return [{"message": "No cogs needed updating.", "category": "info"}]
                # Reload what was updated so the running bot picks up new code.
                reloaded = []
                for name in updated:
                    if name in self.bot.extensions:
                        try:
                            await self.bot.reload_extension(name)
                            reloaded.append(name)
                        except Exception:  # noqa: BLE001
                            log.exception("Reloading %s after a dashboard update failed", name)
                out = [
                    {"message": "Updated: " + ", ".join(updated), "category": "success"}
                ]
                if reloaded:
                    out.append(
                        {"message": "Reloaded: " + ", ".join(reloaded), "category": "info"}
                    )
                return out

            if action == "reinstall_reqs":
                failed_reqs, failed_libs = await _downloader.reinstall_requirements()
                if not failed_reqs and not failed_libs:
                    return [
                        {"message": "Requirements and shared libraries reinstalled.",
                         "category": "success"}
                    ]
                out = []
                if failed_reqs:
                    out.append(
                        {
                            "message": "Failed requirements: " + ", ".join(failed_reqs),
                            "category": "danger",
                        }
                    )
                if failed_libs:
                    out.append(
                        {
                            "message": "Failed libraries: "
                            + ", ".join(sorted(c.name for c in failed_libs)),
                            "category": "danger",
                        }
                    )
                return out

            if action == "pip_install":
                deps = [d for d in (field("deps") or "").split() if d]
                if not deps:
                    return [{"message": "Enter at least one package.", "category": "warning"}]
                if await _downloader.pip_install(*deps):
                    return [
                        {"message": "Installed: " + ", ".join(deps), "category": "success"}
                    ]
                return [
                    {
                        "message": "pip failed; check the bot console for the full output.",
                        "category": "danger",
                    }
                ]
        except errors.DownloaderException as exc:
            return [{"message": f"Downloader error: {exc}", "category": "danger"}]
        except Exception as exc:  # noqa: BLE001
            log.exception("Downloader dashboard action %r failed", action)
            return [{"message": f"Action failed: {exc}", "category": "danger"}]

        return [{"message": f"Unknown action: {action}", "category": "warning"}]

    def _dl_install_notifications(self, result, repo) -> list[dict]:
        out = []
        if result.installed_cogs:
            names = sorted(c.name for c in result.installed_cogs)
            out.append(
                {
                    "message": f"Installed from `{repo.name}`: " + ", ".join(names),
                    "category": "success",
                }
            )
            out.append(
                {
                    "message": "Load them with the Core page or `[p]load "
                    + " ".join(names)
                    + "`.",
                    "category": "info",
                }
            )
        for label, group in (
            ("already installed", result.already_installed),
            ("name already taken", result.name_already_used),
            ("needs a newer Python", result.incompatible_python_version),
            ("needs a newer Red", result.incompatible_bot_version),
            ("failed to install", result.failed_cogs),
        ):
            if group:
                out.append(
                    {
                        "message": f"Skipped ({label}): "
                        + ", ".join(sorted(c.name for c in group)),
                        "category": "warning",
                    }
                )
        if result.unavailable_cogs:
            out.append(
                {
                    "message": "Not in that repo: " + ", ".join(sorted(result.unavailable_cogs)),
                    "category": "warning",
                }
            )
        if result.failed_reqs:
            out.append(
                {
                    "message": "Failed requirements: " + ", ".join(sorted(result.failed_reqs)),
                    "category": "danger",
                }
            )
        return out or [{"message": "Nothing was installed.", "category": "info"}]


DOWNLOADER_TEMPLATE = (
    BASE_CSS
    + MACROS
    + """
<div class="dz">
  <div class="dz-head">
    <h4><i class="fa fa-download"></i> Downloader</h4>
    <p>Add repositories and install, update, pin or remove community cogs.
       Everything here changes the code the bot runs.</p>
  </div>

  {{ stats([('Repos', repos|length),
            ('Installed cogs', installed|length),
            ('Loaded', loaded_count),
            ('Pinned', pinned_count)]) }}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-refresh"></i> Maintenance</h5>
      <p class="dz-hint">Update every repo, look for cog updates, or repair
         requirements after a Python upgrade.</p>
      <div class="dz-row">
        <button class="dz-btn" name="action" value="repo_update">
          <i class="fa fa-cloud-download"></i> Update all repos
        </button>
        <button class="dz-btn" name="action" value="check_updates">
          <i class="fa fa-search"></i> Check for cog updates
        </button>
        {{ confirm('Update all cogs', 'cog_update',
                   'Update every installed cog and reload the loaded ones?',
                   'primary', 'fa-arrow-circle-up') }}
        {{ confirm('Reinstall requirements', 'reinstall_reqs',
                   'Reinstall requirements and shared libraries for every installed cog?',
                   '', 'fa-wrench') }}
      </div>
      {% if update_report %}
        <pre class="dz-hint" style="white-space:pre-wrap; margin-top:10px;">{{ update_report }}</pre>
      {% endif %}
    </div>
  </form>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-plus"></i> Add a repository</h5>
      <p class="dz-hint">Only add repos from creators you trust &mdash; their code
         runs with the bot's full permissions.</p>
      <div class="dz-grid three">
        <div>
          <label class="dz-label">Name</label>
          <input class="dz-input" type="text" name="repo_name" placeholder="mycogs" required />
        </div>
        <div>
          <label class="dz-label">URL</label>
          <input class="dz-input" type="text" name="repo_url"
                 placeholder="https://github.com/user/repo" required />
        </div>
        <div>
          <label class="dz-label">Branch <span class="dz-tag">optional</span></label>
          <input class="dz-input" type="text" name="repo_branch" placeholder="main" />
        </div>
      </div>
      <div class="dz-save">
        <button class="dz-btn primary" name="action" value="repo_add">
          <i class="fa fa-plus"></i> Add repo
        </button>
      </div>
    </div>
  </form>

  {% for r in repos %}
    <div class="dz-panel">
      <h5>
        <i class="fa fa-code-fork"></i> {{ r.name }}
        {% if r.branch %}<span class="dz-tag">{{ r.branch }}</span>{% endif %}
        {% if r.commit %}<span class="dz-tag">{{ r.commit }}</span>{% endif %}
      </h5>
      <p class="dz-hint">
        {{ r.short or r.description }}
        {%- if r.author %} &middot; by {{ r.author }}{% endif %}
        <br /><code>{{ r.url }}</code>
      </p>

      <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <input type="hidden" name="repo_name" value="{{ r.name }}" />
        <input type="hidden" name="repo_names" value="{{ r.name }}" />
        {% if r.available %}
          <label class="dz-label">Cogs in this repo</label>
          {{ picker('cog_names', r.available, true, 10, 'Search cogs...') }}
        {% endif %}
        <div class="dz-row" style="margin-top:10px;">
          <input class="dz-input" type="text" name="cog_rev"
                 placeholder="commit or tag (optional)" style="max-width:230px;" />
          <button class="dz-btn primary" name="action" value="cog_install">
            <i class="fa fa-download"></i> Install selected
          </button>
          <button class="dz-btn" name="action" value="repo_update">
            <i class="fa fa-refresh"></i> Update repo
          </button>
          {{ confirm('Remove repo', 'repo_delete',
                     'Remove the repo ' ~ r.name ~ '? Installed cogs stay but can no longer be updated.') }}
        </div>
      </form>

      {% if r.available %}
        <table class="dz-t" style="margin-top:12px;">
          <tr><th>Cog</th><th>Description</th><th>Status</th></tr>
          {% for c in r.available %}
            <tr>
              <td><code>{{ c.cog_name }}</code></td>
              <td>{{ c.short }}</td>
              <td>
                {% if c.installed %}<span class="dz-tag good">installed</span>{% endif %}
                {% if c.loaded %}<span class="dz-tag good">loaded</span>{% endif %}
                {% if c.disabled %}<span class="dz-tag bad">disabled</span>{% endif %}
              </td>
            </tr>
          {% endfor %}
        </table>
      {% else %}
        <p class="dz-empty">No installable cogs found in this repo.</p>
      {% endif %}
      {% if r.hidden_count %}
        <p class="dz-hint">{{ r.hidden_count }} hidden cog(s) not shown.</p>
      {% endif %}
    </div>
  {% else %}
    <div class="dz-panel"><p class="dz-empty">No repositories added yet.</p></div>
  {% endfor %}

  <div class="dz-panel">
    <h5><i class="fa fa-cubes"></i> Installed cogs</h5>
    {% if installed %}
      <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <table class="dz-t">
          <tr><th></th><th>Cog</th><th>Repo</th><th>Commit</th><th>Status</th></tr>
          {% for c in installed %}
            <tr>
              <td><input type="checkbox" name="cog_names" value="{{ c.name }}" /></td>
              <td><code>{{ c.name }}</code><br /><span class="dz-hint">{{ c.short }}</span></td>
              <td>{{ c.repo }}</td>
              <td><code>{{ c.commit }}</code></td>
              <td>
                {% if c.loaded %}<span class="dz-tag good">loaded</span>
                {% else %}<span class="dz-tag">unloaded</span>{% endif %}
                {% if c.pinned %}<span class="dz-tag warn">pinned</span>{% endif %}
              </td>
            </tr>
          {% endfor %}
        </table>
        <div class="dz-row dz-save">
          <button class="dz-btn" name="action" value="cog_update">
            <i class="fa fa-arrow-circle-up"></i> Update selected
          </button>
          <button class="dz-btn" name="action" value="cog_pin">
            <i class="fa fa-thumb-tack"></i> Pin
          </button>
          <button class="dz-btn" name="action" value="cog_unpin">
            <i class="fa fa-thumb-tack fa-rotate-90"></i> Unpin
          </button>
          {{ confirm('Uninstall selected', 'cog_uninstall',
                     'Uninstall the selected cogs? Their files are deleted.') }}
        </div>
      </form>
    {% else %}
      <p class="dz-empty">Nothing installed from a repo yet.</p>
    {% endif %}
  </div>

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <div class="dz-panel">
      <h5><i class="fa fa-cube"></i> Install Python packages</h5>
      <p class="dz-hint">Same as <code>[p]pipinstall</code>. Space separated.</p>
      <div class="dz-row">
        <input class="dz-input" type="text" name="deps"
               placeholder="requests beautifulsoup4" style="flex:1 1 260px;" />
        <button class="dz-btn" name="action" value="pip_install">
          <i class="fa fa-download"></i> Install
        </button>
      </div>
    </div>
  </form>
</div>
"""
)
