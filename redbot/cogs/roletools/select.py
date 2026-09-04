from __future__ import annotations

import discord
from red_commons.logging import getLogger
from redbot.core.i18n import Translator

from .abc import RoleToolsMixin
from .components import RoleToolsView, SelectRole, SelectRoleOption
from .converter import (
    RoleHierarchyConverter,
    SelectMenuFlags,
    SelectOptionFlags,
    SelectOptionRoleConverter,
)

roletools = RoleToolsMixin.roletools

log = getLogger("red.Trusty-cogs.RoleTools")
_ = Translator("RoleTools", __file__)


class RoleToolsSelect(RoleToolsMixin):
    """This class handles setting up Select menu roles"""

    async def initialize_select(self) -> None:
        for guild_id, settings in self.settings.items():
            if guild_id not in self.views:
                log.trace("Adding guild ID %s to views in selects", guild_id)
                self.views[guild_id] = {}
            for select_name, select_data in settings["select_menus"].items():
                log.verbose("Adding Option %s", select_name)
                options = []
                disabled = []
                for option_name in select_data["options"]:
                    try:
                        option_data = settings["select_options"][option_name]
                        role_id = option_data["role_id"]
                        description = option_data["description"]
                        emoji = option_data["emoji"]
                        if emoji is not None:
                            emoji = discord.PartialEmoji.from_str(option_data["emoji"])
                        label = option_data["label"]
                        if not label:
                            label = "\u200b"
                        option = SelectRoleOption(
                            name=option_name,
                            label=label,
                            value=f"RTSelect-{option_name}-{role_id}",
                            role_id=role_id,
                            description=description,
                            emoji=emoji,
                        )
                        options.append(option)
                    except KeyError:
                        log.info(
                            "Select Option named %s no longer exists, adding to select menus disalbe list.",
                            option_name,
                        )
                        disabled.append(option_name)

                guild = self.bot.get_guild(guild_id)
                for message_id in set(select_data.get("messages", [])):
                    # we need a new instance of this object per message
                    select = SelectRole(
                        name=select_name,
                        custom_id=f"RTSelect-{select_name}-{guild_id}",
                        min_values=select_data["min_values"],
                        max_values=select_data["max_values"],
                        placeholder=select_data["placeholder"],
                        options=options,
                        disabled=disabled,
                    )
                    if guild is not None:
                        select.update_options(guild)
                    if message_id not in self.views[guild_id]:
                        log.trace("Creating view for select %s", select_name)
                        self.views[guild_id][message_id] = RoleToolsView(self)
                    if select.custom_id not in {
                        c.custom_id for c in self.views[guild_id][message_id].children
                    }:
                        try:
                            self.views[guild_id][message_id].add_item(select)
                        except ValueError:
                            log.error(
                                "There was an error adding select %s on message https://discord.com/channels/%s/%s",
                                select.name,
                                guild_id,
                                message_id.replace("-", "/"),
                            )


