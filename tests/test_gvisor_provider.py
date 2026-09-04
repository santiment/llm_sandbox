"""Docker-CLI provider: the argv it builds is the security posture, so pin it. No daemon —
``_docker`` is stubbed."""

from __future__ import annotations

import pytest

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
    assert calls[0][:3] == ["exec", "llmsbx_abc", "sh"]
    await p.write_file("abc", "/workspace/f", "x")
    assert calls[1][:3] == ["exec", "-i", "llmsbx_abc"]          # stdin attached only when needed
