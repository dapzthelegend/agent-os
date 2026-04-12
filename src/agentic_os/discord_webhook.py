"""
FastAPI router for the Discord Interactions endpoint.

Discord POSTs every user interaction (slash commands, button clicks, modal
submissions) to a configured "Interactions Endpoint URL". This router:

  1. Verifies the Ed25519 signature on the request body using the application
     public key (DISCORD_PUBLIC_KEY). Any failure returns 401.
  2. Parses the JSON body and dispatches it to `discord_interactions.handle_interaction`.
  3. Returns the handler's response dict as JSON.

The endpoint is mounted at POST /discord/interactions and is exposed publicly
over the Tailscale Funnel URL.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .discord_interactions import (
    SignatureError,
    handle_interaction,
    verify_signature,
)
from .web_support import get_service

log = logging.getLogger(__name__)
# Surface our INFO logs even when the root logger defaults to WARNING
# (uvicorn's --log-level only configures its own loggers).
if log.level == logging.NOTSET:
    log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(_h)
    log.propagate = False

router = APIRouter(prefix="/discord", tags=["discord"])

_PUBLIC_KEY_ENV = "DISCORD_PUBLIC_KEY"


@router.post("/interactions")
async def discord_interactions(request: Request) -> JSONResponse:
    t_start = time.monotonic()
    public_key = os.environ.get(_PUBLIC_KEY_ENV)
    if not public_key:
        log.error("discord_webhook: DISCORD_PUBLIC_KEY not set; rejecting")
        raise HTTPException(status_code=503, detail="Discord interactions not configured")

    signature = request.headers.get("x-signature-ed25519")
    timestamp = request.headers.get("x-signature-timestamp")
    if not signature or not timestamp:
        raise HTTPException(status_code=401, detail="missing signature headers")

    body = await request.body()

    try:
        verify_signature(
            public_key_hex=public_key,
            signature_hex=signature,
            timestamp=timestamp,
            body=body,
        )
    except SignatureError as exc:
        log.warning("discord_webhook: signature verification failed: %s", exc)
        raise HTTPException(status_code=401, detail="invalid signature") from exc

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("discord_webhook: body parse failed: %s", exc)
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    itype = payload.get("type")
    data = payload.get("data") or {}
    iname = data.get("name") or data.get("custom_id") or ""
    log.info(
        "discord_webhook: received interaction type=%s name=%r (after %.3fs)",
        itype, iname, time.monotonic() - t_start,
    )

    try:
        # Offload the sync service work to a worker thread so the event loop
        # stays free. This is essential for Discord's strict 3-second deadline:
        # if a prior interaction is still processing synchronously, a new one
        # would otherwise queue and time out.
        response = await asyncio.to_thread(
            handle_interaction, payload, service=get_service()
        )
    except Exception as exc:  # pragma: no cover — final safety net
        log.exception("discord_webhook: handler crashed")
        return JSONResponse(
            {
                "type": 4,
                "data": {"content": f"⚠️ Internal error: {exc}", "flags": 64},
            },
            status_code=200,
        )

    log.info(
        "discord_webhook: responded type=%s name=%r in %.3fs",
        itype, iname, time.monotonic() - t_start,
    )
    return JSONResponse(response)


@router.get("/interactions/health")
async def discord_interactions_health() -> dict:
    """Plain readiness probe — reports whether Discord env vars are configured."""
    return {
        "public_key_set": bool(os.environ.get("DISCORD_PUBLIC_KEY")),
        "application_id_set": bool(os.environ.get("DISCORD_APPLICATION_ID")),
        "bot_token_set": bool(os.environ.get("DISCORD_BOT_TOKEN")),
        "operator_user_id_set": bool(os.environ.get("DISCORD_USER_ID")),
    }
