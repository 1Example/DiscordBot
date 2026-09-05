"""PyLavEffects, merged into the Audio cog.

Kept as a package so `Translator(..., Path(__file__))` still finds the
locales this was translated into.
"""
from .cog import PyLavEffects

__all__ = ["PyLavEffects"]
