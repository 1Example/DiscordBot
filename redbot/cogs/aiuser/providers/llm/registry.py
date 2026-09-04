from typing import TYPE_CHECKING, Optional

from ...providers.llm.codex.oauth import is_codex_endpoint_mode
from ...providers.llm.codex.provider import CodexProvider
from ...providers.llm.openai_compatible.client import get_openai_client
from ...providers.llm.openai_compatible.provider import OpenAICompatibleProvider

from .base import LLMProvider

if TYPE_CHECKING:
    from ...core.services import AIUserServices


async def get_llm_provider(services: "AIUserServices") -> Optional[LLMProvider]:
    if await is_codex_endpoint_mode(services.config):
        return CodexProvider(services.config)

    client = await get_openai_client(services)
    if client is None:
        return None

    return OpenAICompatibleProvider(services.config, client)


async def list_llm_models(services: "AIUserServices") -> list:
    provider = await get_llm_provider(services)
    if provider is None:
        return []
    return await provider.list_models()
