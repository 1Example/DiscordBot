"""What is left of VrtUtils' command modules.

The commands themselves are gone - the dashboard page covers the diagnostics,
lookups and reports they used to provide, and this cog now contributes no
top-level slash commands at all. Two things here were never commands and stay:
the "Edit Message" context menu, and the Downloader patch applied at load.
"""

from ..abc import CompositeMetaClass


class Utils(metaclass=CompositeMetaClass):
    """No command mixins remain; kept so the cog's bases stay stable."""
