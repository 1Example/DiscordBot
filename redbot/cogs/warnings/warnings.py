import asyncio
import contextlib
from collections import namedtuple
from copy import copy
from typing import Literal

import discord
from discord import app_commands

from redbot.cogs.warnings.helpers import warning_points_add_check, warning_points_remove_check
from redbot.core import Config, commands, modlog
from redbot.core.app_commands import checks as app_checks
from redbot.core.bot import Red
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.chat_formatting import pagify, warning
from redbot.core.utils.views import ConfirmView
from .dashboard_integration import DashboardIntegration


_ = Translator("Warnings", __file__)


@cog_i18n(_)
class Warnings(DashboardIntegration, commands.Cog):
    """Warn misbehaving users and take automated actions."""

    default_guild = {
        "actions": [],
        "reasons": {},
        "allow_custom_reasons": False,
        "toggle_dm": True,
        "show_mod": False,
        "warn_channel": None,
        "toggle_channel": False,
        "mywarnings_in_dms": False,
    }

    default_member = {"total_points": 0, "status": "", "warnings": {}}

    def __init__(self, bot: Red):
        super().__init__()
        self.config = Config.get_conf(self, identifier=5757575755)
        self.config.register_guild(**self.default_guild)
        self.config.register_member(**self.default_member)
        self.bot = bot

    async def cog_load(self) -> None:
        await self.register_warningtype()

    async def red_delete_data_for_user(
        self,
        *,
        requester: Literal["discord_deleted_user", "owner", "user", "user_strict"],
        user_id: int,
    ):
        if requester != "discord_deleted_user":
            return

        all_members = await self.config.all_members()

        c = 0

        for guild_id, guild_data in all_members.items():
            c += 1
            if not c % 100:
                await asyncio.sleep(0)

            if user_id in guild_data:
                await self.config.member_from_ids(guild_id, user_id).clear()

            for remaining_user, user_warns in guild_data.items():
                c += 1
                if not c % 100:
                    await asyncio.sleep(0)

                for warn_id, warning in user_warns.get("warnings", {}).items():
                    c += 1
                    if not c % 100:
                        await asyncio.sleep(0)

                    if warning.get("mod", 0) == user_id:
                        grp = self.config.member_from_ids(guild_id, remaining_user)
                        await grp.set_raw("warnings", warn_id, "mod", value=0xDE1)

    # We're not utilising modlog yet - no need to register a casetype
    @staticmethod
    async def register_warningtype():
        casetypes_to_register = [
            {
                "name": "warning",
                "default_setting": True,
                "image": "\N{WARNING SIGN}\N{VARIATION SELECTOR-16}",
                "case_str": "Warning",
            },
            {
                "name": "unwarned",
                "default_setting": True,
                "image": "\N{WARNING SIGN}\N{VARIATION SELECTOR-16}",
                "case_str": "Unwarned",
            },
        ]
        try:
            await modlog.register_casetypes(casetypes_to_register)
        except RuntimeError:
            pass

    @app_commands.command(
        name="warn",
        description="Warn a member.",
        extras={"red_force_enable": True},
    )
    @app_commands.guild_only()
    @app_checks.admin_or_permissions(ban_members=True)
    @app_commands.describe(
        user="Who to warn.",
        reason="A registered reason name, or free text if custom reasons are allowed.",
        points="How many points this warning is worth. Preset reasons ignore it.",
    )
    async def warn(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str,
        points: app_commands.Range[int, 1, 100] = 1,
    ):
        """Warn the user for the specified reason.

        `<points>` number of points the warning should be for. If no number is supplied
        1 point will be given. Pre-set warnings disregard this.
        `<reason>` is reason for the warning. This can be a registered reason,
        or a custom reason if ``[p]warningset allowcustomreasons`` is set.
        """
        ctx = await commands.Context.from_interaction(interaction)
        guild = ctx.guild
        member = None
        if isinstance(user, discord.Member):
            member = user
        elif isinstance(user, int):
            if not ctx.channel.permissions_for(ctx.guild.me).ban_members:
                await ctx.send(_("User `{user}` is not in the server.").format(user=user))
                return
            user_obj = self.bot.get_user(user) or discord.Object(id=user)

            confirm = ConfirmView(ctx.author, timeout=30)
            confirm.message = await ctx.send(
                _(
                    "User `{user}` is not in the server. Would you like to ban them instead?"
                ).format(user=user),
                view=confirm,
            )
            await confirm.wait()
            if confirm.result:
                try:
                    await ctx.guild.ban(user_obj, reason=reason)
                    await modlog.create_case(
                        self.bot,
                        guild,
                        ctx.message.created_at,
                        "hackban",
                        user,
                        ctx.author,
                        reason,
                        until=None,
                        channel=None,
                    )
                except discord.HTTPException as error:
                    await ctx.send(
                        _("An error occurred while trying to ban the user. Error: {error}").format(
                            error=error
                        )
                    )
            else:
                confirm.message = await ctx.send(_("No action taken."))

            await ctx.tick()
            return

        if member == ctx.author:
            return await ctx.send(_("You cannot warn yourself."))
        if member.bot:
            return await ctx.send(_("You cannot warn other bots."))
        if member == ctx.guild.owner:
            return await ctx.send(_("You cannot warn the server owner."))
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send(
                _(
                    "The person you're trying to warn is equal or higher than you in the discord hierarchy, you cannot warn them."
                )
            )
        guild_settings = await self.config.guild(ctx.guild).all()
        custom_allowed = guild_settings["allow_custom_reasons"]

        reason_type = None
        async with self.config.guild(ctx.guild).reasons() as registered_reasons:
            if (reason_type := registered_reasons.get(reason.lower())) is None:
                msg = _("That is not a registered reason!")
                if custom_allowed:
                    if points < 0:
                        return await ctx.send(_("You cannot apply negative points."))
                    reason_type = {"description": reason, "points": points}
                else:
                    if await self.bot.is_admin(ctx.author):
                        msg += " " + _(
                            "Custom reasons are off for this server. Turn them on "
                            "under Warnings on the dashboard."
                        )
                    return await ctx.send(msg)
        if reason_type is None:
            return
        member_settings = self.config.member(member)
        current_point_count = await member_settings.total_points()
        warning_to_add = {
            str(ctx.message.id): {
                "points": reason_type["points"],
                "description": reason_type["description"],
                "mod": ctx.author.id,
            }
        }
        dm = guild_settings["toggle_dm"]
        showmod = guild_settings["show_mod"]
        dm_failed = False
        if dm:
            if showmod:
                title = _("Warning from {user}").format(user=ctx.author)
            else:
                title = _("Warning")
            em = discord.Embed(
                title=title,
                description=reason_type["description"],
                color=await ctx.embed_colour(),
            )
            em.add_field(name=_("Points"), value=str(reason_type["points"]))
            try:
                await member.send(
                    _("You have received a warning in {guild_name}.").format(
                        guild_name=ctx.guild.name
                    ),
                    embed=em,
                )
            except discord.HTTPException:
                dm_failed = True

        if dm_failed:
            await ctx.send(
                _(
                    "A warning for {user} has been issued,"
                    " but I wasn't able to send them a warn message."
                ).format(user=member.mention)
            )
        async with member_settings.warnings() as user_warnings:
            user_warnings.update(warning_to_add)
        current_point_count += reason_type["points"]
        await member_settings.total_points.set(current_point_count)
        await warning_points_add_check(self.config, ctx, member, current_point_count)

        toggle_channel = guild_settings["toggle_channel"]
        if toggle_channel:
            if showmod:
                title = _("Warning from {user}").format(user=ctx.author)
            else:
                title = _("Warning")
            em = discord.Embed(
                title=title,
                description=reason_type["description"],
                color=await ctx.embed_colour(),
            )
            em.add_field(name=_("Points"), value=str(reason_type["points"]))
            warn_channel = self.bot.get_channel(guild_settings["warn_channel"])
            if warn_channel:
                if warn_channel.permissions_for(guild.me).send_messages:
                    with contextlib.suppress(discord.HTTPException):
                        await warn_channel.send(
                            _("{user} has been warned.").format(user=member.mention),
                            embed=em,
                        )

            if not dm_failed:
                if warn_channel:
                    await ctx.tick()
                else:
                    await ctx.send(
                        _("{user} has been warned.").format(user=member.mention),
                        embed=em,
                    )
        else:
            if not dm_failed:
                await ctx.tick()
        reason_msg = _(
            "{reason}\n\nUse `{prefix}unwarn {user} {message}` to remove this warning."
        ).format(
            reason=_("{description}\nPoints: {points}").format(
                description=reason_type["description"], points=reason_type["points"]
            ),
            prefix=ctx.clean_prefix,
            user=member.id,
            message=ctx.message.id,
        )
        await modlog.create_case(
            self.bot,
            ctx.guild,
            ctx.message.created_at,
            "warning",
            member,
            ctx.message.author,
            reason_msg,
            until=None,
            channel=None,
        )

    @warn.autocomplete("reason")
    async def _warn_reason_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice]:
        """The reasons registered on this server, which the page maintains.

        Free text still goes through, for servers that allow custom reasons.
        """
        if interaction.guild is None:
            return []
        reasons = await self.config.guild(interaction.guild).reasons()
        current = current.lower()
        return [
            app_commands.Choice(
                name=f"{name} ({data['points']} points)"[:100], value=name
            )
            for name, data in sorted(reasons.items())
            if current in name.lower()
        ][:25]

    @app_commands.command(
        name="warnings",
        description="Show a member's warnings.",
        extras={"red_force_enable": True},
    )
    @app_commands.guild_only()
    @app_checks.admin()
    @app_commands.describe(member="Whose warnings to show.")
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        """List the warnings for the specified user."""
        ctx = await commands.Context.from_interaction(interaction)
        try:
            userid: int = member.id
        except AttributeError:
            userid: int = member
            member = ctx.guild.get_member(userid)
            member = member or namedtuple("Member", "id guild")(userid, ctx.guild)

        msg = ""
        member_settings = self.config.member(member)
        async with member_settings.warnings() as user_warnings:
            if not user_warnings.keys():  # no warnings for the user
                await ctx.send(_("That user has no warnings!"))
            else:
                for key in user_warnings.keys():
                    mod_id = user_warnings[key]["mod"]
                    if mod_id == 0xDE1:
                        mod = _("Deleted Moderator")
                    else:
                        mod = ctx.bot.get_user(mod_id) or _("Unknown Moderator ({})").format(
                            mod_id
                        )
                    msg += _(
                        "{num_points} point warning {reason_name} issued by {user} for "
                        "{description}\n"
                    ).format(
                        num_points=user_warnings[key]["points"],
                        reason_name=key,
                        user=mod,
                        description=user_warnings[key]["description"],
                    )
                await ctx.send_interactive(
                    pagify(msg, shorten_by=58),
                    box_lang=_("Warnings for {user}").format(
                        user=member if isinstance(member, discord.Member) else member.id
                    ),
                )

    @app_commands.command(
        name="mywarnings",
        description="Show your own warnings.",
        extras={"red_force_enable": True},
    )
    @app_commands.guild_only()
    async def mywarnings(self, interaction: discord.Interaction):
        """List warnings for yourself."""
        ctx = await commands.Context.from_interaction(interaction)
        user = ctx.author
        msg = ""
        guild_settings = await self.config.guild(ctx.guild).all()
        member_settings = self.config.member(user)
        user_warnings = await member_settings.warnings()
        if not user_warnings:  # no warnings for the user
            if guild_settings["mywarnings_in_dms"]:
                try:
                    await user.send(_("You have no warnings!"))
                except discord.Forbidden:
                    await ctx.send(_("I could not send you a DM. Do you have DMs disabled?"))
                else:
                    await ctx.tick()
            else:
                await ctx.send(_("You have no warnings!"))
            return

        for key in user_warnings:
            mod_id = user_warnings[key]["mod"]
            if mod_id == 0xDE1:
                mod = _("Deleted Moderator")
            elif not guild_settings["show_mod"]:
                mod = None
            else:
                bot = ctx.bot
                mod = bot.get_user(mod_id) or _("Unknown Moderator ({})").format(mod_id)
            msg += _("{num_points} point warning {reason_name}").format(
                num_points=user_warnings[key]["points"],
                reason_name=key,
            )
            if mod is not None:
                msg += _(" issued by {user}").format(user=mod)
            msg += _(" for {description}\n").format(description=user_warnings[key]["description"])

        if guild_settings["mywarnings_in_dms"]:
            try:
                await ctx.bot.send_interactive(
                    channel=user,
                    messages=pagify(msg, shorten_by=58),
                    user=user,
                    box_lang=_("Warnings for {user}").format(user=user),
                )
            except discord.Forbidden:
                await ctx.send(_("I could not send you a DM. Do you have DMs disabled?"))
            else:
                await ctx.tick()

        else:
            await ctx.send_interactive(
                pagify(msg, shorten_by=58),
                box_lang=_("Warnings for {user}").format(user=user),
            )

    @app_commands.command(
        name="unwarn",
        description="Remove a warning from a member.",
        extras={"red_force_enable": True},
    )
    @app_commands.guild_only()
    @app_checks.admin_or_permissions(ban_members=True)
    @app_commands.describe(
        member="Whose warning to remove.",
        warn_id="The warning's id, as shown by /warnings.",
        reason="Why it is being removed.",
    )
    async def unwarn(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        warn_id: str,
        reason: str = None,
    ):
        """Remove a warning from a user."""
        ctx = await commands.Context.from_interaction(interaction)

        guild = ctx.guild

        try:
            user_id = member.id
            member = member
        except AttributeError:
            user_id = member
            member = guild.get_member(user_id)
            member = member or namedtuple("Member", "guild id")(guild, user_id)

        if user_id == ctx.author.id:
            return await ctx.send(_("You cannot remove warnings from yourself."))

        member_settings = self.config.member(member)
        current_point_count = await member_settings.total_points()
        await warning_points_remove_check(self.config, ctx, member, current_point_count)
        async with member_settings.warnings() as user_warnings:
            if warn_id not in user_warnings.keys():
                return await ctx.send(_("That warning doesn't exist!"))
            else:
                current_point_count -= user_warnings[warn_id]["points"]
                await member_settings.total_points.set(current_point_count)
                user_warnings.pop(warn_id)
        await modlog.create_case(
            self.bot,
            ctx.guild,
            ctx.message.created_at,
            "unwarned",
            member,
            ctx.message.author,
            reason,
            until=None,
            channel=None,
        )

        await ctx.tick()
