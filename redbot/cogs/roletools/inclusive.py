from red_commons.logging import getLogger
from redbot.core.i18n import Translator

from .abc import RoleToolsMixin

roletools = RoleToolsMixin.roletools

log = getLogger("red.Trusty-cogs.RoleTools")
_ = Translator("RoleTools", __file__)


class RoleToolsInclusive(RoleToolsMixin):
    """This class handles setting inclusive roles."""


