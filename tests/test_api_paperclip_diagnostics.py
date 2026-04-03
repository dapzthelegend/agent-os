from __future__ import annotations

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.agentic_os.api_routes import router as api_router


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(api_router)
    return TestClient(app, raise_server_exceptions=False)


def test_paperclip_diagnostics_requires_identifier() -> None:
    client = _make_client()
    with patch("src.agentic_os.api_routes.get_service", return_value=object()):
        response = client.get("/api/paperclip/diagnostics")

    assert response.status_code == 400
    assert response.json()["detail"] == "provide task_id or issue_id"


def test_paperclip_diagnostics_passes_query_params() -> None:
    client = _make_client()
    fake_payload = {"ok": True}
    with patch("src.agentic_os.api_routes.get_service", return_value=object()), patch(
        "src.agentic_os.health.get_paperclip_diagnostics",
        return_value=fake_payload,
    ) as mocked:
        response = client.get(
            "/api/paperclip/diagnostics?task_id=task_1&paperclip_issue_id=iss_1&activity_lookback_seconds=42"
        )

    assert response.status_code == 200
    assert response.json() == fake_payload
    mocked.assert_called_once()
    _, kwargs = mocked.call_args
    assert kwargs["task_id"] == "task_1"
    assert kwargs["issue_id"] == "iss_1"
    assert kwargs["activity_lookback_seconds"] == 42
