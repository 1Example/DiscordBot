from __future__ import annotations

from typing import List

import discord
from red_commons.logging import getLogger
from redbot.core import commands
from redbot.core.commands import Context
from redbot.core.i18n import Translator

from .abc import RoleToolsMixin
from .components import ButtonRole, SelectRole

roletools = RoleToolsMixin.roletools

log = getLogger("red.Trusty-cogs.RoleTools")
_ = Translator("RoleTools", __file__)


class RoleToolsMessages(RoleToolsMixin):

    async def check_totals(self, ctx: commands.Context, buttons: int, menus: int) -> bool:
        menus_total = menus * 5
        total = buttons + menus_total
        if total > 25:
            await ctx.send(
                _(
                    "You have a maximum of 25 slots per message for buttons and menus. "
                    "Buttons count as 1 slot each and menus count as 5 slots each."
                )
            )
            return False
        return True


    async def save_settings(
        self,
        guild: discord.Guild,
        message_key: str,
        *,
        buttons: List[ButtonRole] = [],
        select_menus: List[SelectRole] = [],
    ):
        async with self.config.guild(guild).select_menus() as saved_select_menus:
            for select in select_menus:
                messages = set(saved_select_menus[select.name]["messages"])
                messages.add(message_key)
                saved_select_menus[select.name]["messages"] = list(messages)
                self.settings[guild.id]["select_menus"][select.name]["messages"] = list(messages)
        async with self.config.guild(guild).buttons() as saved_buttons:
            for button in buttons:
                messages = set(saved_buttons[button.name]["messages"])
                messages.add(message_key)
                saved_buttons[button.name]["messages"] = list(messages)
                self.settings[guild.id]["buttons"][button.name]["messages"] = list(messages)

    async def check_and_replace_existing(self, guild_id: int, message_key: str):
        if guild_id not in self.views:
            return
        if message_key not in self.views[guild_id]:
            return
        for c in self.views[guild_id][message_key].children:
            if isinstance(c, SelectRole):
                existing = self.settings[guild_id]["select_menus"].get(c.name, {})
                if message_key in existing.get("messages", []):
                    self.settings[guild_id]["select_menus"][c.name]["messages"].remove(message_key)
            elif isinstance(c, ButtonRole):
                existing = self.settings[guild_id]["buttons"].get(c.name, {})
                if message_key in existing.get("messages", []):
                    self.settings[guild_id]["buttons"][c.name]["messages"].remove(message_key)
        await self.config.guild_from_id(guild_id).buttons.set(self.settings[guild_id]["buttons"])
        await self.config.guild_from_id(guild_id).select_menus.set(
            self.settings[guild_id]["select_menus"]
        )


