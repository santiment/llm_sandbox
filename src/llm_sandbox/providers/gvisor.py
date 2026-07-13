"""gVisor provider — each session is a long-lived container under the ``runsc`` runtime,
orchestrated via the ``docker`` CLI (no SDK dependency). A session = one container, so files
written persist across ``exec`` calls (store → manipulate with shell → run), as promised.

Security posture (prod): ``--runtime runsc`` (gVisor user-space kernel), ``--network none``
(default-deny egress), memory/cpu/pids limits, ephemeral (removed on ``destroy``). Dev
fallback: set ``SANDBOX_DOCKER_RUNTIME=runc`` to run on a machine without gVisor — that is
NOT a security boundary, only for testing the plumbing.
"""

from __future__ import annotations

import logging
import uuid

from .base import WORKDIR, CliSessionMixin, run_cli

log = logging.getLogger("llm_sandbox.gvisor")

_NAME_PREFIX = "llmsbx_"


class GvisorProvider(CliSessionMixin):
    name = "gvisor"

    def __init__(self, *, default_image: str, docker_runtime: str, max_output_bytes: int) -> None:
        self.default_image = default_image
        self.docker_runtime = docker_runtime
        self.max_output_bytes = max_output_bytes

    def _container(self, session_id: str) -> str:
        return f"{_NAME_PREFIX}{session_id}"

    async def _docker(self, *args: str, stdin: bytes | None = None,
                      timeout: float | None = None) -> tuple[int, bytes, bytes]:
        return await run_cli("docker", *args, stdin=stdin, timeout=timeout)

    async def _exec_cli(self, session_id, *cmd, stdin=None, timeout=None):
        interactive = ("-i",) if stdin is not None else ()
        return await self._docker("exec", *interactive, self._container(session_id), *cmd,
                                  stdin=stdin, timeout=timeout)

    async def create(self, *, image=None, timeout_seconds=900, network=False,
                     memory_mb=512, cpus=1.0) -> str:
        session_id = uuid.uuid4().hex[:16]
        name = self._container(session_id)
        # `sleep <timeout>` is the auto-reaper: even if destroy is never called, the
        # container exits on its own after timeout_seconds — and `--rm` makes the daemon
        # remove it then, so abandoned sessions don't pile up as exited containers.
        args = [
            "run", "-d", "--rm", "--name", name,
            "--runtime", self.docker_runtime,
            "--network", "bridge" if network else "none",
            "--memory", f"{memory_mb}m", "--cpus", str(cpus),
            "--pids-limit", "256",
            "--workdir", WORKDIR,
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
        # No mkdir needed: `docker run --workdir WORKDIR` creates the dir, and the image ships it.
        log.info("session %s created (runtime=%s, network=%s)", session_id,
                 self.docker_runtime, network)
        return session_id

    async def destroy(self, session_id: str) -> None:
        await self._docker("rm", "-f", self._container(session_id), timeout=30)
