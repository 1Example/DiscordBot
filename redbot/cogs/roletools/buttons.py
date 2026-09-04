from __future__ import annotations

import discord
from red_commons.logging import getLogger
from redbot.core.i18n import Translator

from .abc import RoleToolsMixin
from .components import ButtonRole, RoleToolsView

roletools = RoleToolsMixin.roletools

log = getLogger("red.Trusty-cogs.RoleTools")
_ = Translator("RoleTools", __file__)


class RoleToolsButtons(RoleToolsMixin):
    """This class handles setting up button roles"""

    async def initialize_buttons(self):
        for guild_id, settings in self.settings.items():
            if guild_id not in self.views:
                log.trace("Adding guild ID %s to views in buttons", guild_id)
                self.views[guild_id] = {}
            for button_name, button_data in settings["buttons"].items():
                log.verbose("Adding Button %s", button_name)
                role_id = button_data["role_id"]
                emoji = button_data["emoji"]
                if emoji is not None:
                    emoji = discord.PartialEmoji.from_str(emoji)

                guild = self.bot.get_guild(guild_id)
                for message_id in set(button_data.get("messages", [])):
                    # we need a new instance of this object for every message
                    button = ButtonRole(
                        style=button_data["style"],
                        label=button_data["label"],
                        emoji=emoji,
                        custom_id=f"{button_name}-{role_id}",
                        role_id=role_id,
                        name=button_name,
                    )
                    if guild is not None:
                        button.replace_label(guild)
                    if message_id not in self.views[guild_id]:
                        log.trace("Creating view for button %s", button_name)
                        self.views[guild_id][message_id] = RoleToolsView(self)
                    if button.custom_id not in {
                        c.custom_id for c in self.views[guild_id][message_id].children
                    }:
                        try:
                            self.views[guild_id][message_id].add_item(button)
                        except ValueError:
                            log.error(
                                "There was an error adding button %s on message https://discord.com/channels/%s/%s",
                                button.name,
                                guild_id,
                                message_id.replace("-", "/"),
                            )


