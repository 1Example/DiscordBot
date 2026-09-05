import datetime
import typing
from copy import deepcopy

import discord

from discord.ext import tasks

from redbot.core import Config, app_commands, commands
from redbot.core.bot import Red
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.commands.converter import parse_timedelta
from redbot.core.app_commands import checks as app_checks
from redbot.core.utils.chat_formatting import humanize_timedelta
from redbot.core.utils.cog_base import CogBase
from redbot.core.utils.views import SimpleMenu, confirm as ask_confirmation

from .anomalies import ANOMALIES, Anomaly
from .constants import ACHIEVEMENTS_COLOR, DEVELOPER, HELPERS, SUPPORTERS, TESTERS
from .game import Game
from .modes import MODES, Mode
from .roles import ACHIEVEMENTS, ROLES, Developer, GodFather, Role, Villager
from .dashboard_integration import DashboardIntegration
from .views import ExplainView, JoinGameView, PollView

# Credits:
# General repo credits.
# Thanks to C & Masterodeath22 for all their explanations and help!
# Thanks to the existing Mafia bots for the inspiration!
# A part of the text for the roles' `description` and `ability` fields has been taken from the Mafia Wiki (https://mafiabot.fandom.com/wiki/Roles)!
# All the images have been generated with Microsoft Copilot (https://copilot.microsoft.com/)!
# Thanks to the https://onepiece.fandom.com/ wiki for the One Piece theme's images!

_: Translator = Translator("MafiaGame", __file__)


class RoleConverter(commands.Converter):
    async def convert(self, ctx: commands.Context, argument: str) -> Role:
        argument = argument.lower().replace(" ", "")
        for role in ROLES:
            if argument == role.name.lower().replace(" ", ""):
                if ctx.command.name == "disabledroles" and isinstance(role, (GodFather, Villager)):
                    raise commands.BadArgument(
                        _("You can't disable the GodFather or the Villager role."),
                    )
                if role is not Developer or ctx.author.id == DEVELOPER:
                    return role
        raise commands.BadArgument(_("Invalid Mafia role."))


class RoleNameConverter(commands.Converter):
    async def convert(self, ctx: commands.Context, argument: str) -> str:
        return (await RoleConverter().convert(ctx, argument)).name


class ModeConverter(commands.Converter):
    async def convert(self, ctx: commands.Context, argument: str) -> Mode:
        argument = argument.lower()
        for mode in MODES:
            if argument == mode.name.lower():
                return mode
        raise commands.BadArgument(_("Invalid Mafia mode."))


class ModeNameConverter(commands.Converter):
    async def convert(self, ctx: commands.Context, argument: str) -> str:
        return (await ModeConverter().convert(ctx, argument)).name


class AnomalyConverter(commands.Converter):
    async def convert(self, ctx: commands.Context, argument: str) -> Anomaly:
        argument = argument.lower()
        for anomaly in ANOMALIES:
            if argument == anomaly.name.lower():
                return anomaly
        raise commands.BadArgument(_("Invalid Mafia anomaly."))


class AnomalyNameConverter(commands.Converter):
    async def convert(self, ctx: commands.Context, argument: str) -> str:
        return (await AnomalyConverter().convert(ctx, argument)).name


DurationConverter: commands.converter.TimedeltaConverter = commands.converter.TimedeltaConverter(
    minimum=datetime.timedelta(minutes=30),
    maximum=None,
    allowed_units=["weeks", "days", "hours", "minutes"],
    default_unit="hours",
)


