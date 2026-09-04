from typing import Any, Dict, Optional

from ...functions import names
from ...functions.context import ToolContext
from ...functions.tool_call import ToolCall
from ...functions.types import Function, Parameters, ToolCallSchema
from ...functions.wolframalpha.query import ask_wolfram_alpha


class WolframAlphaFunctionCall(ToolCall):
    schema = ToolCallSchema(
        Function(
            name=names.ASK_WOLFRAM_ALPHA,
            description="Asks Wolfram Alpha about mathematical operations, unit conversions, or scientific questions.",
            parameters=Parameters(
                properties={
                    "query": {
                        "type": "string",
                        "description": "A math operation, unit conversion, or scientific questions",
                    }
                },
                required=["query"],
            ),
        )
    )
    function_name = schema.function.name
    parallel_safe = True

    async def _handle(
        self, tool_context: ToolContext, arguments: Dict[str, Any]
    ) -> Optional[str]:
        return await ask_wolfram_alpha(
            arguments["query"],
            (
                await tool_context.services.bot.get_shared_api_tokens("wolfram_alpha")
            ).get("app_id"),
            tool_context.ctx,
        )
