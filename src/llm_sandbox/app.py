"""HTTP interface — THE contract every caller (a backend, an agent, …) calls. It is identical
regardless of which provider backs it; swapping the provider is a server-side env change.

Run:  uv run uvicorn llm_sandbox.app:app --host 0.0.0.0 --port 8900   (see README)
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException

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


app = FastAPI(title="llm-sandbox", version="0.1.0", lifespan=lifespan)

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


async def _auth(authorization: str = Header(default="")) -> None:
    if not cfg.auth_token:
        return  # auth disabled (dev only)
    # Constant-time: a plain `!=` leaks the shared token a byte at a time under timing analysis.
    if not hmac.compare_digest(authorization, f"Bearer {cfg.auth_token}"):
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


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


@app.post("/sessions", response_model=Session, dependencies=[Depends(_auth)])
async def create_session(req: CreateSessionRequest):
    # memory_mb/cpus are caller-supplied, so clamp before they reach the scheduler.
    memory_mb, cpus = clamp_resources(req.memory_mb, req.cpus,
                                      max_memory_mb=cfg.max_memory_mb, max_cpus=cfg.max_cpus)
    # Count is capped too, not just per-session size (SANDBOX_MAX_SESSIONS).
    await slots.acquire()
    try:
        sid = await provider.create(image=req.image, timeout_seconds=req.timeout_seconds,
                                    network=req.network, memory_mb=memory_mb, cpus=cpus)
    except BaseException:  # includes CancelledError — a dropped client must free the slot
        slots.rollback()
        raise
    slots.commit(sid, req.timeout_seconds)
    log.info("CREATE   session=%s  provider=%s  network=%s  mem=%sMi  cpus=%s",
             sid, provider.name, req.network, memory_mb, cpus)
    return Session(session_id=sid, provider=provider.name)


@app.delete("/sessions/{sid}", dependencies=[Depends(_auth)])
async def destroy_session(sid: str):
    # Free the slot even if teardown errors: the provider's reaper is the backstop for the
    # pod, and holding the slot would only shrink the replica's capacity for good.
    slots.release(sid)
    await provider.destroy(sid)
    log.info("DESTROY  session=%s", sid)
    return {"ok": True}


@app.post("/sessions/{sid}/exec", response_model=ExecResult, dependencies=[Depends(_auth)])
async def exec_command(sid: str, req: ExecRequest):
    """Run a shell command (awk/sed/bash/anything) — the universal file-manipulation primitive."""
    log.info("EXEC     session=%s  cmd (%s):\n%s", sid, _metrics(req.command),
             _block(req.command, _CMD_PREVIEW))
    r = await provider.exec(sid, req.command, timeout_seconds=req.timeout_seconds,
                            workdir=req.workdir)
    log.info("EXEC     session=%s  exit=%s  dur=%sms  out=[%s]  truncated=%s", sid, r.exit_code,
             r.duration_ms, _metrics(r.stdout + r.stderr), r.truncated)
    return r


@app.post("/sessions/{sid}/run", response_model=ExecResult, dependencies=[Depends(_auth)])
async def run_code(sid: str, req: RunRequest):
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
    await provider.write_file(sid, path, req.code, encoding="utf-8")
    r = await provider.exec(sid, f"{interp} {path}", timeout_seconds=req.timeout_seconds)
    log.info("RUN      session=%s  lang=%s  exit=%s  dur=%sms  out=[%s]", sid, req.language,
             r.exit_code, r.duration_ms, _metrics(r.stdout + r.stderr))
    return r


@app.put("/sessions/{sid}/files", dependencies=[Depends(_auth)])
async def write_file(sid: str, req: WriteFileRequest):
    await provider.write_file(sid, req.path, req.content, encoding=req.encoding)
    log.info("WRITE    session=%s  path=%s  [%s]  enc=%s", sid, req.path,
             _payload_metrics(req.content, req.encoding), req.encoding)
    return {"ok": True}


@app.get("/sessions/{sid}/files", response_model=ReadFileResponse, dependencies=[Depends(_auth)])
async def read_file(sid: str, path: str, max_bytes: int = 1_000_000):
    content, encoding, truncated = await provider.read_file(sid, path, max_bytes=max_bytes)
    log.info("READ     session=%s  path=%s  [%s]  enc=%s  truncated=%s", sid, path,
             _payload_metrics(content, encoding), encoding, truncated)
    return ReadFileResponse(path=path, content=content, encoding=encoding, truncated=truncated)


@app.get("/sessions/{sid}/files/list", response_model=ListFilesResponse, dependencies=[Depends(_auth)])
async def list_files(sid: str, path: str = "/workspace"):
    entries = await provider.list_files(sid, path)
    log.info("LIST     session=%s  path=%s  -> %d entries", sid, path, len(entries))
    return ListFilesResponse(path=path, entries=entries)
