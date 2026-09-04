"""Tests for the Kubernetes provider.

The provider talks to a real apiserver, which these tests must never do — so the two halves
of its wire protocol are exercised against local fakes instead:

* the REST verbs (create / wait-ready / destroy) against a scripted ``FakeApi``;
* ``exec`` against a real local WebSocket server speaking the actual ``v5.channel.k8s.io``
  framing — channel-prefixed binary frames, the stdin close frame, and the JSON ``Status``
  on the error channel that carries the exit code.

That second half is the piece with no other safety net: it replaced ``kubectl exec``, and a
cluster is the only other place it runs.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from urllib.parse import urlencode

import pytest
from websockets.asyncio.server import serve

from llm_sandbox.providers.k8s import (K8sProvider, _exit_code_from_status, parse_csv,
                                       parse_node_selector, parse_toleration)

CH_STDIN, CH_STDOUT, CH_STDERR, CH_ERROR, CH_CLOSE = 0, 1, 2, 3, 255

SUCCESS = json.dumps({"metadata": {}, "status": "Success"}).encode()
READY = (200, {"status": {"phase": "Running",
                          "conditions": [{"type": "Ready", "status": "True"}]}})


def failure(exit_code: int) -> bytes:
    """The Status an apiserver sends on the error channel for a non-zero exit."""
    return json.dumps({
        "metadata": {}, "status": "Failure", "reason": "NonZeroExitCode",
        "details": {"causes": [{"reason": "ExitCode", "message": str(exit_code)}]},
    }).encode()


def waiting(reason: str) -> tuple[int, dict]:
    return 200, {"status": {"phase": "Pending", "containerStatuses": [
        {"state": {"waiting": {"reason": reason}}}]}}


# --- fakes ---------------------------------------------------------------------------------

class FakeApi:
    """Stands in for ApiServer. ``routes`` is a list of ``(method, path_substring, response)``,
    first match wins; an unmatched call returns ``(200, {})``."""

    def __init__(self, routes=(), ws_port=None):
        self.routes = list(routes)
        self.ws_port = ws_port
        self.calls: list[tuple[str, str, dict | None]] = []

    def default_namespace(self):
        return "test-ns"

    async def start(self):
        pass

    async def close(self):
        pass

    async def request(self, method, path, *, params=None, body=None, timeout=None):
        self.calls.append((method, path, body))
        for verb, sub, resp in self.routes:
            if verb == method and sub in path:
                return resp
        return 200, {}

    def methods(self):
        return [m for m, _p, _b in self.calls]

    def bodies(self, method):
        return [b for m, _p, b in self.calls if m == method]

    def ws_url(self, path, query):
        return f"ws://127.0.0.1:{self.ws_port}{path}?{urlencode(query)}"

    def ws_kwargs(self):
        return {}


@asynccontextmanager
async def exec_server(handler, subprotocol="v5.channel.k8s.io"):
    """Serve one k8s-style exec endpoint; yields the bound port."""
    async with serve(handler, "127.0.0.1", 0, subprotocols=[subprotocol]) as server:
        yield server.sockets[0].getsockname()[1]


def provider(api, **kw):
    opts = dict(default_image="img:1", namespace="test-ns", runtime_class="gvisor",
                node_selector="", toleration="", create_timeout=5,
                max_output_bytes=1000, api=api)
    opts.update(kw)
    return K8sProvider(**opts)


# --- parsing helpers -----------------------------------------------------------------------

def test_parse_node_selector():
    assert parse_node_selector("a=1,b=2") == {"a": "1", "b": "2"}
    assert parse_node_selector(" a = 1 ") == {"a": "1"}
    assert parse_node_selector("") == {}


def test_parse_toleration():
    assert parse_toleration("dedicated=ai-sandbox:NoSchedule") == {
        "key": "dedicated", "operator": "Equal", "value": "ai-sandbox", "effect": "NoSchedule"}
    assert parse_toleration("dedicated:NoSchedule") == {
        "key": "dedicated", "operator": "Exists", "effect": "NoSchedule"}
    assert parse_toleration("") is None


def test_parse_csv():
    assert parse_csv("a, b ,") == ["a", "b"]
    assert parse_csv("") == []


def test_exit_code_from_status():
    assert _exit_code_from_status(SUCCESS) == 0
    assert _exit_code_from_status(failure(42)) == 42
    assert _exit_code_from_status(b"") == 0
    assert _exit_code_from_status(b"not json") == 1


# --- guards --------------------------------------------------------------------------------

def test_empty_runtime_class_refuses_to_start():
    """Fail-open is the dangerous direction: no RuntimeClass means untrusted code runs under
    runc with no gVisor boundary at all."""
    with pytest.raises(RuntimeError, match="NOT an isolation boundary"):
        provider(FakeApi(), runtime_class="")


def test_empty_runtime_class_needs_an_explicit_opt_in():
    assert provider(FakeApi(), runtime_class="", allow_no_runtime_class=True).runtime_class == ""


# --- manifest ------------------------------------------------------------------------------

def test_manifest_security_and_placement():
    p = provider(FakeApi(), node_selector="kops.k8s.io/instancegroup=ai-sandbox",
                 toleration="dedicated=ai-sandbox:NoSchedule", image_pull_secrets="regcred,other")
    spec = p._manifest("llmsbx-x", "img:1", 900, network=False, memory_mb=512, cpus=1.0)["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert spec["enableServiceLinks"] is False
    assert spec["runtimeClassName"] == "gvisor"
    assert spec["nodeSelector"] == {"kops.k8s.io/instancegroup": "ai-sandbox"}
    assert spec["tolerations"] == [{"key": "dedicated", "operator": "Equal",
                                    "value": "ai-sandbox", "effect": "NoSchedule"}]
    assert spec["imagePullSecrets"] == [{"name": "regcred"}, {"name": "other"}]
    assert spec["activeDeadlineSeconds"] == 960
    container = spec["containers"][0]
    # `args`, never `command`: a command would displace the image's tini ENTRYPOINT and leave
    # `sleep` as a non-reaping PID 1.
    assert container["args"] == ["sleep", "900"]
    assert "command" not in container
    assert container["resources"]["limits"] == {"cpu": "1.0", "memory": "512Mi",
                                                "ephemeral-storage": "1024Mi"}
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }


def test_manifest_disk_cap_is_configurable():
    p = provider(FakeApi(), disk_mb=256)
    spec = p._manifest("n", "i", 60, network=False, memory_mb=64, cpus=1.0)["spec"]
    assert spec["containers"][0]["resources"]["limits"]["ephemeral-storage"] == "256Mi"


def test_manifest_network_label_gates_the_egress_policy():
    p = provider(FakeApi())
    off = p._manifest("n", "i", 60, network=False, memory_mb=64, cpus=1.0)["metadata"]["labels"]
    on = p._manifest("n", "i", 60, network=True, memory_mb=64, cpus=1.0)["metadata"]["labels"]
    assert off == {"app": "llm-sandbox-session"}
    assert on["llm-sandbox/network"] == "true"


# --- create / destroy ----------------------------------------------------------------------

async def test_create_waits_for_ready():
    api = FakeApi([("POST", "/pods", (201, {})), ("GET", "/pods/", READY)])
    sid = await provider(api).create(timeout_seconds=60)
    assert len(sid) == 16
    assert api.bodies("POST")[0]["metadata"]["name"] == f"llmsbx-{sid}"


async def test_create_forbidden_names_the_fix():
    api = FakeApi([("POST", "/pods", (403, {"message": "pods is forbidden"}))])
    with pytest.raises(RuntimeError, match=r"k8s/rbac\.yaml"):
        await provider(api).create()


async def test_create_missing_runtimeclass_names_the_fix():
    api = FakeApi([("POST", "/pods",
                    (404, {"message": 'runtimeclass.node.k8s.io "gvisor" not found'}))])
    with pytest.raises(RuntimeError, match="gVisor installed"):
        await provider(api).create()


async def test_create_gives_up_early_on_an_unpullable_image():
    """ImagePullBackOff never clears on its own: the create must not sit out the whole
    timeout, must say what to do about it, and must not leak the pod it gave up on."""
    api = FakeApi([("POST", "/pods", (201, {})), ("GET", "/pods/", waiting("ImagePullBackOff"))])
    with pytest.raises(RuntimeError, match="SANDBOX_K8S_IMAGE_PULL_SECRETS"):
        await provider(api, create_timeout=30).create()
    assert "DELETE" in api.methods()


async def test_create_unschedulable_points_at_the_node_group():
    api = FakeApi([("POST", "/pods", (201, {})), ("GET", "/pods/", (200, {"status": {}}))])
    with pytest.raises(RuntimeError, match="ai-sandbox instance group"):
        await provider(api, create_timeout=1).create()


async def test_destroy_tolerates_a_missing_pod():
    api = FakeApi([("DELETE", "/pods/", (404, {"message": "not found"}))])
    await provider(api).destroy("abc")  # must not raise
    assert api.calls[0][1].endswith("/pods/llmsbx-abc")


# --- exec stream ---------------------------------------------------------------------------

async def test_exec_demuxes_stdout_stderr_and_exit_code():
    async def handler(ws):
        await ws.send(bytes([CH_STDOUT]) + b"hello ")
        await ws.send(bytes([CH_STDOUT]) + b"world")
        await ws.send(bytes([CH_STDERR]) + b"warn")
        await ws.send(bytes([CH_ERROR]) + failure(3))

    async with exec_server(handler) as port:
        r = await provider(FakeApi(ws_port=port)).exec("s1", "true", timeout_seconds=5)
    assert (r.stdout, r.stderr, r.exit_code, r.truncated) == ("hello world", "warn", 3, False)


async def test_write_file_streams_stdin_and_half_closes():
    """Why this provider needs v5: without the stdin close frame `cat > file` never sees EOF,
    so it never exits and never reports a status."""
    seen: dict = {}

    async def handler(ws):
        body = bytearray()
        async for frame in ws:
            if frame[0] == CH_CLOSE and frame[1] == CH_STDIN:
                break
            if frame[0] == CH_STDIN:
                body += frame[1:]
        seen["body"] = bytes(body)
        seen["url"] = ws.request.path
        await ws.send(bytes([CH_ERROR]) + SUCCESS)

    async with exec_server(handler) as port:
        await provider(FakeApi(ws_port=port)).write_file("s1", "/workspace/f.txt", "payload")
    assert seen["body"] == b"payload"
    assert "stdin=true" in seen["url"]
    assert "cat" in seen["url"]          # the redirect the mixin builds


async def test_write_file_rejects_a_cluster_without_v5():
    """A v4-only apiserver cannot half-close stdin, so a write would hang until the timeout.
    Saying so beats waiting it out."""
    async def handler(ws):
        await ws.send(bytes([CH_ERROR]) + SUCCESS)

    async with exec_server(handler, subprotocol="v4.channel.k8s.io") as port:
        with pytest.raises(RuntimeError, match="v4 exec protocol"):
            await provider(FakeApi(ws_port=port)).write_file("s1", "/f", "x")


async def test_output_is_capped_and_reported_as_truncated():
    async def handler(ws):
        await ws.send(bytes([CH_STDOUT]) + b"x" * 50_000)
        await ws.send(bytes([CH_ERROR]) + SUCCESS)

    async with exec_server(handler) as port:
        r = await provider(FakeApi(ws_port=port), max_output_bytes=1000).exec(
            "s1", "yes", timeout_seconds=5)
    assert len(r.stdout) == 1000
    assert r.truncated is True


async def test_exec_timeout_reports_the_conventional_code():
    async def handler(ws):
        await ws.wait_closed()          # never answers

    async with exec_server(handler) as port:
        r = await provider(FakeApi(ws_port=port)).exec("s1", "sleep 99", timeout_seconds=0)
    assert r.exit_code == 124
    assert "timed out" in r.stderr


async def test_list_files_parses_the_json_snippet():
    listing = {"truncated": True, "entries": [
        {"name": "a.csv", "path": "/workspace/a.csv", "is_dir": False, "size": 12}]}

    async def handler(ws):
        await ws.send(bytes([CH_STDOUT]) + json.dumps(listing).encode())
        await ws.send(bytes([CH_ERROR]) + SUCCESS)

    async with exec_server(handler) as port:
        out, truncated = await provider(FakeApi(ws_port=port)).list_files("s1", "/workspace")
    assert (out[0].name, out[0].size, out[0].is_dir, truncated) == ("a.csv", 12, False, True)


async def test_exec_runs_under_an_in_session_timeout():
    """The deadline must be enforced inside the pod: dropping the stream kills nothing."""
    seen = {}

    async def handler(ws):
        seen["url"] = ws.request.path
        await ws.send(bytes([CH_ERROR]) + SUCCESS)

    async with exec_server(handler) as port:
        await provider(FakeApi(ws_port=port)).exec("s1", "true", timeout_seconds=7)
    assert "command=timeout&command=-k&command=1&command=7&command=sh&command=-c" in seen["url"]


async def test_read_file_falls_back_to_base64_for_binary():
    async def handler(ws):
        await ws.send(bytes([CH_STDOUT]) + b"\x00\x01\x02\xff")
        await ws.send(bytes([CH_ERROR]) + SUCCESS)

    async with exec_server(handler) as port:
        content, encoding, truncated = await provider(FakeApi(ws_port=port)).read_file(
            "s1", "/f.bin", max_bytes=100)
    assert (content, encoding, truncated) == ("AAEC/w==", "base64", False)


async def test_read_file_reports_a_missing_path_as_not_found():
    async def handler(ws):
        await ws.send(bytes([CH_STDERR]) + b"head: /nope: No such file or directory")
        await ws.send(bytes([CH_ERROR]) + failure(1))

    async with exec_server(handler) as port:
        with pytest.raises(FileNotFoundError, match="No such file"):
            await provider(FakeApi(ws_port=port)).read_file("s1", "/nope", max_bytes=100)
