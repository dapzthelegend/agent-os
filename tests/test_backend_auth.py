from __future__ import annotations

import base64
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.agentic_os.api_routes import router as api_router
from src.agentic_os.backend_auth import install_backend_auth
from src.agentic_os.config import BackendAuthConfig, Paths, load_app_config


def _basic_auth(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _make_app(config: BackendAuthConfig | None) -> FastAPI:
    app = FastAPI()
    install_backend_auth(app, config)

    @app.get("/healthz")
    async def healthz():
        return "ok"

    @app.get("/discord/interactions/health")
    async def discord_health():
        return {"status": "ok"}

    @app.post("/api/executions/callback")
    async def callback():
        return {"status": "ok"}

    @app.get("/")
    async def dashboard():
        return {"status": "ok"}

    return app


def test_backend_auth_disabled_by_default():
    with TestClient(_make_app(None)) as client:
        response = client.post("/api/executions/callback", json={})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_backend_auth_blocks_protected_routes():
    config = BackendAuthConfig(username="operator", password="secret-pass")

    with TestClient(_make_app(config)) as client:
        api_response = client.post("/api/executions/callback", json={})
        html_response = client.get("/")

    assert api_response.status_code == 401
    assert api_response.json() == {"detail": "Authentication required"}
    assert api_response.headers["www-authenticate"] == 'Basic realm="agentic-os"'
    assert html_response.status_code == 401
    assert html_response.text == "Authentication required"


def test_backend_auth_allows_exempt_routes():
    config = BackendAuthConfig(username="operator", password="secret-pass")

    with TestClient(_make_app(config)) as client:
        healthz_response = client.get("/healthz")
        discord_health_response = client.get("/discord/interactions/health")

    assert healthz_response.status_code == 200
    assert healthz_response.json() == "ok"
    assert discord_health_response.status_code == 200
    assert discord_health_response.json() == {"status": "ok"}


def test_backend_auth_accepts_valid_credentials():
    config = BackendAuthConfig(username="operator", password="secret-pass")

    with TestClient(_make_app(config)) as client:
        response = client.post(
            "/api/executions/callback",
            json={},
            headers=_basic_auth("operator", "secret-pass"),
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_load_app_config_parses_backend_auth(tmp_path, monkeypatch):
    config_path = tmp_path / "agentic_os.config.json"
    config_path.write_text(
        json.dumps(
            {
                "backendAuth": {
                    "enabled": True,
                    "username": "operator",
                    "passwordEnv": "TEST_BACKEND_AUTH_PASSWORD",
                    "realm": "ops",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_BACKEND_AUTH_PASSWORD", "secret-pass")
    monkeypatch.setenv("AGENTIC_OS_CONFIG_PATH", str(config_path))

    config = load_app_config(Paths.from_root(tmp_path))

    assert config.backend_auth == BackendAuthConfig(
        username="operator",
        password="secret-pass",
        realm="ops",
    )


def test_load_app_config_supports_env_only_backend_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_CONFIG_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("AGENTIC_OS_BACKEND_AUTH_USERNAME", "operator")
    monkeypatch.setenv("AGENTIC_OS_BACKEND_AUTH_PASSWORD", "secret-pass")

    config = load_app_config(Paths.from_root(tmp_path))

    assert config.backend_auth == BackendAuthConfig(
        username="operator",
        password="secret-pass",
        realm="agentic-os",
    )


def test_api_approval_mutations_are_forbidden_even_with_auth():
    app = FastAPI()
    install_backend_auth(app, BackendAuthConfig(username="operator", password="secret-pass"))
    app.include_router(api_router)

    with TestClient(app) as client:
        response = client.post(
            "/api/approvals/apr_test/approve",
            json={},
            headers=_basic_auth("operator", "secret-pass"),
        )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Approval mutations are only available via the dashboard and Discord surfaces."
    }
