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


def test_missing_credential_is_a_503_not_a_crash(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    response = client.post("/runs", json={"message": "My order #4471 arrived damaged."})

    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]
