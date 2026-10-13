"""Record each demo scenario against the real model.

    make record-replays                 # every scenario on the demo's list
    make record-replays ARGS="--only S01"
    make record-replays ARGS="--dry-run"

This is the only part of the demo that spends money, and it spends it once per
recording rather than once per visitor. One scenario costs roughly 5,800 tokens.

**Run this at the close of every phase that changes agent behavior.** A replay
does not break when the agent changes — it keeps working perfectly while showing
the old behavior, which is the failure mode worth being deliberate about. The
git SHA and model written into each file are what make that visible afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from datetime import UTC, datetime

from app.agents.support_agent import COMPLETED, AgentRun, SupportAgent
from app.config import REPO_ROOT, Settings
from app.data.store import DataStore
from app.demo.catalog import DemoScenario, load_catalog
from app.demo.replay import ReplayIndex, build_replay, write_replay
from app.llm.factory import build_model_client, credential_available
from app.tools.toolset import build_support_toolset


def git_revision() -> tuple[str, bool]:
    """The commit a recording was captured at, and whether the tree was dirty.

    Dirty is recorded rather than refused: recording from a work in progress is
    normal, and a replay whose SHA does not fully describe the code should say so
    instead of pretending otherwise.
    """
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown", True
    return sha, dirty


async def record(scenario: DemoScenario, settings: Settings) -> AgentRun:
    """One scenario, one fresh store — exactly as the evaluation suite runs it.

    No `customer_id` is passed, because the evaluation does not pass one either
    (DD-013). A recording that differs from the evaluated run demonstrates
    something other than what was measured.
    """
    store = DataStore(settings=settings)
    agent = SupportAgent(
        model=build_model_client(settings),
        registry=build_support_toolset(),
        store=store,
        settings=settings,
    )
    return await agent.run(scenario.message)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record demo replays against the real model.")
    parser.add_argument("--only", nargs="*", metavar="ID", help="Record only these scenario ids.")
    parser.add_argument("--dry-run", action="store_true", help="List what would be recorded.")
    parser.add_argument("--model", default=None)
    args = parser.parse_args(argv)

    catalog = load_catalog()
    scenarios = list(catalog.scenarios)
    if args.only:
        wanted = set(args.only)
        scenarios = [s for s in scenarios if s.id in wanted]
        if missing := wanted - {s.id for s in scenarios}:
            parser.error(f"not on the demo's list: {sorted(missing)}")

    settings = Settings(**({"model": args.model} if args.model else {}))
    index = ReplayIndex()

    if args.dry_run:
        for scenario in scenarios:
            existing = index.meta(scenario.id)
            state = (
                f"recorded {existing['recorded_at']} ({existing['model']})"
                if existing["available"]
                else "not recorded"
            )
            print(f"  {scenario.id}  {scenario.headline:<28} {state}")
        return 0

    if not credential_available(build_model_client(settings)):
        parser.error(
            f"No {settings.provider} credential resolved. Run `make preflight` for details."
        )

    sha, dirty = git_revision()
    print(
        f"Recording {len(scenarios)} scenario(s) against {settings.model} "
        f"({settings.provider}) at {sha[:12]}{' +dirty' if dirty else ''}\n"
    )

    failures: list[str] = []
    for scenario in scenarios:
        run = await record(scenario, settings)
        path = write_replay(
            build_replay(
                scenario_id=scenario.id,
                run=run,
                settings=settings,
                recorded_at=datetime.now(UTC).isoformat(timespec="seconds"),
                git_sha=sha,
                git_dirty=dirty,
            ),
            index=index,
        )
        print(
            f"  {scenario.id}  {run.status:<12} {len(run.trace.events):>3} events  "
            f"{run.usage.input_tokens:>6,} in / {run.usage.output_tokens:>6,} out  "
            f"-> {path.relative_to(REPO_ROOT)}"
        )
        print(f"        tools   : {run.tools_executed or '(none)'}")
        if run.refusals:
            print(f"        REFUSED : {[(i.tool_name, i.result.code) for i in run.refusals]}")
        print(f'        reply   : "{run.final_text[:140]}"')

        # A recording is a real run and can take a path the scenario did not
        # anticipate. Written down either way — then reported, so a bad one is
        # replaced deliberately rather than shipped quietly.
        if run.status != COMPLETED:
            failures.append(f"{scenario.id}: status {run.status}")
        if not run.final_text:
            failures.append(f"{scenario.id}: no final message to the customer")

    if failures:
        print("\n  Review before shipping:")
        for failure in failures:
            print(f"    - {failure}")
        return 1

    print(f"\n  {len(scenarios)} recording(s) written. Check them against the expected outcomes.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
