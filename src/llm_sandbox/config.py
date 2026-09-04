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


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)) or default)


def _flag(name: str, default: bool = False) -> bool:
    return _env(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    provider: str            # "gvisor" (docker, local) | "k8s" (pod-per-session on Kubernetes)
    auth_token: str          # shared bearer; callers send `Authorization: Bearer <token>`. Empty = auth off (dev only).
    default_image: str       # sandbox runtime image (built from sandbox.Dockerfile)
    allowed_images: list[str]  # images a caller may pick via `image`; default_image is always allowed
    docker_runtime: str      # "runsc" (gVisor, prod) | "runc" (standard, dev only — NOT isolated)
    docker_network: str      # docker network for network=true sessions ("bridge" = daemon default)
    max_output_bytes: int    # hard cap on any single stdout/stderr/file payload
    max_memory_mb: int       # ceiling on a caller's per-session memory_mb
    max_cpus: float          # ceiling on a caller's per-session cpus
    max_concurrency: int     # in-flight backend operations across all sessions
    max_session_seconds: int # ceiling on a caller's session timeout_seconds (clamped)
    max_exec_seconds: int    # ceiling on a caller's exec/run timeout_seconds (clamped)
    max_request_bytes: int   # HTTP request body cap (413 above it); bounds file/code payloads
    expose_docs: bool        # serve /docs, /redoc, /openapi.json (unauthenticated) — dev only
    max_sessions: int        # live sessions this replica will hold; 0 = unlimited. Bounds the
                             # NUMBER of sandboxes (max_memory_mb/max_cpus only bound each
                             # one's size), so a create loop gets a 429 instead of the node
                             # group. Per-replica: N replicas ⇒ N × this.
    log_payloads: bool       # log command/code bodies. Off in prod: they carry untrusted
                             # LLM output and possibly customer data into cluster logs.

    # --- k8s provider only (SANDBOX_PROVIDER=k8s) ---
    k8s_namespace: str       # namespace for session pods; "" = the service's own namespace
    k8s_runtime_class: str   # RuntimeClass for session pods; "" = cluster default (NOT isolated)
    k8s_node_selector: str   # "k=v[,k=v…]" pinning session pods to the sandbox node group
    k8s_toleration: str      # "key=value:Effect" matching the sandbox node taint; "" = none
    k8s_create_timeout: int  # seconds to wait for a session pod to become Ready
    k8s_image_pull_secrets: str  # "name[,name…]" for a private registry; "" = none
    k8s_reap_interval: int   # seconds between sweeps for finished session pods
    k8s_allow_no_runtime_class: bool  # explicit opt-in to running sessions WITHOUT gVisor

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            provider=_env("SANDBOX_PROVIDER", "gvisor").strip().lower(),
            auth_token=_env("LLM_SANDBOX_TOKEN"),
            default_image=_env("SANDBOX_IMAGE", "llm-sandbox-runtime:latest"),
            allowed_images=[s.strip() for s in _env("SANDBOX_ALLOWED_IMAGES").split(",") if s.strip()],
            docker_runtime=_env("SANDBOX_DOCKER_RUNTIME", "runsc"),
            docker_network=_env("SANDBOX_DOCKER_NETWORK", "bridge"),
            max_output_bytes=_int("SANDBOX_MAX_OUTPUT_BYTES", 1_000_000),
            max_memory_mb=_int("SANDBOX_MAX_MEMORY_MB", 4096),
            max_cpus=float(_env("SANDBOX_MAX_CPUS", "2") or 2),
            max_concurrency=_int("SANDBOX_MAX_CONCURRENCY", 32),
            max_session_seconds=_int("SANDBOX_MAX_SESSION_SECONDS", 3600),
            max_exec_seconds=_int("SANDBOX_MAX_EXEC_SECONDS", 600),
            max_request_bytes=_int("SANDBOX_MAX_REQUEST_BYTES", 32 * 1024 * 1024),
            expose_docs=_flag("SANDBOX_EXPOSE_DOCS"),
            max_sessions=_int("SANDBOX_MAX_SESSIONS", 24),
            log_payloads=_flag("SANDBOX_LOG_PAYLOADS", default=True),
            # Defaults mirror the target cluster's gVisor setup: RuntimeClass `gvisor`,
            # instance group `ai-sandbox`, taint `dedicated=ai-sandbox:NoSchedule`.
            k8s_namespace=_env("SANDBOX_K8S_NAMESPACE"),
            k8s_runtime_class=_env("SANDBOX_K8S_RUNTIME_CLASS", "gvisor"),
            k8s_node_selector=_env("SANDBOX_K8S_NODE_SELECTOR",
                                   "kops.k8s.io/instancegroup=ai-sandbox"),
            k8s_toleration=_env("SANDBOX_K8S_TOLERATION", "dedicated=ai-sandbox:NoSchedule"),
            k8s_create_timeout=_int("SANDBOX_K8S_CREATE_TIMEOUT", 120),
            k8s_image_pull_secrets=_env("SANDBOX_K8S_IMAGE_PULL_SECRETS"),
            k8s_reap_interval=_int("SANDBOX_K8S_REAP_INTERVAL", 120),
            k8s_allow_no_runtime_class=_flag("SANDBOX_K8S_ALLOW_NO_RUNTIME_CLASS"),
        )
