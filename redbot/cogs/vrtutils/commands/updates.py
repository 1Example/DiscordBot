import contextvars
import inspect
import typing as t
from pathlib import Path

from redbot import VersionInfo, version_info


_ORIG_FUNC = None
_INSTALL_REQS_VAR = contextvars.ContextVar("_INSTALL_REQS_VAR")


async def install_raw_requirements(
    self, requirements: t.Iterable[str], target_dir: Path
) -> bool:
    if _INSTALL_REQS_VAR.get(True):
        return await _ORIG_FUNC(self, requirements, target_dir)
    return True


def _get_repo_manager_module() -> t.Any:
    if version_info >= VersionInfo.from_str("3.5.25.dev7"):
        from redbot.core._downloader import repo_manager
    else:
        from redbot.cogs.downloader import repo_manager

    return repo_manager


def monkeypatch_repo() -> None:
    repo_manager = _get_repo_manager_module()
    if inspect.getmodule(repo_manager.Repo.install_raw_requirements) is repo_manager:
        global _ORIG_FUNC
        _ORIG_FUNC = repo_manager.Repo.install_raw_requirements

        setattr(repo_manager.Repo, "install_raw_requirements", install_raw_requirements)


def revert_monkeypatch_repo() -> None:
    repo_manager = _get_repo_manager_module()
    global _ORIG_FUNC
    if _ORIG_FUNC is not None:
        setattr(repo_manager.Repo, "install_raw_requirements", _ORIG_FUNC)
        _ORIG_FUNC = None