@cog_i18n(_)
class MafiaGame(DashboardIntegration, CogBase):
    """Play the Mafia game, with many roles (Mafia/Villagers/Neutral), modes (including Random and Custom), anomalies...!"""

    def __init__(self, bot: Red) -> None:
        super().__init__(bot=bot)
        self.config: Config = Config.get_conf(
            self,
            identifier=205192943327321000143939875896557571750,
            force_registration=True,
        )
        self.config.register_guild(
            # General settings.
            category=None,
            allow_spectators=True,
            add_reactions=True,
            default_mode="Classic",
            disabled_roles=[],
            more_roles=True,
            custom_roles=[],
            display_roles_when_starting=False,
            afk_days_before_kick=None,
            afk_temp_ban_duration=None,
            channel_auto_delete=False,
            game_logs=False,
            ping_role=None,
            blacklisted_roles=[],
            poll_threshold=None,
            theme=None,
            # Game settings.
            show_dead_role=True,
            dying_message=False,
            anonymous_voting=False,
            defend_judgement=True,
            anonymous_judgement=False,
            mafia_communication=True,
            town_traitor=False,
            town_vip=False,
            anomalies=False,
            disabled_anomalies=[],
            # Roles settings.
            vigilante_shoot_night_1=False,
            alchemist_lethal_potion_night_1=True,
            hoarder_hoard_same_player_if_failed=False,
            judge_prosecute_day_1=True,
            # Timeouts.
            perform_action_timeout=60,
            talk_timeout=50,
            voting_timeout=45,
            defend_timeout=30,
            judgement_timeout=20,
            # Red economy's integration.
            red_economy=False,
            cost_to_play=50,
            reward_for_winning=100,
            reward_for_winning_based_on_costs=False,
        )
        self.config.register_user(
            wins={},
            games={},
            achievements={},
            default_dying_message=None,
        )
        self.config.register_member(
            temp_banned_until=None,
        )

        _settings: dict[str, dict[str, list[str] | bool | str]] = {
            # General settings.
            "category": {
                "converter": discord.CategoryChannel,
                "description": "The category where the channel will be created.",
            },
            "allow_spectators": {
                "converter": bool,
                "description": "If this option is enabled, the cog will allow spectators to watch the game.",
            },
            "add_reactions": {
                "converter": bool,
                "description": "If this option is enabled, the alive players will be able to react to the messages.",
            },
            "default_mode": {
                "converter": ModeNameConverter,
                "description": "The default mode that will be used.",
            },
            "disabled_roles": {
                "converter": commands.Greedy[RoleNameConverter],
                "description": "The roles that will be disabled.",
                "aliases": ["droles"],
            },
            "more_roles": {
                "converter": bool,
                "description": "If this option is enabled, the cog will add more roles to the game.",
            },
            "custom_roles": {
                "converter": commands.Greedy[RoleNameConverter],
                "description": "The roles that will be assigned at the beginning of the game, if the mode is `Custom`.",
                "aliases": ["croles"],
            },
            "display_roles_when_starting": {
                "converter": bool,
                "description": "If this option is enabled, the cog will display the roles in game and their abilities when starting.",
            },
            "afk_days_before_kick": {
                "converter": commands.Range[int, 2, 15],
                "description": "The number of days before a player is kicked for being AFK.",
            },
            "afk_temp_ban_duration": {
                "converter": commands.Range[int, 1, None],
                "description": "The duration in hours of the temp ban for being AFK.",
            },
            "channel_auto_delete": {
                "converter": bool,
                "description": "If this option is enabled, the channel will be automatically deleted after the game.",
            },
            "game_logs": {
                "converter": bool,
                "description": "If this option is enabled, the cog will log the game in an HTML file.",
            },
            "ping_role": {
                "converter": discord.Role,
                "description": "The role that will be pinged when the game starts.",
            },
            "blacklisted_roles": {
                "converter": commands.Greedy[discord.Role],
                "description": "The roles that will be blacklisted from the game.",
            },
            "poll_threshold": {
                "converter": commands.Range[int, 2, 25],
                "description": "The votes needed to start the game.",
                "no_slash": True,
            },
            "theme": {
                "converter": typing.Literal["one-piece"],
                "description": "The theme of the game.",
                "no_slash": True,
            },
            # Game settings.
            "show_dead_role": {
                "converter": bool,
                "description": "If this option is enabled, the cog will show the dead role to the players.",
            },
            "dying_message": {
                "converter": bool,
                "description": "If this option is enabled, the players will be able to set a custom death message.",
            },
            "anonymous_voting": {
                "converter": bool,
                "description": "If this option is enabled, the voting will be anonymous.",
            },
            "defend_judgement": {
                "converter": bool,
                "description": "If this option is enabled, the player who has been voted will be able to defend.",
            },
            "anonymous_judgement": {
                "converter": bool,
                "description": "If this option is enabled, the judgement will be anonymous.",
            },
            "mafia_communication": {
                "converter": bool,
                "description": "If this option is enabled, the Mafia members will be able to communicate.",
            },
            "town_traitor": {
                "converter": bool,
                "description": "Give the town a Traitor, who must be killed within 3 days of the last mafia death.",
            },
            "town_vip": {
                "converter": bool,
                "description": "If this option is enabled, the town will have a VIP who have to be killed by Mafia before win.",
            },
            "anomalies": {
                "converter": bool,
                "description": "If this option is enabled, the anomalies will be enabled.",
                "no_slash": True,
            },
            "disabled_anomalies": {
                "converter": commands.Greedy[AnomalyNameConverter],
                "description": "The anomalies that will be disabled.",
                "aliases": ["danomalies"],
                "no_slash": True,
            },
            # Roles settings.
            "vigilante_shoot_night_1": {
                "converter": bool,
                "description": "If this option is enabled, the Vigilante will be able to shoot on Night 1.",
                "no_slash": True,
            },
            "alchemist_lethal_potion_night_1": {
                "converter": bool,
                "description": "If this option is enabled, the Alchemist will be able to use the lethal potion on Night 1.",
                "no_slash": True,
            },
            "hoarder_hoard_same_player_if_failed": {
                "converter": bool,
                "description": "If this option is enabled, the Hoarder can hoard the same player again if they failed previously.",
                "no_slash": True,
            },
            "judge_prosecute_day_1": {
                "converter": bool,
                "description": "If this option is enabled, the Judge will be able to prosecute on Day 1.",
                "no_slash": True,
            },
            # Timeouts.
            "perform_action_timeout": {
                "converter": commands.Range[int, 10, 300],
                "description": "The time in seconds to perform an action.",
                "no_slash": True,
            },
            "talk_timeout": {
                "converter": commands.Range[int, 10, 300],
                "description": "The time in seconds to talk.",
                "no_slash": True,
            },
            "voting_timeout": {
                "converter": commands.Range[int, 10, 300],
                "description": "The time in seconds to vote.",
                "no_slash": True,
            },
            "defend_timeout": {
                "converter": commands.Range[int, 10, 300],
                "description": "The time in seconds to defend.",
                "no_slash": True,
            },
            "judgement_timeout": {
                "converter": commands.Range[int, 10, 300],
                "description": "The time in seconds to judge.",
                "no_slash": True,
            },
            # Red economy's integration.
            "red_economy": {
                "converter": bool,
                "description": "If this option is enabled, the cog will integrate with the Red economy.",
                "no_slash": True,
            },
            "cost_to_play": {
                "converter": commands.Range[int, 1, None],
                "description": "The cost to play the game.",
                "no_slash": True,
            },
            "reward_for_winning": {
                "converter": commands.Range[int, 1, None],
                "description": "The reward for winning the game.",
                "no_slash": True,
            },
            "reward_for_winning_based_on_costs": {
                "converter": bool,
                "description": "If this option is enabled, the reward for winning will be based on the costs and shared between the winners.",
                "no_slash": True,
            },
        }
        # The schema stays: the page reads it to decide what control to
        # draw for each setting. What went with AAA3A_utils' Settings is
        # the [p]setmafia subcommand it generated per entry - all 39 of
        # them are on the page.
        self._settings: dict[str, dict[str, typing.Any]] = _settings

        self.games: dict[discord.Guild, Game] = {}
        self.last_games: dict[discord.Guild, Game] = {}

    async def cog_load(self) -> None:
        await super().cog_load()
        check = tasks.loop(seconds=30, name="Check Temp Bans")(self.check_temp_bans)
        self.loops.append(check)
        check.start()
        if self.bot.get_cog("Dev") is not None:
            self.bot.add_dev_env_value(
                name="mafia_game",
                value=lambda ctx: self.games.get(ctx.guild) or self.last_games.get(ctx.guild),
            )

    async def cog_unload(self) -> None:
        if self.bot.get_cog("Dev") is not None:
            self.bot.remove_dev_env_value("mafia_game")
        await super().cog_unload()

    async def check_temp_bans(self) -> None:
        member_group = self.config._get_base_group(self.config.MEMBER)
        async with member_group.all() as members_data:
            _members_data = deepcopy(members_data)
            for guild_id in _members_data:
                for member_id in _members_data[guild_id]:
                    if (
                        temp_banned_until := _members_data[guild_id][member_id].get(
                            "temp_banned_until",
                        )
                    ) is not None and datetime.datetime.now(
                        tz=datetime.timezone.utc,
                    ) > datetime.datetime.fromtimestamp(
                        temp_banned_until,
                        tz=datetime.timezone.utc,
                    ):
                        del members_data[guild_id][member_id]["temp_banned_until"]

    @commands.Cog.listener()
    async def on_message_without_command(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is None:  # Communication between Mafia side's players.
            if (
                (
                    games := [
                        game
                        for game in self.games.values()
                        if game.get_player(message.author) is not None
                    ]
                )
                and len(games) == 1
                and (game := games[0])
                and game.config["mafia_communication"]
                and (player := game.get_player(message.author))
                and not player.is_dead
                and player.role is not None
                and (
                    (player.role.side == "Mafia" and player.role.name != "Alchemist")
                    or player.is_town_traitor
                )
            ):
                for p in game.alive_players:
                    if p.role is not None and (
                        (p.role.side == "Mafia" and p.role.name != "Alchemist") or p.is_town_traitor
                    ):  # and p != player
                        try:
                            await p.member.send(
                                f"📨 **{player.member.display_name} ({player.role.display_name(game)}{_(' - Town Traitor') if player.is_town_traitor else ''})**: {message.content}",
                            )
                        except discord.HTTPException:
                            pass
        elif (  # AFK management.
            (game := self.games.get(message.guild)) is not None
            and message.channel == game.channel
            and (player := game.get_player(message.author)) is not None
        ):
            game.afk_players.pop(player, None)

    async def cog_check(self, ctx: commands.Context) -> bool:
        if (
            ctx.interaction is not None
            and ctx.interaction.is_user_integration()
            and ctx.command.name in ("start", "tempban", "unban", "afkkill")
        ):
            raise commands.UserFeedbackCheckFailure(
                _("This command doesn't work as user installable."),
            )
        return True

    mafia = app_commands.Group(
        name="mafia",
        description="Play Mafia.",
        extras={"red_force_enable": True},
        guild_only=True,
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
    )

    @staticmethod
    async def _pick(ctx, pool, name: str, kind: str, required: bool = False):
        """One of `pool` by name, or False once the complaint is sent.

        The options autocomplete from these same lists, so a miss means
        somebody typed over the suggestion.
        """
        if name is None:
            if required:
                await ctx.send(_("Pick a {kind}.").format(kind=kind))
                return False
            return None
        wanted = name.lower().replace(" ", "")
        for item in pool:
            if wanted == item.name.lower().replace(" ", ""):
                if item is Developer and ctx.author.id != DEVELOPER:
                    break
                return item
        await ctx.send(_("`{name}` is not a Mafia {kind}.").format(name=name[:100], kind=kind))
        return False

    @staticmethod
    async def _duration(ctx, raw: str):
        """A ban length of at least thirty minutes, or None once refused."""
        delta = parse_timedelta(
            raw, allowed_units=["weeks", "days", "hours", "minutes"], default_unit="hours"
        )
        if delta is None or delta < datetime.timedelta(minutes=30):
            await ctx.send(_("Give a length of at least 30 minutes, like `2 days`."))
            return None
        return delta

    @staticmethod
    def _names(pool, current: str) -> list:
        """Autocomplete choices from one of the game's lists."""
        current = current.lower()
        return [
            app_commands.Choice(name=item.name[:100], value=item.name)
            for item in pool
            if current in item.name.lower()
        ][:25]

    @mafia.command(name="start", description="Start a game of Mafia.")
    @app_checks.admin_or_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @app_commands.describe(mode="Which mode to play. Leave empty for the server default.")
    async def start(
        self,
        interaction: discord.Interaction,
        mode: str = None,
    ) -> None:
        """Start a game of Mafia!"""
        ctx = await commands.Context.from_interaction(interaction)
        mode = await self._pick(ctx, MODES, mode, _("mode"))
        if mode is False:
            return
        if self.games.get(ctx.guild) is not None:
            raise commands.UserFeedbackCheckFailure(_("A game is already running in this guild."))
        config = await self.config.guild(ctx.guild).all()
        category = (
            category
            if (
                (category_id := config["category"]) is not None
                and (category := ctx.guild.get_channel(category_id)) is not None
            )
            else ctx.channel.category
        )
        if not (
            category.permissions_for(ctx.guild.me)
            if category is not None
            else ctx.me.guild_permissions
        ).manage_channels:
            raise commands.UserFeedbackCheckFailure(
                _("I don't have the permission to create channels in the category."),
            )
        join_view: JoinGameView = JoinGameView(
            self,
            MODES=MODES,
            mode=(
                mode
                if mode is not None
                else discord.utils.get(MODES, name=config["default_mode"] or "Classic")
            ),
            config=config,
        )
        await join_view.start(ctx)
        await join_view.wait()
        if join_view.cancelled:
            return
        config = await self.config.guild(ctx.guild).all()
        for key in ("show_dead_role", "dying_message", "town_traitor", "town_vip", "anomalies"):
            config[key] = join_view.config[key]
        players = join_view.players
        game: Game = Game(self, mode=join_view.mode, config=config)
        game.start_task(ctx, players=players)

    @mafia.command(name="end", description="End the game running here.")
    @app_checks.admin_or_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @app_commands.describe(confirm="Skip the are-you-sure prompt.")
    async def end(
        self,
        interaction: discord.Interaction,
        confirm: bool = False,
    ) -> None:
        """End the current game of Mafia."""
        ctx = await commands.Context.from_interaction(interaction)
        if (game := self.games.get(ctx.guild)) is None:
            raise commands.UserFeedbackCheckFailure(
                _("No game is currently running in this guild."),
            )
        if not confirm and not await ask_confirmation(
            ctx,
            _("Are you sure you want to end the current game of Mafia?"),
        ):
            return
        await game.end()

    @mafia.command(name="explain", description="Read up on how Mafia works.")
    @app_commands.describe(page="Which page of the guide.")
    async def explain(
        self,
        interaction: discord.Interaction,
        page: str = "main",
    ) -> None:
        """Explain how to play the Mafia game."""
        ctx = await commands.Context.from_interaction(interaction)
        await ExplainView(
            ROLES=ROLES,
            MODES=MODES,
            ANOMALIES=ANOMALIES,
            page=page,
        ).start(ctx)

    @mafia.command(name="role", description="Show what one role does.")
    @app_commands.describe(role="Which role.")
    async def role(
        self,
        interaction: discord.Interaction,
        role: str,
    ) -> None:
        """Show the informations about a specific role."""
        ctx = await commands.Context.from_interaction(interaction)
        role = await self._pick(ctx, ROLES, role, _("role"), required=True)
        if role is False:
            return
        theme = await self.config.guild(ctx.guild).theme()
        await SimpleMenu(pages=[role.get_kwargs(theme=theme)]).start(ctx)

    @mafia.command(name="roles", description="List every role in the game.")
    async def roles(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """Show the different roles of the Mafia game."""
        ctx = await commands.Context.from_interaction(interaction)
        theme = await self.config.guild(ctx.guild).theme()
        await SimpleMenu(
            pages=[
                role.get_kwargs(theme=theme)
                for role in ROLES
                if role is not Developer or ctx.author.id == DEVELOPER
            ],
        ).start(ctx)

    @mafia.command(name="mode", description="Show what one mode changes.")
    @app_commands.describe(mode="Which mode.")
    async def mode(
        self,
        interaction: discord.Interaction,
        mode: str,
    ) -> None:
        """Show the informations about a specific mode."""
        ctx = await commands.Context.from_interaction(interaction)
        mode = await self._pick(ctx, MODES, mode, _("mode"), required=True)
        if mode is False:
            return
        await SimpleMenu(pages=[mode.get_kwargs()]).start(ctx)

    @mafia.command(name="modes", description="List every mode.")
    async def modes(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """Show the different modes of the Mafia game."""
        ctx = await commands.Context.from_interaction(interaction)
        await SimpleMenu(
            pages=[mode.get_kwargs() for mode in MODES],
        ).start(ctx)

    @mafia.command(name="defaultdyingmessage", description="Set the message posted when you die, for every game.")
    @app_commands.describe(default_dying_message="Up to 200 characters. Leave empty to clear it.")
    async def defaultdyingmessage(
        self,
        interaction: discord.Interaction,
        default_dying_message: app_commands.Range[str, 1, 200] = None,
    ) -> None:
        """Set your default custom dying message."""
        ctx = await commands.Context.from_interaction(interaction)
        if default_dying_message is not None:
            await self.config.user(ctx.author).default_dying_message.set(default_dying_message)
        else:
            await self.config.user(ctx.author).default_dying_message.clear()

    @mafia.command(name="anomaly", description="Show what one anomaly does.")
    @app_commands.describe(anomaly="Which anomaly.")
    async def anomaly(
        self,
        interaction: discord.Interaction,
        anomaly: str,
    ) -> None:
        """Show the information about a specific anomaly."""
        ctx = await commands.Context.from_interaction(interaction)
        anomaly = await self._pick(ctx, ANOMALIES, anomaly, _("anomaly"), required=True)
        if anomaly is False:
            return
        await SimpleMenu(pages=[anomaly.get_kwargs()]).start(ctx)

    @mafia.command(name="anomalies", description="List every anomaly.")
    async def anomalies(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """Show the different anomalies of the Mafia game."""
        ctx = await commands.Context.from_interaction(interaction)
        await SimpleMenu(
            pages=[anomaly.get_kwargs() for anomaly in ANOMALIES],
        ).start(ctx)

    @anomaly.autocomplete("anomaly")
    async def mafia_anomaly_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=anomaly.name, value=anomaly.name)
            for anomaly in ANOMALIES
            if anomaly.name.lower().startswith(current.lower())
        ][:25]

    @mafia.command(name="achievements", description="Show someone's achievements.")
    @app_commands.describe(
        role="Only achievements for this role. Leave empty for all of them.",
        user="Whose achievements. Defaults to yours.",
    )
    async def achievements(
        self,
        interaction: discord.Interaction,
        role: str = None,
        user: discord.User = None,
    ) -> None:
        """Show your achievements or the achievements of a specific member."""
        ctx = await commands.Context.from_interaction(interaction)
        user = user or ctx.author
        role = await self._pick(ctx, ROLES, role, _("role"))
        if role is False:
            return
        if user.bot:
            raise commands.UserFeedbackCheckFailure(_("A bot can't play the Mafia game."))
        achievements = ACHIEVEMENTS if role is None else role.achievements
        user_data = await self.config.user(user).all()
        user_achievements = user_data["achievements"]
        theme = await self.config.guild(ctx.guild).theme()
        embed: discord.Embed = discord.Embed(
            title=(
                _("General Achievements")
                if role is None
                else _("Achievements — {role_name}").format(
                    role_name=role.display_name(theme=theme),
                )
            ),
            color=ACHIEVEMENTS_COLOR,
        )
        embed.set_author(name=user.display_name, icon_url=user.display_avatar)
        for achievement, data in achievements.items():
            embed.add_field(
                name=(
                    f"✅ ~~{achievement}~~"
                    if achievement
                    in user_achievements.get(str(None) if role is None else role.name, [])
                    else f"🔒 {achievement}"
                    + (
                        f" ({(sum(user_data[data['check']].values()) if role is None else user_data[data['check']].get(role.name, 0))}/{data['value']})"
                        if data["check"] in ("games", "wins")
                        else ""
                    )
                ),
                value=_(data["description"]),
                inline=True,
            )
        completed = len(user_achievements.get(str(None) if role is None else role.name, []))
        if completed == len(achievements):
            embed.set_footer(text=_("✅ All achievements completed!"))
        else:
            embed.set_footer(
                text=_("Completed {completed} out of {total} achievements ({percentage}%)!").format(
                    completed=completed,
                    total=len(achievements),
                    percentage=f"{completed / len(achievements) * 100:.2f}",
                ),
            )
        if role is None:
            if user.id == DEVELOPER:
                embed.add_field(
                    name=_("✨ Developer ✨"),
                    value=_("Has developed this Mafia game."),
                    inline=True,
                )
            if user.id in HELPERS:
                embed.add_field(
                    name=_("✨ Helper ✨"),
                    value=_("Has helped to create this Mafia game."),
                    inline=True,
                )
            if user.id in TESTERS:
                embed.add_field(
                    name=_("✨ Tester ✨"),
                    value=_("Has helped to test this Mafia game."),
                    inline=True,
                )
            if user.id in SUPPORTERS:
                embed.add_field(
                    name=_("✨ Supporter ✨"),
                    value=_("Has donated to support the developper."),
                    inline=True,
                )
        else:
            image = role.name.lower().replace(" ", "_")
            embed.set_thumbnail(url=f"attachment://{image}.png")
        await SimpleMenu(
            pages=[
                {
                    "embed": embed,
                    "file": None if role is None else role.get_image(theme=theme),
                },
            ],
        ).start(ctx)

    @achievements.autocomplete("role")
    @role.autocomplete("role")
    async def mafia_role_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=role.name, value=role.name)
            for role in ROLES
            if role.name.lower().startswith(current.lower())
        ][:25]

    @mafia.command(name="tempban", description="Ban a member from the next few games.")
    @app_checks.mod_or_permissions(manage_guild=True)
    @app_commands.describe(
        member="Who to ban.",
        duration="How long, e.g. 2 days. At least 30 minutes.",
    )
    async def tempban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration: str,
    ) -> None:
        """Ban a member temporary from the Mafia games in this server."""
        ctx = await commands.Context.from_interaction(interaction)
        duration = await self._duration(ctx, duration)
        if duration is None:
            return
        if member.bot:
            raise commands.UserFeedbackCheckFailure(_("A bot can't play a Mafia game."))
        await self.config.member(member).temp_banned_until.set(
            int(
                (datetime.datetime.now(tz=datetime.timezone.utc) + duration)
                .replace(second=0, microsecond=0)
                .timestamp(),
            ),
        )
        await ctx.send(
            _(
                "This member has been **temporarily banned for {duration}** from the Mafia games in this server.",
            ).format(duration=humanize_timedelta(timedelta=duration)),
        )

    @mafia.command(name="unban", description="Lift a Mafia ban.")
    @app_checks.mod_or_permissions(manage_guild=True)
    @app_commands.describe(member="Who to unban.")
    async def unban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        """Unban a member from the Mafia game in this server."""
        ctx = await commands.Context.from_interaction(interaction)
        if member.bot:
            raise commands.UserFeedbackCheckFailure(_("A bot can't play a Mafia game."))
        if await self.config.member(member).temp_banned_until() is None:
            raise commands.UserFeedbackCheckFailure(
                _("The member is not banned from the Mafia games in this server."),
            )
        await self.config.member(member).temp_banned_until.clear()
        await ctx.send(_("This member has been **unbanned** from the Mafia games in this server."))

    @mafia.command(name="afkkill", description="Kill an idle player so the game can move on.")
    @app_checks.mod_or_permissions(manage_guild=True)
    @app_commands.describe(member="Which player.")
    async def afkkill(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        """Kill a member for AFK from the Mafia game in this server."""
        ctx = await commands.Context.from_interaction(interaction)
        if member.bot:
            raise commands.UserFeedbackCheckFailure(_("A bot can't play the Mafia game."))
        if (game := self.games.get(ctx.guild)) is None:
            raise commands.UserFeedbackCheckFailure(_("A game is not running in this guild."))
        if (player := game.get_player(member)) is None:
            raise commands.UserFeedbackCheckFailure(
                _("This member is not playing the Mafia game in this server."),
            )
        if player.is_dead:
            raise commands.UserFeedbackCheckFailure(
                _("This player is already dead in the Mafia game in this server."),
            )
        await player.kill(cause="afk")
        await ctx.send(_("This player has been **killed** from the Mafia game in this server."))

    @mafia.command(name="poll", description="Ask the server whether anyone wants a game.")
    async def poll(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """Create a poll for the game."""
        ctx = await commands.Context.from_interaction(interaction)
        if self.games.get(ctx.guild) is not None:
            raise commands.UserFeedbackCheckFailure(_("A game is already running in this guild."))
        if (threshold := await self.config.guild(ctx.guild).poll_threshold()) is None:
            raise commands.UserFeedbackCheckFailure(
                _("The poll threshold is not set in this server."),
            )
        if any(
            ctx.author.get_role(role_id)
            for role_id in await self.config.guild(ctx.guild).blacklisted_roles()
        ):
            raise commands.UserFeedbackCheckFailure(
                _(
                    "You aren't allowed to join a Mafia game in this server because you have a blacklisted role!",
                ),
            )
        if (
            temp_banned_until := await self.config.member(ctx.author).temp_banned_until()
        ) is not None:
            raise commands.UserFeedbackCheckFailure(
                _(
                    "You are **temporarily banned for {duration}** from joining Mafia games in this server!",
                ).format(
                    duration=humanize_timedelta(
                        timedelta=datetime.datetime.fromtimestamp(
                            temp_banned_until,
                            tz=datetime.timezone.utc,
                        )
                        - datetime.datetime.now(tz=datetime.timezone.utc),
                    ),
                ),
            )
        if any(game for game in self.games.values() if game.get_player(ctx.author) is not None):
            raise commands.UserFeedbackCheckFailure(
                _("You are already in a game of Mafia in another server!"),
            )
        poll_view: PollView = PollView(self, threshold)
        await poll_view.start(ctx)
        await poll_view.wait()

    @start.autocomplete("mode")
    @mode.autocomplete("mode")
    async def _mode_autocomplete(self, interaction, current: str) -> list:
        return self._names(MODES, current)

    @role.autocomplete("role")
    @achievements.autocomplete("role")
    async def _role_autocomplete(self, interaction, current: str) -> list:
        return self._names(ROLES, current)

    @anomaly.autocomplete("anomaly")
    async def _anomaly_autocomplete(self, interaction, current: str) -> list:
        return self._names(ANOMALIES, current)
