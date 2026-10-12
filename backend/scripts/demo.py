"""Walk through what the Phase 1 agent does, with no API key required.

Each scenario below is driven by a scripted model so the output is identical
every time. The point is not to show the model being clever — it is to show what
the application does with the model's decisions: which tool calls it allows,
which it refuses, and which business rules it enforces on its own.

    make demo
"""

from __future__ import annotations

import asyncio
from datetime import date

from app.agents.support_agent import SupportAgent
from app.config import Settings
from app.data.store import DataStore
from app.llm.scripted import LoopingModelClient, ScriptedModelClient, call, say
from app.tools.base import Fault, FaultInjector

SETTINGS = Settings(simulated_today=date(2026, 10, 12), max_steps=6)
RULE = "=" * 78


def header(title: str, explanation: str) -> None:
    print(f"\n{RULE}\n  {title}\n{RULE}")
    print(f"  {explanation}\n")


async def scenario(
    title: str,
    explanation: str,
    customer_message: str,
    responses: list,
    *,
    faults: list[Fault] | None = None,
    watch_orders: tuple[str, ...] = (),
) -> None:
    header(title, explanation)
    store = DataStore(settings=SETTINGS)
    agent = SupportAgent(
        model=ScriptedModelClient(responses),
        store=store,
        settings=SETTINGS,
        faults=FaultInjector(faults) if faults else None,
    )
    run = await agent.run(customer_message)

    print(f'  Customer: "{customer_message}"\n')
    print(run.trace.render())
    print(f"\n  status    : {run.status}")
    print(f"  executed  : {run.tools_executed or '(none)'}")
    if run.refusals:
        print(f"  REFUSED   : {[(i.tool_name, i.result.code) for i in run.refusals]}")
    for order_id in watch_orders:
        rows = [
            (r.refund_id, f"${r.amount_cents / 100:,.2f}", r.status)
            for r in store.refunds_for_order(order_id)
        ]
        print(f"  refunds on {order_id}: {rows or '(none created)'}")
    if run.final_text:
        print(f'\n  Agent reply: "{run.final_text}"')


async def runaway() -> None:
    header(
        "6. The loop cannot run away",
        "A model that never stops asking for tools is stopped by the step ceiling.",
    )
    store = DataStore(settings=SETTINGS)
    client = LoopingModelClient("lookup_order", {"order_id": "4471"})
    agent = SupportAgent(model=client, store=store, settings=SETTINGS)
    run = await agent.run("Where is my order?")
    print(f"  max_steps      : {SETTINGS.max_steps}")
    print(f"  model calls    : {client.call_count}")
    print(f"  status         : {run.status}")
    print("\n  Without this ceiling, a confused model bills you until you notice.")


async def main() -> None:
    await scenario(
        "1. The happy path — the agent investigates, then acts",
        "Nothing here is hardcoded: the model chose each tool, in this order, "
        "from what the previous result told it.",
        "My order #4471 arrived damaged. I'd like a refund.",
        [
            call("lookup_order", {"order_id": "4471"}),
            call("get_refund_policy", {}),
            call(
                "create_refund_request",
                {"order_id": "4471", "reason": "damaged_on_arrival", "amount_cents": 4299},
            ),
            say(
                "I checked order 4471 - delivered 2026-10-06 for $42.99. Since the mug "
                "arrived cracked and you're inside the 30-day window, I've approved a "
                "full refund (RF-0002) back to your original payment method."
            ),
        ],
        watch_orders=("4471",),
    )

    await scenario(
        "2. A tool the agent is NOT allowed to use",
        "The customer asks for store credit. The model tries. The application "
        "refuses it before the handler is reachable, and the agent recovers "
        "through the path it is authorized to take.",
        "My order #4471 came damaged - just put the money on my account as store credit.",
        [
            call("lookup_order", {"order_id": "4471"}),
            call("apply_account_credit", {"customer_id": "C-1001", "amount_cents": 4299}),
            call(
                "create_refund_request",
                {"order_id": "4471", "reason": "damaged_on_arrival", "amount_cents": 4299},
            ),
            say(
                "I can't apply store credit directly, but I've approved a $42.99 refund "
                "for order 4471 (RF-0002) to your original payment method instead."
            ),
        ],
        watch_orders=("4471",),
    )

    await scenario(
        "3. The model asks for something policy forbids",
        "Order 4472 was delivered 185 days ago. Even though the model decided to "
        "refund it, the refund tool rejects the request itself - the rule lives in "
        "code, not in the prompt.",
        "I bought something about six months ago and want a refund. It was order 4472.",
        [
            call("lookup_order", {"order_id": "4472"}),
            call(
                "create_refund_request",
                {"order_id": "4472", "reason": "changed_mind", "amount_cents": 2999},
            ),
            say(
                "Order 4472 was delivered on 2026-04-10, which is outside our 30-day "
                "refund window, so I'm not able to refund it. If the item is faulty, a "
                "manufacturer warranty claim may still apply."
            ),
        ],
        watch_orders=("4472",),
    )

    await scenario(
        "4. A refund too large to release",
        "$75.00 is above the $50.00 automatic limit. The refund is created as "
        "PENDING, not paid - and the agent is told to say so.",
        "The hand grinder from order 4475 showed up with a cracked body.",
        [
            call("lookup_order", {"order_id": "4475"}),
            call(
                "create_refund_request",
                {"order_id": "4475", "reason": "damaged_on_arrival", "amount_cents": 7500},
            ),
            say(
                "I've submitted a $75.00 refund for order 4475. Because it's above our "
                "automatic limit it needs a manager's approval first - it's in the queue "
                "now and you'll hear from us once it's released."
            ),
        ],
        watch_orders=("4475",),
    )

    await scenario(
        "5. A tool fails, and the agent recovers",
        "The order system times out on the first call. The failure reaches the "
        "model as data, it retries, and the run completes.",
        "Can you check the status of order #4471 for me?",
        [
            call("lookup_order", {"order_id": "4471"}, call_id="attempt-1"),
            call("lookup_order", {"order_id": "4471"}, call_id="attempt-2"),
            say("Order 4471 was delivered on 2026-10-06 by UPS. Anything else I can check?"),
        ],
        faults=[Fault(tool="lookup_order", times=1)],
    )

    await runaway()

    print(f"\n{RULE}")
    print("  Every scenario above is also an automated test. Run: make test")
    print(f"{RULE}\n")


if __name__ == "__main__":
    asyncio.run(main())
