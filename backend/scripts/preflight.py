"""Verify the model path end to end before spending money on the full suite.

Reports whether a credential resolved — masked, never printed — and makes one
minimal request to confirm it actually authenticates. Costs a fraction of a cent
and turns "the eval failed on all 20 scenarios" into a one-line answer.

    make preflight
"""

from __future__ import annotations

import asyncio
import os
import sys

from app.config import REPO_ROOT, Settings
from app.llm.client import AnthropicModelClient


def mask(secret: str) -> str:
    return f"{secret[:7]}...{secret[-4:]}" if len(secret) > 14 else "set (too short to mask)"


async def main() -> int:
    settings = Settings()
    print(f"  repo root   : {REPO_ROOT}")
    print(f"  .env present: {(REPO_ROOT / '.env').exists()}")

    key = os.environ.get("ANTHROPIC_API_KEY")
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if key:
        print(f"  credential  : ANTHROPIC_API_KEY = {mask(key)}")
    elif token:
        print(f"  credential  : ANTHROPIC_AUTH_TOKEN = {mask(token)}")
    else:
        print("  credential  : NONE FOUND")
        print("\n  Add your key to the repo-root .env file (it is gitignored):")
        print("      echo 'ANTHROPIC_API_KEY=sk-ant-...' >> .env")
        return 1

    print(f"  model       : {settings.model} (effort={settings.effort})")
    print("\n  Making one minimal request to verify authentication...")

    client = AnthropicModelClient(settings=settings)
    try:
        response = await client.create(
            system="Reply with the single word: ready",
            messages=[{"role": "user", "content": "ready?"}],
            tools=[],
        )
    except Exception as exc:  # noqa: BLE001 - the whole job here is to report the failure
        print(f"\n  FAILED: {type(exc).__name__}: {exc}")
        return 1

    print(f"  OK — {response.model} replied {response.text.strip()[:40]!r}")
    print(
        f"  tokens: {response.usage.input_tokens} in / "
        f"{response.usage.output_tokens} out (stop_reason={response.stop_reason})"
    )
    print("\n  Ready. Next: make eval")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
