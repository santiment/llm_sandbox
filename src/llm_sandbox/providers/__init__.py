"""Provider factory — picks the backend from config. The import is lazy so a future
provider with heavy/optional deps never burdens the gVisor path."""

from __future__ import annotations

from ..config import Config
from .base import SandboxProvider


def build_provider(cfg: Config) -> SandboxProvider:
    if cfg.provider == "gvisor":
        from .gvisor import GvisorProvider
        return GvisorProvider(
            default_image=cfg.default_image,
            docker_runtime=cfg.docker_runtime,
            max_output_bytes=cfg.max_output_bytes,
        )
    raise ValueError(f"unknown SANDBOX_PROVIDER={cfg.provider!r} (expected 'gvisor')")
