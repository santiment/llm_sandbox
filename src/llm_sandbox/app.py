"""HTTP interface. Run: uv run uvicorn llm_sandbox.app:app --host 0.0.0.0 --port 8900"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import Config
from .models import (CreateSessionRequest, ExecRequest, ExecResult, ListFilesResponse,
                     ReadFileResponse, RunRequest, Session, WriteFileRequest)
from .providers import build_provider
from .providers.base import PathNotFound, SandboxError, SessionNotFound, clamp_resources

# How much of a command/script body lands in the log.
_CMD_PREVIEW = 2000
_CODE_PREVIEW = 4000


def _setup_logging() -> logging.Logger:
    """Own handler, no propagation: uvicorn's log config must not strip our format."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d  %(levelname)-4s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root = logging.getLogger("llm_sandbox")
    root.setLevel(logging.INFO)
    root.handlers[:] = [handler]
    root.propagate = False
    return logging.getLogger("llm_sandbox.app")


log = _setup_logging()

cfg = Config.from_env()
provider = build_provider(cfg)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await provider.startup()
    try:
        await provider.preflight()
        _app.state.ready, _app.state.not_ready_reason = True, ""
        log.info("provider %s ready", provider.name)
    except Exception as exc:
        # Stay up but unready: /readyz then serves the reason instead of a restart loop.
        _app.state.ready, _app.state.not_ready_reason = False, str(exc)
        log.error("provider %s NOT ready: %s", provider.name, exc)
    try:
        yield
    finally:
        await provider.shutdown()


# /docs is unauthenticated; off unless asked for.
_docs = {} if cfg.expose_docs else {"docs_url": None, "redoc_url": None, "openapi_url": None}
app = FastAPI(title="llm-sandbox", version="0.1.0", lifespan=lifespan, **_docs)

# language → (file extension, interpreter binary in the sandbox image)
_RUNNERS = {"python": ("py", "python3")}

def _preview(s: str, n: int = 800) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + f"…(+{len(s) - n} chars)"


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1_048_576:
        return f"{n / 1024:.1f}KB"
    return f"{n / 1_048_576:.1f}MB"


def _metrics(*texts: str) -> str:
    """Compact content summary: bytes · lines · rough token estimate (~chars÷4)."""
    nbytes = nlines = nchars = 0
    for text in texts:
        if not text:
            continue
        nbytes += len(text) if text.isascii() else len(text.encode("utf-8", errors="replace"))
        nlines += text.count("\n") + 1
        nchars += len(text)
    return f"{_human_bytes(nbytes)} · {nlines}L · ~{(nchars + 3) // 4}tok"


def _payload_metrics(content: str, encoding: str) -> str:
    """Size summary for a file payload; base64 → decoded size, computed, not decoded."""
    if encoding == "base64":
        stripped = content.rstrip("=\n\r ")
        raw = len(stripped) * 3 // 4
        return f"{_human_bytes(raw)} · binary/base64"
    return _metrics(content)


def _block(text: str, cap: int) -> str:
    """Indent a command/script under a ``  |`` gutter; placeholder when payload logging is off."""
    if not cfg.log_payloads:
        return "  | <payload logging disabled (SANDBOX_LOG_PAYLOADS)>"
    body = _preview(text, cap)
    return "\n".join("  | " + line for line in body.splitlines()) or "  | "


