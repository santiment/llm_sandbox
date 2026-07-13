"""Kubernetes provider — each session is a long-lived Pod under the cluster's gVisor
``RuntimeClass``, orchestrated via the ``kubectl`` CLI (no SDK dependency, mirroring the
docker-CLI approach of the gVisor provider). A session = one pod, so files written persist
across ``exec`` calls until ``destroy``.

Cluster contract (devops doc "Add gVisor to stage kubernetes cluster"): session pods set
``runtimeClassName: gvisor``, pin to the ``ai-sandbox`` instance group via ``nodeSelector``
and tolerate its ``dedicated=ai-sandbox:NoSchedule`` taint. All three are env-tunable
(``SANDBOX_K8S_*``) so another cluster works without code changes.

Security posture (prod): gVisor around the pod, no service-account token or service links
inside it, memory/cpu limits, ephemeral (deleted on ``destroy``, auto-stops after
``timeout_seconds``). Egress control is a NetworkPolicy concern (k8s/networkpolicy.yaml):
session pods are default-deny; ``network=True`` adds the ``llm-sandbox/network: "true"``
label the allow-policy matches. Enforcement requires a CNI that implements NetworkPolicy.
Unlike the docker provider there is no per-pod pids cap — set ``podPidsLimit`` on the
kubelet of the sandbox nodes for the equivalent guard.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from .base import WORKDIR, CliSessionMixin, run_cli

log = logging.getLogger("llm_sandbox.k8s")

_NAME_PREFIX = "llmsbx-"                 # k8s names: lowercase alphanumerics + dashes
_SESSION_LABEL = "llm-sandbox-session"   # every session pod carries app=<this>
_NETWORK_LABEL = "llm-sandbox/network"   # "true" → matched by the allow-egress NetworkPolicy


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


class K8sProvider(CliSessionMixin):
    name = "k8s"

    def __init__(self, *, default_image: str, namespace: str, runtime_class: str,
                 node_selector: str, toleration: str, create_timeout: int,
                 max_output_bytes: int) -> None:
        self.default_image = default_image
        self.namespace = namespace  # "" = the service account's own namespace (in-cluster)
        self.runtime_class = runtime_class
        self.node_selector = parse_node_selector(node_selector)
        self.toleration = parse_toleration(toleration)
        self.create_timeout = create_timeout
        self.max_output_bytes = max_output_bytes
        self._bg_tasks: set[asyncio.Task] = set()  # keeps fire-and-forget reaps alive

    def _pod(self, session_id: str) -> str:
        return f"{_NAME_PREFIX}{session_id}"

    async def _kubectl(self, *args: str, stdin: bytes | None = None,
                       timeout: float | None = None) -> tuple[int, bytes, bytes]:
        ns = ("-n", self.namespace) if self.namespace else ()
        return await run_cli("kubectl", *ns, *args, stdin=stdin, timeout=timeout)

    async def _exec_cli(self, session_id, *cmd, stdin=None, timeout=None):
        interactive = ("-i",) if stdin is not None else ()
        return await self._kubectl("exec", *interactive, self._pod(session_id), "--", *cmd,
                                   stdin=stdin, timeout=timeout)

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
            "containers": [{
                "name": "sandbox",
                "image": image,
                # `sleep <timeout>` is the auto-reaper: even if destroy is never called, the
                # pod's container exits on its own after timeout_seconds.
                "command": ["sleep", str(int(timeout_seconds))],
                "workingDir": WORKDIR,
                "imagePullPolicy": "IfNotPresent",
                "resources": {
                    # Low request = dense packing on the sandbox node; the limit is the
                    # hard cap untrusted code can actually allocate.
                    "requests": {"cpu": "100m", "memory": "64Mi"},
                    "limits": {"cpu": str(cpus), "memory": f"{memory_mb}Mi"},
                },
            }],
        }
        if self.runtime_class:
            spec["runtimeClassName"] = self.runtime_class
        if self.node_selector:
            spec["nodeSelector"] = self.node_selector
        if self.toleration:
            spec["tolerations"] = [self.toleration]
        return {"apiVersion": "v1", "kind": "Pod",
                "metadata": {"name": name, "labels": labels}, "spec": spec}

    def _reap_finished_soon(self) -> None:
        """Fire-and-forget deletion of session pods whose ``sleep`` already ended
        (Succeeded/Failed) — pod objects outlive their containers, so without this the
        namespace accumulates dead pods. Off the create hot path on purpose."""
        task = asyncio.get_running_loop().create_task(self._kubectl(
            "delete", "pods", "-l", f"app={_SESSION_LABEL}",
            "--field-selector", "status.phase!=Running,status.phase!=Pending",
            "--wait=false", "--ignore-not-found", timeout=15))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def create(self, *, image=None, timeout_seconds=900, network=False,
                     memory_mb=512, cpus=1.0) -> str:
        session_id = uuid.uuid4().hex[:16]
        name = self._pod(session_id)
        self._reap_finished_soon()
        manifest = self._manifest(name, image or self.default_image, timeout_seconds,
                                  network, memory_mb, cpus)
        rc, _out, err = await self._kubectl("create", "-f", "-",
                                            stdin=json.dumps(manifest).encode(), timeout=30)
        if rc != 0:
            msg = err.decode(errors="replace").strip()
            low = msg.lower()
            if "runtimeclass" in low:
                raise RuntimeError(
                    f"RuntimeClass {self.runtime_class!r} rejected — is gVisor installed on "
                    f"the cluster (RuntimeClass + runsc node group)? (kubectl: {msg})")
            if "forbidden" in low:
                raise RuntimeError(
                    "service account may not create pods — apply k8s/rbac.yaml "
                    f"(kubectl: {msg})")
            raise RuntimeError(f"sandbox create failed: {msg}")
        rc, _out, err = await self._kubectl(
            "wait", "--for=condition=Ready", f"pod/{name}",
            f"--timeout={self.create_timeout}s", timeout=self.create_timeout + 10)
        if rc != 0:
            # `wait` only says "timed out" — grab the scheduler/kubelet reason before the
            # pod is deleted (afterwards `kubectl describe` has nothing to show).
            _rc, why, _e = await self._kubectl(
                "get", "pod", name, "-o",
                "jsonpath={.status.phase} {.status.containerStatuses[*].state.waiting.reason}",
                timeout=10)
            await self._kubectl("delete", "pod", name, "--wait=false", "--ignore-not-found",
                                timeout=30)
            reason = why.decode(errors="replace").strip()
            hint = ""
            if "ErrImagePull" in reason or "ImagePullBackOff" in reason:
                hint = (f" — image {(image or self.default_image)!r} is not pullable from the "
                        "cluster; push it to your registry and point SANDBOX_IMAGE at it")
            elif "Pending" in reason:
                hint = (" — pod unschedulable? check the ai-sandbox instance group is up and "
                        "SANDBOX_K8S_NODE_SELECTOR / SANDBOX_K8S_TOLERATION match it")
            raise RuntimeError(f"sandbox pod not ready ({reason or 'unknown'}){hint} "
                               f"(kubectl: {err.decode(errors='replace').strip()})")
        log.info("session %s created (runtime_class=%s, network=%s)", session_id,
                 self.runtime_class or "<cluster default>", network)
        return session_id

    async def destroy(self, session_id: str) -> None:
        await self._kubectl("delete", "pod", self._pod(session_id),
                            "--wait=false", "--ignore-not-found", timeout=30)
