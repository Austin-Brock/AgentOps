"""The agent loop, written by hand.

    Receive goal -> call model -> model decides
                                   |
                        +----------+----------+
                        |                     |
                   call tool             final answer
                        |
                 authorize + validate
                        |
                    execute
                        |
                 observe result -> call model again

Every edge of that diagram is visible below on purpose (DD-002). The two
invariants worth naming: the loop is bounded by a step ceiling so it can never
run away, and tool authorization happens in application code — never in the
prompt (DD-004).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from app.agents.prompts import SUPPORT_AGENT_SYSTEM
from app.config import Settings, get_settings
from app.data.store import DataStore
from app.llm.client import ModelClient, Usage
from app.observability.trace import RunTrace
from app.tools.base import FaultInjector, ToolContext
from app.tools.executor import ToolExecutor, ToolInvocation
from app.tools.registry import ToolRegistry
from app.tools.toolset import SUPPORT_AGENT_SCOPES, build_support_toolset

COMPLETED = "completed"
MAX_STEPS_EXCEEDED = "max_steps_exceeded"
REFUSED = "refused"
TRUNCATED = "truncated"


@dataclass
class AgentStep:
    index: int
    stop_reason: str
    text: str
    invocations: list[ToolInvocation]
    usage: Usage


@dataclass
class AgentRun:
    run_id: str
    goal: str
    status: str
    final_text: str
    steps: list[AgentStep]
    usage: Usage
    trace: RunTrace
    messages: list[dict[str, Any]]

    @property
    def invocations(self) -> list[ToolInvocation]:
        return [inv for step in self.steps for inv in step.invocations]

    @property
    def tools_attempted(self) -> list[str]:
        """Every tool the model asked for, including refused calls."""
        return [inv.tool_name for inv in self.invocations]

    @property
    def tools_executed(self) -> list[str]:
        """Only the calls that reached a handler."""
        return [inv.tool_name for inv in self.invocations if inv.executed]

    @property
    def refusals(self) -> list[ToolInvocation]:
        return [inv for inv in self.invocations if not inv.executed]

    def invocations_of(self, tool_name: str) -> list[ToolInvocation]:
        return [inv for inv in self.invocations if inv.tool_name == tool_name]


@dataclass
class SupportAgent:
    model: ModelClient
    registry: ToolRegistry = field(default_factory=build_support_toolset)
    store: DataStore = field(default_factory=DataStore)
    settings: Settings = field(default_factory=get_settings)
    granted_scopes: frozenset[str] = SUPPORT_AGENT_SCOPES
    system_prompt: str = SUPPORT_AGENT_SYSTEM
    faults: FaultInjector | None = None

    async def run(
        self,
        goal: str,
        *,
        customer_id: str | None = None,
        history: list[dict[str, Any]] | None = None,
        run_id: str | None = None,
    ) -> AgentRun:
        run_id = run_id or f"agent-run-{uuid.uuid4().hex[:8]}"
        trace = RunTrace(run_id=run_id)
        executor = ToolExecutor(registry=self.registry, trace=trace)
        ctx = ToolContext(
            store=self.store,
            settings=self.settings,
            customer_id=customer_id,
            granted_scopes=self.granted_scopes,
            faults=self.faults,
        )

        messages: list[dict[str, Any]] = [*(history or []), {"role": "user", "content": goal}]
        tool_specs = self.registry.specs(ctx)
        steps: list[AgentStep] = []
        total_usage = Usage()

        trace.record("request_received", chars=len(goal), tools_offered=len(tool_specs))

        for index in range(self.settings.max_steps):
            response = await self.model.create(
                system=self.system_prompt, messages=messages, tools=tool_specs
            )
            total_usage = total_usage + response.usage
            trace.record(
                "model_responded",
                step=index,
                stop_reason=response.stop_reason,
                tool_calls=len(response.tool_uses),
                output_tokens=response.usage.output_tokens,
            )

            # Thinking blocks and tool_use blocks are echoed back exactly as the
            # provider produced them; the loop never rewrites an assistant turn.
            messages.append({"role": "assistant", "content": response.assistant_content})

            if response.stop_reason == "refusal":
                steps.append(
                    AgentStep(index, response.stop_reason, response.text, [], response.usage)
                )
                trace.record("run_finished", status=REFUSED)
                return self._finish(
                    run_id, goal, REFUSED, response.text, steps, total_usage, trace, messages
                )

            if not response.tool_uses:
                # No tools requested: this turn is the answer. `max_tokens` means
                # the answer was cut off, which is a different outcome from a
                # deliberate stop and is reported as such.
                status = TRUNCATED if response.stop_reason == "max_tokens" else COMPLETED
                steps.append(
                    AgentStep(index, response.stop_reason, response.text, [], response.usage)
                )
                trace.record("run_finished", status=status, steps=len(steps))
                return self._finish(
                    run_id, goal, status, response.text, steps, total_usage, trace, messages
                )

            # Execute every requested tool, then return all results in a single
            # user turn. Splitting them across turns teaches the model to stop
            # asking for tools in parallel.
            invocations: list[ToolInvocation] = []
            for tool_use in response.tool_uses:
                trace.record("tool_requested", tool=tool_use.name, step=index)
                invocations.append(await executor.execute(tool_use, ctx))

            steps.append(
                AgentStep(index, response.stop_reason, response.text, invocations, response.usage)
            )
            messages.append(
                {
                    "role": "user",
                    "content": [inv.to_tool_result_param() for inv in invocations],
                }
            )

        trace.record("run_finished", status=MAX_STEPS_EXCEEDED, steps=len(steps))
        return self._finish(
            run_id,
            goal,
            MAX_STEPS_EXCEEDED,
            steps[-1].text if steps else "",
            steps,
            total_usage,
            trace,
            messages,
        )

    @staticmethod
    def _finish(
        run_id: str,
        goal: str,
        status: str,
        final_text: str,
        steps: list[AgentStep],
        usage: Usage,
        trace: RunTrace,
        messages: list[dict[str, Any]],
    ) -> AgentRun:
        return AgentRun(
            run_id=run_id,
            goal=goal,
            status=status,
            final_text=final_text,
            steps=steps,
            usage=usage,
            trace=trace,
            messages=messages,
        )
