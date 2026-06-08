"""The provider seam.

Implement this Protocol to back the HTTP API with gVisor or anything else. The HTTP
layer (``app.py``) composes ``run`` (python/js) on top of ``write_file`` + ``exec``, so a
provider only needs these six primitives. A *session* is a persistent workspace: files
written via ``write_file`` survive across ``exec`` calls until ``destroy``.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from ..models import ExecResult, FileEntry


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


@runtime_checkable
class SandboxProvider(Protocol):
    name: str

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
