"""Wire contract for the sandbox HTTP API — the SAME request/response shapes for every
caller, regardless of which provider (gVisor, …) runs underneath.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

Encoding = Literal["utf-8", "base64"]


class CreateSessionRequest(BaseModel):
    image: Optional[str] = None          # override the default sandbox image
    timeout_seconds: int = 900           # session auto-reaps after this (abandoned-run guard)
    network: bool = False                # default-deny egress; True opens outbound
    memory_mb: int = 512                 # provider may clamp
    cpus: float = 1.0


class Session(BaseModel):
    session_id: str
    provider: str


class ExecRequest(BaseModel):
    command: str                         # a shell command line: awk / sed / bash / anything
    timeout_seconds: int = 60
    workdir: Optional[str] = None        # defaults to /workspace


class RunRequest(BaseModel):
    language: Literal["python", "javascript"]
    code: str
    timeout_seconds: int = 60


class ExecResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    truncated: bool = False              # output exceeded the byte cap and was trimmed
    duration_ms: Optional[int] = None


class WriteFileRequest(BaseModel):
    path: str
    content: str
    encoding: Encoding = "utf-8"         # base64 to store binary


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
