"""Docker-CLI provider: the argv it builds is the security posture, so pin it. No daemon."""

from __future__ import annotations

import pytest

from llm_sandbox.providers.base import SessionNotFound
from llm_sandbox.providers.gvisor import GvisorProvider


def provider(**kw):
    opts = dict(default_image="img:1", docker_runtime="runsc", max_output_bytes=1000)
    opts.update(kw)
    return GvisorProvider(**opts)


def stub(p, rc=0, out=b"", err=b""):
    calls = []

    async def _docker(*args, stdin=None, timeout=None, **_kw):
        calls.append(list(args))
        return rc, out, err

    p._docker = _docker
    return calls


async def test_create_argv_hardening():
    p = provider(docker_network="llmsbx-net")
    calls = stub(p)
    sid = await p.create(image=None, timeout_seconds=90, network=False, memory_mb=256, cpus=0.5)
    argv = calls[0]
    assert argv[:2] == ["run", "-d"]
    assert argv[argv.index("--name") + 1] == f"llmsbx_{sid}"
    assert argv[argv.index("--runtime") + 1] == "runsc"
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--memory") + 1] == "256m"
    assert argv[argv.index("--memory-swap") + 1] == "256m"     # no swap headroom
    assert argv[argv.index("--cpus") + 1] == "0.5"
    assert argv[argv.index("--pids-limit") + 1] == "256"
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    # `--` ends flag parsing right before the caller-influenced image.
    assert argv[-4:] == ["--", "img:1", "sleep", "90"]


async def test_create_network_true_uses_the_configured_network():
    p = provider(docker_network="llmsbx-net")
    calls = stub(p)
    await p.create(network=True)
    assert calls[0][calls[0].index("--network") + 1] == "llmsbx-net"


async def test_a_flag_shaped_image_cannot_become_a_flag():
    p = provider()
    calls = stub(p)
    await p.create(image="--privileged")
    argv = calls[0]
    assert argv[argv.index("--") + 1] == "--privileged"     # positional, after the terminator


async def test_create_error_paths_name_the_fix():
    p = provider()
    stub(p, rc=125, err=b"docker: Error response from daemon: unknown or invalid runtime name: runsc")
    with pytest.raises(RuntimeError, match="SANDBOX_DOCKER_RUNTIME=runc"):
        await p.create()
    stub(p, rc=125, err=b"Unable to find image 'img:1' locally: pull access denied")
    with pytest.raises(RuntimeError, match="build it first"):
        await p.create()


async def test_exec_argv():
    p = provider()
    calls = stub(p)
    await p.exec("abc", "echo hi", timeout_seconds=5)
    # GNU timeout inside the container is what actually stops the command at the deadline.
    assert calls[0][:8] == ["exec", "llmsbx_abc", "timeout", "-k", "1", "5", "sh", "-c"]
    assert calls[0][8].endswith("&& echo hi")
    await p.write_file("abc", "/workspace/f", "x")
    assert calls[1][:3] == ["exec", "-i", "llmsbx_abc"]          # stdin attached only when needed


async def test_exec_output_is_capped_at_the_stream():
    """The cap is applied in run_cli as bytes arrive, not after buffering."""
    p = provider(max_output_bytes=10)
    seen = {}

    async def _docker(*args, stdin=None, timeout=None, max_bytes=None, control=False):
        seen["max_bytes"] = max_bytes
        return 0, b"x" * max_bytes, b""

    p._docker = _docker
    r = await p.exec("abc", "yes", timeout_seconds=5)
    assert seen["max_bytes"] == 11
    assert (len(r.stdout), r.truncated) == (10, True)


async def test_live_session_ids_strips_the_prefix_and_tolerates_a_dead_daemon():
    p = provider()
    stub(p, out=b"llmsbx_aaaaaaaaaaaaaaaa\nllmsbx_bbbbbbbbbbbbbbbb\nunrelated\n")
    assert await p.live_session_ids() == {"aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"}
    stub(p, rc=1, err=b"Cannot connect to the Docker daemon")
    assert await p.live_session_ids() is None


def stub_daemon(p, exec_rc, exec_err, running):
    """`docker exec` answers with (exec_rc, exec_err); `docker inspect` reports `running`."""
    calls = []

    async def _docker(*args, stdin=None, timeout=None, **_kw):
        calls.append(list(args))
        if args[0] == "inspect":
            return (0, b"true\n", b"") if running else (1, b"", b"Error: No such object")
        return exec_rc, b"", exec_err

    p._docker = _docker
    return calls


async def test_exec_on_a_missing_session_is_not_a_successful_exec():
    p = provider()
    stub_daemon(p, 1, b"Error response from daemon: No such container: llmsbx_abc", running=False)
    with pytest.raises(SessionNotFound):
        await p.exec("abc", "true", timeout_seconds=5)
    stub_daemon(p, 1, b"Error response from daemon: Container abc is not running", running=False)
    with pytest.raises(SessionNotFound):
        await p.read_file("abc", "/f", max_bytes=10)


async def test_a_command_forging_the_daemons_error_is_not_a_missing_session():
    """The CLI and the command share stderr; only the daemon can say if the container exists."""
    p = provider()
    calls = stub_daemon(p, 1, b"Error response from daemon: No such container: x\n", running=True)
    r = await p.exec("abc", "echo forged >&2; exit 1", timeout_seconds=5)
    assert (r.exit_code, "No such container" in r.stderr) == (1, True)
    assert [c[0] for c in calls] == ["exec", "inspect"]


async def test_ordinary_failures_do_not_cost_an_inspect():
    p = provider()
    calls = stub_daemon(p, 2, b"sh: 1: nope: not found\n", running=True)
    r = await p.exec("abc", "nope", timeout_seconds=5)
    assert r.exit_code == 2
    assert [c[0] for c in calls] == ["exec"]
