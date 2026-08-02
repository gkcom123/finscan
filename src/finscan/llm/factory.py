"""Chat model factory.

Provider is config-driven so the same graph runs against a plain OpenAI key
today and Azure OpenAI later, with no code change — only `.env`. Kept behind a
function so the graph never imports langchain_openai directly and tests can
monkeypatch `get_chat_model` with a stub.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from finscan.config import settings


@lru_cache(maxsize=1)
def get_chat_model() -> Any:
    provider = settings.finscan_llm_provider.lower()

    if provider == "azure":
        if not (settings.azure_openai_endpoint and settings.azure_openai_api_key):
            raise RuntimeError(
                "FINSCAN_LLM_PROVIDER=azure but AZURE_OPENAI_ENDPOINT / "
                "AZURE_OPENAI_API_KEY are not set (see .env.example)."
            )
        from langchain_openai import AzureChatOpenAI

        return AzureChatOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,
            api_version=settings.azure_openai_api_version,
            azure_deployment=settings.azure_openai_deployment,
            temperature=settings.llm_temperature,
            max_retries=3,
            timeout=120,
        )

    if provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError(
                "FINSCAN_LLM_PROVIDER=openai but OPENAI_API_KEY is not set "
                "(see .env.example)."
            )
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            base_url=settings.openai_base_url or None,
            temperature=settings.llm_temperature,
            max_retries=3,
            timeout=120,
        )

    raise RuntimeError(
        f"Unknown FINSCAN_LLM_PROVIDER '{provider}'. Use 'openai' or 'azure'."
    )


def structured(schema: type) -> Any:
    """Chat model bound to a Pydantic output schema (function-calling mode)."""
    return get_chat_model().with_structured_output(schema, method="function_calling")
