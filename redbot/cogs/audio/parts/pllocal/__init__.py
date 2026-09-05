"""PyLavLocalFiles, merged into the Audio cog.

Kept as a package so `Translator(..., Path(__file__))` still finds the
locales this was translated into.
"""
from .cog import PyLavLocalFiles

__all__ = ["PyLavLocalFiles"]
