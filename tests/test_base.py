"""Shared plumbing: the subprocess runner and the in-session command wrappers. No docker."""

from __future__ import annotations

import sys

from llm_sandbox.providers.base import TIMEOUT_EXIT, SessionOpsMixin, run_cli

PY = sys.executable


async def test_run_cli_caps_each_stream_while_it_streams():
    rc, out, err = await run_cli(
        PY, "-c", "import sys; sys.stdout.write('o'*200000); sys.stderr.write('e'*200000)",
        timeout=20, max_bytes=1000)
    assert rc == 0
    assert (len(out), len(err)) == (1000, 1000)           # bounded, exit code intact
    assert out == b"o" * 1000 and err == b"e" * 1000


async def test_run_cli_feeds_stdin_and_reads_all_when_uncapped():
    rc, out, _err = await run_cli(PY, "-c", "import sys; sys.stdout.write(sys.stdin.read())",
                                  stdin=b"hello " * 50_000, timeout=20)
    assert (rc, len(out)) == (0, 300_000)


async def test_run_cli_survives_a_child_that_ignores_stdin():
    rc, out, _err = await run_cli(PY, "-c", "print('bye')", stdin=b"x" * 1_000_000, timeout=20)
    assert (rc, out.strip()) == (0, b"bye")


async def test_run_cli_kills_on_timeout():
    rc, _out, err = await run_cli(PY, "-c", "import time; time.sleep(30)", timeout=0.3)
    assert rc == TIMEOUT_EXIT
    assert b"timed out" in err


async def test_run_cli_reports_the_exit_code():
    rc, _out, _err = await run_cli(PY, "-c", "raise SystemExit(7)", timeout=20)
    assert rc == 7


class Recorder(SessionOpsMixin):
    max_output_bytes = 100

    def __init__(self):
        self.calls = []

    async def _exec_cli(self, session_id, *cmd, stdin=None, timeout=None):
        self.calls.append((cmd, stdin, timeout))
        return 0, b"", b""


async def test_exec_is_wrapped_in_gnu_timeout_with_a_backstop_wait():
    r = Recorder()
    await r.exec("s", "echo hi", timeout_seconds=9, workdir="/data")
    cmd, _stdin, timeout = r.calls[0]
    assert cmd[:6] == ("timeout", "-k", "1", "9", "sh", "-c")
    assert cmd[6] == "cd /data && echo hi"
    assert timeout == 11                                   # in-session deadline + 2s slack


async def test_exec_timeout_floor_is_one_second():
    r = Recorder()
    await r.exec("s", "true", timeout_seconds=0)
    assert r.calls[0][0][3] == "1"


async def test_run_script_is_one_exec_with_the_code_on_stdin():
    r = Recorder()
    await r.run_script("s", "print(1)", interpreter="python3", ext="py", timeout_seconds=9)
    cmd, stdin, timeout = r.calls[0]
    assert len(r.calls) == 1
    assert cmd[:2] == ("sh", "-c")
    script = cmd[2]
    assert script.startswith("cat > /tmp/_run_") and "&& cd /workspace && timeout -k 1 9 python3 /tmp/_run_" in script
    assert script.endswith("; rc=$?; rm -f " + script.split("cat > ")[1].split(" ")[0] + "; exit $rc")
    assert stdin == b"print(1)"
    assert timeout == 14


async def test_write_file_creates_the_right_parent_for_every_path_shape():
    """`"/foo".rsplit("/", 1)[0]` is "" — `mkdir -p ''` failed every root-level write."""
    r = Recorder()
    for path, parent in [("/foo", "/"), ("/workspace/f.txt", "/workspace"),
                         ("data.csv", "/workspace"), ("sub/x.csv", "sub"), ("/a/b/c", "/a/b")]:
        r.calls.clear()
        await r.write_file("s", path, "x")
        cmd, stdin, _t = r.calls[0]
        assert cmd[2].startswith(f"mkdir -p {parent} && cat > {path}.partial-"), (path, cmd[2])
        assert stdin == b"x"


async def test_write_file_is_size_checked_and_atomic():
    """A cut stream gives `cat` a clean EOF; the byte check and temp+mv keep partials out."""
    r = Recorder()
    await r.write_file("s", "/workspace/f.txt", "héllo")          # 6 bytes utf-8
    script = r.calls[0][0][2]
    tmp = script.split("cat > ")[1].split(" ")[0]
    assert tmp.startswith("/workspace/f.txt.partial-")
    assert f'&& [ "$(wc -c < {tmp})" -eq 6 ] && mv -f {tmp} /workspace/f.txt; rc=$?; rm -f {tmp}; exit $rc' in script

    r.calls.clear()
    await r.write_file("s", "/workspace/b.bin", "AAEC/w==", encoding="base64")
    script = r.calls[0][0][2]
    tmp = script.split("cat > ")[1].split(" ")[0]
    assert f'-eq 8 ] && base64 -d {tmp} > /workspace/b.bin || {{ rm -f /workspace/b.bin; false; }}; rc=$?' in script


async def test_run_script_refuses_to_run_a_truncated_upload():
    r = Recorder()
    await r.run_script("s", "print(1)", interpreter="python3", ext="py", timeout_seconds=5)
    script = r.calls[0][0][2]
    path = script.split("cat > ")[1].split(" ")[0]
    assert f'cat > {path} && [ "$(wc -c < {path})" -eq 8 ] && cd /workspace && timeout -k 1 5 python3 {path};' in script
