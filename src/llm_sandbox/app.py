"""HTTP interface — THE contract every caller (a backend, an agent, …) calls. It is identical
regardless of which provider backs it; swapping the provider is a server-side env change.

Run:  uv run uvicorn llm_sandbox.app:app --host 0.0.0.0 --port 8900   (see README)
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import Config
from .models import (CreateSessionRequest, ExecRequest, ExecResult, ListFilesResponse,
                     ReadFileResponse, RunRequest, Session, WriteFileRequest)
from .providers import build_provider
from .providers.base import clamp_resources

# Preview caps — how much of a command/script body lands in the log. Scripts often live inside
# an EXEC heredoc (`cat << EOF > file.py …`), so the cmd cap is generous: the log is the audit
# trail for "what actually ran".
_CMD_PREVIEW = 2000
_CODE_PREVIEW = 4000


def _setup_logging() -> logging.Logger:
    """Human-readable, timestamped logs for the whole ``llm_sandbox`` tree (app + providers).
    Own handler + no propagation so uvicorn's root config can't strip our timestamps."""
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
    """Provider pools and background tasks need a running loop, so they start here rather
    than at import. Preflight runs once and its result backs ``/readyz``."""
    await provider.startup()
    try:
        await provider.preflight()
        _app.state.ready, _app.state.not_ready_reason = True, ""
        log.info("provider %s ready", provider.name)
    except Exception as exc:
        # Do NOT crash: a CrashLoopBackOff hides the reason behind a restart counter,
        # whereas an unready pod keeps /readyz serving the actual error to whoever looks.
        _app.state.ready, _app.state.not_ready_reason = False, str(exc)
        log.error("provider %s NOT ready: %s", provider.name, exc)
    try:
        yield
    finally:
        await provider.shutdown()


# The OpenAPI/docs endpoints are unauthenticated; off unless explicitly asked for (dev).
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


def _metrics(text: str) -> str:
    """Compact content summary: bytes · lines · rough token estimate (~chars÷4)."""
    text = text or ""
    nbytes = len(text.encode("utf-8", errors="replace"))
    nlines = text.count("\n") + 1 if text else 0
    ntokens = (len(text) + 3) // 4
    return f"{_human_bytes(nbytes)} · {nlines}L · ~{ntokens}tok"


def _payload_metrics(content: str, encoding: str) -> str:
    """Size summary for a file payload. base64 → report decoded byte size (binary)."""
    if encoding == "base64":
        try:
            raw = base64.b64decode(content, validate=False)
        except Exception:
            return f"{_human_bytes(len(content.encode()))} · base64"
        return f"{_human_bytes(len(raw))} · binary/base64"
    return _metrics(content)


def _block(text: str, cap: int) -> str:
    """Render a (possibly multi-line) command/script under a ``  |`` gutter for readability.
    Returns a placeholder when payload logging is off — see ``SANDBOX_LOG_PAYLOADS``."""
    if not cfg.log_payloads:
        return "  | <payload logging disabled (SANDBOX_LOG_PAYLOADS)>"
    body = _preview(text, cap)
    return "\n".join("  | " + line for line in body.splitlines()) or "  | "


class SessionSlots:
    """Ceiling on LIVE sessions, enforced by this replica.

    Every session is a container/pod. Nothing else bounds their NUMBER: the per-session
    memory/cpu clamps bound each one's size, and the namespace ResourceQuota
    (``k8s/quota.yaml``) is a cluster-side backstop that exists only under k8s and surfaces
    as an opaque admission failure. This cap is provider-agnostic and answers a clean 429,
    so a caller looping ``POST /sessions`` gets told to back off instead of pinning the
    sandbox node group (or, under the docker provider, the host).

    Sessions are tracked as ``session_id → deadline``, not as a plain count: a session can
    also disappear on its own (it self-terminates after ``timeout_seconds`` with no DELETE),
    and a counter that only went up on create would drift until the service refused every
    request. A slot therefore frees on ``DELETE`` or once its deadline passes — the same
    deadline the provider reaps on, so the count follows reality without polling for it.

    PER-REPLICA: N replicas allow N × limit sessions. Size the ResourceQuota accordingly.
    ``limit <= 0`` disables the cap entirely.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._deadlines: dict[str, float] = {}  # session_id -> monotonic reap time
        self._pending = 0                       # creates in flight, no session id yet
        self._lock = asyncio.Lock()

    def _live(self) -> int:
        now = time.monotonic()
        for sid in [s for s, deadline in self._deadlines.items() if deadline <= now]:
            self._deadlines.pop(sid, None)
        return len(self._deadlines) + self._pending

    async def acquire(self) -> None:
        """Reserve a slot or raise 429. Held under a lock and counted BEFORE the (awaited)
        create, so concurrent requests can't all pass the check and overshoot together."""
        if self._limit <= 0:
            return
        async with self._lock:
            live = self._live()
            if live >= self._limit:
                log.warning("REJECT   create: %d/%d live sessions on this replica",
                            live, self._limit)
                raise HTTPException(
                    status_code=429, headers={"Retry-After": "5"},
                    detail=f"session limit reached ({live}/{self._limit} live on this "
                           "replica) — retry once a running session finishes")
            self._pending += 1

    def commit(self, sid: str, timeout_seconds: int) -> None:
        """Creation succeeded: turn the reservation into a real, expiring slot."""
        if self._limit <= 0:
            return
        self._pending = max(0, self._pending - 1)
        self._deadlines[sid] = time.monotonic() + max(0, timeout_seconds)

    def rollback(self) -> None:
        """Creation failed: give the reservation back, or the cap ratchets shut."""
        if self._limit <= 0:
            return
        self._pending = max(0, self._pending - 1)

    def release(self, sid: str) -> None:
        self._deadlines.pop(sid, None)


