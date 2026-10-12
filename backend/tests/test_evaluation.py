"""The evaluation suite is itself code, and gets tested like code.

A grader that silently accepts everything is worse than no grader, so these
tests drive it with runs that should pass and runs that should fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.evaluation.grader import grade
from app.evaluation.scenario import DatasetError, load_dataset
from app.llm.scripted import call, say
from app.tools.toolset import build_support_toolset


@pytest.fixture
def dataset():
    return load_dataset(known_tools=set(build_support_toolset().tools))


class TestDataset:
    def test_phase1_dataset_is_valid(self, dataset) -> None:
        assert len(dataset) == 20
        assert dataset.phase == 1
        assert len({s.id for s in dataset.scenarios}) == 20

    def test_every_scenario_has_expectations(self, dataset) -> None:
        for scenario in dataset.scenarios:
            assert scenario.expect, f"{scenario.id} can never fail"

    def test_known_gaps_explain_themselves(self, dataset) -> None:
        gaps = [s for s in dataset.scenarios if s.is_known_gap]
        assert gaps, "expected at least one scenario tracking an acknowledged gap"
        for scenario in gaps:
            assert "Phase" in (scenario.known_gap or ""), (
                f"{scenario.id}: a known gap must name the phase that closes it"
            )

    def test_faults_reference_real_tools(self, dataset) -> None:
        known = set(build_support_toolset().tools)
        for scenario in dataset.scenarios:
            for fault in scenario.faults:
                assert fault.tool in known


class TestDatasetValidation:
    def _write(self, tmp_path: Path, scenarios: list[dict]) -> Path:
        path = tmp_path / "dataset.yaml"
        path.write_text(yaml.safe_dump({"version": 1, "phase": 1, "scenarios": scenarios}))
        return path

    def _scenario(self, **overrides) -> dict:
        base = {
            "id": "T01",
            "title": "t",
            "message": "m",
            "expect": {"status": "completed"},
        }
        base.update(overrides)
        return base

    def test_unknown_expectation_key_is_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, [self._scenario(expect={"tools_called": ["x"]})])
        with pytest.raises(DatasetError, match="unknown expectation key"):
            load_dataset(path)

    def test_uncompilable_regex_is_rejected(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path, [self._scenario(expect={"response_matches_any": ["[unclosed"]})]
        )
        with pytest.raises(DatasetError, match="does not compile"):
            load_dataset(path)

    def test_unknown_tool_reference_is_rejected(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path, [self._scenario(expect={"tools_executed_include": ["teleport"]})]
        )
        with pytest.raises(DatasetError, match="unknown tool"):
            load_dataset(path, known_tools={"lookup_order"})

    def test_duplicate_ids_are_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, [self._scenario(), self._scenario()])
        with pytest.raises(DatasetError, match="duplicate scenario id"):
            load_dataset(path)

    def test_empty_expectations_are_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, [self._scenario(expect={})])
        with pytest.raises(DatasetError, match="no expectations"):
            load_dataset(path)

    def test_refund_expectation_requires_an_order(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, [self._scenario(expect={"refund": {"created": True}})])
        with pytest.raises(DatasetError, match="needs an order_id"):
            load_dataset(path)


class TestGrader:
    async def test_a_correct_run_passes_every_check(self, dataset, make_agent, store) -> None:
        scenario = dataset.get("S01")
        agent, _ = make_agent(
            [
                call("lookup_order", {"order_id": "4471"}),
                call("get_refund_policy", {}),
                call(
                    "create_refund_request",
                    {
                        "order_id": "4471",
                        "reason": "damaged_on_arrival",
                        "amount_cents": 4299,
                    },
                ),
                say("I've approved a $42.99 refund for order 4471."),
            ]
        )
        run = await agent.run(scenario.message)
        result = grade(scenario, run, store, refunds_before={"4471": 0})

        assert result.passed, [c for c in result.failures]
        assert result.counts_toward_gate

    async def test_a_lazy_run_fails_the_checks_it_should(self, dataset, make_agent, store) -> None:
        scenario = dataset.get("S01")
        agent, _ = make_agent([say("Sorry to hear that! I've refunded you.")])
        run = await agent.run(scenario.message)
        result = grade(scenario, run, store, refunds_before={"4471": 0})

        assert not result.passed
        failed = {c.name for c in result.failures}
        assert "tools_executed_include" in failed
        assert "refund.created[4471]" in failed

    async def test_an_unauthorized_tool_call_fails_the_scenario(
        self, dataset, make_agent, store
    ) -> None:
        scenario = dataset.get("S09")
        agent, _ = make_agent(
            [
                call("apply_account_credit", {"customer_id": "C-1001", "amount_cents": 4299}),
                say("All set, I've credited your account."),
            ],
            scopes=frozenset({"orders:read", "policy:read", "refunds:create", "finance:write"}),
        )
        run = await agent.run(scenario.message)
        result = grade(scenario, run, store)

        # Granting the scope is what makes this run *execute* the tool, which is
        # exactly what the scenario forbids.
        assert not result.passed
        assert "tools_not_executed" in {c.name for c in result.failures}

    async def test_baseline_refunds_are_not_counted_as_newly_created(
        self, dataset, make_agent, store
    ) -> None:
        scenario = dataset.get("S07")  # order 4476 arrives with one refund already
        agent, _ = make_agent(
            [
                call("lookup_order", {"order_id": "4476"}),
                say("Order 4476 was already refunded under RF-0001."),
            ]
        )
        run = await agent.run(scenario.message)
        result = grade(scenario, run, store, refunds_before={"4476": 1})

        assert result.passed, [c.detail for c in result.failures]

    async def test_forbidden_phrasing_fails_the_response_check(
        self, dataset, make_agent, store
    ) -> None:
        scenario = dataset.get("S05")
        agent, _ = make_agent(
            [
                call("lookup_order", {"order_id": "4475"}),
                call(
                    "create_refund_request",
                    {"order_id": "4475", "reason": "damaged_on_arrival", "amount_cents": 7500},
                ),
                say("Good news - your refund has been issued, the money is on its way!"),
            ]
        )
        run = await agent.run(scenario.message)
        result = grade(scenario, run, store, refunds_before={"4475": 0})

        failed = {c.name for c in result.failures}
        assert "response_not_matches" in failed
        assert "response_matches_any" in failed
