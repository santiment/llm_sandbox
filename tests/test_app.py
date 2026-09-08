"""HTTP layer against a fake provider: auth, input bounds, status mapping, slot accounting."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

import llm_sandbox.app as appmod
from llm_sandbox.models import ExecResult, FileEntry
from llm_sandbox.providers.base import PathNotFound, SandboxError, SessionNotFound

AUTH = {"Authorization": "Bearer test-token"}


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.sessions: set[str] = set()

    async def startup(self): pass
    async def shutdown(self): pass
    async def preflight(self): pass

    async def create(self, *, image, timeout_seconds, network, memory_mb, cpus):
        self.calls.append(("create", dict(image=image, timeout_seconds=timeout_seconds,
                                          network=network, memory_mb=memory_mb, cpus=cpus)))
        sid = uuid.uuid4().hex[:16]
        self.sessions.add(sid)
        return sid

    async def destroy(self, sid):
        self.calls.append(("destroy", sid))
        self.sessions.discard(sid)

    async def exec(self, sid, command, *, timeout_seconds, workdir=None):
        self.calls.append(("exec", dict(sid=sid, command=command,
                                        timeout_seconds=timeout_seconds, workdir=workdir)))
        return ExecResult(stdout="ok", stderr="", exit_code=0, duration_ms=1)

    async def run_script(self, sid, code, *, interpreter, ext, timeout_seconds):
        self.calls.append(("run_script", dict(sid=sid, code=code, interpreter=interpreter, ext=ext,
                                              timeout_seconds=timeout_seconds)))
        return ExecResult(stdout="42", stderr="", exit_code=0, duration_ms=1)

    async def live_session_ids(self):
        self.calls.append(("live_session_ids", None))
        return set(self.sessions)

    async def write_file(self, sid, path, content, *, encoding="utf-8"):
        self.calls.append(("write_file", dict(sid=sid, path=path, content=content,
                                              encoding=encoding)))

    async def read_file(self, sid, path, *, max_bytes):
        self.calls.append(("read_file", dict(sid=sid, path=path, max_bytes=max_bytes)))
        return "data", "utf-8", False

    async def list_files(self, sid, path):
        return [FileEntry(name="a", path=f"{path}/a", is_dir=False, size=1)], True

    def last(self, kind):
        return [args for k, args in self.calls if k == kind][-1]


@pytest.fixture
def client(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(appmod, "provider", fake)
    monkeypatch.setattr(appmod, "slots", appmod.SessionSlots(appmod.cfg.max_sessions,
                                                             recount=fake.live_session_ids))
    with TestClient(appmod.app) as c:
        c.fake = fake
        yield c


def create(client, body=None):
    r = client.post("/sessions", json=body or {}, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


# --- auth ----------------------------------------------------------------------------------

def test_auth_required(client):
    assert client.post("/sessions", json={}).status_code == 401
    assert client.post("/sessions", json={}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/sessions", json={}, headers=AUTH).status_code == 200


async def test_auth_compare_survives_a_non_ascii_header():
    """The str form of compare_digest raises TypeError on non-ASCII header values (→ 500)."""
    with pytest.raises(appmod.HTTPException) as e:
        await appmod._auth("Bearer t\xe9st")
    assert e.value.status_code == 401


def test_health_is_open_but_docs_are_off(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


# --- bounds on caller-supplied values -------------------------------------------------------

def test_session_timeout_is_clamped_and_rejected_when_nonpositive(client):
    create(client, {"timeout_seconds": 10**12})
    assert client.fake.last("create")["timeout_seconds"] == 100  # SANDBOX_MAX_SESSION_SECONDS
    assert client.post("/sessions", json={"timeout_seconds": 0}, headers=AUTH).status_code == 422
    assert client.post("/sessions", json={"timeout_seconds": -5}, headers=AUTH).status_code == 422


def test_resources_are_clamped_and_nan_is_rejected(client):
    create(client, {"memory_mb": 10**9, "cpus": 64})
    c = client.fake.last("create")
    assert (c["memory_mb"], c["cpus"]) == (1024, 2.0)
    r = client.post("/sessions", content=b'{"cpus": NaN}',
                    headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 422           # the default handler would echo NaN and 500 here
    assert "input" not in r.json()["detail"][0]


def test_exec_and_run_timeouts_are_clamped(client):
    sid = create(client)
    client.post(f"/sessions/{sid}/exec", json={"command": "true", "timeout_seconds": 10**6}, headers=AUTH)
    assert client.fake.last("exec")["timeout_seconds"] == 30  # SANDBOX_MAX_EXEC_SECONDS
    r = client.post(f"/sessions/{sid}/run", json={"language": "python", "code": "1", "timeout_seconds": 999},
                    headers=AUTH)
    assert r.json()["stdout"] == "42"
    run = client.fake.last("run_script")
    assert (run["timeout_seconds"], run["interpreter"], run["ext"], run["code"]) == (30, "python3", "py", "1")
    assert not [c for c in client.fake.calls if c[0] == "write_file"]  # one round-trip, no write
    r = client.post(f"/sessions/{sid}/exec", json={"command": "true", "timeout_seconds": -1}, headers=AUTH)
    assert r.status_code == 422


def test_session_id_must_look_like_ours(client):
    """Anything else would be spliced into a docker argv or an apiserver URL path."""
    for bad in ("x", "ZZZZZZZZZZZZZZZZ", "0123456789abcdef0", "a%3Fb%3Dc0000000"):
        assert client.post(f"/sessions/{bad}/exec", json={"command": "true"},
                           headers=AUTH).status_code == 422, bad
        assert client.delete(f"/sessions/{bad}", headers=AUTH).status_code == 422, bad
    assert client.fake.calls == []


def test_read_file_max_bytes_must_be_positive(client):
    """`head -c -N` means "all but the last N bytes" — a negative cap is no cap."""
    sid = create(client)
    for mb in (0, -1, -2):
        r = client.get(f"/sessions/{sid}/files", params={"path": "/x", "max_bytes": mb}, headers=AUTH)
        assert r.status_code == 422, mb
    r = client.get(f"/sessions/{sid}/files", params={"path": "/x", "max_bytes": 10}, headers=AUTH)
    assert r.status_code == 200
    assert client.fake.last("read_file")["max_bytes"] == 10
    assert client.get(f"/sessions/{sid}/files", params={"path": ""}, headers=AUTH).status_code == 422


def test_write_file_rejects_malformed_base64_and_empty_path(client):
    sid = create(client)
    r = client.put(f"/sessions/{sid}/files", json={"path": "/f", "content": "not base64!", "encoding": "base64"},
                   headers=AUTH)
    assert r.status_code == 422
    r = client.put(f"/sessions/{sid}/files", json={"path": "/f", "content": "aGk=", "encoding": "base64"},
                   headers=AUTH)
    assert r.status_code == 200
    r = client.put(f"/sessions/{sid}/files", json={"path": "", "content": "x"}, headers=AUTH)
    assert r.status_code == 422


def test_oversized_body_is_rejected_with_413(client):
    sid = create(client)
    big = {"path": "/f", "content": "x" * 5000}      # > SANDBOX_MAX_REQUEST_BYTES (4096)
    r = client.put(f"/sessions/{sid}/files", json=big, headers=AUTH)
    assert r.status_code == 413
    assert "SANDBOX_MAX_REQUEST_BYTES" in r.text
    assert not [c for c in client.fake.calls if c[0] == "write_file"]
    # A body that lies about (or omits) Content-Length is counted as it streams.
    r = client.put(f"/sessions/{sid}/files", content=iter([b'{"path":"/f","content":"' , b"x" * 5000, b'"}']),
                   headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 413


# --- slot accounting ------------------------------------------------------------------------

def test_session_cap_returns_429_and_delete_frees_a_slot(client):
    a = create(client)
    create(client)
    r = client.post("/sessions", json={}, headers=AUTH)
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "5"
    assert client.delete(f"/sessions/{a}", headers=AUTH).status_code == 200
    create(client)


def test_slots_resync_with_the_backend_when_the_cap_is_hit(client):
    """A DELETE served by a sibling replica never reaches this replica's release()."""
    a = create(client)
    create(client)
    client.fake.sessions.discard(a)                       # destroyed "via another replica"
    assert not [c for c in client.fake.calls if c[0] == "live_session_ids"]  # never off the 429 path
    create(client)                                       # would have been a 429
    assert [c for c in client.fake.calls if c[0] == "live_session_ids"]
    assert client.post("/sessions", json={}, headers=AUTH).status_code == 429  # genuinely full now


