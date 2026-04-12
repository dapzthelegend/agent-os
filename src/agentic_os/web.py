from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .api_routes import router as api_router
from .discord_webhook import router as discord_router
from .web_routes import router as web_router
from .web_support import STATIC_DIR, TEMPLATES_DIR


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start the background scheduler on startup; stop it on shutdown."""
    import logging
    from .config import default_paths, load_app_config
    from .scheduler import BackgroundScheduler
    from .jobs import register_all_jobs

    scheduler = BackgroundScheduler()
    try:
        paths = default_paths()
        config = load_app_config(paths)
        register_all_jobs(scheduler, paths, config)
        scheduler.start()
        app.state.scheduler = scheduler
    except Exception as exc:
        logging.getLogger(__name__).error("scheduler startup failed: %s", exc)
        app.state.scheduler = None

    yield

    scheduler.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="agentic-os dashboard", lifespan=_lifespan)
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates.env.filters["status_tone"] = _status_tone
    app.state.templates.env.filters["risk_tone"] = _risk_tone
    app.state.templates.env.filters["event_tone"] = _event_tone
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(web_router)
    app.include_router(api_router)
    app.include_router(discord_router)

    @app.exception_handler(FastAPIHTTPException)
    async def http_error(request: Request, exc: FastAPIHTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return app.state.templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "status_code": exc.status_code,
                "detail": exc.detail if isinstance(exc.detail, str) else "Request failed.",
            },
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": str(exc)}, status_code=500)
        return app.state.templates.TemplateResponse(
            "error.html",
            {"request": request, "status_code": 500, "detail": "Internal server error."},
            status_code=500,
        )

    @app.get("/healthz", response_class=HTMLResponse, include_in_schema=False)
    async def healthz() -> str:
        return "ok"

    return app


def _status_tone(value: Optional[str]) -> str:
    tones = {
        "to_do": "warning",
        "in_progress": "info",
        "done": "success",
        "pending": "warning",
        "denied": "danger",
    }
    return tones.get(value or "", "default")


def _risk_tone(value: Optional[str]) -> str:
    tones = {"low": "success", "medium": "warning", "high": "danger"}
    return tones.get(value or "", "default")


def _event_tone(value: Optional[str]) -> str:
    tones = {
        "policy_evaluated": "warning",
        "draft_generated": "info",
        "summary_recorded": "success",
        "action_execution_requested": "warning",
        "action_execution_recorded": "success",
        "operation_rejected": "danger",
        "approval_denied": "danger",
        "approval_cancelled": "muted",
        "task_failed": "danger",
    }
    return tones.get(value or "", "default")


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "agentic_os.web:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
    )