class SessionSlots:
    """Per-replica cap on live sessions (429 when full). Tracked as id → deadline, not a
    counter, so a session that self-terminates frees its slot without a DELETE.
    N replicas allow N × limit; ``limit <= 0`` disables the cap."""

    def __init__(self, limit: int, recount=None) -> None:
        self._limit = limit
        self._deadlines: dict[str, float] = {}  # session_id -> monotonic reap time
        self._pending = 0                       # creates in flight, no session id yet
        self._lock = asyncio.Lock()
        self._recount = recount                 # async () -> set[str] | None (provider hook)

    def _live(self) -> int:
        now = time.monotonic()
        for sid in [s for s, deadline in self._deadlines.items() if deadline <= now]:
            self._deadlines.pop(sid, None)
        return len(self._deadlines) + self._pending

    async def acquire(self) -> None:
        """Reserve a slot or raise 429; counted under the lock, before the awaited create."""
        if self._limit <= 0:
            return
        async with self._lock:
            live = self._live()
            if live >= self._limit and self._recount is not None:
                live = await self._resync()
            if live >= self._limit:
                log.warning("REJECT   create: %d/%d live sessions on this replica",
                            live, self._limit)
                raise HTTPException(
                    status_code=429, headers={"Retry-After": "5"},
                    detail=f"session limit reached ({live}/{self._limit} live on this "
                           "replica) — retry once a running session finishes")
            self._pending += 1

    async def _resync(self) -> int:
        """At the cap only: drop slots for sessions the backend no longer has."""
        try:
            actual = await asyncio.wait_for(self._recount(), timeout=10)
        except Exception as exc:
            log.warning("slot resync failed, keeping local view: %r", exc)
            return self._live()
        if actual is None:
            return self._live()
        stale = [sid for sid in self._deadlines if sid not in actual]
        for sid in stale:
            self._deadlines.pop(sid, None)
        if stale:
            log.info("RESYNC   dropped %d slot(s) for sessions destroyed via another replica",
                     len(stale))
        return self._live()

    def commit(self, sid: str, timeout_seconds: int) -> None:
        if self._limit <= 0:
            return
        self._pending = max(0, self._pending - 1)
        self._deadlines[sid] = time.monotonic() + max(0, timeout_seconds)

    def rollback(self) -> None:
        if self._limit <= 0:
            return
        self._pending = max(0, self._pending - 1)

    def release(self, sid: str) -> None:
        self._deadlines.pop(sid, None)


slots = SessionSlots(cfg.max_sessions, recount=provider.live_session_ids)


