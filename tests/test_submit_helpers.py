from __future__ import annotations

import base64
from pathlib import Path
from importlib.machinery import SourceFileLoader


ROOT = Path(__file__).resolve().parents[1]
SUBMIT_RESULT_PATH = ROOT.parent / "bin" / "submit-result"
SUBMIT_PLAN_PATH = ROOT.parent / "bin" / "submit-plan"


def _load_module(name: str, path: Path):
    return SourceFileLoader(name, str(path)).load_module()


def _decode_basic(header: str) -> tuple[str, str]:
    token = header.split(" ", 1)[1]
    decoded = base64.b64decode(token.encode("ascii")).decode("utf-8")
    username, _, password = decoded.partition(":")
    return username, password


def test_submit_result_loads_backend_auth_from_repo_config(monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_BACKEND_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("AGENTIC_OS_BACKEND_AUTH_PASSWORD", raising=False)
    module = _load_module("submit_result_mod", SUBMIT_RESULT_PATH)

    headers = module._backend_auth_headers()

    assert "Authorization" in headers
    username, password = _decode_basic(headers["Authorization"])
    assert username
    assert password


def test_submit_plan_loads_backend_auth_from_repo_config(monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_BACKEND_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("AGENTIC_OS_BACKEND_AUTH_PASSWORD", raising=False)
    module = _load_module("submit_plan_mod", SUBMIT_PLAN_PATH)

    headers = module._backend_auth_headers()

    assert "Authorization" in headers
    username, password = _decode_basic(headers["Authorization"])
    assert username
    assert password
