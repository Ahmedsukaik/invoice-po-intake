"""The web demo: the review queue, each PDF as a sheet of paper, and the reviewer's decision.

    python -m intake.demo_web --port 8320        (then open http://127.0.0.1:8320)

The server keeps nothing between requests: the browser holds the signed list of what the
visitor did, and every request replays it into a fresh copy of the product (demo_session.py).
The AI reader runs when ANTHROPIC_API_KEY is set and INTAKE_DEMO_AI is not "off". Without a
key, or when its budget is used up, the demo runs on the rules reader alone, and the page says so.

This is separate from the product's own server (intake/server.py), which has sign-in, roles,
uploads and an export folder. The demo has no uploads: only the sample PDFs in samples/pdfs.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .demo_session import MODEL, NAMES, REVIEWERS, SAMPLES, TODAY, ActionError, SessionError, handle, live_reader_from_env, new_session

log = logging.getLogger("intake.demo_web")
PUBLIC = Path(__file__).resolve().parent.parent / "public"
REPO = "https://github.com/Ahmedsukaik/invoice-po-intake"
MAX_BODY = 300_000                 # a whole demo session, AI reading included, is far smaller
MAX_DEPTH = 14
PER_VISITOR_REQUESTS = (300, 600)  # requests of any kind per visitor (IP) per 10 minutes
PER_VISITOR_AI = (5, 600)          # AI reads per visitor per 10 minutes
PER_INSTANCE_AI = (200, 3600)      # AI reads per server instance per hour
MAX_ACTIVE = 2                     # requests in progress at once per visitor
MAX_KEYS = 5000                    # visitors the limiter remembers per instance


class Limiter:
    """Sliding-window counters, per server instance. The hard limit is the spend cap on the API
    key itself; this only stops one visitor from using up the demo's AI budget alone."""

    def __init__(self) -> None:
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._active: dict[str, int] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits.pop(key, None) or deque()
            self._hits[key] = hits  # most recently seen last
            while hits and now - hits[0] > window:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            while len(self._hits) > MAX_KEYS:  # forget the visitor seen longest ago
                self._hits.popitem(last=False)
            return True

    def enter(self, key: str) -> bool:
        """One more request in progress for this visitor, if under MAX_ACTIVE."""
        with self._lock:
            if self._active.get(key, 0) >= MAX_ACTIVE:
                return False
            self._active[key] = self._active.get(key, 0) + 1
            return True

    def leave(self, key: str) -> None:
        with self._lock:
            left = self._active.get(key, 1) - 1
            if left > 0:
                self._active[key] = left
            else:
                self._active.pop(key, None)


def _no_constants(name: str) -> Any:
    raise ValueError(f"{name} is not allowed")


def _depth(value: Any, level: int = 0) -> int:
    if level > MAX_DEPTH:
        return level
    if isinstance(value, dict):
        return max([_depth(v, level + 1) for v in value.values()] or [level + 1])
    if isinstance(value, list):
        return max([_depth(v, level + 1) for v in value] or [level + 1])
    return level


