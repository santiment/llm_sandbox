"""Wire contract for the HTTP API."""

from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

Encoding = Literal["utf-8", "base64"]

# Hard floors live here; the config-driven ceilings are applied by clamping in app.py.
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]*$")


class CreateSessionRequest(BaseModel):
    image: Optional[str] = Field(None, min_length=1, max_length=512)  # allowlisted (SANDBOX_ALLOWED_IMAGES)
    timeout_seconds: int = Field(900, ge=1)   # clamped to SANDBOX_MAX_SESSION_SECONDS
    network: bool = False                     # default-deny egress; True opens outbound
    memory_mb: int = 512                      # clamped to SANDBOX_MAX_MEMORY_MB
    cpus: float = Field(1.0, allow_inf_nan=False)  # clamped to SANDBOX_MAX_CPUS


class Session(BaseModel):
    session_id: str
    provider: str


class ExecRequest(BaseModel):
    command: str
    timeout_seconds: int = Field(60, ge=1)   # clamped to SANDBOX_MAX_EXEC_SECONDS
    workdir: Optional[str] = Field(None, min_length=1)  # defaults to /workspace


class RunRequest(BaseModel):
    language: Literal["python"]
    code: str
    timeout_seconds: int = Field(60, ge=1)   # clamped to SANDBOX_MAX_EXEC_SECONDS


class ExecResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int                       # 124 = hit timeout_seconds; 137 = did so and ignored TERM
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
    truncated: bool = False              # more entries exist than the listing cap returns
