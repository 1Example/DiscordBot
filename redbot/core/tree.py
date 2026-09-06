import discord
from discord.abc import Snowflake
from discord.utils import MISSING

from .app_commands import (
    AppCommand,
    AppCommandError,
    BotMissingPermissions,
    CheckFailure,
    Command,
    CommandAlreadyRegistered,
    CommandInvokeError,
    CommandNotFound,
    CommandOnCooldown,
    CommandTree,
    ContextMenu,
    Group,
    NoPrivateMessage,
    TransformerError,
    UserFeedbackCheckFailure,
)
from redbot.core.i18n import (
    Translator,
    set_contextual_locales_from_guild,
)
from .utils.chat_formatting import humanize_list, inline

import logging
import traceback
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Tuple, Union, Optional, Sequence

__all__ = ("RedTree",)

log = logging.getLogger("red")

_ = Translator(__name__, __file__)


class RedTree(CommandTree):
    """A container that holds application command information.

    Commands are added as they are registered. Red used to hold anything
    not marked ``red_force_enable`` in a second store until someone ran
    ``[p]slash enable`` and ``[p]slash sync``; nothing in this fork ships
    without that mark, so the store was always empty and the two commands
    had nothing to do.
    See ``discord.app_commands.CommandTree`` for more information.
    """

    async def red_check_enabled(self) -> None:
        """Kept for callers outside this repository; it has nothing to do.

        Commands used to be held back until they were enabled, and this moved
        the enabled ones into the tree. They are added when they are
        registered now, so by the time anyone can call this the tree is
        already what it is going to be.

        AAA3A_utils calls this from ``add_hybrid_commands``, which is why it
        is still here rather than deleted.
        """

    async def sync(self, *args, guild: Optional[Snowflake] = None, **kwargs) -> List[AppCommand]:
        """Wrapper to store command IDs when commands are synced."""
        commands = await super().sync(*args, guild=guild, **kwargs)
        if guild:
            return commands
        async with self.client._config.all() as cfg:
            for command in commands:
                if command.type is discord.AppCommandType.chat_input:
                    cfg["enabled_slash_commands"][command.name] = command.id
                elif command.type is discord.AppCommandType.message:
                    cfg["enabled_message_commands"][command.name] = command.id
                elif command.type is discord.AppCommandType.user:
                    cfg["enabled_user_commands"][command.name] = command.id
        return commands

    def red_fingerprint(self) -> str:
        """A stable digest of everything Discord would be told about this tree.

        Covers names, descriptions, option shapes and permission defaults, at
        every nesting level - the things a sync actually publishes. Two runs
        with an unchanged tree produce the same digest, so the bot can tell
        whether a sync would be a no-op.
        """
        import hashlib
        import json

        def enum_value(x):
            """discord.py's enums are not plain ints across versions."""
            return getattr(x, "value", x)

        def shape(command) -> dict:
            data = {
                "n": command.name,
                "d": getattr(command, "description", "") or "",
                "t": str(enum_value(getattr(command, "type", None))),
                "dm": bool(getattr(command, "allowed_contexts", None) is None
                           and getattr(command, "dm_permission", True)),
                "p": str(getattr(command, "default_permissions", None)),
            }
            params = getattr(command, "parameters", None)
            if params:
                data["o"] = [
                    {
                        "n": p.name,
                        "d": p.description or "",
                        "r": bool(p.required),
                        "t": str(enum_value(p.type)),
                        "c": [str(c.value) for c in (p.choices or [])],
                    }
                    for p in params
                ]
            subs = getattr(command, "commands", None)
            if subs:
                data["s"] = [shape(c) for c in sorted(subs, key=lambda c: c.name)]
            return data

        payload = [shape(c) for c in sorted(self.get_commands(), key=lambda c: c.name)]
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    async def red_auto_sync(self, *, force: bool = False) -> bool:
        """Publish the tree to Discord, but only when it has actually changed.

        Command creates are rate limited, and a sync that changes nothing still
        spends one, so this compares a fingerprint of the tree against the last
        published one and returns without calling Discord when they match.

        Returns True when a sync was performed.
        """
        current = self.red_fingerprint()
        if not force and getattr(self, "_red_rejected_fingerprint", None) == current:
            # Discord already refused this exact tree; the error was logged the
            # first time and nothing has changed since.
            return False
        if not force:
            try:
                previous = await self.client._config.app_command_fingerprint()
            except Exception:  # noqa: BLE001 - never block startup on config
                previous = ""
            if previous == current:
                return False
        try:
            await self.sync()
        except discord.HTTPException as exc:
            # A rejected sync must not take the bot down with it; the usual
            # causes are a description outside Discord's 1-100 characters, or
            # more than 100 top-level commands.
            log.error(
                "Could not publish application commands to Discord: %s. "
                "The tree currently has %d top-level command(s); Discord allows 100.",
                exc, len(self.get_commands()),
            )
            # A 4xx means *this tree* is invalid, so sending it again changes
            # nothing - and every cog load would send it again. Remember the
            # attempt so it is not retried until the tree itself changes.
            if 400 <= exc.status < 500:
                self._red_rejected_fingerprint = current
            return False
        self._red_rejected_fingerprint = None
        await self.client._config.app_command_fingerprint.set(current)
        log.info("Application commands published to Discord (%d top-level).",
                 len(self.get_commands()))
        return True

    @staticmethod
    async def _send_from_interaction(interaction, *args, **kwargs):
        """Util for safely sending a message from an interaction."""
        if interaction.response.is_done():
            if interaction.is_expired():
                return await interaction.channel.send(*args, **kwargs)
            delete_after = kwargs.pop("delete_after", None)
            kwargs["wait"] = True
            msg = await interaction.followup.send(*args, ephemeral=True, **kwargs)
            if delete_after is not None:
                await msg.delete(delay=delete_after)
            return msg
        return await interaction.response.send_message(*args, ephemeral=True, **kwargs)

    async def on_error(
        self, interaction: discord.Interaction, error: AppCommandError, /, *args, **kwargs
    ) -> None:
        """Fallback error handler for app commands."""
        if isinstance(error, CommandNotFound):
            await self._send_from_interaction(interaction, _("Command not found."))
            log.warning(
                f"Application command {error.name} could not be resolved. "
                "It may be from a cog that was updated or unloaded. "
                "It will go once the tree next syncs."
            )
        elif isinstance(error, CommandInvokeError):
            log.exception(
                "Exception in command '{}'".format(error.command.qualified_name),
                exc_info=error.original,
            )
            exception_log = "Exception in command '{}'\n" "".format(error.command.qualified_name)
            exception_log += "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
            interaction.client._last_exception = exception_log

            message = await interaction.client._config.invoke_error_msg()
            if not message:
                if interaction.user.id in interaction.client.owner_ids:
                    message = inline(
                        _("Error in command '{command}'. Check your console or logs for details.")
                    )
                else:
                    message = inline(_("Error in command '{command}'."))
            await self._send_from_interaction(
                interaction, message.replace("{command}", error.command.qualified_name)
            )
        elif isinstance(error, TransformerError):
            if error.__cause__:
                log.exception("Error in an app command transformer.", exc_info=error.__cause__)
            await self._send_from_interaction(interaction, str(error))
        elif isinstance(error, BotMissingPermissions):
            formatted = [
                '"' + perm.replace("_", " ").title() + '"' for perm in error.missing_permissions
            ]
            formatted = humanize_list(formatted).replace("Guild", "Server")
            if len(error.missing_permissions) == 1:
                msg = _("I require the {permission} permission to execute that command.").format(
                    permission=formatted
                )
            else:
                msg = _("I require {permission_list} permissions to execute that command.").format(
                    permission_list=formatted
                )
            await self._send_from_interaction(interaction, msg)
        elif isinstance(error, NoPrivateMessage):
            # Seems to be only called normally by the has_role check
            await self._send_from_interaction(
                interaction, _("That command is not available in DMs.")
            )
        elif isinstance(error, CommandOnCooldown):
            relative_time = discord.utils.format_dt(
                datetime.now(timezone.utc) + timedelta(seconds=error.retry_after), "R"
            )
            msg = _("This command is on cooldown. Try again {relative_time}.").format(
                relative_time=relative_time
            )
            await self._send_from_interaction(interaction, msg, delete_after=error.retry_after)
        elif isinstance(error, UserFeedbackCheckFailure):
            if error.message:
                await self._send_from_interaction(interaction, error.message)
        elif isinstance(error, CheckFailure):
            await self._send_from_interaction(
                interaction, _("You are not permitted to use this command.")
            )
        else:
            log.exception(type(error).__name__, exc_info=error)

    async def _send_interaction_check_failure(
        self, interaction: discord.Interaction, message: str
    ):
        """Handles responding to interaction check failures.
        Mainly used for when an interaction is an autocomplete and
        providing the message in the autocomplete response.
        """
        if interaction.type is discord.InteractionType.autocomplete:
            await interaction.response.autocomplete(
                [discord.app_commands.Choice(name=message[:80], value="None")]
            )
            return
        await interaction.response.send_message(message, ephemeral=True)

    async def interaction_check(self, interaction: discord.Interaction):
        """Global checks for app commands."""
        if interaction.user.bot:
            return False

        # A command marked always-available skips these. The data
        # protection commands are the reason: the allowlist and the
        # blocklist shut out exactly the people most likely to want their
        # data deleted, and the prefix versions bypassed checks through
        # _AlwaysAvailableCommand for that reason. discord.py does not set
        # interaction.command until after this runs, so the root name comes
        # off the raw payload.
        root = (interaction.data or {}).get("name")
        command = self.get_command(root) if root else None
        if command is not None and command.extras.get("red_always_available"):
            return True

        if interaction.guild:
            if not (await self.client.ignored_channel_or_guild(interaction)):
                await self._send_interaction_check_failure(
                    interaction, _("This channel or server is ignored.")
                )
                return False

        if not (await self.client.allowed_by_whitelist_blacklist(interaction.user)):
            await self._send_interaction_check_failure(
                interaction,
                _("You are not permitted to use commands because of an allowlist or blocklist."),
            )
            return False

        return True

    # DEP-WARN
    async def _call(self, interaction: discord.Interaction, *args, **kwargs) -> None:
        """Configure the contextual locale based on the interaction guild prior to invoking."""
        await set_contextual_locales_from_guild(interaction.client, interaction.guild)
        await super()._call(interaction, *args, **kwargs)
