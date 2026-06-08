"""HTTP interface — THE contract every caller (a backend, an agent, …) calls. It is identical
regardless of which provider backs it; swapping the provider is a server-side env change.

Run:  uv run uvicorn llm_sandbox.app:app --host 0.0.0.0 --port 8900   (see README)
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, Header, HTTPException

from .config import Config
from .models import (CreateSessionRequest, ExecRequest, ExecResult, ListFilesResponse,
                     ReadFileResponse, RunRequest, Session, WriteFileRequest)
from .providers import build_provider

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("llm_sandbox.app")

cfg = Config.from_env()
provider = build_provider(cfg)
app = FastAPI(title="llm-sandbox", version="0.1.0")

# language → (file extension, interpreter binary in the sandbox image)
_RUNNERS = {"python": ("py", "python3"), "javascript": ("js", "node")}


def _preview(s: str, n: int = 800) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + f"…(+{len(s) - n} chars)"


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
    log.info("SESSION CREATE session=%s provider=%s network=%s", sid, provider.name, req.network)
    return Session(session_id=sid, provider=provider.name)


@app.delete("/sessions/{sid}", dependencies=[Depends(_auth)])
async def destroy_session(sid: str):
    await provider.destroy(sid)
    log.info("SESSION DESTROY session=%s", sid)
    return {"ok": True}


@app.post("/sessions/{sid}/exec", response_model=ExecResult, dependencies=[Depends(_auth)])
async def exec_command(sid: str, req: ExecRequest):
    """Run a shell command (awk/sed/bash/anything) — the universal file-manipulation primitive."""
    log.info("EXEC session=%s cmd=%s", sid, _preview(req.command, 300))
    r = await provider.exec(sid, req.command, timeout_seconds=req.timeout_seconds,
                            workdir=req.workdir)
    log.info("EXEC session=%s exit=%s dur=%sms truncated=%s", sid, r.exit_code, r.duration_ms,
             r.truncated)
    return r


@app.post("/sessions/{sid}/run", response_model=ExecResult, dependencies=[Depends(_auth)])
async def run_code(sid: str, req: RunRequest):
    """Run Python or JavaScript: write the code to a file in the session, then execute it.
    Composed on write_file + exec so every provider supports it uniformly."""
    # The full executed program is logged (preview) — this is the audit trail for "what ran".
    log.info("RUN session=%s lang=%s code=\n%s", sid, req.language, _preview(req.code))
    ext, interp = _RUNNERS[req.language]
    path = f"/workspace/_run_{req.language}.{ext}"
    await provider.write_file(sid, path, req.code, encoding="utf-8")
    r = await provider.exec(sid, f"{interp} {path}", timeout_seconds=req.timeout_seconds)
    log.info("RUN session=%s lang=%s exit=%s dur=%sms", sid, req.language, r.exit_code, r.duration_ms)
    return r


@app.put("/sessions/{sid}/files", dependencies=[Depends(_auth)])
async def write_file(sid: str, req: WriteFileRequest):
    await provider.write_file(sid, req.path, req.content, encoding=req.encoding)
    return {"ok": True}


@app.get("/sessions/{sid}/files", response_model=ReadFileResponse, dependencies=[Depends(_auth)])
async def read_file(sid: str, path: str, max_bytes: int = 1_000_000):
    content, encoding, truncated = await provider.read_file(sid, path, max_bytes=max_bytes)
    return ReadFileResponse(path=path, content=content, encoding=encoding, truncated=truncated)


@app.get("/sessions/{sid}/files/list", response_model=ListFilesResponse, dependencies=[Depends(_auth)])
async def list_files(sid: str, path: str = "/workspace"):
    return ListFilesResponse(path=path, entries=await provider.list_files(sid, path))
