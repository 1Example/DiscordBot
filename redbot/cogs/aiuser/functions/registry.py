"""The explicit registry of every available tool.

Adding a tool = write the ToolCall subclass, then list it here, add tool call name constants in aiuser.functions.names
"""

from typing import List

from redbot.core import Config, commands

from ..functions import names
from ..functions.coderunner.tool_call import CodeRunnerToolCall
from ..functions.discord.tool_call import (
    AddReactionToolCall,
    GetDiscordInfoToolCall,
)
from ..functions.imagerequest.tool_call import ImageRequestToolCall
from ..functions.memory.tool_call import ReadMemoryToolCall, SaveMemoryToolCall
from ..functions.noresponse.tool_call import NoResponseToolCall
from ..functions.scrape.tool_call import ScrapeToolCall
from ..functions.search.tool_call import SearchToolCall
from ..functions.tool_call import ToolCall
from ..functions.voice.tool_call import VoiceRequestToolCall
from ..functions.weather.tool_call import (
    IsDaytimeToolCall,
    LocationWeatherToolCall,
)
from ..functions.wolframalpha.tool_call import WolframAlphaFunctionCall

ALL_TOOLS = [
    NoResponseToolCall,
    AddReactionToolCall,
    GetDiscordInfoToolCall,
    ImageRequestToolCall,
    VoiceRequestToolCall,
    ScrapeToolCall,
    SearchToolCall,
    LocationWeatherToolCall,
    IsDaytimeToolCall,
    WolframAlphaFunctionCall,
    CodeRunnerToolCall,
    SaveMemoryToolCall,
    ReadMemoryToolCall,
]

TOOLS_BY_NAME = {cls.function_name: cls for cls in ALL_TOOLS}


async def get_enabled_tools(config: Config, ctx: commands.Context) -> List[ToolCall]:
    """Instantiate the tools enabled for this guild."""
    enabled = set(await config.guild(ctx.guild).function_calling_functions())

    if ctx.interaction:
        # reactions cannot be added to the invoking message of a slash command
        enabled.discard(names.ADD_REACTION)

    return [tool() for tool in ALL_TOOLS if tool.function_name in enabled]