# --- image allowlist ------------------------------------------------------------------------

def test_image_override_is_an_allowlist(client, monkeypatch):
    monkeypatch.setattr(appmod.cfg, "allowed_images", ["registry/extra:1"])
    create(client, {"image": appmod.cfg.default_image})
    assert client.fake.last("create")["image"] == appmod.cfg.default_image
    create(client, {"image": "registry/extra:1"})
    assert client.fake.last("create")["image"] == "registry/extra:1"
    r = client.post("/sessions", json={"image": "evil/thing:latest"}, headers=AUTH)
    assert r.status_code == 400
    assert "SANDBOX_ALLOWED_IMAGES" in r.json()["detail"]
    r = client.post("/sessions", json={"image": "--privileged"}, headers=AUTH)
    assert r.status_code == 400
    assert len([c for c in client.fake.calls if c[0] == "create"]) == 2


def test_no_image_means_the_default(client):
    create(client)
    assert client.fake.last("create")["image"] == appmod.cfg.default_image


def test_list_files_reports_truncation(client):
    sid = create(client)
    r = client.get(f"/sessions/{sid}/files/list", params={"path": "/workspace"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["truncated"] is True
    assert r.json()["entries"][0]["name"] == "a"


# --- status mapping --------------------------------------------------------------------------

def test_provider_errors_become_the_right_status(client, monkeypatch):
    sid = create(client)

    async def gone(*_a, **_k):
        raise SessionNotFound(sid)

    async def no_file(*_a, **_k):
        raise PathNotFound("head: /nope: No such file or directory")

    async def broken(*_a, **_k):
        raise SandboxError("sandbox pod not ready (Pending) — pod unschedulable?")

    monkeypatch.setattr(client.fake, "exec", gone)
    r = client.post(f"/sessions/{sid}/exec", json={"command": "true"}, headers=AUTH)
    assert (r.status_code, r.json()["detail"]) == (404, f"no such session: {sid}")

    monkeypatch.setattr(client.fake, "read_file", no_file)
    r = client.get(f"/sessions/{sid}/files", params={"path": "/nope"}, headers=AUTH)
    assert r.status_code == 404 and "No such file" in r.json()["detail"]

    monkeypatch.setattr(client.fake, "create", broken)
    r = client.post("/sessions", json={}, headers=AUTH)
    assert r.status_code == 502 and "unschedulable" in r.json()["detail"]


def test_a_plain_runtime_error_is_still_a_500(monkeypatch):
    """Only provider failures get the 502 treatment."""
    fake = FakeProvider()

    async def bug(*_a, **_k):
        raise RuntimeError("internal")

    fake.create = bug
    monkeypatch.setattr(appmod, "provider", fake)
    monkeypatch.setattr(appmod, "slots", appmod.SessionSlots(0))
    with TestClient(appmod.app, raise_server_exceptions=False) as c:
        assert c.post("/sessions", json={}, headers=AUTH).status_code == 500
