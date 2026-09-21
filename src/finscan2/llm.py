"""The one place v2 touches a model provider.

Reuses v1's factory so there is a single provider configuration for the repo —
same .env, same OpenAI/Azure switch, same temperature. When v1 is eventually
retired, this module is the only thing that has to grow its own client, and
nothing else in finscan2 changes.
"""
from __future__ import annotations

from typing import Any


def chat_model() -> Any:
    """A chat model for free-form calls (vision transcription)."""
    from finscan.llm.factory import get_chat_model

    return get_chat_model()


def structured(model_cls: type) -> Any:
    """A model constrained to return `model_cls`. Used by stage 3, not stage 1."""
    from finscan.llm.factory import structured as _structured

    return _structured(model_cls)
