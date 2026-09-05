import asyncio
import io
import textwrap
from copy import copy
from typing import Union, Optional, Dict, List, Tuple, Any, Iterator, ItemsView, Literal

import discord
from discord import app_commands
import yaml
from schema import And, Or, Schema, SchemaError, Optional as UseOptional
from redbot.core import commands, config
from redbot.core.bot import Red
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils import can_user_react_in
from redbot.core.utils.chat_formatting import box, error, success
from redbot.core.utils.menus import start_adding_reactions
from redbot.core.utils.predicates import ReactionPredicate, MessagePredicate

from .converters import CogOrCommand
from .dashboard_integration import DashboardIntegration

_ = Translator("Permissions", __file__)

COG = "COG"
COMMAND = "COMMAND"
GLOBAL = 0

_OldConfigSchema = Dict[int, Dict[str, Dict[str, Dict[str, Dict[str, List[int]]]]]]
_NewConfigSchema = Dict[str, Dict[int, Dict[str, Dict[int, bool]]]]

# The strings in the schema are constants and should get extracted, but not translated until
# runtime.
translate = _
_ = lambda s: s
YAML_SCHEMA = Schema(
    Or(
        {
            UseOptional(COMMAND): Or(
                {
                    Or(str, int): Or(
                        {
                            Or(int, "default"): And(
                                bool, error=_("Rules must be either `true` or `false`.")
                            )
                        },
                        {},
                        error=_("Keys under command names must be IDs (numbers) or `default`."),
                    )
                },
                {},
                error=_("Keys under `COMMAND` must be command names (strings)."),
            ),
            UseOptional(COG): Or(
                {
                    Or(str, int): Or(
                        {
                            Or(int, "default"): And(
                                bool, error=_("Rules must be either `true` or `false`.")
                            )
                        },
                        {},
                        error=_("Keys under cog names must be IDs or `default`."),
                    )
                },
                {},
                error=_("Keys under `COG` must be cog names (strings)."),
            ),
        },
        {},
        error=_("Top-level keys must be either `COG` or `COMMAND`."),
    )
)
_ = translate

__version__ = "1.0.0"


