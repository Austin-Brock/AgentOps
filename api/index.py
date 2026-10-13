"""Vercel's entry point into the existing FastAPI app.

Vercel's Python runtime looks for an ASGI application here and serves it as one
function. Nothing about the app changes: this file exists only so the platform
can find it, and so `backend/` stays importable at its real path rather than
being restructured around a host.

The deployed function has **no model credential**. Replay mode reads recorded
runs from disk and never calls a provider, so there is nothing here for a
visitor to spend and nothing to leak. Live mode (step 6) is what introduces a
key, together with the controls that make one safe to expose.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The app lives in backend/, which is a package root rather than the repository
# root. Vercel runs this file from the repository root, so the path is added
# here instead of being baked into a build step.
BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.main import app  # noqa: E402 - the path above has to be set first

__all__ = ["app"]
