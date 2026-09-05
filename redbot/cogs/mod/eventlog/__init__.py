"""Server event logging, formerly the standalone ExtendedModLog cog.

Kept as a subpackage rather than flattened into ``mod/`` so that
``Translator("ExtendedModLog", __file__)`` still resolves to the ``locales``
directory these strings were translated into.
"""
from .cog import EventLogMixin
from .settings import inv_settings

__all__ = ["EventLogMixin", "inv_settings"]
