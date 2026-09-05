from __future__ import annotations

import typing

import discord
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.chat_formatting import humanize_list
from tabulate import tabulate
from pylav.logging import getLogger
from pylav.type_hints.bot import DISCORD_COG_TYPE_MIXIN
from pylav.storage.models.player.config import PlayerConfig

LOGGER = getLogger("PyLav.cog.Player.commands.config")

_ = Translator("PyLavPlayer", __file__)


@cog_i18n(_)
class ConfigCommands(DISCORD_COG_TYPE_MIXIN):


    @staticmethod
    async def _process_remove_multiple_dj_roles_or_users(
        config: PlayerConfig, roles_or_users: list[discord.Role | discord.Member | int]
    ) -> str:
        roles = {r for r in roles_or_users if isinstance(r, discord.Role)}
        users = {u for u in roles_or_users if isinstance(u, discord.Member)}
        ints = {i for i in roles_or_users if isinstance(i, int)}
        message = None
        if roles and users and ints:
            message = _(
                "I have removed {role_list_variable_do_not_translate} from the disc jockey roles and {user_list_variable_do_not_translate} from the disc jockey users, as well as {number_list_variable_do_not_translate} from the disc jockey roles and users."
            ).format(
                role_list_variable_do_not_translate=humanize_list([r.mention for r in roles]),
                user_list_variable_do_not_translate=humanize_list([u.mention for u in users]),
                number_list_variable_do_not_translate=humanize_list([str(i) for i in ints]),
            )
        elif roles and users:
            message = _(
                "I have removed {role_list_variable_do_not_translate} from the disc jockey roles and {user_list_variable_do_not_translate} from the disc jockey users."
            ).format(
                role_list_variable_do_not_translate=humanize_list([r.mention for r in roles]),
                user_list_variable_do_not_translate=humanize_list([u.mention for u in users]),
            )
        if roles:
            if not message:
                message = _("I have removed {role_list_variable_do_not_translate} from the disc jockey roles.").format(
                    role_list_variable_do_not_translate=humanize_list([r.mention for r in roles])
                )
            await config.bulk_remove_dj_roles(*roles)
        if users:
            if not message:
                message = _("I have removed {user_list_variable_do_not_translate} from the disc jockey users.").format(
                    user_list_variable_do_not_translate=humanize_list([u.mention for u in users])
                )
            await config.bulk_remove_dj_users(*users)
        if ints:
            if not message:
                message = _(
                    "I have removed {user_or_role_id_list_variable_do_not_translate} from the disc jockey roles and users."
                ).format(user_or_role_id_list_variable_do_not_translate=humanize_list([str(u) for u in users]))
            await config.bulk_remove_dj_users(*[discord.Object(id=i) for i in ints])
            await config.bulk_remove_dj_roles(*[discord.Object(id=i) for i in ints])
        return message

    @staticmethod
    async def _precess_remove_single_dj_role_or_user(
        config: PlayerConfig, roles_or_users: list[discord.Role | discord.Member | int]
    ):
        role_or_user = roles_or_users[0]
        if isinstance(role_or_user, int):
            await config.remove_from_dj_roles(typing.cast(discord.Role, discord.Object(id=role_or_user)))
            await config.remove_from_dj_users(typing.cast(discord.Member, discord.Object(id=role_or_user)))
            message = _(
                "I have Removed `{user_or_role_id_variable_do_not_translate}` from the disc jockey roles and users."
            ).format(user_or_role_id_variable_do_not_translate=role_or_user)
        elif isinstance(role_or_user, discord.Role):
            message = _("I have removed {role_name_variable_do_not_translate} from the disc jockey roles.").format(
                role_name_variable_do_not_translate=role_or_user.mention
            )
            await config.remove_from_dj_roles(role_or_user)
        else:
            message = _("I have removed {user_name_variable_do_not_translate} from the disc jockey users.").format(
                user_name_variable_do_not_translate=role_or_user.mention
            )
            await config.remove_from_dj_users(role_or_user)
        return message


