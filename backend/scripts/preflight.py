"""Verify the model path end to end before spending money on the full suite.

Reports whether a credential resolved — masked, never printed — lists the models
that credential can actually reach, and makes one minimal request to confirm it
authenticates. Costs a fraction of a cent, and turns "the eval failed on all 20
scenarios" into a one-line answer.

    make preflight
"""

from __future__ import annotations

import asyncio
import os
import sys

from app.config import REPO_ROOT, Settings
from app.llm.errors import CREDENTIAL_ENV_VARS
from app.llm.factory import build_model_client, credential_available


def mask(secret: str) -> str:
    return f"{secret[:7]}...{secret[-4:]}" if len(secret) > 14 else "set (too short to mask)"


async def main() -> int:
    settings = Settings()
    env_var = CREDENTIAL_ENV_VARS[settings.provider]

    print(f"  repo root   : {REPO_ROOT}")
    print(f"  .env present: {(REPO_ROOT / '.env').exists()}")
    print(f"  provider    : {settings.provider}")

    secret = os.environ.get(env_var)
    print(f"  credential  : {env_var} = {mask(secret) if secret else 'NOT FOUND'}")

    client = build_model_client(settings)
    if not credential_available(client):
        # A key for a *different* provider is the likeliest mistake, and we can
        # see it from here — so say so rather than reporting only what is absent.
        others = [
            (provider, var)
            for provider, var in CREDENTIAL_ENV_VARS.items()
            if provider != settings.provider and os.environ.get(var)
        ]
        if others:
            provider, var = others[0]
            print(f"\n  But {var} IS set. Provider is '{settings.provider}'.")
            print("  Add this line to .env to use the key you have:")
            print(f"      AGENTOPS_PROVIDER={provider}")
        else:
            print("\n  Add your key to the repo-root .env file (it is gitignored):")
            print(f"      echo '{env_var}=...' >> .env")
            print(f"\n  Providers: {', '.join(sorted(CREDENTIAL_ENV_VARS))}")
        return 1

    print(f"  model       : {settings.model} (effort={settings.effort})")

    lister = getattr(client, "list_models", None)
    if callable(lister):
        try:
            available = await lister()
        except Exception as exc:  # noqa: BLE001 - informational, must not block preflight
            print(f"  available   : could not list ({type(exc).__name__})")
        else:
            # Membership is checked against the full list; only the display is
            # trimmed. Checking against a trimmed list is how this reported a
            # model as unavailable that it then successfully called.
            configured_ok = settings.model in available
            shown = [m for m in available if settings.model in m][:3]
            shown += [m for m in available if m not in shown][: 10 - len(shown)]
            print(f"  reachable   : {len(available)} chat models, e.g. {', '.join(shown)}")
            verdict = "reachable" if configured_ok else "NOT REACHABLE"
            print(f"  configured  : {settings.model} -> {verdict}")
            if not configured_ok:
                print(
                    f"\n  WARNING: {settings.model!r} is not in the reachable list. "
                    "Set AGENTOPS_MODEL in .env to one that is."
                )

    print("\n  Making one minimal request to verify authentication...")
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
        f"{response.usage.output_tokens} out "
        f"({response.usage.reasoning_tokens} reasoning) "
        f"stop_reason={response.stop_reason}"
    )
    print("\n  Ready. Next: make eval")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
