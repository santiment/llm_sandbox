"""Wire contract for the sandbox HTTP API — the SAME request/response shapes for every
caller, regardless of which provider (gVisor, …) runs underneath.
"""

from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

Encoding = Literal["utf-8", "base64"]

# Every field below is caller-supplied and therefore attacker-controlled input. Bounds here
# are the hard floor; the config-driven ceilings (SANDBOX_MAX_*_SECONDS, ...) are applied by
# clamping in app.py, so an out-of-range ask degrades to the cap instead of a 422.

# base64 alphabet + padding + whitespace; anything else would only fail inside the sandbox
# (`base64 -d`) and surface as a 500 — reject it at the edge as a 422 instead.
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]*$")


class CreateSessionRequest(BaseModel):
    image: Optional[str] = Field(None, min_length=1, max_length=512)  # must be allowlisted (SANDBOX_ALLOWED_IMAGES)
    timeout_seconds: int = Field(900, ge=1)   # session auto-reaps after this; clamped to SANDBOX_MAX_SESSION_SECONDS
    network: bool = False                     # default-deny egress; True opens outbound
    memory_mb: int = 512                      # clamped to SANDBOX_MAX_MEMORY_MB
    cpus: float = Field(1.0, allow_inf_nan=False)  # clamped to SANDBOX_MAX_CPUS


class Session(BaseModel):
    session_id: str
    provider: str


class ExecRequest(BaseModel):
    command: str                         # a shell command line: awk / sed / bash / anything
    timeout_seconds: int = Field(60, ge=1)   # clamped to SANDBOX_MAX_EXEC_SECONDS
    workdir: Optional[str] = Field(None, min_length=1)  # defaults to /workspace


class RunRequest(BaseModel):
    language: Literal["python"]          # python-only sandbox (runtime image ships no node)
    code: str
    timeout_seconds: int = Field(60, ge=1)   # clamped to SANDBOX_MAX_EXEC_SECONDS


class ExecResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    truncated: bool = False              # output exceeded the byte cap and was trimmed
    duration_ms: Optional[int] = None


class WriteFileRequest(BaseModel):
    path: str = Field(min_length=1)
    content: str
    encoding: Encoding = "utf-8"         # base64 to store binary

    @model_validator(mode="after")
    def _base64_is_well_formed(self) -> "WriteFileRequest":
        if self.encoding == "base64" and not _BASE64_RE.match(self.content):
            raise ValueError("content is not valid base64")
        return self


class ReadFileResponse(BaseModel):
    path: str
    content: str
    encoding: Encoding = "utf-8"
    truncated: bool = False


class FileEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int = 0


class ListFilesResponse(BaseModel):
    path: str
    entries: list[FileEntry]
