from __future__ import annotations

import hashlib
import hmac
import secrets


_PROCESS_SECRET = secrets.token_bytes(32)
_VERSION = "v1"


def mint_approval_token(*, action: str, approval_id: str) -> str:
    message = f"{_VERSION}:{action}:{approval_id}".encode("utf-8")
    digest = hmac.new(_PROCESS_SECRET, message, hashlib.sha256).hexdigest()
    return f"{_VERSION}:{digest}"


def verify_approval_token(*, action: str, approval_id: str, token: str | None) -> bool:
    if not token or ":" not in token:
        return False
    version, _, presented = token.partition(":")
    if version != _VERSION or not presented:
        return False
    expected = mint_approval_token(action=action, approval_id=approval_id)
    return hmac.compare_digest(expected, token)
