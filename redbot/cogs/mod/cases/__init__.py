"""Modlog case commands, formerly the standalone ModLog cog.

Kept as a subpackage rather than flattened into ``mod/`` so that
``Translator("ModLog", __file__)`` still resolves to the ``locales`` directory
these strings were translated into.
"""
from .modlog import CaseCommands

__all__ = ["CaseCommands"]
