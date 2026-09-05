from random import shuffle

import aiohttp

from redbot.core.i18n import Translator, cog_i18n
import discord
from redbot.core import Config, app_commands, commands
from .dashboard_integration import DashboardIntegration

_ = Translator("Image", __file__)


@cog_i18n(_)
class Image(DashboardIntegration, commands.Cog):
    """Find a picture or a GIF, from Imgur, Giphy or a subreddit."""

    image = app_commands.Group(
        name="image",
        description="Find a picture or a GIF.",
        extras={"red_force_enable": True},
    )

    default_global = {"imgur_client_id": None}

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.config = Config.get_conf(self, identifier=2652104208, force_registration=True)
        self.config.register_global(**self.default_global)
        self.session = aiohttp.ClientSession()
        self.imgur_base_url = "https://api.imgur.com/3/"

    async def cog_load(self) -> None:
        """Move the API keys from cog stored config to core bot config if they exist."""
        imgur_token = await self.config.imgur_client_id()
        if imgur_token is not None:
            if not await self.bot.get_shared_api_tokens("imgur"):
                await self.bot.set_shared_api_tokens("imgur", client_id=imgur_token)
            await self.config.imgur_client_id.clear()

    async def cog_unload(self):
        await self.session.close()

    async def red_delete_data_for_user(self, **kwargs):
        """Nothing to delete"""
        return


    @image.command(name="imgur", description="Search Imgur.")
    @app_commands.describe(
        term="What to search for.",
        count="How many results, up to 5.",
    )
    async def imgur_search(
        self,
        interaction: discord.Interaction,
        term: str,
        count: app_commands.Range[int, 1, 5] = 1,
    ):
        """Search Imgur for the specified term.

        - `[count]`: How many images should be returned (maximum 5). Defaults to 1.
        - `<terms...>`: The terms used to search Imgur.
        """
        ctx = await commands.Context.from_interaction(interaction)
        if count < 1 or count > 5:
            await ctx.send(_("Image count has to be between 1 and 5."))
            return
        url = self.imgur_base_url + "gallery/search/time/all/0"
        params = {"q": term}
        imgur_client_id = (await ctx.bot.get_shared_api_tokens("imgur")).get("client_id")
        if not imgur_client_id:
            await ctx.send(
                _(
                    "A Client ID has not been set! Please set one with `{prefix}imgurcreds`."
                ).format(prefix=ctx.clean_prefix)
            )
            return
        headers = {"Authorization": "Client-ID {}".format(imgur_client_id)}
        async with self.session.get(url, headers=headers, params=params) as search_get:
            data = await search_get.json()

        if data["success"]:
            results = data["data"]
            if not results:
                await ctx.send(_("Your search returned no results."))
                return
            shuffle(results)
            msg = _("Search results...\n")
            for r in results[:count]:
                msg += r["gifv"] if "gifv" in r else r["link"]
                msg += "\n"
            await ctx.send(msg)
        else:
            await ctx.send(
                _("Something went wrong. Error code is {code}.").format(code=data["status"])
            )

    @image.command(name="subreddit", description="Images from a subreddit, via Imgur.")
    @app_commands.describe(
        subreddit="The subreddit to pull from.",
        count="How many results, up to 5.",
        sort_type="Top posts or the newest ones.",
        window="How far back to look, when sorting by top.",
    )
    @app_commands.choices(
        sort_type=[
            app_commands.Choice(name="Top", value="top"),
            app_commands.Choice(name="Newest", value="new"),
        ],
        window=[
            app_commands.Choice(name="Today", value="day"),
            app_commands.Choice(name="This week", value="week"),
            app_commands.Choice(name="This month", value="month"),
            app_commands.Choice(name="This year", value="year"),
            app_commands.Choice(name="All time", value="all"),
        ],
    )
    async def imgur_subreddit(
        self,
        interaction: discord.Interaction,
        subreddit: str,
        count: app_commands.Range[int, 1, 5] = 1,
        sort_type: str = "top",
        window: str = "day",
    ):
        """Get images from a subreddit.

        - `<subreddit>`: The subreddit to get images from.
        - `[count]`: The number of images to return (maximum 5). Defaults to 1.
        - `[sort_type]`: New, or top results. Defaults to top.
        - `[window]`: The timeframe, can be the past day, week, month, year or all. Defaults to day.
        """
        ctx = await commands.Context.from_interaction(interaction)
        if count < 1 or count > 5:
            await ctx.send(_("Image count has to be between 1 and 5."))
            return
        sort_type = sort_type.lower()
        window = window.lower()

        if sort_type == "new":
            sort = "time"
        elif sort_type == "top":
            sort = "top"
        else:
            await ctx.send(_("Only 'new' and 'top' are a valid sort type."))
            return

        if window not in ("day", "week", "month", "year", "all"):
            await ctx.send_help()
            return

        imgur_client_id = (await ctx.bot.get_shared_api_tokens("imgur")).get("client_id")
        if not imgur_client_id:
            await ctx.send(
                _(
                    "A Client ID has not been set! Please set one with `{prefix}imgurcreds`."
                ).format(prefix=ctx.clean_prefix)
            )
            return

        links = []
        headers = {"Authorization": "Client-ID {}".format(imgur_client_id)}
        url = self.imgur_base_url + "gallery/r/{}/{}/{}/0".format(subreddit, sort, window)

        async with self.session.get(url, headers=headers) as sub_get:
            data = await sub_get.json()

        if data["success"]:
            items = data["data"]
            if items:
                for item in items[:count]:
                    link = item["gifv"] if "gifv" in item else item["link"]
                    links.append("{}\n{}".format(item["title"], link))

                if links:
                    await ctx.send("\n".join(links))
            else:
                await ctx.send(_("No results found."))
        else:
            await ctx.send(
                _("Something went wrong. Error code is {code}.").format(code=data["status"])
            )


    @image.command(name="gif", description="The first Giphy result for a search.")
    @app_commands.describe(keywords="What to search for.")
    async def gif(self, interaction: discord.Interaction, keywords: str):
        """Retrieve the first search result from Giphy.

        - `<keywords...>`: The keywords used to search Giphy.
        """
        ctx = await commands.Context.from_interaction(interaction)
        giphy_api_key = (await ctx.bot.get_shared_api_tokens("GIPHY")).get("api_key")
        if not giphy_api_key:
            await ctx.send(
                _("An API key has not been set! Please set one with `{prefix}giphycreds`.").format(
                    prefix=ctx.clean_prefix
                )
            )
            return

        url = "http://api.giphy.com/v1/gifs/search"
        async with self.session.get(url, params={"api_key": giphy_api_key, "q": keywords}) as r:
            result = await r.json()
            if r.status == 200:
                if result["data"]:
                    await ctx.send(result["data"][0]["url"])
                else:
                    await ctx.send(_("No results found."))
            else:
                await ctx.send(_("Error contacting the Giphy API."))

    @image.command(name="gifrandom", description="A random Giphy result for a search.")
    @app_commands.describe(keywords="What to search for.")
    async def gifr(self, interaction: discord.Interaction, keywords: str):
        """Retrieve a random GIF from a Giphy search.

        - `<keywords...>`: The keywords used to generate a random GIF.
        """
        ctx = await commands.Context.from_interaction(interaction)
        giphy_api_key = (await ctx.bot.get_shared_api_tokens("GIPHY")).get("api_key")
        if not giphy_api_key:
            await ctx.send(
                _("An API key has not been set! Please set one with `{prefix}giphycreds`.").format(
                    prefix=ctx.clean_prefix
                )
            )
            return

        url = "http://api.giphy.com/v1/gifs/random"
        async with self.session.get(url, params={"api_key": giphy_api_key, "tag": keywords}) as r:
            result = await r.json()
            if r.status == 200:
                if result["data"]:
                    await ctx.send(result["data"]["url"])
                else:
                    await ctx.send(_("No results found."))
            else:
                await ctx.send(_("Error contacting the API."))

