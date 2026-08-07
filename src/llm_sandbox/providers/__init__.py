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
            max_concurrency=cfg.max_concurrency,
        )
    if cfg.provider == "k8s":
        from .k8s import K8sProvider
        return K8sProvider(
            default_image=cfg.default_image,
            namespace=cfg.k8s_namespace,
            runtime_class=cfg.k8s_runtime_class,
            node_selector=cfg.k8s_node_selector,
            toleration=cfg.k8s_toleration,
            create_timeout=cfg.k8s_create_timeout,
            max_output_bytes=cfg.max_output_bytes,
            image_pull_secrets=cfg.k8s_image_pull_secrets,
            max_concurrency=cfg.max_concurrency,
            reap_interval=cfg.k8s_reap_interval,
            allow_no_runtime_class=cfg.k8s_allow_no_runtime_class,
        )
    raise ValueError(f"unknown SANDBOX_PROVIDER={cfg.provider!r} (expected 'gvisor' or 'k8s')")
