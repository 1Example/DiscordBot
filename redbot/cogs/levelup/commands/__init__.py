from redbot.core.i18n import Translator, cog_i18n

from ..abc import CompositeMetaClass
from .data import DataAdmin
from .owner import Owner
from .user import User
from .weekly import Weekly

_ = Translator("LevelUp", __file__)


@cog_i18n(_)
class Commands(
    DataAdmin,
    Owner,
    User,
    Weekly,
    metaclass=CompositeMetaClass,
):
    """Subclass all command classes"""
