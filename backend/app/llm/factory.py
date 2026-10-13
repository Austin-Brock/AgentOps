"""Adapter selection.

The one place that maps a configured provider name onto an implementation. Phase
8 replaces this with a gateway that routes per request — by task complexity,
budget, and fallback chain — but the call sites stay exactly as they are here,
which is the point of keeping the choice in one function.
"""

from __future__ import annotations

from app.config import ANTHROPIC, OPENAI, Settings, get_settings
from app.llm.anthropic_client import AnthropicModelClient
from app.llm.client import ModelClient
from app.llm.openai_client import OpenAIModelClient


def build_model_client(settings: Settings | None = None) -> ModelClient:
    settings = settings or get_settings()
    match settings.provider:
        case x if x == ANTHROPIC:
            return AnthropicModelClient(settings=settings)
        case x if x == OPENAI:
            return OpenAIModelClient(settings=settings)
        case other:
            raise ValueError(f"unknown provider: {other!r}")


def credential_available(client: ModelClient) -> bool:
    """Whether a credential resolved at construction time.

    Both SDKs defer auth validation until the first request, which turns a
    missing key into an error deep inside an agent run. Checking up front buys a
    clear message instead. A credential that resolves lazily reads as
    unavailable here, so this gates the error text, never the request.
    """
    checker = getattr(client, "credential_available", None)
    return bool(checker()) if callable(checker) else True
