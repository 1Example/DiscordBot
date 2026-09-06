import datetime
import time
from enum import Enum
from random import randint, choice
from typing import Final
import urllib.parse
import aiohttp
import discord
from discord import app_commands
from redbot.core import commands
from redbot.core.bot import Red
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.menus import menu
from redbot.core.utils.chat_formatting import (
    bold,
    escape,
    italics,
    humanize_number,
    humanize_timedelta,
    pagify,
)


_ = T_ = Translator("General", __file__)


class RPS(Enum):
    """The three throws. discord.py turns this into the option's choices,
    so the member names are what a person picks from.
    """

    rock = "rock"
    paper = "paper"
    scissors = "scissors"


EMOJI = {
    RPS.rock: "\N{MOYAI}",
    RPS.paper: "\N{PAGE FACING UP}",
    RPS.scissors: "\N{BLACK SCISSORS}\N{VARIATION SELECTOR-16}",
}


# Discord will not accept an integer option bound outside 2**53, which is
# where a double stops counting whole numbers. The old ceiling was 2**63-1.
MAX_ROLL: Final[int] = 2**53 - 1


@cog_i18n(_)
class General(commands.Cog):
    """Dice, coin flips, 8 ball, server info and other odds and ends."""

    fun = app_commands.Group(
        name="fun",
        description="Dice, coin flips, the 8 ball and other odds and ends.",
        extras={"red_force_enable": True},
    )

    global _
    _ = lambda s: s
    ball = [
        _("As I see it, yes"),
        _("It is certain"),
        _("It is decidedly so"),
        _("Most likely"),
        _("Outlook good"),
        _("Signs point to yes"),
        _("Without a doubt"),
        _("Yes"),
        _("Yes – definitely"),
        _("You may rely on it"),
        _("Reply hazy, try again"),
        _("Ask again later"),
        _("Better not tell you now"),
        _("Cannot predict now"),
        _("Concentrate and ask again"),
        _("Don't count on it"),
        _("My reply is no"),
        _("My sources say no"),
        _("Outlook not so good"),
        _("Very doubtful"),
    ]
    _ = T_

    def __init__(self, bot: Red) -> None:
        super().__init__()
        self.bot = bot
        self.stopwatches = {}

    async def red_delete_data_for_user(self, **kwargs):
        """Nothing to delete"""
        return

    @fun.command(
        name="choose",
        description="Choose between multiple options.",
        extras={"red_force_enable": True},
    )
    @app_commands.describe(
        options="At least two options, separated by | - or by spaces if you use no |."
    )
    async def choose(self, interaction: discord.Interaction, options: str):
        """Choose between multiple options."""
        ctx = await commands.Context.from_interaction(interaction)
        # A slash command gets one string, so | is the separator when it is
        # there. Without it this falls back to whitespace, which is what the
        # prefix version did.
        parts = options.split("|") if "|" in options else options.split()
        choices = [escape(c.strip(), mass_mentions=True) for c in parts if c.strip()]
        if len(choices) < 2:
            await ctx.send(_("Not enough options to pick from."))
        else:
            await ctx.send(choice(choices))

    @fun.command(
        name="roll",
        description="Roll a random number between 1 and the number you give.",
        extras={"red_force_enable": True},
    )
    @app_commands.describe(number="The highest number to roll. Defaults to 100.")
    async def roll(
        self,
        interaction: discord.Interaction,
        number: app_commands.Range[int, 2, MAX_ROLL] = 100,
    ):
        """Roll a random number."""
        # Discord enforces the range before the command runs, so the two
        # out-of-bounds replies the prefix version needed are gone.
        ctx = await commands.Context.from_interaction(interaction)
        n = randint(1, number)
        await ctx.send(
            "{author.mention} :game_die: {n} :game_die:".format(
                author=interaction.user, n=humanize_number(n)
            )
        )

    @fun.command(
        name="flip",
        description="Flip a coin, or flip a user upside down.",
        extras={"red_force_enable": True},
    )
    @app_commands.describe(user="Who to flip. Leave empty to flip a coin.")
    async def flip(
        self, interaction: discord.Interaction, user: discord.Member = None
    ):
        """Flip a coin... or a user."""
        ctx = await commands.Context.from_interaction(interaction)
        if user is not None:
            msg = ""
            if user.id == self.bot.user.id:
                user = interaction.user
                msg = _("Nice try. You think this is funny?\n How about *this* instead:\n\n")
            char = "abcdefghijklmnopqrstuvwxyz"
            tran = "ɐqɔpǝɟƃɥᴉɾʞlɯuodbɹsʇnʌʍxʎz"
            table = str.maketrans(char, tran)
            name = user.display_name.translate(table)
            char = char.upper()
            tran = "∀qƆpƎℲפHIſʞ˥WNOԀQᴚS┴∩ΛMX⅄Z"
            table = str.maketrans(char, tran)
            name = name.translate(table)
            await ctx.send(msg + "(╯°□°）╯︵ " + name[::-1])
        else:
            await ctx.send(_("*flips a coin and... ") + choice([_("HEADS!*"), _("TAILS!*")]))

    @fun.command(
        name="rps",
        description="Play Rock Paper Scissors.",
        extras={"red_force_enable": True},
    )
    @app_commands.describe(your_choice="Rock, paper or scissors.")
    async def rps(self, interaction: discord.Interaction, your_choice: RPS):
        """Play Rock Paper Scissors."""
        ctx = await commands.Context.from_interaction(interaction)
        author = interaction.user
        player_choice = your_choice
        red_choice = choice((RPS.rock, RPS.paper, RPS.scissors))
        cond = {
            (RPS.rock, RPS.paper): False,
            (RPS.rock, RPS.scissors): True,
            (RPS.paper, RPS.rock): True,
            (RPS.paper, RPS.scissors): False,
            (RPS.scissors, RPS.rock): False,
            (RPS.scissors, RPS.paper): True,
        }

        if red_choice == player_choice:
            outcome = None  # Tie
        else:
            outcome = cond[(player_choice, red_choice)]

        if outcome is True:
            await ctx.send(
                _("{choice} You win {author.mention}!").format(
                    choice=EMOJI[red_choice], author=author
                )
            )
        elif outcome is False:
            await ctx.send(
                _("{choice} You lose {author.mention}!").format(
                    choice=EMOJI[red_choice], author=author
                )
            )
        else:
            await ctx.send(
                _("{choice} We're square {author.mention}!").format(
                    choice=EMOJI[red_choice], author=author
                )
            )

    @fun.command(
        name="8ball",
        description="Ask 8 ball a question.",
        extras={"red_force_enable": True},
    )
    @app_commands.describe(question="Your question. It must end with a question mark.")
    async def _8ball(self, interaction: discord.Interaction, question: str):
        """Ask 8 ball a question.

        Question must end with a question mark.
        """
        ctx = await commands.Context.from_interaction(interaction)
        if question.endswith("?") and question != "?":
            await ctx.send("`" + T_(choice(self.ball)) + "`")
        else:
            await ctx.send(_("That doesn't look like a question."))

    @fun.command(
        name="stopwatch",
        description="Start or stop your stopwatch.",
        extras={"red_force_enable": True},
    )
    async def stopwatch(self, interaction: discord.Interaction):
        """Start or stop the stopwatch."""
        ctx = await commands.Context.from_interaction(interaction)
        author = interaction.user
        if author.id not in self.stopwatches:
            self.stopwatches[author.id] = int(time.perf_counter())
            await ctx.send(author.mention + _(" Stopwatch started!"))
        else:
            tmp = abs(self.stopwatches[author.id] - int(time.perf_counter()))
            tmp = str(datetime.timedelta(seconds=tmp))
            await ctx.send(
                author.mention + _(" Stopwatch stopped! Time: **{seconds}**").format(seconds=tmp)
            )
            self.stopwatches.pop(author.id, None)

    @fun.command(
        name="lmgtfy",
        description="Create a let-me-google-that-for-you link.",
        extras={"red_force_enable": True},
    )
    @app_commands.describe(search_terms="What to search for.")
    async def lmgtfy(self, interaction: discord.Interaction, search_terms: str):
        """Create a lmgtfy link."""
        ctx = await commands.Context.from_interaction(interaction)
        search_terms = escape(urllib.parse.quote_plus(search_terms), mass_mentions=True)
        await ctx.send(
            f"https://cog-creators.github.io/lmgtfy/search?q={search_terms}&btnK=Google+Search"
        )

    @fun.command(
        name="hug",
        description="Because everyone likes hugs!",
        extras={"red_force_enable": True},
    )
    @app_commands.guild_only()
    @app_commands.describe(
        user="Who to hug.",
        intensity="How big a hug, 0 to 10. Defaults to 1.",
    )
    async def hug(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        intensity: app_commands.Range[int, 0, 10] = 1,
    ):
        """Because everyone likes hugs!"""
        ctx = await commands.Context.from_interaction(interaction)
        name = italics(user.display_name)
        if intensity <= 0:
            msg = "(っ˘̩╭╮˘̩)っ" + name
        elif intensity <= 3:
            msg = "(っ´▽｀)っ" + name
        elif intensity <= 6:
            msg = "╰(*´︶`*)╯" + name
        elif intensity <= 9:
            msg = "(つ≧▽≦)つ" + name
        elif intensity >= 10:
            msg = "(づ￣ ³￣)づ{} ⊂(´・ω・｀⊂)".format(name)
        else:
            # For the purposes of "msg might not be defined" linter errors
            raise RuntimeError
        await ctx.send(msg)

    @app_commands.command(
        name="serverinfo",
        description="Show information about this server.",
        extras={"red_force_enable": True},
    )
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(embed_links=True)
    @app_commands.describe(details="Show the long version, with every feature listed.")
    async def serverinfo(self, interaction: discord.Interaction, details: bool = False):
        """
        Show server information.

        `details`: Shows more information when set to `True`.
        Default to False.
        """
        ctx = await commands.Context.from_interaction(interaction)
        guild = ctx.guild
        created_at = _("Created on {date_and_time}. That's {relative_time}!").format(
            date_and_time=discord.utils.format_dt(guild.created_at),
            relative_time=discord.utils.format_dt(guild.created_at, "R"),
        )
        online = humanize_number(
            len([m.status for m in guild.members if m.status != discord.Status.offline])
        )
        total_users = guild.member_count and humanize_number(guild.member_count)
        text_channels = humanize_number(len(guild.text_channels))
        voice_channels = humanize_number(len(guild.voice_channels))
        stage_channels = humanize_number(len(guild.stage_channels))
        if not details:
            data = discord.Embed(description=created_at, colour=await ctx.embed_colour())
            data.add_field(
                name=_("Users online"),
                value=f"{online}/{total_users}" if total_users else _("Not available"),
            )
            data.add_field(name=_("Text Channels"), value=text_channels)
            data.add_field(name=_("Voice Channels"), value=voice_channels)
            data.add_field(name=_("Roles"), value=humanize_number(len(guild.roles)))
            data.add_field(name=_("Owner"), value=str(guild.owner))
            data.set_footer(
                text=_("Server ID: ")
                + str(guild.id)
                + _("  •  Use {command} for more info on the server.").format(
                    command=f"{ctx.clean_prefix}serverinfo 1"
                )
            )
            if guild.icon:
                data.set_author(name=guild.name, url=guild.icon)
                data.set_thumbnail(url=guild.icon)
            else:
                data.set_author(name=guild.name)
        else:

            def _size(num: int):
                for unit in ["B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB"]:
                    if abs(num) < 1024.0:
                        return "{0:.1f}{1}".format(num, unit)
                    num /= 1024.0
                return "{0:.1f}{1}".format(num, "YB")

            def _bitsize(num: int):
                for unit in ["B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB"]:
                    if abs(num) < 1000.0:
                        return "{0:.1f}{1}".format(num, unit)
                    num /= 1000.0
                return "{0:.1f}{1}".format(num, "YB")

            shard_info = (
                _("\nShard ID: **{shard_id}/{shard_count}**").format(
                    shard_id=humanize_number(guild.shard_id + 1),
                    shard_count=humanize_number(ctx.bot.shard_count),
                )
                if ctx.bot.shard_count > 1
                else ""
            )
            # Logic from: https://github.com/TrustyJAID/Trusty-cogs/blob/master/serverstats/serverstats.py#L159
            online_stats = {
                _("Humans: "): lambda x: not x.bot,
                _(" • Bots: "): lambda x: x.bot,
                "\N{LARGE GREEN CIRCLE}": lambda x: x.status is discord.Status.online,
                "\N{LARGE ORANGE CIRCLE}": lambda x: x.status is discord.Status.idle,
                "\N{LARGE RED CIRCLE}": lambda x: x.status is discord.Status.do_not_disturb,
                "\N{MEDIUM WHITE CIRCLE}\N{VARIATION SELECTOR-16}": lambda x: (
                    x.status is discord.Status.offline
                ),
                "\N{LARGE PURPLE CIRCLE}": lambda x: any(
                    a.type is discord.ActivityType.streaming for a in x.activities
                ),
                "\N{MOBILE PHONE}": lambda x: x.is_on_mobile(),
            }
            member_msg = _("Users online: **{online}/{total_users}**\n").format(
                online=online, total_users=total_users
            )
            count = 1
            for emoji, value in online_stats.items():
                try:
                    num = len([m for m in guild.members if value(m)])
                except Exception as error:
                    print(error)
                    continue
                else:
                    member_msg += f"{emoji} {bold(humanize_number(num))} " + (
                        "\n" if count % 2 == 0 else ""
                    )
                count += 1

            verif = {
                "none": _("0 - None"),
                "low": _("1 - Low"),
                "medium": _("2 - Medium"),
                "high": _("3 - High"),
                "highest": _("4 - Highest"),
            }

            joined_on = _(
                "{bot_name} joined this server on {bot_join}. That's over {since_join} days ago!"
            ).format(
                bot_name=ctx.bot.user.display_name,
                bot_join=guild.me.joined_at.strftime("%d %b %Y %H:%M:%S"),
                since_join=humanize_number((ctx.message.created_at - guild.me.joined_at).days),
            )

            data = discord.Embed(
                description=(f"{guild.description}\n\n" if guild.description else "") + created_at,
                colour=await ctx.embed_colour(),
            )
            data.set_author(
                name=guild.name,
                icon_url="https://cdn.discordapp.com/emojis/457879292152381443.png"
                if "VERIFIED" in guild.features
                else "https://cdn.discordapp.com/emojis/508929941610430464.png"
                if "PARTNERED" in guild.features
                else None,
            )
            if guild.icon:
                data.set_thumbnail(url=guild.icon)
            data.add_field(name=_("Members:"), value=member_msg)
            data.add_field(
                name=_("Channels:"),
                value=_(
                    "\N{SPEECH BALLOON} Text: {text}\n"
                    "\N{SPEAKER WITH THREE SOUND WAVES} Voice: {voice}\n"
                    "\N{STUDIO MICROPHONE} Stage: {stage}"
                ).format(
                    text=bold(text_channels),
                    voice=bold(voice_channels),
                    stage=bold(stage_channels),
                ),
            )
            data.add_field(
                name=_("Utility:"),
                value=_(
                    "Owner: {owner}\nVerif. level: {verif}\nServer ID: {id}{shard_info}"
                ).format(
                    owner=bold(str(guild.owner)),
                    verif=bold(verif[str(guild.verification_level)]),
                    id=bold(str(guild.id)),
                    shard_info=shard_info,
                ),
                inline=False,
            )
            data.add_field(
                name=_("Misc:"),
                value=_(
                    "AFK channel: {afk_chan}\nAFK timeout: {afk_timeout}\nCustom emojis: {emoji_count}\nRoles: {role_count}"
                ).format(
                    afk_chan=bold(str(guild.afk_channel))
                    if guild.afk_channel
                    else bold(_("Not set")),
                    afk_timeout=bold(humanize_timedelta(seconds=guild.afk_timeout)),
                    emoji_count=bold(humanize_number(len(guild.emojis))),
                    role_count=bold(humanize_number(len(guild.roles))),
                ),
                inline=False,
            )

            excluded_features = {
                # available to everyone since forum channels private beta
                "THREE_DAY_THREAD_ARCHIVE",
                "SEVEN_DAY_THREAD_ARCHIVE",
                # rolled out to everyone already
                "NEW_THREAD_PERMISSIONS",
                "TEXT_IN_VOICE_ENABLED",
                "THREADS_ENABLED",
                # available to everyone sometime after forum channel release
                "PRIVATE_THREADS",
            }
            custom_feature_names = {
                "VANITY_URL": "Vanity URL",
                "VIP_REGIONS": "VIP regions",
            }
            features = sorted(guild.features)
            if "COMMUNITY" in features:
                features.remove("NEWS")
            feature_names = [
                custom_feature_names.get(feature, " ".join(feature.split("_")).capitalize())
                for feature in features
                if feature not in excluded_features
            ]
            if guild.features:
                feature_list = "\n".join(
                    f"\N{WHITE HEAVY CHECK MARK} {feature}" for feature in feature_names
                )
                feature_pages = list(pagify(feature_list, delims=["\n"], page_length=1024))
                for i, page in enumerate(feature_pages):
                    field_name = (
                        _("Server features:") if i == 0 else _("Server features (continued):")
                    )
                    data.add_field(name=field_name, value=page, inline=False)

            if guild.premium_tier != 0:
                nitro_boost = _(
                    "Tier {boostlevel} with {nitroboosters} boosts\n"
                    "File size limit: {filelimit}\n"
                    "Emoji limit: {emojis_limit}\n"
                    "VCs max bitrate: {bitrate}"
                ).format(
                    boostlevel=bold(str(guild.premium_tier)),
                    nitroboosters=bold(humanize_number(guild.premium_subscription_count)),
                    filelimit=bold(_size(guild.filesize_limit)),
                    emojis_limit=bold(str(guild.emoji_limit)),
                    bitrate=bold(_bitsize(guild.bitrate_limit)),
                )
                data.add_field(name=_("Nitro Boost:"), value=nitro_boost)
            if guild.splash:
                data.set_image(url=guild.splash.replace(format="png"))
            data.set_footer(text=joined_on)

        await ctx.send(embed=data)

    @fun.command(
        name="urban",
        description="Look a word up on Urban Dictionary.",
        extras={"red_force_enable": True},
    )
    @app_commands.describe(word="The word to look up.")
    async def urban(self, interaction: discord.Interaction, word: str):
        """Search the Urban Dictionary.

        This uses the unofficial Urban Dictionary API.
        """
        ctx = await commands.Context.from_interaction(interaction)

        try:
            url = "https://api.urbandictionary.com/v0/define"

            params = {"term": str(word).lower()}

            headers = {"content-type": "application/json"}

            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, params=params) as response:
                    data = await response.json()

        except aiohttp.ClientError:
            await ctx.send(
                _("No Urban Dictionary entries were found, or there was an error in the process.")
            )
            return

        if data.get("error") != 404:
            if not data.get("list"):
                return await ctx.send(_("No Urban Dictionary entries were found."))
            if await ctx.embed_requested():
                # a list of embeds
                embeds = []
                for ud in data["list"]:
                    embed = discord.Embed(color=await ctx.embed_color())
                    title = _("{word} by {author}").format(
                        word=ud["word"].capitalize(), author=ud["author"]
                    )
                    if len(title) > 256:
                        title = "{}...".format(title[:253])
                    embed.title = title
                    embed.url = ud["permalink"]

                    description = _("{definition}\n\n**Example:** {example}").format(**ud)
                    if len(description) > 2048:
                        description = "{}...".format(description[:2045])
                    embed.description = description

                    embed.set_footer(
                        text=_(
                            "{thumbs_down} Down / {thumbs_up} Up, Powered by Urban Dictionary."
                        ).format(**ud)
                    )
                    embeds.append(embed)

                if embeds is not None and len(embeds) > 0:
                    await menu(
                        ctx,
                        pages=embeds,
                        message=None,
                        page=0,
                        timeout=30,
                    )
            else:
                messages = []
                for ud in data["list"]:
                    ud.setdefault("example", "N/A")
                    message = _(
                        "<{permalink}>\n {word} by {author}\n\n{description}\n\n"
                        "{thumbs_down} Down / {thumbs_up} Up, Powered by Urban Dictionary."
                    ).format(
                        word=ud.pop("word").capitalize(),
                        description="{description}",
                        **ud,
                    )
                    max_desc_len = 2000 - len(message)

                    description = _("{definition}\n\n**Example:** {example}").format(**ud)
                    if len(description) > max_desc_len:
                        description = "{}...".format(description[: max_desc_len - 3])

                    message = message.format(description=description)
                    messages.append(message)

                if messages is not None and len(messages) > 0:
                    await menu(
                        ctx,
                        pages=messages,
                        message=None,
                        page=0,
                        timeout=30,
                    )
        else:
            await ctx.send(
                _("No Urban Dictionary entries were found, or there was an error in the process.")
            )
