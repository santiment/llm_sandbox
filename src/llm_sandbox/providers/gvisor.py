"""gVisor provider: one long-lived container per session via the ``docker`` CLI under the
``runsc`` runtime. ``--network none`` by default, memory/cpu/pids caps, no capabilities.
``SANDBOX_DOCKER_RUNTIME=runc`` runs the same plumbing with NO isolation (dev only)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from .base import WORKDIR, SandboxError, SessionNotFound, SessionOpsMixin, run_cli

log = logging.getLogger("llm_sandbox.gvisor")

_NAME_PREFIX = "llmsbx_"


class GvisorProvider(SessionOpsMixin):
    name = "gvisor"

    def __init__(self, *, default_image: str, docker_runtime: str, max_output_bytes: int,
                 max_concurrency: int = 16, docker_network: str = "bridge") -> None:
        self.default_image = default_image
        self.docker_runtime = docker_runtime
        # Joined by network=true sessions; the default bridge fences nothing (see README).
        self.docker_network = docker_network
        self.max_output_bytes = max_output_bytes
        # Create/destroy get their own lane so long execs cannot hold up a DELETE.
        self._sem = asyncio.Semaphore(max_concurrency)
        self._ctl_sem = asyncio.Semaphore(8)

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def preflight(self) -> None:
        rc, _out, err = await self._docker("version", "--format", "{{.Server.Version}}",
                                           timeout=10)
        if rc != 0:
            raise SandboxError(
                f"docker daemon unreachable: {err.decode(errors='replace').strip()}")
        # Surface a missing runtime at readiness instead of on the first create.
        rc, out, _err = await self._docker("info", "--format", "{{json .Runtimes}}", timeout=10)
        if rc != 0:
            return  # old daemon: leave it to create
        try:
            runtimes = json.loads(out.decode() or "{}")
        except ValueError:
            return
        if runtimes and self.docker_runtime not in runtimes:
            raise SandboxError(
                f"docker runtime {self.docker_runtime!r} is not registered with the daemon "
                f"(available: {', '.join(sorted(runtimes)) or 'none'}) — install gVisor and "
                "register runsc, or set SANDBOX_DOCKER_RUNTIME=runc to accept a box with NO "
                "isolation (dev only)")

    def _container(self, session_id: str) -> str:
        return f"{_NAME_PREFIX}{session_id}"

    async def _docker(self, *args: str, stdin: bytes | None = None,
                      timeout: float | None = None, max_bytes: int | None = None,
                      control: bool = False) -> tuple[int, bytes, bytes]:
        async with (self._ctl_sem if control else self._sem):
            return await run_cli("docker", *args, stdin=stdin, timeout=timeout,
                                 max_bytes=max_bytes)

    async def _exec_cli(self, session_id, *cmd, stdin=None, timeout=None):
        interactive = ("-i",) if stdin is not None else ()
        # +1 so SessionOpsMixin can still tell "exactly at the cap" from "over it".
        rc, out, err = await self._docker("exec", *interactive, self._container(session_id),
                                          *cmd, stdin=stdin, timeout=timeout,
                                          max_bytes=self.max_output_bytes + 1)
        # Missing container = rc 1 + daemon text; stderr is shared with the command, so confirm.
        if rc != 0 and err.startswith(b"Error response from daemon:") and (
                b"No such container" in err or b"is not running" in err):
            if not await self._is_running(session_id):
                raise SessionNotFound(session_id)
        return rc, out, err

    async def _is_running(self, session_id: str) -> bool:
        rc, out, _err = await self._docker("inspect", "-f", "{{.State.Running}}",
                                           self._container(session_id), timeout=15, control=True)
        return rc == 0 and out.strip() == b"true"

    async def create(self, *, image=None, timeout_seconds=900, network=False,
                     memory_mb=512, cpus=1.0) -> str:
        session_id = uuid.uuid4().hex[:16]
        name = self._container(session_id)
        # `sleep <timeout>` + --rm: the container removes itself when the session expires.
        args = [
            "run", "-d", "--rm", "--name", name,
            "--runtime", self.docker_runtime,
            "--network", self.docker_network if network else "none",
            # memory-swap == memory: otherwise the container gets as much swap again.
            "--memory", f"{memory_mb}m", "--memory-swap", f"{memory_mb}m",
            "--cpus", str(cpus),
            "--pids-limit", "256",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--workdir", WORKDIR,
            # `--`: the image is caller-influenced and must never parse as a flag.
            "--",
            image or self.default_image,
            "sleep", str(int(timeout_seconds)),
        ]
        rc, _out, err = await self._docker(*args, timeout=60, control=True)
        if rc != 0:
            msg = err.decode(errors="replace").strip()
            low = msg.lower()
            if "unknown or invalid runtime" in low:
                raise SandboxError(
                    f"Docker runtime {self.docker_runtime!r} is not available. On a host without "
                    "gVisor (e.g. macOS Docker Desktop) set SANDBOX_DOCKER_RUNTIME=runc — the "
                    f"standard runtime, NO isolation, dev only. (docker: {msg})")
            if any(s in low for s in ("pull access denied", "no such image", "not found",
                                      "manifest unknown")):
                raise SandboxError(
                    f"Sandbox image {(image or self.default_image)!r} not found — build it first: "
                    f"`docker build -f sandbox.Dockerfile -t {self.default_image} .` (docker: {msg})")
            raise SandboxError(f"sandbox create failed: {msg}")
        log.info("session %s created (runtime=%s, network=%s)", session_id,
                 self.docker_runtime, network)
        return session_id

    async def destroy(self, session_id: str) -> None:
        await self._docker("rm", "-f", self._container(session_id), timeout=30, control=True)

    async def live_session_ids(self) -> set[str] | None:
        rc, out, _err = await self._docker("ps", "--filter", f"name=^{_NAME_PREFIX}",
                                           "--format", "{{.Names}}", timeout=15, control=True)
        if rc != 0:
            return None
        return {line[len(_NAME_PREFIX):] for line in out.decode(errors="replace").split()
                if line.startswith(_NAME_PREFIX)}
