"""The shared plumbing every provider stands on: the subprocess runner (docker provider) and
the in-session command wrapper. Real local subprocesses, no docker."""

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
    assert script.startswith("cat > /tmp/_run_") and ".py && cd /workspace && timeout -k 1 9 python3 /tmp/_run_" in script
    assert script.endswith("; rc=$?; rm -f " + script.split("cat > ")[1].split(" ")[0] + "; exit $rc")
    assert stdin == b"print(1)"
    assert timeout == 14
