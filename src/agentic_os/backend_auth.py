from __future__ import annotations

import base64
import binascii
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .config import BackendAuthConfig


_EXEMPT_PATHS = {
    "/healthz",
    "/api/health",
    "/api/watchdog",
    "/api/paperclip/health",
    "/discord/interactions/health",
}
_EXEMPT_PREFIXES = ("/static/", "/discord/interactions")


def install_backend_auth(app: FastAPI, config: BackendAuthConfig | None) -> None:
    app.state.backend_auth = config
    if config is None:
        return

    @app.middleware("http")
    async def require_backend_auth(request: Request, call_next):
        if _is_exempt_path(request.url.path):
            return await call_next(request)
        if _credentials_match(request, config):
            return await call_next(request)
        return _unauthorized_response(request, config.realm)


def _is_exempt_path(path: str) -> bool:
    if path in _EXEMPT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _EXEMPT_PREFIXES)


def _credentials_match(request: Request, config: BackendAuthConfig) -> bool:
    username, password = _decode_basic_credentials(request.headers.get("Authorization"))
    if username is None or password is None:
        return False
    return secrets.compare_digest(username, config.username) and secrets.compare_digest(password, config.password)


def _decode_basic_credentials(header: str | None) -> tuple[str | None, str | None]:
    if not header:
        return None, None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic" or not token:
        return None, None
    try:
        decoded = base64.b64decode(token.encode("ascii"), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None, None
    username, separator, password = decoded.partition(":")
    if not separator:
        return None, None
    return username, password


def _unauthorized_response(request: Request, realm: str):
    headers = {"WWW-Authenticate": f'Basic realm="{realm}"'}
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Authentication required"}, status_code=401, headers=headers)
    return PlainTextResponse("Authentication required", status_code=401, headers=headers)
