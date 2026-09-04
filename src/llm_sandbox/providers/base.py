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
import uuid
from typing import Optional, Protocol, runtime_checkable

from ..models import ExecResult, FileEntry

WORKDIR = "/workspace"
TIMEOUT_EXIT = 124  # conventional "timed out" exit code (GNU timeout's, too)
KILLED_EXIT = 137   # 128+SIGKILL: GNU timeout's code when the command ignored TERM
LIST_MAX_ENTRIES = 2000  # list_files returns at most this many entries (+ truncated flag)
_READ_CHUNK = 64 * 1024


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


async def run_cli(*argv: str, stdin: bytes | None = None, timeout: float | None = None,
                  max_bytes: int | None = None) -> tuple[int, bytes, bytes]:
    """Run a CLI command (docker/…). Returns (exit_code, stdout, stderr); on timeout the
    process is killed and the exit code is ``TIMEOUT_EXIT``.

    stdout/stderr are read incrementally and each kept to ``max_bytes`` — the rest is drained
    and dropped. ``communicate()`` would buffer everything the child ever printed, so a
    ``yes`` running for its whole timeout would grow the service's heap by gigabytes before
    any cap could apply. Pass ``max_output_bytes + 1`` so truncation stays detectable.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = bytearray(), bytearray()

    async def pump(stream, buf: bytearray) -> None:
        while True:
            chunk = await stream.read(_READ_CHUNK)
            if not chunk:
                return
            if max_bytes is None:
                buf += chunk
            else:
                room = max_bytes - len(buf)
                if room > 0:
                    buf += chunk[:room]

    async def feed() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(stdin)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # child exited before reading everything; its exit code tells the story
        finally:
            proc.stdin.close()

    tasks = [pump(proc.stdout, out), pump(proc.stderr, err), proc.wait()]
    if stdin is not None:
        tasks.append(feed())
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return TIMEOUT_EXIT, b"", b"sandbox: operation timed out"
    return proc.returncode if proc.returncode is not None else -1, bytes(out), bytes(err)


# python3 is in the sandbox image → reliable JSON listing (beats parsing `ls`). Bounded:
# a directory with a million files must not become a multi-megabyte exec payload. A stat
# that fails (dangling symlink, vanished file) reports size 0 instead of failing the listing.
_LIST_FILES_SNIPPET = (
    "import os,sys,json,itertools;p=sys.argv[1];n=int(sys.argv[2]);"
    "es=list(itertools.islice(os.scandir(p),n+1))\n"
    "def sz(e):\n"
    "  try: return e.stat().st_size if e.is_file() else 0\n"
    "  except OSError: return 0\n"
    "print(json.dumps({'truncated':len(es)>n,'entries':[{'name':e.name,"
    "'path':os.path.join(p,e.name),'is_dir':e.is_dir(),'size':sz(e)} for e in es[:n]]}))"
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
        seconds = max(1, int(timeout_seconds))
        start = time.monotonic()
        # The deadline is enforced INSIDE the session by GNU `timeout`, which signals the whole
        # process group (TERM, then KILL a second later). Dropping the exec stream from our
        # side does not kill anything: a `docker exec` client that goes away leaves the
        # process running, and so does a kubelet exec without a TTY — the command would keep
        # burning its CPU share until the session itself is reaped. Exit 124 = timed out,
        # 137 = timed out and ignored TERM. Our own wait is only the backstop behind it.
        rc, out, err = await self._exec_cli(session_id, "timeout", "-k", "1", str(seconds),
                                            "sh", "-c", full, timeout=seconds + 2)
        return self._result(rc, out, err, start)

    async def run_script(self, session_id, code, *, interpreter, ext,
                         timeout_seconds=60) -> ExecResult:
        """Write ``code`` to a scratch file and run it — in ONE round-trip. A write followed
        by an exec costs two forks (docker) or two TLS+WebSocket handshakes (k8s) per call;
        here the script arrives on stdin and the same shell that saves it runs it.

        Per-call path, OUTSIDE /workspace: a constant name raced (two concurrent runs on one
        session overwrote each other between write and exec), and /tmp keeps scratch files
        out of the listing the model reads back. The file is removed afterwards whatever the
        exit code; cwd is still /workspace, so the script's relative paths resolve as before.
        """
        seconds = max(1, int(timeout_seconds))
        path = shlex.quote(f"/tmp/_run_{uuid.uuid4().hex}.{ext}")
        script = (f"cat > {path} && cd {shlex.quote(WORKDIR)} && "
                  f"timeout -k 1 {seconds} {shlex.quote(interpreter)} {path}; "
                  f"rc=$?; rm -f {path}; exit $rc")
        start = time.monotonic()
        # +5 not +2: the deadline only starts once `cat` has the whole script, and the
        # backstop must not fire while a large payload is still streaming in.
        rc, out, err = await self._exec_cli(session_id, "sh", "-c", script,
                                            stdin=code.encode("utf-8"), timeout=seconds + 5)
        return self._result(rc, out, err, start)

    def _result(self, rc: int, out: bytes, err: bytes, start: float) -> ExecResult:
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

    async def list_files(self, session_id, path) -> tuple[list[FileEntry], bool]:
        """Return ``(entries, truncated)`` — at most ``LIST_MAX_ENTRIES`` entries."""
        rc, out, err = await self._exec_cli(session_id, "python3", "-c", _LIST_FILES_SNIPPET,
                                            path, str(LIST_MAX_ENTRIES), timeout=30)
        if rc != 0:
            raise FileNotFoundError(err.decode(errors="replace").strip() or path)
        listing = json.loads(out.decode() or '{"truncated":false,"entries":[]}')
        return [FileEntry(**e) for e in listing["entries"]], bool(listing["truncated"])


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

    async def run_script(self, session_id: str, code: str, *, interpreter: str, ext: str,
                         timeout_seconds: int) -> ExecResult:
        """Save ``code`` to a scratch file in the session and run it with ``interpreter``."""
        ...

    async def live_session_ids(self) -> set[str] | None:
        """Ids of the sessions that currently exist on the backend, or ``None`` if that
        cannot be determined right now. Lets the HTTP layer drop slots for sessions that
        were destroyed via another replica (see ``SessionSlots``)."""
        ...

    async def write_file(self, session_id: str, path: str, content: str, *,
                         encoding: str = "utf-8") -> None:
        ...

    async def read_file(self, session_id: str, path: str, *, max_bytes: int) -> tuple[str, str, bool]:
        """Return ``(content, encoding, truncated)``."""
        ...

    async def list_files(self, session_id: str, path: str) -> tuple[list[FileEntry], bool]:
        """Return ``(entries, truncated)``."""
        ...
