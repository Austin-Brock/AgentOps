"""Provider error types, collected so call sites do not import SDKs directly.

Catching provider failures means naming provider exception classes, and a route
handler that imports `anthropic` is a route handler that cannot run on OpenAI.
"""

from __future__ import annotations

from anthropic import AnthropicError
from openai import OpenAIError

from app.config import ANTHROPIC, OPENAI

#: Every provider failure the adapters can raise. Catch this, not an SDK class.
ProviderError = (AnthropicError, OpenAIError)

CREDENTIAL_ENV_VARS = {
    ANTHROPIC: "ANTHROPIC_API_KEY",
    OPENAI: "OPENAI_API_KEY",
}