slots = SessionSlots(cfg.max_sessions)


class BodyLimitMiddleware:
    """Reject request bodies above ``limit`` bytes with 413.

    Neither uvicorn nor Starlette caps the body, and every JSON body here is read whole into
    memory before pydantic sees it — so without this a single oversized ``PUT .../files``
    (or a chunked upload with no Content-Length) is an OOM of the service. Declared size is
    checked up front; the actual bytes are counted as they stream in, so a lying or absent
    Content-Length cannot get past it either.

    The streaming check raises ``HTTPException`` from inside ``receive``: FastAPI's body
    reader re-raises exactly that type (anything else becomes a generic 400), and the
    ExceptionMiddleware below us turns it into the 413.
    """

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
        # We sit outside the ExceptionMiddleware, so an early rejection is written by hand.
        body = json.dumps({"detail": self._detail()}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


app.add_middleware(BodyLimitMiddleware, limit=cfg.max_request_bytes)


@app.exception_handler(RequestValidationError)
async def _validation_error(_request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's default 422 echoes ``input`` — the whole offending payload (megabytes of file
    content, say) and, for a NaN, a value json cannot serialise, which turns the 422 into a
    500. Report where and why, not what."""
    errors = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")}
              for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


async def _auth(authorization: str = Header(default="")) -> None:
    if not cfg.auth_token:
        return  # auth disabled (dev only)
    # Constant-time: a plain `!=` leaks the shared token a byte at a time under timing analysis.
    # Compared as bytes: the str form of compare_digest raises TypeError (→ 500) on any
    # non-ASCII header value, and uvicorn decodes header bytes as latin-1.
    presented = authorization.encode("latin-1", errors="replace")
    expected = f"Bearer {cfg.auth_token}".encode("utf-8")
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


# Both providers mint ids as uuid4().hex[:16]; anything else is not ours and must not reach a
# docker argv or an apiserver URL (a `?` or `#` in a path segment would rewrite the request).
SessionId = Annotated[str, Path(pattern=r"^[0-9a-f]{16}$")]


@app.get("/healthz")
async def healthz():
    """Liveness: is the process serving? Deliberately shallow — a backend outage must not
    restart-loop this pod."""
    return {"ok": True, "provider": provider.name}


@app.get("/readyz")
async def readyz():
    """Readiness: can we actually reach the backend with the rights we need?"""
    if not getattr(app.state, "ready", False):
        raise HTTPException(status_code=503,
                            detail=getattr(app.state, "not_ready_reason", "starting"))
    return {"ok": True, "provider": provider.name}


def _resolve_image(requested: str | None) -> str:
    """The `image` field is public API and lands in a pod spec / docker argv, so it is an
    allowlist, not a free string: any pullable ref would otherwise run under our registry
    credentials (image pull secrets, the daemon's ECR login) — i.e. a token holder could pull
    and read any private image, or pull anything at all onto the sandbox nodes."""
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
    # memory_mb/cpus are caller-supplied, so clamp before they reach the scheduler.
    memory_mb, cpus = clamp_resources(req.memory_mb, req.cpus,
                                      max_memory_mb=cfg.max_memory_mb, max_cpus=cfg.max_cpus)
    # Lifetime too: an unbounded timeout is a slot (and a container) held forever.
    timeout_seconds = min(req.timeout_seconds, cfg.max_session_seconds)
    # Count is capped too, not just per-session size (SANDBOX_MAX_SESSIONS).
    await slots.acquire()
    try:
        sid = await provider.create(image=image, timeout_seconds=timeout_seconds,
                                    network=req.network, memory_mb=memory_mb, cpus=cpus)
    except BaseException:  # includes CancelledError — a dropped client must free the slot
        slots.rollback()
        raise
    slots.commit(sid, timeout_seconds)
    log.info("CREATE   session=%s  provider=%s  image=%s  network=%s  mem=%sMi  cpus=%s",
             sid, provider.name, image, req.network, memory_mb, cpus)
    return Session(session_id=sid, provider=provider.name)


@app.delete("/sessions/{sid}", dependencies=[Depends(_auth)])
async def destroy_session(sid: SessionId):
    # Free the slot even if teardown errors: the provider's reaper is the backstop for the
    # pod, and holding the slot would only shrink the replica's capacity for good.
    slots.release(sid)
    await provider.destroy(sid)
    log.info("DESTROY  session=%s", sid)
    return {"ok": True}


@app.post("/sessions/{sid}/exec", response_model=ExecResult, dependencies=[Depends(_auth)])
async def exec_command(sid: SessionId, req: ExecRequest):
    """Run a shell command (awk/sed/bash/anything) — the universal file-manipulation primitive."""
    log.info("EXEC     session=%s  cmd (%s):\n%s", sid, _metrics(req.command),
             _block(req.command, _CMD_PREVIEW))
    # Bounded: an exec holds a backend slot (SANDBOX_MAX_CONCURRENCY) for its whole duration.
    timeout_seconds = min(req.timeout_seconds, cfg.max_exec_seconds)
    r = await provider.exec(sid, req.command, timeout_seconds=timeout_seconds,
                            workdir=req.workdir)
    log.info("EXEC     session=%s  exit=%s  dur=%sms  out=[%s]  truncated=%s", sid, r.exit_code,
             r.duration_ms, _metrics(r.stdout + r.stderr), r.truncated)
    return r


@app.post("/sessions/{sid}/run", response_model=ExecResult, dependencies=[Depends(_auth)])
async def run_code(sid: SessionId, req: RunRequest):
    """Run Python (the only language in ``_RUNNERS`` — the image is python-only by design):
    write the code to a file in the session, then execute it. Composed on write_file + exec
    so every provider supports it uniformly."""
    # The full executed program is logged (preview) — this is the audit trail for "what ran".
    log.info("RUN      session=%s  lang=%s  code (%s):\n%s", sid, req.language,
             _metrics(req.code), _block(req.code, _CODE_PREVIEW))
    ext, interp = _RUNNERS[req.language]
    # Per-call path, and OUTSIDE /workspace. A constant name raced: two concurrent /run calls
    # on one session (the agent fans out) would overwrite each other's file between write and
    # exec, so one call silently ran the other's code. /tmp also keeps these scratch files out
    # of the workspace listing the model reads back — cwd is still /workspace, so the script's
    # own relative paths resolve exactly as before.
    path = f"/tmp/_run_{req.language}_{uuid.uuid4().hex}.{ext}"
    timeout_seconds = min(req.timeout_seconds, cfg.max_exec_seconds)
    await provider.write_file(sid, path, req.code, encoding="utf-8")
    r = await provider.exec(sid, f"{interp} {path}", timeout_seconds=timeout_seconds)
    log.info("RUN      session=%s  lang=%s  exit=%s  dur=%sms  out=[%s]", sid, req.language,
             r.exit_code, r.duration_ms, _metrics(r.stdout + r.stderr))
    return r


@app.put("/sessions/{sid}/files", dependencies=[Depends(_auth)])
async def write_file(sid: SessionId, req: WriteFileRequest):
    await provider.write_file(sid, req.path, req.content, encoding=req.encoding)
    log.info("WRITE    session=%s  path=%s  [%s]  enc=%s", sid, req.path,
             _payload_metrics(req.content, req.encoding), req.encoding)
    return {"ok": True}


@app.get("/sessions/{sid}/files", response_model=ReadFileResponse, dependencies=[Depends(_auth)])
async def read_file(sid: SessionId, path: Annotated[str, Query(min_length=1)],
                    max_bytes: Annotated[int, Query(ge=1)] = 1_000_000):
    # ge=1 matters: a negative value reaches `head -c` as "all but the last N bytes", which
    # would return the whole file and defeat the output cap.
    content, encoding, truncated = await provider.read_file(sid, path, max_bytes=max_bytes)
    log.info("READ     session=%s  path=%s  [%s]  enc=%s  truncated=%s", sid, path,
             _payload_metrics(content, encoding), encoding, truncated)
    return ReadFileResponse(path=path, content=content, encoding=encoding, truncated=truncated)


@app.get("/sessions/{sid}/files/list", response_model=ListFilesResponse, dependencies=[Depends(_auth)])
async def list_files(sid: SessionId, path: Annotated[str, Query(min_length=1)] = "/workspace"):
    entries, truncated = await provider.list_files(sid, path)
    log.info("LIST     session=%s  path=%s  -> %d entries  truncated=%s", sid, path,
             len(entries), truncated)
    return ListFilesResponse(path=path, entries=entries, truncated=truncated)
