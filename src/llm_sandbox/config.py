"""Env-driven configuration. ``SANDBOX_PROVIDER`` selects the backend; the HTTP API is
identical either way, so callers never change when you switch providers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_local_dotenv() -> None:
    """Load this project's ``.env`` (if present) into the environment so a plain
    ``uvicorn`` / ``uv run`` launch picks it up. Only ``setdefault`` — real env vars and
    uvicorn ``--env-file`` still win."""
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_local_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class Config:
    provider: str            # "gvisor"
    auth_token: str          # shared bearer; callers send `Authorization: Bearer <token>`. Empty = auth off (dev only).
    default_image: str       # sandbox runtime image (built from sandbox.Dockerfile)
    docker_runtime: str      # "runsc" (gVisor, prod) | "runc" (standard, dev only — NOT isolated)
    max_output_bytes: int    # hard cap on any single stdout/stderr/file payload

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            provider=_env("SANDBOX_PROVIDER", "gvisor").strip().lower(),
            auth_token=_env("LLM_SANDBOX_TOKEN"),
            default_image=_env("SANDBOX_IMAGE", "llm-sandbox-runtime:latest"),
            docker_runtime=_env("SANDBOX_DOCKER_RUNTIME", "runsc"),
            max_output_bytes=int(_env("SANDBOX_MAX_OUTPUT_BYTES", "1000000") or 1_000_000),
        )