class BodyLimitMiddleware:
    """413 for bodies above ``limit``: declared Content-Length up front, actual bytes as they
    stream. Raises HTTPException from ``receive`` — the one type FastAPI re-raises as-is."""

    def __init__(self, app, limit: int) -> None:
        self.app, self.limit = app, limit

    def _detail(self) -> str:
        return f"request body exceeds {self.limit} bytes (SANDBOX_MAX_REQUEST_BYTES)"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > self.limit:
                    return await self._reject(send)
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise HTTPException(status_code=413, detail=self._detail())
            return message

        await self.app(scope, limited_receive, send)

    async def _reject(self, send) -> None:
        body = json.dumps({"detail": self._detail()}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


app.add_middleware(BodyLimitMiddleware, limit=cfg.max_request_bytes)


@app.exception_handler(SessionNotFound)
async def _session_not_found(_request, exc: SessionNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": f"no such session: {exc}"})


@app.exception_handler(PathNotFound)
async def _path_not_found(_request, exc: PathNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(SandboxError)
async def _backend_failure(_request, exc: SandboxError) -> JSONResponse:
    """Provider failures → 502 carrying the provider's hint. Anything else stays a 500."""
    log.error("BACKEND  %s", exc)
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.exception_handler(RequestValidationError)
async def _validation_error(_request, exc: RequestValidationError) -> JSONResponse:
    """422 without echoing ``input``: it can be megabytes, or a NaN json cannot serialise."""
    errors = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")}
              for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


async def _auth(authorization: str = Header(default="")) -> None:
    if not cfg.auth_token:
        return  # auth disabled (dev only)
    # Constant-time, and as bytes: the str form raises TypeError on non-ASCII header values.
    presented = authorization.encode("latin-1", errors="replace")
    expected = f"Bearer {cfg.auth_token}".encode("utf-8")
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


# Providers mint ids as uuid4().hex[:16]; anything else must not reach an argv or URL.
SessionId = Annotated[str, Path(pattern=r"^[0-9a-f]{16}$")]


@app.get("/healthz")
async def healthz():
    """Liveness, shallow on purpose: a backend outage must not restart-loop the pod."""
    return {"ok": True, "provider": provider.name}


@app.get("/readyz")
async def readyz():
    """Readiness: can we actually reach the backend with the rights we need?"""
    if not getattr(app.state, "ready", False):
        raise HTTPException(status_code=503,
                            detail=getattr(app.state, "not_ready_reason", "starting"))
    return {"ok": True, "provider": provider.name}


def _resolve_image(requested: str | None) -> str:
    """Allowlist, not a free string: any pullable ref would run under our registry credentials."""
    if requested is None or requested == cfg.default_image:
        return cfg.default_image
    if requested in cfg.allowed_images:
        return requested
    raise HTTPException(
        status_code=400,
        detail=f"image {requested!r} is not allowed — the default image is {cfg.default_image!r}; "
               "other images must be listed in SANDBOX_ALLOWED_IMAGES")


@app.post("/sessions", response_model=Session, dependencies=[Depends(_auth)])
async def create_session(req: CreateSessionRequest):
    image = _resolve_image(req.image)
    memory_mb, cpus = clamp_resources(req.memory_mb, req.cpus,
                                      max_memory_mb=cfg.max_memory_mb, max_cpus=cfg.max_cpus)
    timeout_seconds = min(req.timeout_seconds, cfg.max_session_seconds)
    await slots.acquire()
    try:
        sid = await provider.create(image=image, timeout_seconds=timeout_seconds,
                                    network=req.network, memory_mb=memory_mb, cpus=cpus)
    except BaseException:  # incl. CancelledError: a dropped client must free the slot
        slots.rollback()
        raise
    slots.commit(sid, timeout_seconds)
    log.info("CREATE   session=%s  provider=%s  image=%s  network=%s  mem=%sMi  cpus=%s",
             sid, provider.name, image, req.network, memory_mb, cpus)
    return Session(session_id=sid, provider=provider.name)


@app.delete("/sessions/{sid}", dependencies=[Depends(_auth)])
async def destroy_session(sid: SessionId):
    # Free the slot even if teardown fails; the provider's reaper is the backstop.
    slots.release(sid)
    await provider.destroy(sid)
    log.info("DESTROY  session=%s", sid)
    return {"ok": True}


@app.post("/sessions/{sid}/exec", response_model=ExecResult, dependencies=[Depends(_auth)])
async def exec_command(sid: SessionId, req: ExecRequest):
    log.info("EXEC     session=%s  workdir=%r  cmd (%s):\n%s", sid, req.workdir,
             _metrics(req.command), _block(req.command, _CMD_PREVIEW))
    timeout_seconds = min(req.timeout_seconds, cfg.max_exec_seconds)
    r = await provider.exec(sid, req.command, timeout_seconds=timeout_seconds,
                            workdir=req.workdir)
    log.info("EXEC     session=%s  exit=%s  dur=%sms  out=[%s]  truncated=%s", sid, r.exit_code,
             r.duration_ms, _metrics(r.stdout, r.stderr), r.truncated)
    return r


@app.post("/sessions/{sid}/run", response_model=ExecResult, dependencies=[Depends(_auth)])
async def run_code(sid: SessionId, req: RunRequest):
    log.info("RUN      session=%s  lang=%s  code (%s):\n%s", sid, req.language,
             _metrics(req.code), _block(req.code, _CODE_PREVIEW))
    ext, interp = _RUNNERS[req.language]
    timeout_seconds = min(req.timeout_seconds, cfg.max_exec_seconds)
    r = await provider.run_script(sid, req.code, interpreter=interp, ext=ext,
                                  timeout_seconds=timeout_seconds)
    log.info("RUN      session=%s  lang=%s  exit=%s  dur=%sms  out=[%s]", sid, req.language,
             r.exit_code, r.duration_ms, _metrics(r.stdout, r.stderr))
    return r


@app.put("/sessions/{sid}/files", dependencies=[Depends(_auth)])
async def write_file(sid: SessionId, req: WriteFileRequest):
    await provider.write_file(sid, req.path, req.content, encoding=req.encoding)
    log.info("WRITE    session=%s  path=%r  [%s]  enc=%s", sid, req.path,
             _payload_metrics(req.content, req.encoding), req.encoding)
    return {"ok": True}


@app.get("/sessions/{sid}/files", response_model=ReadFileResponse, dependencies=[Depends(_auth)])
async def read_file(sid: SessionId, path: Annotated[str, Query(min_length=1)],
                    max_bytes: Annotated[int, Query(ge=1)] = 1_000_000):
    # ge=1: a negative value reaches `head -c` as "all but the last N bytes".
    content, encoding, truncated = await provider.read_file(sid, path, max_bytes=max_bytes)
    log.info("READ     session=%s  path=%r  [%s]  enc=%s  truncated=%s", sid, path,
             _payload_metrics(content, encoding), encoding, truncated)
    return ReadFileResponse(path=path, content=content, encoding=encoding, truncated=truncated)


@app.get("/sessions/{sid}/files/list", response_model=ListFilesResponse, dependencies=[Depends(_auth)])
async def list_files(sid: SessionId, path: Annotated[str, Query(min_length=1)] = "/workspace"):
    entries, truncated = await provider.list_files(sid, path)
    log.info("LIST     session=%s  path=%r  -> %d entries  truncated=%s", sid, path,
             len(entries), truncated)
    return ListFilesResponse(path=path, entries=entries, truncated=truncated)
