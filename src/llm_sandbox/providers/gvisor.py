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
import json
import logging
import uuid

from .base import WORKDIR, SessionNotFound, SessionOpsMixin, run_cli

log = logging.getLogger("llm_sandbox.gvisor")

_NAME_PREFIX = "llmsbx_"


class GvisorProvider(SessionOpsMixin):
    name = "gvisor"

    def __init__(self, *, default_image: str, docker_runtime: str, max_output_bytes: int,
                 max_concurrency: int = 16, docker_network: str = "bridge") -> None:
        self.default_image = default_image
        self.docker_runtime = docker_runtime
        # The network a network=true session joins. The daemon's default `bridge` lets sessions
        # reach each other, the host's LAN and (on EC2) the metadata service — see README for
        # the hardened network this should point at in anything but local dev.
        self.docker_network = docker_network
        self.max_output_bytes = max_output_bytes
        # This provider really does fork a `docker` binary per call, so the cap here is a
        # memory guard, not just a politeness limit. Session control (create/destroy) has
        # its own lane: a burst of long-running execs must not make DELETE wait behind them.
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
            raise RuntimeError(
                f"docker daemon unreachable: {err.decode(errors='replace').strip()}")
        # A runtime the daemon does not have only fails on the first `create`. Surface it at
        # readiness instead, so a misconfigured box reports "no gVisor here" up front rather
        # than handing the first caller a 500 — and so nobody assumes they are isolated.
        rc, out, _err = await self._docker("info", "--format", "{{json .Runtimes}}", timeout=10)
        if rc != 0:
            return  # can't tell (old daemon): leave it to create's error path
        try:
            runtimes = json.loads(out.decode() or "{}")
        except ValueError:
            return
        if runtimes and self.docker_runtime not in runtimes:
            raise RuntimeError(
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
        # Otherwise the daemon's complaint would come back as a "successful" ExecResult with
        # exit_code 1 and the error text in stderr — a 200 for a session that does not exist.
        if rc != 0 and (b"No such container" in err or b"is not running" in err):
            raise SessionNotFound(session_id)
        return rc, out, err

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
            "--network", self.docker_network if network else "none",
            # --memory alone still allows as much swap again on a host that has swap; pinning
            # memory-swap to the same value makes the cap a cap.
            "--memory", f"{memory_mb}m", "--memory-swap", f"{memory_mb}m",
            "--cpus", str(cpus),
            "--pids-limit", "256",
            # root inside, but a root with no capabilities and no way to gain any: agent code
            # writes files, it does not chown/mount/raw-socket. Cheap under runc, and gVisor
            # honours both in its own kernel.
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--workdir", WORKDIR,
            # `--` ends flag parsing: `image` is caller-influenced (allowlisted upstream, but
            # belt and braces) and a value like `--privileged` must never be read as a flag.
            "--",
            image or self.default_image,
            "sleep", str(int(timeout_seconds)),
        ]
        rc, _out, err = await self._docker(*args, timeout=60, control=True)
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
        await self._docker("rm", "-f", self._container(session_id), timeout=30, control=True)

    async def live_session_ids(self) -> set[str] | None:
        rc, out, _err = await self._docker("ps", "--filter", f"name=^{_NAME_PREFIX}",
                                           "--format", "{{.Names}}", timeout=15, control=True)
        if rc != 0:
            return None
        return {line[len(_NAME_PREFIX):] for line in out.decode(errors="replace").split()
                if line.startswith(_NAME_PREFIX)}
