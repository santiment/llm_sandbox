"""The provider seam.

Implement this Protocol to back the HTTP API with gVisor or anything else. The HTTP
layer (``app.py``) composes ``run`` (python) on top of ``write_file`` + ``exec``, so a
provider only needs these six primitives. A *session* is a persistent workspace: files
written via ``write_file`` survive across ``exec`` calls until ``destroy``.

``SessionOpsMixin`` implements the four in-session primitives once, on top of a single
"run this argv inside the session" hook. Both providers supply that hook — the docker one
by forking ``docker exec``, the Kubernetes one by opening an apiserver exec stream — so
file and command semantics stay identical across backends by construction.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shlex
import time
from typing import Optional, Protocol, runtime_checkable

from ..models import ExecResult, FileEntry

WORKDIR = "/workspace"
TIMEOUT_EXIT = 124  # conventional "timed out" exit code


def clamp_resources(memory_mb: int, cpus: float, *, max_memory_mb: int,
                    max_cpus: float) -> tuple[int, float]:
    """Bound caller-supplied session limits. Without this a caller can ask for 64 CPUs and
    30 Gi per session and pin the whole sandbox node group — the request fields are part of
    the public API, so they are attacker-controlled input, not configuration."""
    memory_mb = max(64, min(int(memory_mb), max_memory_mb))
    cpus = max(0.1, min(float(cpus), max_cpus))
    return memory_mb, cpus


def cap_output(data: bytes | str, limit: int) -> tuple[str, bool]:
    """Cap ``data`` to ``limit`` and report whether it was truncated. Used by every provider
    to enforce ``max_output_bytes`` on stdout/stderr. ``bytes`` (raw process output) are capped
    by length and decoded utf-8 with replacement; ``str`` (already-decoded SDK output) are
    capped by character count."""
    truncated = len(data) > limit
    data = data[:limit]
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace"), truncated
    return data, truncated


async def run_cli(*argv: str, stdin: bytes | None = None,
                  timeout: float | None = None) -> tuple[int, bytes, bytes]:
    """Run a CLI command (docker/kubectl/…). Returns (exit_code, stdout, stderr); on
    timeout the process is killed and the exit code is ``TIMEOUT_EXIT``."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(input=stdin), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return TIMEOUT_EXIT, b"", b"sandbox: operation timed out"
    return proc.returncode if proc.returncode is not None else -1, out, err


# python3 is in the sandbox image → reliable JSON listing (beats parsing `ls`).
_LIST_FILES_SNIPPET = (
    "import os,sys,json;p=sys.argv[1];"
    "print(json.dumps([{'name':e.name,'path':os.path.join(p,e.name),"
    "'is_dir':e.is_dir(),'size':(e.stat().st_size if e.is_file() else 0)} "
    "for e in os.scandir(p)]))"
)


class SessionOpsMixin:
    """``exec``/``write_file``/``read_file``/``list_files`` implemented once against a
    single provider hook::

        _exec_cli(session_id, *cmd, stdin=None, timeout=None) -> (exit_code, stdout, stderr)

    which runs ``cmd`` inside the session's container/pod, feeding it ``stdin`` when that is
    not None. Requires ``self.max_output_bytes``."""

    max_output_bytes: int

    async def _exec_cli(self, session_id: str, *cmd: str, stdin: bytes | None = None,
                        timeout: float | None = None) -> tuple[int, bytes, bytes]:
        raise NotImplementedError

    async def exec(self, session_id, command, *, timeout_seconds=60, workdir=None) -> ExecResult:
        full = f"cd {shlex.quote(workdir or WORKDIR)} && {command}"
        start = time.monotonic()
        rc, out, err = await self._exec_cli(session_id, "sh", "-c", full,
                                            timeout=timeout_seconds + 2)
        dur_ms = int((time.monotonic() - start) * 1000)
        stdout, t1 = cap_output(out, self.max_output_bytes)
        stderr, t2 = cap_output(err, self.max_output_bytes)
        return ExecResult(stdout=stdout, stderr=stderr, exit_code=rc,
                          truncated=t1 or t2, duration_ms=dur_ms)

    async def write_file(self, session_id, path, content, *, encoding="utf-8") -> None:
        parent = path.rsplit("/", 1)[0] if "/" in path else WORKDIR
        # One round-trip: ensure the parent dir, then stream stdin into the file. `mkdir` does
        # not touch stdin, so the redirect below still consumes the piped content.
        sink = "base64 -d" if encoding == "base64" else "cat"
        cmd = f"mkdir -p {shlex.quote(parent)} && {sink} > {shlex.quote(path)}"
        rc, _out, err = await self._exec_cli(session_id, "sh", "-c", cmd,
                                             stdin=content.encode("utf-8"), timeout=60)
        if rc != 0:
            raise RuntimeError(f"write_file failed: {err.decode(errors='replace').strip()}")

    async def read_file(self, session_id, path, *, max_bytes) -> tuple[str, str, bool]:
        limit = min(max_bytes, self.max_output_bytes)
        # Fetch limit+1 bytes so truncation is detectable without shipping the whole file
        # across the exec stream.
        rc, out, err = await self._exec_cli(
            session_id, "sh", "-c", f"head -c {limit + 1} {shlex.quote(path)}", timeout=30)
        if rc != 0:
            raise FileNotFoundError(err.decode(errors="replace").strip() or path)
        truncated = len(out) > limit
        out = out[:limit]
        try:
            return out.decode("utf-8"), "utf-8", truncated
        except UnicodeDecodeError:
            return base64.b64encode(out).decode(), "base64", truncated

    async def list_files(self, session_id, path) -> list[FileEntry]:
        rc, out, err = await self._exec_cli(session_id, "python3", "-c",
                                            _LIST_FILES_SNIPPET, path, timeout=30)
        if rc != 0:
            raise FileNotFoundError(err.decode(errors="replace").strip() or path)
        return [FileEntry(**e) for e in json.loads(out.decode() or "[]")]


@runtime_checkable
class SandboxProvider(Protocol):
    name: str

    async def startup(self) -> None:
        """Called once from the app lifespan, inside the event loop. Open pools, start
        background tasks."""
        ...

    async def shutdown(self) -> None:
        """Called once on app shutdown."""
        ...

    async def preflight(self) -> None:
        """Raise if the backend is not usable (unreachable, missing rights). Backs
        ``/readyz`` so misconfiguration shows up as an unready pod instead of as the first
        caller's 500."""
        ...

    async def create(self, *, image: Optional[str], timeout_seconds: int, network: bool,
                     memory_mb: int, cpus: float) -> str:
        """Start a session; return its id."""
        ...

    async def destroy(self, session_id: str) -> None:
        """Tear the session down (ephemeral — never reuse across users/tasks)."""
        ...

    async def exec(self, session_id: str, command: str, *, timeout_seconds: int,
                   workdir: Optional[str] = None) -> ExecResult:
        """Run a shell command line (awk/sed/bash/...) in the session."""
        ...

    async def write_file(self, session_id: str, path: str, content: str, *,
                         encoding: str = "utf-8") -> None:
        ...

    async def read_file(self, session_id: str, path: str, *, max_bytes: int) -> tuple[str, str, bool]:
        """Return ``(content, encoding, truncated)``."""
        ...

    async def list_files(self, session_id: str, path: str) -> list[FileEntry]:
        ...
