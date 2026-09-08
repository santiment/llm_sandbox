"""The provider seam. ``SandboxProvider`` is what the HTTP layer calls; ``SessionOpsMixin``
implements every in-session primitive once over a single "run this argv in the session"
hook, so file and command semantics cannot drift between backends."""

from __future__ import annotations

import asyncio
import base64
import json
import posixpath
import shlex
import time
import uuid
from typing import Optional, Protocol, runtime_checkable

from ..models import ExecResult, FileEntry

WORKDIR = "/workspace"
TIMEOUT_EXIT = 124  # GNU timeout's exit code too; 137 when the command ignored TERM
LIST_MAX_ENTRIES = 2000
_READ_CHUNK = 64 * 1024


class SandboxError(RuntimeError):
    """Backend refused or failed; the message names the fix. HTTP layer → 502."""


class SessionNotFound(LookupError):
    """No container/pod for this session id. HTTP layer → 404."""


class PathNotFound(FileNotFoundError):
    """No such path inside the session. HTTP layer → 404."""


def clamp_resources(memory_mb: int, cpus: float, *, max_memory_mb: int,
                    max_cpus: float) -> tuple[int, float]:
    """Bound caller-supplied session limits (public API fields, so attacker-controlled)."""
    memory_mb = max(64, min(int(memory_mb), max_memory_mb))
    cpus = max(0.1, min(float(cpus), max_cpus))
    return memory_mb, cpus


def cap_output(data: bytes | str, limit: int) -> tuple[str, bool]:
    """Cap ``data`` to ``limit``; bytes are decoded utf-8 with replacement."""
    truncated = len(data) > limit
    data = data[:limit]
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace"), truncated
    return data, truncated


async def run_cli(*argv: str, stdin: bytes | None = None, timeout: float | None = None,
                  max_bytes: int | None = None) -> tuple[int, bytes, bytes]:
    """Run a CLI command. Returns (exit_code, stdout, stderr); ``TIMEOUT_EXIT`` on timeout.
    Each stream is capped at ``max_bytes`` as it arrives, never buffered whole."""
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
            pass  # child exited early; its exit code says why
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


# python3 is in the sandbox image → reliable JSON listing, bounded to n entries.
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
    """In-session primitives over ``_exec_cli(session_id, *cmd, stdin, timeout) ->
    (exit_code, stdout, stderr)``. Requires ``self.max_output_bytes``."""

    max_output_bytes: int

    async def _exec_cli(self, session_id: str, *cmd: str, stdin: bytes | None = None,
                        timeout: float | None = None) -> tuple[int, bytes, bytes]:
        raise NotImplementedError

    async def exec(self, session_id, command, *, timeout_seconds=60, workdir=None) -> ExecResult:
        full = f"cd {shlex.quote(workdir or WORKDIR)} && {command}"
        seconds = max(1, int(timeout_seconds))
        start = time.monotonic()
        # Deadline enforced in-session by GNU timeout (dropping the exec stream kills nothing).
        # 124 = timed out, 137 = timed out and ignored TERM. Our own wait is the backstop.
        rc, out, err = await self._exec_cli(session_id, "timeout", "-k", "1", str(seconds),
                                            "sh", "-c", full, timeout=seconds + 2)
        return self._result(rc, out, err, start)

    async def run_script(self, session_id, code, *, interpreter, ext,
                         timeout_seconds=60) -> ExecResult:
        """One exec: the script arrives on stdin, is saved to a per-call /tmp file, run, removed."""
        seconds = max(1, int(timeout_seconds))
        data = code.encode("utf-8")
        path = shlex.quote(f"/tmp/_run_{uuid.uuid4().hex}.{ext}")
        # Size check first: a cut stream still gives `cat` a clean EOF.
        script = (f"cat > {path} && [ \"$(wc -c < {path})\" -eq {len(data)} ] && "
                  f"cd {shlex.quote(WORKDIR)} && "
                  f"timeout -k 1 {seconds} {shlex.quote(interpreter)} {path}; "
                  f"rc=$?; rm -f {path}; exit $rc")
        start = time.monotonic()
        # +5: the backstop must not fire while the payload is still streaming in.
        rc, out, err = await self._exec_cli(session_id, "sh", "-c", script, stdin=data,
                                            timeout=seconds + 5)
        return self._result(rc, out, err, start)

    def _result(self, rc: int, out: bytes, err: bytes, start: float) -> ExecResult:
        dur_ms = int((time.monotonic() - start) * 1000)
        stdout, t1 = cap_output(out, self.max_output_bytes)
        stderr, t2 = cap_output(err, self.max_output_bytes)
        return ExecResult(stdout=stdout, stderr=stderr, exit_code=rc,
                          truncated=t1 or t2, duration_ms=dur_ms)

    async def write_file(self, session_id, path, content, *, encoding="utf-8") -> None:
        parent = posixpath.dirname(path) or WORKDIR
        data = content.encode("utf-8")
        dest = shlex.quote(path)
        tmp = shlex.quote(f"{path}.partial-{uuid.uuid4().hex[:8]}")
        # Temp file + size check + atomic mv: a cut stream still gives `cat` a clean EOF.
        if encoding == "base64":
            finish = f"base64 -d {tmp} > {dest} || {{ rm -f {dest}; false; }}"
        else:
            finish = f"mv -f {tmp} {dest}"
        cmd = (f"mkdir -p {shlex.quote(parent)} && cat > {tmp} && "
               f"[ \"$(wc -c < {tmp})\" -eq {len(data)} ] && {finish}; "
               f"rc=$?; rm -f {tmp}; exit $rc")
        rc, _out, err = await self._exec_cli(session_id, "sh", "-c", cmd, stdin=data, timeout=60)
        if rc != 0:
            raise SandboxError(f"write_file failed: {err.decode(errors='replace').strip()}")

    async def read_file(self, session_id, path, *, max_bytes) -> tuple[str, str, bool]:
        limit = min(max_bytes, self.max_output_bytes)
        # limit+1 so truncation is detectable.
        rc, out, err = await self._exec_cli(
            session_id, "sh", "-c", f"head -c {limit + 1} {shlex.quote(path)}", timeout=30)
        if rc != 0:
            raise PathNotFound(err.decode(errors="replace").strip() or path)
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
            raise PathNotFound(err.decode(errors="replace").strip() or path)
        listing = json.loads(out.decode() or '{"truncated":false,"entries":[]}')
        return [FileEntry(**e) for e in listing["entries"]], bool(listing["truncated"])


@runtime_checkable
class SandboxProvider(Protocol):
    name: str

    async def startup(self) -> None:
        """Called once from the app lifespan, inside the event loop."""
        ...

    async def shutdown(self) -> None:
        """Called once on app shutdown."""
        ...

    async def preflight(self) -> None:
        """Raise if the backend is unusable; backs /readyz."""
        ...

    async def create(self, *, image: Optional[str], timeout_seconds: int, network: bool,
                     memory_mb: int, cpus: float) -> str:
        """Start a session; return its id."""
        ...

    async def destroy(self, session_id: str) -> None:
        """Tear the session down."""
        ...

    async def exec(self, session_id: str, command: str, *, timeout_seconds: int,
                   workdir: Optional[str] = None) -> ExecResult:
        """Run a shell command line in the session."""
        ...

    async def run_script(self, session_id: str, code: str, *, interpreter: str, ext: str,
                         timeout_seconds: int) -> ExecResult:
        """Save ``code`` to a scratch file in the session and run it with ``interpreter``."""
        ...

    async def live_session_ids(self) -> set[str] | None:
        """Session ids that exist on the backend, or ``None`` if unknown right now."""
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
