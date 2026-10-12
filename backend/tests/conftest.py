from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date

import pytest

from app.agents.support_agent import SupportAgent
from app.config import Settings
from app.data.store import DataStore
from app.llm.client import ModelResponse
from app.llm.scripted import ScriptedModelClient
from app.tools.base import Fault, FaultInjector
from app.tools.registry import ToolRegistry
from app.tools.toolset import SUPPORT_AGENT_SCOPES, build_support_toolset


@pytest.fixture
def settings() -> Settings:
    """Explicit settings so the suite cannot be perturbed by a local .env."""
    return Settings(
        model="scripted",
        max_steps=6,
        max_tokens=4096,
        refund_window_days=30,
        refund_auto_approve_max_cents=5_000,
        refund_manager_approval_max_cents=20_000,
        simulated_today=date(2026, 10, 12),
        expose_unauthorized_tools=True,
    )


@pytest.fixture
def store(settings: Settings) -> DataStore:
    """A fresh store per test: refunds created in one test never leak into another."""
    return DataStore(settings=settings)


@pytest.fixture
def registry() -> ToolRegistry:
    return build_support_toolset()


@pytest.fixture
def make_agent(
    settings: Settings, store: DataStore, registry: ToolRegistry
) -> Callable[..., tuple[SupportAgent, ScriptedModelClient]]:
    def factory(
        responses: Sequence[ModelResponse],
        *,
        scopes: frozenset[str] = SUPPORT_AGENT_SCOPES,
        faults: Sequence[Fault] = (),
    ) -> tuple[SupportAgent, ScriptedModelClient]:
        client = ScriptedModelClient(responses)
        agent = SupportAgent(
            model=client,
            registry=registry,
            store=store,
            settings=settings,
            granted_scopes=scopes,
            faults=FaultInjector(list(faults)) if faults else None,
        )
        return agent, client

    return factory
