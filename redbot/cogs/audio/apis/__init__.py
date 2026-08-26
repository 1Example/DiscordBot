"""Legacy Audio API layer, kept for the PyLav migrator to read old Audio data.

Submodules are resolved on first access rather than imported eagerly. Several of
them (``global_db``, ``interface``, ``spotify``, ``youtube``) need the standalone
``lavalink`` package the pre-PyLav Audio cog depended on, which is no longer what
``import lavalink`` resolves to in this fork. Importing them up front made the
whole package unimportable, which in turn broke ``plmigrator`` - even though it
only ever wants ``api_utils`` and ``playlist_wrapper``, neither of which needs
that package.
"""
import importlib
import typing

__all__ = (
    "api_utils",
    "global_db",
    "interface",
    "local_db",
    "playlist_interface",
    "playlist_wrapper",
    "spotify",
    "youtube",
)


def __getattr__(name: str) -> typing.Any:
    if name in __all__:
        module = importlib.import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list:
    return sorted({*globals(), *__all__})
