"""Kubernetes provider: one pod per session under the gVisor RuntimeClass, driven by direct
apiserver calls (httpx for REST, a WebSocket for exec) — no kubectl in the image.

Requires Kubernetes >= 1.30: the v5 exec subprotocol's stdin half-close is what lets
``write_file`` stream a payload and still read an exit code. Egress is a NetworkPolicy
concern (session pods are default-deny; ``network=True`` adds the label the allow-policy
matches). Per-pod pid caps come from the kubelet's ``podPidsLimit``."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import time
import uuid
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import InvalidStatus

from .base import TIMEOUT_EXIT, WORKDIR, SandboxError, SessionNotFound, SessionOpsMixin

log = logging.getLogger("llm_sandbox.k8s")

_NAME_PREFIX = "llmsbx-"                 # k8s names: lowercase alphanumerics + dashes
_SESSION_LABEL = "llm-sandbox-session"   # every session pod carries app=<this>
_NETWORK_LABEL = "llm-sandbox/network"   # "true" → matched by the allow-egress NetworkPolicy

_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
_TOKEN_TTL = 60.0          # projected ServiceAccount tokens rotate; re-read at most this often
_STDIN_CHUNK = 256 * 1024  # websocket frame size when streaming a file payload

# Channel prefixes of the k8s exec stream protocol (first byte of every frame).
_CH_STDIN, _CH_STDOUT, _CH_STDERR, _CH_ERROR, _CH_CLOSE = 0, 1, 2, 3, 255

# Waiting reasons that never resolve on their own.
_FATAL_WAITING = {"ErrImagePull", "ImagePullBackOff", "InvalidImageName",
                  "CreateContainerConfigError", "CreateContainerError"}


def parse_node_selector(raw: str) -> dict[str, str]:
    """``"k1=v1,k2=v2"`` → ``{"k1": "v1", "k2": "v2"}``; empty string → ``{}``."""
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, _, val = pair.partition("=")
        out[key.strip()] = val.strip()
    return out


def parse_toleration(raw: str) -> dict[str, str] | None:
    """``"key=value:Effect"`` → toleration dict; ``"key:Effect"`` → operator Exists;
    empty string → ``None`` (no toleration)."""
    raw = raw.strip()
    if not raw:
        return None
    head, sep, effect = raw.rpartition(":")
    if not sep:
        head, effect = raw, ""
    key, eq, value = head.partition("=")
    tol: dict[str, str] = {"key": key.strip()}
    if eq:
        tol["operator"] = "Equal"
        tol["value"] = value.strip()
    else:
        tol["operator"] = "Exists"
    if effect:
        tol["effect"] = effect.strip()
    return tol


def parse_csv(raw: str) -> list[str]:
    """``"a, b"`` → ``["a", "b"]``; empty string → ``[]``."""
    return [s.strip() for s in raw.split(",") if s.strip()]


def _exit_code_from_status(payload: bytes) -> int:
    """Exit code from the JSON ``Status`` on the exec error channel."""
    if not payload:
        return 0
    try:
        status = json.loads(payload)
    except ValueError:
        return 1
    if status.get("status") == "Success":
        return 0
    for cause in status.get("details", {}).get("causes", []):
        if cause.get("reason") == "ExitCode":
            try:
                return int(cause.get("message", "1"))
            except ValueError:
                return 1
    return 1


class ApiServer:
    """Minimal in-cluster apiserver client. The projected ServiceAccount token rotates, so it
    is re-read from disk rather than cached for the life of the process."""

    def __init__(self, sa_dir: str = _SA_DIR) -> None:
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
        port = (os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS")
                or os.environ.get("KUBERNETES_SERVICE_PORT") or "443")
        if not host:
            raise RuntimeError(
                "KUBERNETES_SERVICE_HOST is unset — SANDBOX_PROVIDER=k8s only works from "
                "inside the cluster (the service runs as a Deployment; see example_k8s/deployment.yaml)")
        if ":" in host:  # IPv6 literal
            host = f"[{host}]"
        self.authority = f"{host}:{port}"
        self._token_path = os.path.join(sa_dir, "token")
        self._ns_path = os.path.join(sa_dir, "namespace")
        self._token = ""
        self._token_at = 0.0
        self._ssl = ssl.create_default_context(cafile=os.path.join(sa_dir, "ca.crt"))
        self._client: httpx.AsyncClient | None = None

    def default_namespace(self) -> str:
        try:
            with open(self._ns_path) as fh:
                return fh.read().strip()
        except OSError:
            return "default"

    def _bearer(self) -> str:
        now = time.monotonic()
        if not self._token or now - self._token_at > _TOKEN_TTL:
            with open(self._token_path) as fh:
                self._token = fh.read().strip()
            self._token_at = now
        return f"Bearer {self._token}"

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=f"https://{self.authority}", verify=self._ssl,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def request(self, method: str, path: str, *, params=None, body=None,
                      timeout: float | None = None) -> tuple[int, dict]:
        """Returns ``(http_status, json)``; never raises on a non-2xx."""
        if self._client is None:
            await self.start()
        assert self._client is not None
        headers = {"Authorization": self._bearer(), "Accept": "application/json"}
        kwargs: dict = {"params": params, "headers": headers}
        if body is not None:
            kwargs["json"] = body
        if timeout is not None:
            kwargs["timeout"] = timeout
        resp = await self._client.request(method, path, **kwargs)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {}

    def ws_url(self, path: str, query: list[tuple[str, str]]) -> str:
        return f"wss://{self.authority}{path}?{urlencode(query)}"

    def ws_kwargs(self) -> dict:
        return {"additional_headers": {"Authorization": self._bearer()}, "ssl": self._ssl}


def api_error(status: int, payload: dict) -> str:
    return f"HTTP {status}: {payload.get('message') or payload or 'no detail'}"


class K8sProvider(SessionOpsMixin):
    name = "k8s"

    def __init__(self, *, default_image: str, namespace: str, runtime_class: str,
                 node_selector: str, toleration: str, create_timeout: int,
                 max_output_bytes: int, image_pull_secrets: str = "",
                 max_concurrency: int = 16, reap_interval: int = 120,
                 allow_no_runtime_class: bool = False, disk_mb: int = 256,
                 api: ApiServer | None = None) -> None:
        if not runtime_class and not allow_no_runtime_class:
            raise RuntimeError(
                "SANDBOX_K8S_RUNTIME_CLASS is empty — session pods would run on the cluster's "
                "default runtime (runc), which is NOT an isolation boundary for untrusted code. "
                "Set it to 'gvisor', or set SANDBOX_K8S_ALLOW_NO_RUNTIME_CLASS=1 to accept that.")
        self.default_image = default_image
        self.runtime_class = runtime_class
        self.node_selector = parse_node_selector(node_selector)
        self.toleration = parse_toleration(toleration)
        self.image_pull_secrets = parse_csv(image_pull_secrets)
        self.create_timeout = create_timeout
        self.max_output_bytes = max_output_bytes
        self.disk_mb = disk_mb
        self.reap_interval = reap_interval
        self.api = api or ApiServer()
        self.namespace = namespace or self.api.default_namespace()
        # Create/destroy get their own lane so long execs cannot hold up a DELETE.
        self._sem = asyncio.Semaphore(max_concurrency)
        self._ctl_sem = asyncio.Semaphore(8)
        self._reaper: asyncio.Task | None = None

    # --- lifecycle -------------------------------------------------------------------

    async def startup(self) -> None:
        await self.api.start()
        self._reaper = asyncio.get_running_loop().create_task(self._reap_loop())

    async def shutdown(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            try:
                await self._reaper
            except asyncio.CancelledError:
                pass
            self._reaper = None
        await self.api.close()

    async def preflight(self) -> None:
        """Raise unless the apiserver is reachable and the RBAC is in place; backs /readyz."""
        status, payload = await self.api.request(
            "GET", f"/api/v1/namespaces/{self.namespace}/pods",
            params={"limit": "1", "labelSelector": f"app={_SESSION_LABEL}"}, timeout=10)
        if status == 403:
            raise SandboxError(f"service account may not list pods in namespace "
                               f"{self.namespace!r} — apply example_k8s/rbac.yaml ({api_error(status, payload)})")
        if status >= 400:
            raise SandboxError(f"apiserver unreachable or rejecting us ({api_error(status, payload)})")

    # --- pod plumbing ----------------------------------------------------------------

    def _pod(self, session_id: str) -> str:
        return f"{_NAME_PREFIX}{session_id}"

    def _pods_path(self, name: str = "") -> str:
        base = f"/api/v1/namespaces/{self.namespace}/pods"
        return f"{base}/{name}" if name else base

    def _manifest(self, name: str, image: str, timeout_seconds: int, network: bool,
                  memory_mb: int, cpus: float) -> dict:
        labels = {"app": _SESSION_LABEL}
        if network:
            labels[_NETWORK_LABEL] = "true"
        spec: dict = {
            "restartPolicy": "Never",
            # Untrusted code must never see cluster credentials or cluster service env vars.
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            # Backstop for `sleep`: the control plane stops the pod at the deadline regardless.
            "activeDeadlineSeconds": int(timeout_seconds) + 60,
            "terminationGracePeriodSeconds": 0,
            "containers": [{
                "name": "sandbox",
                "image": image,
                # `args`, not `command`: `command` would replace the image's tini ENTRYPOINT
                # and leave `sleep` as a non-reaping PID 1.
                "args": ["sleep", str(int(timeout_seconds))],
                "workingDir": WORKDIR,
                # SANDBOX_IMAGE must be an immutable tag (README): no re-pull per session.
                "imagePullPolicy": "IfNotPresent",
                "resources": {
                    # Low request = dense packing; the limit is the hard cap.
                    "requests": {"cpu": "100m", "memory": "64Mi", "ephemeral-storage": "64Mi"},
                    "limits": {"cpu": str(cpus), "memory": f"{memory_mb}Mi",
                               "ephemeral-storage": f"{self.disk_mb}Mi"},
                },
                # Root inside by design (gVisor is the boundary), but without capabilities.
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
            }],
        }
        if self.runtime_class:
            spec["runtimeClassName"] = self.runtime_class
        if self.node_selector:
            spec["nodeSelector"] = self.node_selector
        if self.toleration:
            spec["tolerations"] = [self.toleration]
        if self.image_pull_secrets:
            spec["imagePullSecrets"] = [{"name": n} for n in self.image_pull_secrets]
        return {"apiVersion": "v1", "kind": "Pod",
                "metadata": {"name": name, "labels": labels}, "spec": spec}

    async def _reap_loop(self) -> None:
        """Finished session pods linger as Succeeded/Failed; sweep them off the create path."""
        while True:
            try:
                await asyncio.sleep(self.reap_interval)
                status, payload = await self.api.request(
                    "DELETE", self._pods_path(),
                    params={"labelSelector": f"app={_SESSION_LABEL}",
                            "fieldSelector": "status.phase!=Running,status.phase!=Pending"},
                    timeout=30)
                if status >= 400:
                    log.warning("reap failed (%s)", api_error(status, payload))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a reaper that dies silently leaks pods forever
                log.warning("reap error: %r", exc)

    # --- SandboxProvider ---------------------------------------------------------------

    async def create(self, *, image=None, timeout_seconds=900, network=False,
                     memory_mb=512, cpus=1.0) -> str:
        session_id = uuid.uuid4().hex[:16]
        name = self._pod(session_id)
        image = image or self.default_image
        manifest = self._manifest(name, image, timeout_seconds, network, memory_mb, cpus)
        async with self._ctl_sem:
            status, payload = await self.api.request(
                "POST", self._pods_path(), body=manifest, timeout=30)
        if status >= 400:
            msg = str(payload.get("message", "")).lower()
            if "runtimeclass" in msg:
                raise SandboxError(
                    f"RuntimeClass {self.runtime_class!r} rejected — is gVisor installed on "
                    f"the cluster (RuntimeClass + runsc node group)? ({api_error(status, payload)})")
            if status == 403:
                raise SandboxError("service account may not create pods — apply example_k8s/rbac.yaml "
                                   f"({api_error(status, payload)})")
            raise SandboxError(f"sandbox create failed: {api_error(status, payload)}")
        try:
            await self._wait_ready(name, image)
        except Exception:
            await self.destroy(session_id)
            raise
        log.info("session %s created (runtime_class=%s, network=%s)", session_id,
                 self.runtime_class or "<cluster default>", network)
        return session_id

    async def _wait_ready(self, name: str, image: str) -> None:
        """Poll until Ready; bail early on a waiting reason that never clears."""
        deadline = time.monotonic() + self.create_timeout
        delay, reason = 0.1, "unknown"
        while time.monotonic() < deadline:
            status, pod = await self.api.request("GET", self._pods_path(name), timeout=10)
            if status < 400:
                pod_status = pod.get("status", {})
                phase = pod_status.get("phase", "Pending")
                if any(c.get("type") == "Ready" and c.get("status") == "True"
                       for c in pod_status.get("conditions", [])):
                    return
                waiting = ""
                for cs in pod_status.get("containerStatuses", []):
                    waiting = cs.get("state", {}).get("waiting", {}).get("reason", "") or waiting
                reason = f"{phase} {waiting}".strip()
                if waiting in _FATAL_WAITING:
                    break
                if phase in ("Failed", "Succeeded"):
                    break
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 1.0)  # tight at first (warm node ≈ 1s), then back off
        hint = ""
        if any(w in reason for w in ("ErrImagePull", "ImagePullBackOff", "InvalidImageName")):
            hint = (f" — image {image!r} is not pullable from the cluster; push it to your "
                    "registry, point SANDBOX_IMAGE at it, and set SANDBOX_K8S_IMAGE_PULL_SECRETS "
                    "if the registry is private")
        elif reason.startswith("Pending"):
            hint = (" — pod unschedulable? check the ai-sandbox instance group is up, that "
                    "SANDBOX_K8S_NODE_SELECTOR / SANDBOX_K8S_TOLERATION match it, and that the "
                    "namespace ResourceQuota is not exhausted")
        raise SandboxError(f"sandbox pod not ready ({reason or 'unknown'}){hint}")

    async def destroy(self, session_id: str) -> None:
        async with self._ctl_sem:
            status, payload = await self.api.request(
                "DELETE", self._pods_path(self._pod(session_id)),
                params={"gracePeriodSeconds": "0", "propagationPolicy": "Background"},
                timeout=30)
        if status >= 400 and status != 404:
            log.warning("destroy %s failed (%s)", session_id, api_error(status, payload))

    async def live_session_ids(self) -> set[str] | None:
        status, payload = await self.api.request(
            "GET", self._pods_path(),
            params={"labelSelector": f"app={_SESSION_LABEL}",
                    "fieldSelector": "status.phase!=Succeeded,status.phase!=Failed"},
            timeout=15)
        if status >= 400:
            return None
        names = (item.get("metadata", {}).get("name", "") for item in payload.get("items", []))
        return {n[len(_NAME_PREFIX):] for n in names if n.startswith(_NAME_PREFIX)}

    # --- exec transport ----------------------------------------------------------------

    async def _exec_cli(self, session_id, *cmd, stdin=None, timeout=None):
        """Run ``cmd`` in the pod over the exec WebSocket; returns ``(exit_code, stdout, stderr)``."""
        query = [("command", c) for c in cmd]
        query += [("stdout", "true"), ("stderr", "true"), ("tty", "false"),
                  ("stdin", "true" if stdin is not None else "false")]
        url = self.api.ws_url(f"/api/v1/namespaces/{self.namespace}/pods/"
                              f"{self._pod(session_id)}/exec", query)
        try:
            async with self._sem:
                return await asyncio.wait_for(self._stream(url, stdin), timeout=timeout)
        except asyncio.TimeoutError:
            # Backstop only: the in-session `timeout` is what stops the command.
            return TIMEOUT_EXIT, b"", b"sandbox: operation timed out"
        except InvalidStatus as exc:
            if exc.response.status_code == 404:
                raise SessionNotFound(session_id) from exc
            raise SandboxError(f"apiserver refused exec (HTTP {exc.response.status_code})") from exc

    async def _stream(self, url: str, stdin: bytes | None) -> tuple[int, bytes, bytes]:
        # One byte past the cap: enough to detect truncation, no unbounded buffering.
        limit = self.max_output_bytes + 1
        out, err, status_payload = bytearray(), bytearray(), b""
        async with ws_connect(url, subprotocols=["v5.channel.k8s.io", "v4.channel.k8s.io"],
                              max_size=None, open_timeout=30, **self.api.ws_kwargs()) as ws:
            if stdin is not None:
                if ws.subprotocol != "v5.channel.k8s.io":
                    raise SandboxError(
                        "apiserver negotiated the v4 exec protocol, which cannot half-close "
                        "stdin — writing files needs Kubernetes >= 1.30 (v5.channel.k8s.io)")
                for i in range(0, len(stdin), _STDIN_CHUNK):
                    await ws.send(bytes([_CH_STDIN]) + stdin[i:i + _STDIN_CHUNK])
                # v5 close frame = stdin EOF; the connection stays up for the exit status.
                await ws.send(bytes([_CH_CLOSE, _CH_STDIN]))
            async for frame in ws:
                if not isinstance(frame, (bytes, bytearray)) or not frame:
                    continue
                channel, payload = frame[0], frame[1:]
                if channel == _CH_ERROR:
                    status_payload += payload
                elif channel in (_CH_STDOUT, _CH_STDERR):
                    buf = out if channel == _CH_STDOUT else err
                    room = limit - len(buf)
                    if room > 0:
                        buf += payload[:room]
        return _exit_code_from_status(bytes(status_payload)), bytes(out), bytes(err)