@cog_i18n(_)
class Permissions(DashboardIntegration, commands.Cog):
    """Customise permissions for commands and cogs."""

    # The command groups in this cog should never directly take any configuration actions
    # These should be delegated to specific commands so that it remains trivial
    # to prevent the guild owner from ever locking themselves out
    # see ``Permissions.__permissions_hook`` for more details

    def __init__(self, bot: Red):
        super().__init__()
        self.bot = bot
        # Config Schema:
        # "COG"
        # -> Cog names...
        #   -> Guild IDs...
        #     -> Model IDs...
        #       -> True|False
        #     -> "default"
        #       -> True|False
        # "COMMAND"
        # -> Command names...
        #   -> Guild IDs...
        #     -> Model IDs...
        #       -> True|False
        #     -> "default"
        #       -> True|False

        # Note that GLOBAL rules are denoted by an ID of 0.
        self.config = config.Config.get_conf(self, identifier=78631113035100160)
        self.config.register_global(version="")
        self.config.init_custom(COG, 1)
        self.config.register_custom(COG)
        self.config.init_custom(COMMAND, 1)
        self.config.register_custom(COMMAND)

    async def red_delete_data_for_user(
        self,
        *,
        requester: Literal["discord_deleted_user", "owner", "user", "user_strict"],
        user_id: int,
    ):
        if requester != "discord_deleted_user":
            return

        count = 0

        _uid = str(user_id)

        # The dict as returned here as string keys. Above is for comparison,
        # there's a below recast to int where needed for guild ids

        for typename, getter in ((COG, self.bot.get_cog), (COMMAND, self.bot.get_command)):
            obj_type_rules = await self.config.custom(typename).all()

            count += 1
            if not count % 100:
                await asyncio.sleep(0)

            for obj_name, rules_dict in obj_type_rules.items():
                count += 1
                if not count % 100:
                    await asyncio.sleep(0)

                obj = getter(obj_name)

                for guild_id, guild_rules in rules_dict.items():
                    count += 1
                    if not count % 100:
                        await asyncio.sleep(0)

                    if _uid in guild_rules:
                        if obj:
                            # delegate to remove rule here
                            await self._remove_rule(
                                CogOrCommand(typename, obj.qualified_name, obj),
                                user_id,
                                int(guild_id),
                            )
                        else:
                            grp = self.config.custom(typename, obj_name)
                            await grp.clear_raw(guild_id, user_id)

    async def __permissions_hook(self, ctx: commands.Context) -> Optional[bool]:
        """
        Purpose of this hook is to prevent guild owner lockouts of permissions specifically
        without modifying rule behavior in any other case.

        Guild owner is not special cased outside of these configuration commands
        to allow guild owner to restrict the use of potentially damaging commands
        such as, but not limited to, cleanup to specific channels.

        Leaving the configuration commands special cased allows guild owners to fix
        any misconfigurations.
        """

        if ctx.guild:
            if ctx.author == ctx.guild.owner:
                # the below should contain all commands from this cog
                # which configure or are useful to the
                # configuration of guild permissions and should never
                # have a potential impact on global configuration
                # as well as the parent groups
                if ctx.command in (
                    self.permissions_canrun,
                    self.permissions_acl_export,
                    self.permissions_acl_import,
                    self.permissions_acl_yaml_example,
                ):
                    return True  # permission rules will be ignored at this case

        # this delegates to permissions rules, do not change to False which would deny
        return None

    permissions = app_commands.Group(
        name="permissions",
        description="Command permission rules.",
        extras={"red_force_enable": True},
    )
    acl = app_commands.Group(
        name="acl",
        description="Import and export the rules as a YAML file.",
        parent=permissions,
    )

    @permissions.command(
        name="canrun", description="Check whether someone can run a command."
    )
    @app_commands.describe(
        user="Who to check.",
        command="The command, as you would type it without the leading slash.",
    )
    async def permissions_canrun(
        self, interaction: discord.Interaction, user: discord.Member, command: str
    ):
        """Check if a user can run a command.

        Answered for this channel and server, which is where the rules that
        matter usually live.
        """
        ctx = await commands.Context.from_interaction(interaction)
        name = command.strip().lstrip("/")
        com = ctx.bot.get_command(name)
        if com is None:
            await ctx.send(_("No such command"))
            return

        fake_message = copy(ctx.message)
        fake_message.author = user
        fake_message.content = f"{ctx.prefix}{name}"
        fake_context = await ctx.bot.get_context(fake_message)
        try:
            can = await com.can_run(
                fake_context, check_all_parents=True, change_permission_state=False
            )
        except commands.CommandError:
            can = False

        await ctx.send(
            success(_("That user can run the specified command."))
            if can
            else error(_("That user can not run the specified command."))
        )

    @acl.command(name="export", description="Get a YAML file of every rule.")
    @app_commands.describe(scope="Whose rules. Global is owner only.")
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="This server", value="server"),
            app_commands.Choice(name="Global", value="global"),
        ]
    )
    async def permissions_acl_export(
        self, interaction: discord.Interaction, scope: str = "server"
    ):
        """Get a YAML file detailing the rules at one level."""
        ctx = await commands.Context.from_interaction(interaction)
        guild_id = await self._acl_scope(ctx, scope)
        if guild_id is None:
            return
        file = await self._yaml_get_acl(guild_id=guild_id)
        try:
            await ctx.author.send(file=file)
        except discord.Forbidden:
            await ctx.send(_("I'm not allowed to DM you."))
        else:
            await ctx.send(_("I've just sent the file to you via DM."))
        finally:
            file.close()

    @acl.command(name="import", description="Set rules from a YAML file.")
    @app_commands.describe(
        file="The YAML file of rules.",
        scope="Whose rules to change. Global is owner only.",
        merge="Keep rules the file does not mention. Off replaces everything.",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="This server", value="server"),
            app_commands.Choice(name="Global", value="global"),
        ]
    )
    async def permissions_acl_import(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
        scope: str = "server",
        merge: bool = False,
    ):
        """Set rules from a YAML file.

        With merge off this replaces every rule at that level. Command and cog
        names in the file are not checked against what is loaded.
        """
        ctx = await commands.Context.from_interaction(interaction)
        guild_id = await self._acl_scope(ctx, scope)
        if guild_id is None:
            return
        await self._permissions_acl_set(ctx, guild_id=guild_id, update=merge, source=file)

    @acl.command(name="example", description="Show the YAML layout an import expects.")
    async def permissions_acl_yaml_example(self, interaction: discord.Interaction):
        """Sends an example of the yaml layout for permissions"""
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.send(
            _("Example YAML for setting rules:\n")
            + box(
                textwrap.dedent(
                    """\
                        COMMAND:
                            ping:
                                12345678901234567: true
                                56789012345671234: false
                        COG:
                            General:
                                56789012345671234: true
                                12345678901234567: false
                                default: false
                        """
                ),
                lang="yaml",
            )
        )

    async def _acl_scope(self, ctx, scope: str):
        """The guild_id a scope means, or None once the refusal is sent.

        The prefix commands split this into a global pair and a server pair,
        each with its own check. One option, one check.
        """
        if scope == "global":
            if not await ctx.bot.is_owner(ctx.author):
                await ctx.send(_("Only a bot owner can touch the global rules."))
                return None
            return GLOBAL
        if ctx.guild is None:
            await ctx.send(_("There is no server here to read rules from."))
            return None
        if not (
            ctx.author.id == ctx.guild.owner_id
            or ctx.author.guild_permissions.administrator
            or await ctx.bot.is_owner(ctx.author)
        ):
            await ctx.send(_("You need to be the server owner or an administrator."))
            return None
        return ctx.guild.id

    @commands.Cog.listener()
    async def on_cog_add(self, cog: commands.Cog) -> None:
        """Event listener for `cog_add`.

        This loads rules whenever a new cog is added.
        """
        if cog is self:
            # This cog has its rules loaded manually in setup()
            return
        await self._on_cog_add(cog)

    @commands.Cog.listener()
    async def on_command_add(self, command: commands.Command) -> None:
        """Event listener for `command_add`.

        This loads rules whenever a new command is added.
        """
        if command.cog is self:
            # This cog's commands have their rules loaded manually in setup()
            return
        await self._on_command_add(command)

    async def _on_cog_add(self, cog: commands.Cog) -> None:
        self._load_rules_for(
            cog_or_command=cog,
            rule_dict=await self.config.custom(COG, cog.__class__.__name__).all(),
        )
        cog.requires.ready_event.set()

    async def _on_command_add(self, command: commands.Command) -> None:
        self._load_rules_for(
            cog_or_command=command,
            rule_dict=await self.config.custom(COMMAND, command.qualified_name).all(),
        )
        command.requires.ready_event.set()

    async def _add_rule(
        self, rule: bool, cog_or_cmd: CogOrCommand, model_id: int, guild_id: int
    ) -> None:
        """Add a rule.

        Guild ID should be 0 for global rules.

        Handles config.
        """
        if rule is True:
            cog_or_cmd.obj.allow_for(model_id, guild_id=guild_id)
        else:
            cog_or_cmd.obj.deny_to(model_id, guild_id=guild_id)

        async with self.config.custom(cog_or_cmd.type, cog_or_cmd.name).all() as rules:
            rules.setdefault(str(guild_id), {})[str(model_id)] = rule

    async def _remove_rule(self, cog_or_cmd: CogOrCommand, model_id: int, guild_id: int) -> None:
        """Remove a rule.

        Guild ID should be 0 for global rules.

        Handles config.
        """
        cog_or_cmd.obj.clear_rule_for(model_id, guild_id=guild_id)
        guild_id, model_id = str(guild_id), str(model_id)
        async with self.config.custom(cog_or_cmd.type, cog_or_cmd.name).all() as rules:
            if (guild_rules := rules.get(guild_id)) is not None:
                guild_rules.pop(model_id, None)

    async def _set_default_rule(
        self, rule: Optional[bool], cog_or_cmd: CogOrCommand, guild_id: int
    ) -> None:
        """Set the default rule.

        Guild ID should be 0 for the global default.

        Handles config.
        """
        cog_or_cmd.obj.set_default_rule(rule, guild_id)
        async with self.config.custom(cog_or_cmd.type, cog_or_cmd.name).all() as rules:
            rules.setdefault(str(guild_id), {})["default"] = rule

    async def _clear_rules(self, guild_id: int) -> None:
        """Clear all global rules or rules for a guild.

        Guild ID should be 0 for global rules.

        Handles config.
        """
        self.bot.clear_permission_rules(guild_id, preserve_default_rule=False)
        for category in (COG, COMMAND):
            async with self.config.custom(category).all() as all_rules:
                for name, rules in all_rules.items():
                    rules.pop(str(guild_id), None)

    async def _permissions_acl_set(
        self,
        ctx: commands.Context,
        guild_id: int,
        update: bool,
        source: discord.Attachment,
    ) -> None:
        """Set rules from a YAML file and handle response to users too."""
        parsedfile = source

        try:
            await self._yaml_set_acl(parsedfile, guild_id=guild_id, update=update)
        except yaml.MarkedYAMLError as e:
            await ctx.send(_("Invalid syntax: ") + str(e))
        except SchemaError as e:
            await ctx.send(
                _("Your YAML file did not match the schema: ") + translate(e.errors[-1])
            )
        else:
            await ctx.send(_("Rules set."))

    async def _yaml_set_acl(self, source: discord.Attachment, guild_id: int, update: bool) -> None:
        """Set rules from a YAML file."""
        with io.BytesIO() as fp:
            await source.save(fp)
            rules = yaml.safe_load(fp)

        if rules is None:
            rules = {}
        YAML_SCHEMA.validate(rules)
        if update is False:
            await self._clear_rules(guild_id)

        for category, getter in ((COG, self.bot.get_cog), (COMMAND, self.bot.get_command)):
            rules_dict = rules.get(category)
            if not rules_dict:
                continue
            conf = self.config.custom(category)
            for cmd_name, cmd_rules in rules_dict.items():
                cmd_rules = {str(model_id): rule for model_id, rule in cmd_rules.items()}
                await conf.set_raw(cmd_name, str(guild_id), value=cmd_rules)
                cmd_obj = getter(str(cmd_name))
                if cmd_obj is not None:
                    self._load_rules_for(cmd_obj, {guild_id: cmd_rules})

    async def _yaml_get_acl(self, guild_id: int) -> discord.File:
        """Get a YAML file for all rules set in a guild."""
        guild_rules = {}
        for category in (COG, COMMAND):
            guild_rules.setdefault(category, {})
            rules_dict = await self.config.custom(category).all()
            for cmd_name, cmd_rules in rules_dict.items():
                model_rules = cmd_rules.get(str(guild_id))
                if model_rules is not None:
                    guild_rules[category][cmd_name] = dict(_int_key_map(model_rules.items()))

        fp = io.BytesIO(yaml.dump(guild_rules, default_flow_style=False).encode("utf-8"))
        return discord.File(fp, filename="acl.yaml")

    @staticmethod
    async def _confirm(ctx: commands.Context) -> bool:
        """Ask "Are you sure?" and get the response as a bool."""
        if ctx.guild is None or can_user_react_in(ctx.guild.me, ctx.channel):
            msg = await ctx.send(_("Are you sure?"))
            # noinspection PyAsyncCall
            task = start_adding_reactions(msg, ReactionPredicate.YES_OR_NO_EMOJIS)
            pred = ReactionPredicate.yes_or_no(msg, ctx.author)
            try:
                await ctx.bot.wait_for("reaction_add", check=pred, timeout=30)
            except asyncio.TimeoutError:
                await ctx.send(_("Response timed out."))
                return False
            else:
                task.cancel()
                agreed = pred.result
            finally:
                await msg.delete()
        else:
            await ctx.send(_("Are you sure?") + " (yes/no)")
            pred = MessagePredicate.yes_or_no(ctx)
            try:
                await ctx.bot.wait_for("message", check=pred, timeout=30)
            except asyncio.TimeoutError:
                await ctx.send(_("Response timed out."))
                return False
            else:
                agreed = pred.result

        if agreed is False:
            await ctx.send(_("Action cancelled."))
        return agreed

    async def initialize(self) -> None:
        """Initialize this cog.

        This will load all rules from config onto every currently
        loaded command.
        """
        await self._maybe_update_schema()
        await self._load_all_rules()

    async def _maybe_update_schema(self) -> None:
        """Maybe update rules set by config prior to permissions 1.0.0."""
        if await self.config.version():
            return
        old_config = await self.config.all_guilds()
        old_config[GLOBAL] = await self.config.all()
        new_cog_rules, new_cmd_rules = self._get_updated_schema(old_config)
        await self.config.custom(COG).set(new_cog_rules)
        await self.config.custom(COMMAND).set(new_cmd_rules)
        await self.config.version.set(__version__)

    @staticmethod
    def _get_updated_schema(
        old_config: _OldConfigSchema,
    ) -> Tuple[_NewConfigSchema, _NewConfigSchema]:
        # Prior to 1.0.0, the schema was in this form for both global
        # and guild-based rules:
        # "owner_models"
        # -> "cogs"
        #   -> Cog names...
        #     -> "allow"
        #       -> [Model IDs...]
        #     -> "deny"
        #       -> [Model IDs...]
        #     -> "default"
        #       -> "allow"|"deny"
        # -> "commands"
        #   -> Command names...
        #     -> "allow"
        #       -> [Model IDs...]
        #     -> "deny"
        #       -> [Model IDs...]
        #     -> "default"
        #       -> "allow"|"deny"

        new_cog_rules = {}
        new_cmd_rules = {}
        for guild_id, old_rules in old_config.items():
            if "owner_models" not in old_rules:
                continue
            old_rules = old_rules["owner_models"]
            for category, new_rules in zip(("cogs", "commands"), (new_cog_rules, new_cmd_rules)):
                if category in old_rules:
                    for name, rules in old_rules[category].items():
                        these_rules = new_rules.setdefault(name, {})
                        guild_rules = these_rules.setdefault(str(guild_id), {})
                        # Since allow rules would take precedence if the same model ID
                        # sat in both the allow and deny list, we add the deny entries
                        # first and let any conflicting allow entries overwrite.
                        for model_id in rules.get("deny", []):
                            guild_rules[str(model_id)] = False
                        for model_id in rules.get("allow", []):
                            guild_rules[str(model_id)] = True
                        if "default" in rules:
                            default = rules["default"]
                            if default == "allow":
                                guild_rules["default"] = True
                            elif default == "deny":
                                guild_rules["default"] = False
        return new_cog_rules, new_cmd_rules

    async def _load_all_rules(self):
        """Load all of this cog's rules into loaded commands and cogs."""
        for category, getter in ((COG, self.bot.get_cog), (COMMAND, self.bot.get_command)):
            all_rules = await self.config.custom(category).all()
            for name, rules in all_rules.items():
                obj = getter(name)
                if obj is None:
                    continue
                self._load_rules_for(obj, rules)

    @staticmethod
    def _load_rules_for(
        cog_or_command: Union[commands.Command, commands.Cog],
        rule_dict: Dict[Union[int, str], Dict[Union[int, str], bool]],
    ) -> None:
        """Load the rules into a command or cog object.

        rule_dict should be a dict mapping Guild IDs to Model IDs to
        rules.
        """
        for guild_id, guild_dict in _int_key_map(rule_dict.items()):
            for model_id, rule in _int_key_map(guild_dict.items()):
                if model_id == "default":
                    cog_or_command.set_default_rule(rule, guild_id=guild_id)
                elif rule is True:
                    cog_or_command.allow_for(model_id, guild_id=guild_id)
                elif rule is False:
                    cog_or_command.deny_to(model_id, guild_id=guild_id)

    async def cog_unload(self) -> None:
        await self._unload_all_rules()

    async def _unload_all_rules(self) -> None:
        """Unload all rules set by this cog.

        This is done instead of just clearing all rules, which could
        clear rules set by other cogs.
        """
        for category, getter in ((COG, self.bot.get_cog), (COMMAND, self.bot.get_command)):
            all_rules = await self.config.custom(category).all()
            for name, rules in all_rules.items():
                obj = getter(name)
                if obj is None:
                    continue
                self._unload_rules_for(obj, rules)

    @staticmethod
    def _unload_rules_for(
        cog_or_command: Union[commands.Command, commands.Cog],
        rule_dict: Dict[Union[int, str], Dict[Union[int, str], bool]],
    ) -> None:
        """Unload the rules from a command or cog object.

        rule_dict should be a dict mapping Guild IDs to Model IDs to
        rules.
        """
        for guild_id, guild_dict in _int_key_map(rule_dict.items()):
            for model_id in guild_dict.keys():
                if model_id == "default":
                    cog_or_command.set_default_rule(None, guild_id=guild_id)
                else:
                    cog_or_command.clear_rule_for(int(model_id), guild_id=guild_id)


def _int_key_map(items_view: ItemsView[str, Any]) -> Iterator[Tuple[Union[str, int], Any]]:
    for k, v in items_view:
        if k == "default":
            yield k, v
        else:
            yield int(k), v
