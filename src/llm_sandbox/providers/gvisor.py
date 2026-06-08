"""gVisor provider — each session is a long-lived container under the ``runsc`` runtime,
orchestrated via the ``docker`` CLI (no SDK dependency). A session = one container, so files
written persist across ``exec`` calls (store → manipulate with shell → run), as promised.

Security posture (prod): ``--runtime runsc`` (gVisor user-space kernel), ``--network none``
(default-deny egress), memory/cpu/pids limits, ephemeral (removed on ``destroy``). Dev
fallback: set ``SANDBOX_DOCKER_RUNTIME=runc`` to run on a machine without gVisor — that is
NOT a security boundary, only for testing the plumbing.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import shlex
import time
import uuid

from ..models import ExecResult, FileEntry
from .base import cap_output

log = logging.getLogger("llm_sandbox.gvisor")

_NAME_PREFIX = "llmsbx_"
_WORKDIR = "/workspace"
_TIMEOUT_EXIT = 124  # conventional "timed out" exit code


class GvisorProvider:
    name = "gvisor"

    def __init__(self, *, default_image: str, docker_runtime: str, max_output_bytes: int) -> None:
        self.default_image = default_image
        self.docker_runtime = docker_runtime
        self.max_output_bytes = max_output_bytes

    def _container(self, session_id: str) -> str:
        return f"{_NAME_PREFIX}{session_id}"

    async def _docker(self, *args: str, stdin: bytes | None = None,
                      timeout: float | None = None) -> tuple[int, bytes, bytes]:
        """Run a `docker` subcommand. Returns (exit_code, stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(input=stdin), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return _TIMEOUT_EXIT, b"", b"sandbox: operation timed out"
        return proc.returncode if proc.returncode is not None else -1, out, err

    async def create(self, *, image=None, timeout_seconds=900, network=False,
                     memory_mb=512, cpus=1.0) -> str:
        session_id = uuid.uuid4().hex[:16]
        name = self._container(session_id)
        # `sleep <timeout>` is the auto-reaper: even if destroy is never called, the
        # container exits on its own after timeout_seconds.
        args = [
            "run", "-d", "--name", name,
            "--runtime", self.docker_runtime,
            "--network", "bridge" if network else "none",
            "--memory", f"{memory_mb}m", "--cpus", str(cpus),
            "--pids-limit", "256",
            "--workdir", _WORKDIR,
            image or self.default_image,
            "sleep", str(int(timeout_seconds)),
        ]
        rc, _out, err = await self._docker(*args, timeout=60)
        if rc != 0:
            msg = err.decode(errors="replace").strip()
            low = msg.lower()
            if "unknown or invalid runtime" in low:
                raise RuntimeError(
                    f"Docker runtime {self.docker_runtime!r} is not available. On a host without "
                    "gVisor (e.g. macOS Docker Desktop) set SANDBOX_DOCKER_RUNTIME=runc — the "
                    f"standard runtime, NO isolation, dev only. (docker: {msg})")
            if any(s in low for s in ("pull access denied", "no such image", "not found",
                                      "manifest unknown")):
                raise RuntimeError(
                    f"Sandbox image {(image or self.default_image)!r} not found — build it first: "
                    f"`docker build -f sandbox.Dockerfile -t {self.default_image} .` (docker: {msg})")
            raise RuntimeError(f"sandbox create failed: {msg}")
        # No mkdir needed: `docker run --workdir _WORKDIR` creates the dir, and the image ships it.
        log.info("session %s created (runtime=%s, network=%s)", session_id,
                 self.docker_runtime, network)
        return session_id

    async def destroy(self, session_id: str) -> None:
        await self._docker("rm", "-f", self._container(session_id), timeout=30)

    async def exec(self, session_id, command, *, timeout_seconds=60, workdir=None) -> ExecResult:
        name = self._container(session_id)
        wd = workdir or _WORKDIR
        full = f"cd {shlex.quote(wd)} && {command}"
        start = time.monotonic()
        rc, out, err = await self._docker("exec", name, "sh", "-c", full,
                                          timeout=timeout_seconds + 2)
        dur_ms = int((time.monotonic() - start) * 1000)
        stdout, t1 = cap_output(out, self.max_output_bytes)
        stderr, t2 = cap_output(err, self.max_output_bytes)
        return ExecResult(stdout=stdout, stderr=stderr, exit_code=rc,
                          truncated=t1 or t2, duration_ms=dur_ms)

    async def write_file(self, session_id, path, content, *, encoding="utf-8") -> None:
        name = self._container(session_id)
        parent = path.rsplit("/", 1)[0] if "/" in path else _WORKDIR
        # One round-trip: ensure the parent dir, then stream stdin into the file. `mkdir` does
        # not touch stdin, so the redirect below still consumes the piped content.
        sink = "base64 -d" if encoding == "base64" else "cat"
        data = content.encode("utf-8")
        cmd = f"mkdir -p {shlex.quote(parent)} && {sink} > {shlex.quote(path)}"
        rc, _out, err = await self._docker("exec", "-i", name, "sh", "-c", cmd,
                                           stdin=data, timeout=60)
        if rc != 0:
            raise RuntimeError(f"write_file failed: {err.decode(errors='replace').strip()}")

    async def read_file(self, session_id, path, *, max_bytes) -> tuple[str, str, bool]:
        name = self._container(session_id)
        rc, out, err = await self._docker("exec", name, "sh", "-c",
                                          f"cat {shlex.quote(path)}", timeout=30)
        if rc != 0:
            raise FileNotFoundError(err.decode(errors="replace").strip() or path)
        limit = min(max_bytes, self.max_output_bytes)
        truncated = len(out) > limit
        out = out[:limit]
        try:
            return out.decode("utf-8"), "utf-8", truncated
        except UnicodeDecodeError:
            return base64.b64encode(out).decode(), "base64", truncated

    async def list_files(self, session_id, path) -> list[FileEntry]:
        name = self._container(session_id)
        # python3 is in the sandbox image → reliable JSON listing (beats parsing `ls`).
        snippet = (
            "import os,sys,json;p=sys.argv[1];"
            "print(json.dumps([{'name':e.name,'path':os.path.join(p,e.name),"
            "'is_dir':e.is_dir(),'size':(e.stat().st_size if e.is_file() else 0)} "
            "for e in os.scandir(p)]))"
        )
        rc, out, err = await self._docker("exec", name, "python3", "-c", snippet, path, timeout=30)
        if rc != 0:
            raise FileNotFoundError(err.decode(errors="replace").strip() or path)
        return [FileEntry(**e) for e in json.loads(out.decode() or "[]")]
