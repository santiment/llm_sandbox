"""HTTP interface — THE contract every caller (a backend, an agent, …) calls. It is identical
regardless of which provider backs it; swapping the provider is a server-side env change.

Run:  uv run uvicorn llm_sandbox.app:app --host 0.0.0.0 --port 8900   (see README)
"""

from __future__ import annotations

import base64
import logging
import sys

from fastapi import Depends, FastAPI, Header, HTTPException

from .config import Config
from .models import (CreateSessionRequest, ExecRequest, ExecResult, ListFilesResponse,
                     ReadFileResponse, RunRequest, Session, WriteFileRequest)
from .providers import build_provider

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
app = FastAPI(title="llm-sandbox", version="0.1.0")

# language → (file extension, interpreter binary in the sandbox image)
_RUNNERS = {"python": ("py", "python3"), "javascript": ("js", "node")}


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
    """Render a (possibly multi-line) command/script under a ``  |`` gutter for readability."""
    body = _preview(text, cap)
    return "\n".join("  | " + line for line in body.splitlines()) or "  | "


async def _auth(authorization: str = Header(default="")) -> None:
    if not cfg.auth_token:
        return  # auth disabled (dev only)
    if authorization != f"Bearer {cfg.auth_token}":
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


@app.get("/healthz")
async def healthz():
    return {"ok": True, "provider": provider.name}


@app.post("/sessions", response_model=Session, dependencies=[Depends(_auth)])
async def create_session(req: CreateSessionRequest):
    sid = await provider.create(image=req.image, timeout_seconds=req.timeout_seconds,
                                network=req.network, memory_mb=req.memory_mb, cpus=req.cpus)
    log.info("CREATE   session=%s  provider=%s  network=%s", sid, provider.name, req.network)
    return Session(session_id=sid, provider=provider.name)


@app.delete("/sessions/{sid}", dependencies=[Depends(_auth)])
async def destroy_session(sid: str):
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
    """Run Python or JavaScript: write the code to a file in the session, then execute it.
    Composed on write_file + exec so every provider supports it uniformly."""
    # The full executed program is logged (preview) — this is the audit trail for "what ran".
    log.info("RUN      session=%s  lang=%s  code (%s):\n%s", sid, req.language,
             _metrics(req.code), _block(req.code, _CODE_PREVIEW))
    ext, interp = _RUNNERS[req.language]
    path = f"/workspace/_run_{req.language}.{ext}"
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
