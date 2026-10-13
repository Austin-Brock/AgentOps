"""HTTP surface.

Thin on purpose: the API constructs an agent and returns its run. All the
behavior worth testing lives below this layer, which is why the test suite
drives the agent directly rather than going through HTTP.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.agents.support_agent import AgentRun, SupportAgent
from app.config import get_settings
from app.data.store import DataStore
from app.demo.catalog import get_catalog
from app.llm.errors import CREDENTIAL_ENV_VARS, ProviderError
from app.llm.factory import build_model_client, credential_available
from app.tools.base import ToolContext
from app.tools.toolset import SUPPORT_AGENT_SCOPES, build_support_toolset

app = FastAPI(title="AgentOps", version="0.1.0")

_store = DataStore()
_registry = build_support_toolset()


class RunRequest(BaseModel):
    message: str = Field(description="The customer's message.")
    customer_id: str | None = Field(default=None, description="Authenticated customer, if known.")


class ToolCallView(BaseModel):
    tool: str
    arguments: dict[str, Any]
    executed: bool
    is_error: bool
    error_code: str | None


class RunResponse(BaseModel):
    run_id: str
    status: str
    response: str
    tool_calls: list[ToolCallView]
    trace: list[str]
    usage: dict[str, int]


def _view(run: AgentRun) -> RunResponse:
    return RunResponse(
        run_id=run.run_id,
        status=run.status,
        response=run.final_text,
        tool_calls=[
            ToolCallView(
                tool=inv.tool_name,
                arguments=inv.arguments,
                executed=inv.executed,
                is_error=inv.is_error,
                error_code=inv.result.code,
            )
            for inv in run.invocations
        ],
        trace=[event.render() for event in run.trace.events],
        usage={
            "input_tokens": run.usage.input_tokens,
            "output_tokens": run.usage.output_tokens,
        },
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "provider": settings.provider,
        "model": settings.model,
        "orders_loaded": len(_store.orders),
        "tools": len(_registry),
    }


@app.get("/tools")
async def list_tools() -> dict[str, Any]:
    settings = get_settings()
    ctx = ToolContext(store=_store, settings=settings, granted_scopes=SUPPORT_AGENT_SCOPES)
    return {
        "granted_scopes": sorted(SUPPORT_AGENT_SCOPES),
        "tools": [
            {
                "name": tool.name,
                "required_scopes": sorted(tool.required_scopes),
                "authorized": tool.required_scopes <= SUPPORT_AGENT_SCOPES,
                "destructive": tool.destructive,
            }
            for tool in _registry
        ],
        "advertised_to_model": [spec["name"] for spec in _registry.specs(ctx)],
    }


@app.get("/demo/scenarios")
async def demo_scenarios() -> dict[str, Any]:
    """What a visitor may click, with the order facts looked up fresh.

    No message is accepted here or anywhere else in the demo: the client sends a
    scenario id and the server resolves the prompt from the evaluation dataset
    (DD-011). The list is small, fixed, and the only thing a run can be about.
    """
    return get_catalog().view(_store, get_settings())


@app.post("/runs", response_model=RunResponse)
async def create_run(request: RunRequest) -> RunResponse:
    settings = get_settings()
    model = build_model_client(settings)
    if not credential_available(model):
        raise HTTPException(
            status_code=503,
            detail=(
                f"No {settings.provider} credential resolved. Set "
                f"{CREDENTIAL_ENV_VARS[settings.provider]} and retry."
            ),
        )

    agent = SupportAgent(model=model, registry=_registry, store=_store, settings=settings)
    try:
        run = await agent.run(request.message, customer_id=request.customer_id)
    except ProviderError as exc:
        # The provider failed, not the agent. Phase 8 turns this into routing and
        # fallback; for now it is reported honestly as an upstream failure.
        raise HTTPException(status_code=502, detail=f"Model provider error: {exc}") from exc
    return _view(run)