def create_app(reader: Any = "env", serve_static: bool = True) -> FastAPI:
    """reader: the AI reader (a ClaudeExtractor, or a stand-in in tests), None for the rules reader
    alone, or "env" to decide from the environment."""
    app = FastAPI(title="Invoice and PO Intake (demo)", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.reader = live_reader_from_env() if reader == "env" else reader
    app.state.limiter = Limiter()

    def visitor(request: Request) -> str:
        """The visitor's address. Behind Vercel, its own headers carry the real client; anywhere
        else, a client could write X-Forwarded-For itself, so the connection's address is used."""
        if os.environ.get("VERCEL"):
            forwarded = request.headers.get("x-vercel-forwarded-for") or request.headers.get("x-forwarded-for", "")
            if forwarded.strip():
                return forwarded.split(",")[0].strip()[:64]
        return request.client.host if request.client else "unknown"

    async def body(request: Request) -> dict[str, Any]:
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY:
            raise ActionError("This demo session is too large. Replay the demo to start again.")
        raw = bytearray()
        async for chunk in request.stream():  # stop reading as soon as the body is too big
            raw += chunk
            if len(raw) > MAX_BODY:
                raise ActionError("This demo session is too large. Replay the demo to start again.")
        try:
            data = json.loads(bytes(raw) or b"{}", parse_constant=_no_constants)  # NaN and Infinity are refused
        except (ValueError, RecursionError) as e:
            raise ActionError("Bad request.") from e
        if not isinstance(data, dict) or _depth(data) > MAX_DEPTH:
            raise ActionError("Bad request.")
        return data

    @app.exception_handler(SessionError)
    async def _session_error(request: Request, exc: SessionError) -> JSONResponse:
        return JSONResponse({"error": "session", "message": "This demo session can't be continued, so a new one was "
                                                             "started."}, status_code=409)

    @app.exception_handler(ActionError)
    async def _action_error(request: Request, exc: ActionError) -> JSONResponse:
        return JSONResponse({"error": "action", "message": str(exc)}, status_code=409)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unexpected error")
        return JSONResponse({"error": "server", "message": "Something went wrong on our side. Try again, or replay "
                                                            "the demo."}, status_code=500)

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        live = app.state.reader is not None
        return {
            "live": live, "model": MODEL if live else None, "today": TODAY.isoformat(), "repo": REPO,
            "samples": [{"key": s.key, "file": s.file, "title": s.title, "note": s.note, "layout": s.layout,
                         "pdf": f"/api/samples/{s.key}/pdf"} for s in SAMPLES.values()],
            "reviewers": [{"id": key, "name": NAMES[key]} for key in REVIEWERS],
        }

    @app.post("/api/new")
    def new() -> dict[str, Any]:
        return handle(new_session(), {"type": "view"})

    @app.post("/api/step")
    async def step(request: Request) -> dict[str, Any]:
        who = visitor(request)
        if not app.state.limiter.allow("r:" + who, *PER_VISITOR_REQUESTS):
            raise ActionError("Too many requests from this connection. Wait a few minutes and try again.")
        data = await body(request)
        action = data.get("action") if isinstance(data.get("action"), dict) else {"type": "view"}
        want_ai = data.get("ai", True) is not False

        def ai_gate() -> str | None:
            """Asked right before an AI read: may this visitor have one now?"""
            if not app.state.limiter.allow("ai:" + who, *PER_VISITOR_AI):
                return "visitor_limit"
            if not app.state.limiter.allow("instance-ai", *PER_INSTANCE_AI):
                return "busy"
            return None

        if not app.state.limiter.enter(who):
            raise ActionError("One moment: the previous step is still on its way.")
        try:
            # Replaying and an AI read block: run them in a worker thread so other visitors are not kept waiting.
            result: dict[str, Any] = await run_in_threadpool(handle, data.get("session"), action,
                                                             reader=app.state.reader, want_ai=want_ai, ai_gate=ai_gate)
        finally:
            app.state.limiter.leave(who)
        return result

    @app.get("/api/samples/{key}/pdf")
    def sample_pdf(key: str) -> Response:
        sample = SAMPLES.get(key)
        if sample is None:
            return JSONResponse({"error": "not_found", "message": "No such sample."}, status_code=404)
        return FileResponse(sample.path, media_type="application/pdf",
                            headers={"Content-Disposition": f'inline; filename="{sample.file}"',
                                     "Cache-Control": "public, max-age=3600"})

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if serve_static and PUBLIC.is_dir():
        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(PUBLIC / "index.html")

        app.mount("/", StaticFiles(directory=PUBLIC), name="static")
    return app


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description="Invoice and PO intake: the web demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8320)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app()
    reader = "the AI reader (Claude Haiku 5.5) for layouts the rules do not know" if app.state.reader else \
        "the rules reader only (no ANTHROPIC_API_KEY, or INTAKE_DEMO_AI=off)"
    print(f"Demo: http://{args.host}:{args.port}/   (reading with {reader})", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
