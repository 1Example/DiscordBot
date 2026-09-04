import json
import logging
from typing import Optional

import discord
from redbot.core import app_commands, commands
from redbot.core.app_commands import checks as app_checks
from redbot.core.utils.menus import SimpleMenu

from ..config.defaults import DEFAULT_STT_PROVIDER
from ..providers.llm.openai_compatible.endpoints import (
    CompatEndpointKind,
    get_openai_compat_kind,
)
from ..settings.owner import OwnerSettings
from ..settings.utilities import rank_choices_for_query
from ..providers.speech.stt import DEFAULT_MODELS as STT_DEFAULT_MODELS
from ..types.abc import MixinMeta

logger = logging.getLogger("red.bz_cogs.aiuser")


class Settings(
    # The per-setting command mixins are gone: every one of those settings is
    # on the dashboard now, and the tree they formed was four levels deep -
    # deeper than a slash command can express. What is left here is the handful
    # of things a person still does from Discord.
    OwnerSettings,
    MixinMeta,
):
    # What is left of `/aiuser` once its settings moved to the dashboard: a
    # consent pair that has to be reachable where people actually are, a status
    # readout, and the in-channel context reset.
    aiuser = app_commands.Group(
        name="aiuser",
        description="Opt in or out, and reset the conversation.",
        guild_only=True,
        extras={"red_force_enable": True},
    )

    @aiuser.command(name="forget")
    @app_checks.bot_has_permissions(add_reactions=True)
    async def forget(self, interaction: discord.Interaction):
        """Forces the bot to forget the current conversation up to this point

        This is useful if the LLM is stuck doing unwanted behaviour or giving undesirable results.
        See `[p]aiuser triggers public_forget` to allow non-admins to use this command.
        """
        ctx = await commands.Context.from_interaction(interaction)
        if (
            not ctx.channel.permissions_for(ctx.author).manage_messages
            and not await self.config.guild(ctx.guild).public_forget()
        ):
            return await ctx.react_quietly("❌")

        self.services.override_prompt_start_time[ctx.guild.id] = ctx.message.created_at
        await ctx.react_quietly("✅")

    @aiuser.command(name="status")
    @app_checks.bot_has_permissions(embed_links=True)
    async def status(self, interaction: discord.Interaction):
        """Returns current settings

        (Current config per server)
        """
        ctx = await commands.Context.from_interaction(interaction)
        config = await self.config.guild(ctx.guild).get_raw()
        glob_config = await self.config.get_raw()
        whitelist = await self.config.guild(ctx.guild).channels_whitelist()
        channels = [
            channel.mention
            if (channel := ctx.guild.get_channel_or_thread(channel_id))
            else f"Unknown channel (`{channel_id}`)"
            for channel_id in whitelist
        ]
        embeds = []

        main_embed = discord.Embed(
            title="AI User Settings", color=await ctx.embed_color()
        )
        main_embed.add_field(name="Version", inline=True, value=f"`{self.__version__}`")

        main_embed.add_field(name="Model", inline=True, value=f"`{config['model']}`")
        main_embed.add_field(
            name="Server Reply Chance",
            inline=True,
            value=f"`{config['reply_percent'] * 100:.2f}`%",
        )

        main_embed.add_field(
            name="Opt In By Default",
            inline=True,
            value=f"`{config['optin_by_default']}`",
        )

        main_embed.add_field(
            name="Always Reply to Mentions/Replies",
            inline=True,
            value=f"`{config['reply_to_mentions_replies']}`",
        )
        main_embed.add_field(
            name="Context Message Limit",
            inline=True,
            value=f"`{config['messages_backread']}` messages",
        )
        main_embed.add_field(
            name="Context Message Gap",
            inline=True,
            value=f"`{config['messages_backread_seconds']}` seconds",
        )

        compaction_enabled = config.get("compaction_enabled", False)
        main_embed.add_field(
            name="Context Compaction",
            inline=True,
            value="`Enabled`" if compaction_enabled else "`Disabled`",
        )

        main_embed.add_field(
            name="Reply Channels",
            inline=True,
            value=" ".join(channels) if channels else "`None`",
        )

        endpoint_url = str(glob_config["custom_openai_endpoint"] or "")
        if endpoint_url == "codex":
            endpoint_text = "Using [OpenAI](https://openai.com/) via Codex"
        elif get_openai_compat_kind(endpoint_url) is CompatEndpointKind.OPENROUTER:
            endpoint_text = "Using [OpenRouter](https://openrouter.ai) endpoint"
        elif endpoint_url:
            endpoint_text = "Using a custom endpoint"
        else:
            endpoint_text = "Using [OpenAI](https://openai.com/)"
        main_embed.add_field(name="LLM Endpoint", inline=True, value=endpoint_text)

        main_embed.add_field(
            name="",
            inline=False,
            value="",
        )

        main_embed.add_field(
            name="Memory Retrieval",
            inline=True,
            value="Enabled" if config["query_memories"] else "Disabled",
        )

        main_embed.add_field(
            name="Tools",
            inline=True,
            value="Enabled" if config["function_calling"] else "Disabled",
        )
        main_embed.add_field(
            name="Random Messages",
            inline=True,
            value=(
                f"Enabled: `{config['random_messages_percent'] * 100:.2f}`% every `33` min"
                if config["random_messages_enabled"]
                else "Disabled"
            ),
        )

        media_embed = discord.Embed(
            title="Media Settings", color=await ctx.embed_color()
        )
        media_embed.add_field(
            name="Image Processing",
            inline=True,
            value="Enabled" if config["scan_images"] else "Disabled",
        )
        if config["scan_images"]:
            media_embed.add_field(
                name="Maximum Image Size",
                inline=True,
                value=f"`{config['max_image_size'] / 1024 / 1024:.2f}` MB",
            )
            media_embed.add_field(
                name="Image Detail",
                inline=True,
                value=f"`{config['scan_images_detail']}`",
            )
            media_embed.add_field(
                name="Image Model",
                inline=True,
                value=f"`{config['scan_images_model'] or 'Chat model'}`",
            )
        media_embed.add_field(
            name="Audio Transcription",
            inline=True,
            value="Enabled" if config["scan_audio"] else "Disabled",
        )
        if config["scan_audio"]:
            stt_provider = (
                config["scan_audio_provider"] or DEFAULT_STT_PROVIDER
            ).lower()
            media_embed.add_field(
                name="Audio Provider",
                inline=True,
                value=f"`{stt_provider}`",
            )
            media_embed.add_field(
                name="Maximum Audio Duration",
                inline=True,
                value=f"`{config['max_audio_duration']}` seconds",
            )
            stt_model = config["scan_audio_model"] or STT_DEFAULT_MODELS.get(
                stt_provider
            )
            media_embed.add_field(
                name="Audio Model",
                inline=True,
                value=f"`{stt_model}`",
            )

        whitelisted_trigger = bool(
            config["members_whitelist"] or config["roles_whitelist"]
        )

        main_embed.add_field(
            name="Trigger Allowlist Active",
            inline=True,
            value=f"`{whitelisted_trigger}`",
        )

        main_embed.add_field(
            name="Allowed Members",
            inline=True,
            value=" ".join(
                [f"<@{member_id}>" for member_id in config["members_whitelist"]]
            )
            or "`None`",
        )

        main_embed.add_field(
            name="Allowed Roles",
            inline=True,
            value=" ".join([f"<@&{role_id}>" for role_id in config["roles_whitelist"]])
            or "`None`",
        )

        removelist_regexes = config["removelist_regexes"]
        regexes_num = 0
        if removelist_regexes is not None:
            regexes_num = len(removelist_regexes)
        main_embed.add_field(
            name="Response Filters", value=f"`{regexes_num}` regexes set"
        )
        main_embed.add_field(name="Ignore Regex", value=f"`{config['ignore_regex']}`")
        main_embed.add_field(
            name="Public Forget Command",
            inline=True,
            value=f"`{config['public_forget']}`",
        )
        embeds.append(main_embed)
        embeds.append(media_embed)

        parameters = config["parameters"]
        if parameters is not None:
            parameters = json.loads(parameters)
            parameters_embed = discord.Embed(
                title="Custom Parameters to Endpoint", color=await ctx.embed_color()
            )
            for key, value in parameters.items():
                parameters_embed.add_field(
                    name=key, value=f"```{json.dumps(value, indent=4)}```", inline=False
                )
            embeds.append(parameters_embed)

        for embed in embeds:
            await ctx.send(embed=embed)
        return


    async def _send_channel_whitelist(self, ctx: commands.Context, whitelist):
        embed = discord.Embed(
            title="Enabled reply channels:", color=await ctx.embed_color()
        )
        channels = [
            channel.mention
            if (channel := ctx.guild.get_channel_or_thread(channel_id))
            else f"Unknown channel (`{channel_id}`)"
            for channel_id in whitelist
        ]
        embed.description = "\n".join(channels) if channels else "None"
        return await ctx.send(embed=embed)


    async def _paginate_models(self, ctx, models, query: Optional[str] = None):
        if not models:
            return await ctx.send(":warning: No models are currently available.")

        if query:
            models = rank_choices_for_query(models, query)

        pagified_models = [models[i : i + 10] for i in range(0, len(models), 10)]
        menu_pages = []

        for models_page in pagified_models:
            embed = discord.Embed(
                title=("Available Models"),
                color=await ctx.embed_color(),
            )
            embed.description = "\n".join([f"`{model}`" for model in models_page])
            menu_pages.append(embed)

        endpoint_kind = get_openai_compat_kind(
            await self.config.custom_openai_endpoint()
        )
        if endpoint_kind is CompatEndpointKind.OPENROUTER:
            menu_pages[0].add_field(
                name="For pricing and more details go to:",
                value="https://openrouter.ai/models",
                inline=False,
            )

        if len(menu_pages) == 1:
            return await ctx.send(embed=menu_pages[0])
        for i, page in enumerate(menu_pages):
            page.set_footer(text=f"Page {i + 1} of {len(menu_pages)}")
        return await SimpleMenu(menu_pages).start(ctx)

    @aiuser.command(name="optin")
    async def optin(self, interaction: discord.Interaction):
        """Opt in of sending your messages / images to OpenAI or another endpoint (bot-wide)

        This will allow the bot to reply to your messages or use your messages.
        """
        ctx = await commands.Context.from_interaction(interaction)
        if not await self.services.consent.opt_in(ctx.author.id):
            return await ctx.send("You are already opted in.")
        await ctx.send("You are now opted in bot-wide")

    @aiuser.command(name="optout")
    async def optout(self, interaction: discord.Interaction):
        """Opt out of sending your messages / images to OpenAI or another endpoint (bot-wide)

        This will prevent the bot from replying to your messages or using your messages.
        """
        ctx = await commands.Context.from_interaction(interaction)
        if not await self.services.consent.opt_out(ctx.author.id):
            return await ctx.send("You are already opted out.")
        await ctx.send("You are now opted out bot-wide")


    async def _set_consent_default(self, ctx: commands.Context, value: bool):
        await self.config.guild(ctx.guild).optin_by_default.set(value)
        embed = discord.Embed(
            title="Users are now opted in by default in this server:",
            description=f"{value}",
            color=await ctx.embed_color(),
        )
        return await ctx.send(embed=embed)


    async def _set_consent_warning(self, ctx: commands.Context, enabled: bool):
        await self.config.guild(ctx.guild).optin_disable_embed.set(not enabled)
        embed = discord.Embed(
            title="Opt-in warning embed is now:",
            description="Enabled" if enabled else "Disabled",
            color=await ctx.embed_color(),
        )
        if not enabled:
            embed.add_field(
                name=":warning: Warning :warning:",
                value="Users not yet opt-in/out will be unaware their messages are not being processed",
                inline=False,
            )
        return await ctx.send(embed=embed)
