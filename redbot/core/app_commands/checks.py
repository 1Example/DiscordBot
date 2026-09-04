########## SENSITIVE SECTION WARNING ###########
################################################
# Any edits of any of the exported names       #
# may result in a breaking change.             #
# Ensure no names are removed without warning. #
################################################

### DEP-WARN: Check this *every* discord.py update
from discord.app_commands.checks import (
    bot_has_permissions,
    cooldown,
    dynamic_cooldown,
    has_any_role,
    has_role,
    has_permissions,
)

__all__ = (
    "bot_has_permissions",
    "cooldown",
    "dynamic_cooldown",
    "has_any_role",
    "has_role",
    "has_permissions",
    # Red's own privilege checks, defined below.
    "is_owner",
    "guildowner",
    "guildowner_or_permissions",
    "admin",
    "admin_or_permissions",
    "mod",
    "mod_or_permissions",
)


# ---------------------------------------------------------------------------
# Red's own privilege checks, for application commands.
#
# `redbot.core.commands` has these for text commands, but they are written
# against `Context` and cannot be reused on an app command, which is handed an
# `Interaction` instead. Cogs migrating to slash commands need them, so rather
# than each one growing its own copy they live here beside the checks
# re-exported from discord.py above.
#
# Each mirrors the text-command version, including the rule that a bot owner
# passes everything and that `PrivilegeLevel` is cumulative - an admin
# satisfies a mod check.
# ---------------------------------------------------------------------------

import discord
from discord import app_commands as _app_commands


def _has_perms(member: discord.Member, perms: dict) -> bool:
    if not perms:
        return False
    resolved = member.guild_permissions
    return any(getattr(resolved, name, False) for name, value in perms.items() if value)


def is_owner():
    """Only a bot owner may run this command."""

    async def predicate(interaction: discord.Interaction) -> bool:
        return await interaction.client.is_owner(interaction.user)

    return _app_commands.check(predicate)


def guildowner_or_permissions(**perms: bool):
    """The server owner, anyone holding one of `perms`, or a bot owner."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if await interaction.client.is_owner(interaction.user):
            return True
        if interaction.guild is None:
            return False
        if interaction.user.id == interaction.guild.owner_id:
            return True
        return _has_perms(interaction.user, perms)

    return _app_commands.check(predicate)


def guildowner():
    """Only the server owner, or a bot owner."""
    return guildowner_or_permissions()


def admin_or_permissions(**perms: bool):
    """Red's admin role, anyone holding one of `perms`, or above."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if await interaction.client.is_owner(interaction.user):
            return True
        if interaction.guild is None:
            return False
        if interaction.user.id == interaction.guild.owner_id:
            return True
        if _has_perms(interaction.user, perms):
            return True
        return await interaction.client.is_admin(interaction.user)

    return _app_commands.check(predicate)


def admin():
    """Red's admin role, or above."""
    return admin_or_permissions()


def mod_or_permissions(**perms: bool):
    """Red's mod role, anyone holding one of `perms`, or above."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if await interaction.client.is_owner(interaction.user):
            return True
        if interaction.guild is None:
            return False
        if interaction.user.id == interaction.guild.owner_id:
            return True
        if _has_perms(interaction.user, perms):
            return True
        # Cumulative: an admin satisfies a mod check.
        return await interaction.client.is_mod(interaction.user)

    return _app_commands.check(predicate)


def mod():
    """Red's mod role, or above."""
    return mod_or_permissions()
