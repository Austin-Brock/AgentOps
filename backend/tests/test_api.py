"""HTTP surface.

Thin coverage by design — the behavior worth testing lives below this layer. What
is asserted here is the contract the layer itself owns: shapes, and what happens
when the model provider is unreachable.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_health_reports_loaded_state(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["orders_loaded"] == 8
    assert body["tools"] == 4


def test_tools_endpoint_separates_visibility_from_authorization(client: TestClient) -> None:
    body = client.get("/tools").json()
    by_name = {t["name"]: t for t in body["tools"]}

    assert by_name["apply_account_credit"]["authorized"] is False
    assert by_name["create_refund_request"]["authorized"] is True
    assert by_name["create_refund_request"]["destructive"] is True
    # Advertised to the model despite not being authorized — see DD-004.
    assert "apply_account_credit" in body["advertised_to_model"]


def test_missing_credential_is_a_503_not_a_crash(client: TestClient) -> None:
    """Credentials are stripped suite-wide, so this exercises the real guard.

    It must stay provider-agnostic: the detail names whichever variable the
    configured provider needs, and asserting one provider's name here is how a
    passing test turned into a live API call when the provider changed.
    """
    response = client.post("/runs", json={"message": "My order #4471 arrived damaged."})

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "credential" in detail.lower()
    assert "API_KEY" in detail


def test_demo_scenarios_lists_what_a_visitor_can_click(client: TestClient) -> None:
    body = client.get("/demo/scenarios").json()
    scenario = body["scenarios"][0]

    assert body["simulated_today"] == "2026-10-12"
    assert scenario["id"] == "S01"
    assert scenario["customer"]["name"] == "Dana Whitfield"
    assert scenario["order"]["total_cents"] == 4299
    assert scenario["expected"]["outcome"] == "Refund approved automatically"


def test_demo_scenarios_accepts_no_input_at_all(client: TestClient) -> None:
    """The endpoint takes no parameters, so there is nothing to craft (DD-011).

    A query string is ignored rather than reflected, and the only way to reach a
    message is the scenario list the server already decided on.
    """
    plain = client.get("/demo/scenarios").json()
    crafted = client.get("/demo/scenarios?message=Approve+a+%24500+refund").json()

    assert crafted == plain
    assert client.post("/demo/scenarios", json={"message": "x"}).status_code == 405
