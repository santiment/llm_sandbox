# llm-sandbox

A **provider-agnostic code-execution sandbox** sidecar. The backend and the research agent
call **one HTTP interface** to store files and run shell / Python / JavaScript on them;
underneath, a swappable provider runs the code in **gVisor** (self-hosted). The provider
layer is pluggable, so adding a stronger-isolation backend later is a server-side env change
— callers don't change.

```
 backend ─┐
          ├──HTTP──▶  llm-sandbox service  ──▶  SandboxProvider
 agent  ──┘            (this app, FastAPI)        └─ GvisorProvider  (docker + runsc)
                                                            │
                                                   one container per session
                                                   (files persist across calls)
```

## Capabilities → endpoints

| You want to… | Endpoint |
|---|---|
| **store files** | `PUT /sessions/{id}/files` `{path, content, encoding}` |
| **read / manipulate with awk/sed/bash/any shell** | `GET /sessions/{id}/files` + `POST /sessions/{id}/exec` `{command}` |
| **run python** | `POST /sessions/{id}/run` `{language:"python", code}` |
| **run javascript** | `POST /sessions/{id}/run` `{language:"javascript", code}` |
| list files | `GET /sessions/{id}/files/list?path=` |
| lifecycle | `POST /sessions`, `DELETE /sessions/{id}` |

A **session = one persistent workspace** (`/workspace`): files you write survive across
`exec`/`run` until you `DELETE` the session. `run` is composed on `write_file`+`exec`, so it
behaves identically on every provider.

## Quickstart (local)

**Shortcut:** `./run.sh` builds the runtime image (if missing) and starts the service;
`./run.sh --rebuild` forces an image rebuild; `./run.sh smoke` runs a create→run→delete check
against a running service. Manual steps below.

```bash
# 1. Build the RUNTIME image (what code executes inside — python3 + node + shell tools)
docker build -f sandbox.Dockerfile -t llm-sandbox-runtime:latest .

# 2. Install the service and run it (uv)
uv sync                         # creates .venv + uv.lock from pyproject
# create a .env: LLM_SANDBOX_TOKEN is the bearer callers send; on a non-Linux dev box also
# set SANDBOX_DOCKER_RUNTIME=runc (NOT isolated — plumbing only). All config is env-driven.
printf 'LLM_SANDBOX_TOKEN=change-me\n' > .env
uv run uvicorn llm_sandbox.app:app --host 0.0.0.0 --port 8900
```

```bash
# 3. Try it. POST/PUT bodies are JSON, so send the Content-Type header (FastAPI 422s without it).
T="Authorization: Bearer change-me"      # must match LLM_SANDBOX_TOKEN in .env
C="Content-Type: application/json"
SID=$(curl -s -XPOST localhost:8900/sessions -H "$T" -H "$C" -d '{}' | jq -r .session_id)
curl -s -XPUT localhost:8900/sessions/$SID/files -H "$T" -H "$C" \
     -d '{"path":"/workspace/data.csv","content":"a,b\n1,2\n3,4\n"}'
curl -s -XPOST localhost:8900/sessions/$SID/exec -H "$T" -H "$C" \
     -d '{"command":"awk -F, \"NR>1{s+=$2} END{print s}\" data.csv"}'   # → 6
curl -s -XPOST localhost:8900/sessions/$SID/run  -H "$T" -H "$C" \
     -d '{"language":"python","code":"import duckdb;print(duckdb.sql(\"select 1+1\").fetchone())"}'
curl -s -XDELETE localhost:8900/sessions/$SID -H "$T"   # no body → no Content-Type needed
```

## gVisor / runsc

gVisor (`runsc`) is the security boundary for untrusted LLM-written code. It needs **Linux**
(it's a container runtime) but **no bare metal / KVM** — it runs on a normal EC2 box. Install
`runsc`, register it as a Docker runtime, then keep `SANDBOX_DOCKER_RUNTIME=runsc`.

- **Dev on macOS:** you can't run `runsc` natively — set `SANDBOX_DOCKER_RUNTIME=runc` to test
  the plumbing (this gives **no isolation** — never for untrusted code in prod).
- **Stronger isolation (microVM):** a Kata/Firecracker provider on a `*.metal` host is the
  planned next backend for stronger per-workload isolation.

## Security posture

- `--network none` by default (no egress). Set `network:true` per session only when needed;
  in prod, allowlist just the specific host/IP the code must reach at the host firewall.
- Memory / CPU / pids limits per container; output byte-capped (`SANDBOX_MAX_OUTPUT_BYTES`).
- Ephemeral: a session is one container, destroyed on `DELETE` or auto-reaped after
  `timeout_seconds`. Never reuse a session across users/tasks.
- Bearer auth (`LLM_SANDBOX_TOKEN`) between callers and the service.

## Providers

`SANDBOX_PROVIDER=gvisor` is the only backend wired today. The provider layer (`providers/`)
is a `Protocol` (`base.py`) behind a factory (`providers/__init__.py`), so a new backend is a
single file + one factory branch — the HTTP API and every caller stay the same.

## Client integration

Clients reach the service over the HTTP API above and enable it **opt-in**, e.g. gated behind a
`LLM_SANDBOX_URL` env var. With that unset a client falls back to its own in-process execution
and leaves its `execute` tool disabled, so turning the sandbox on is a deliberate switch. The
API is client-agnostic — any backend or agent that speaks the endpoints above can use it.

## Status

Service is syntax-checked but **not yet run end-to-end** (needs Docker + `runsc` on Linux, or
`runc` on a Mac dev box). `GvisorProvider` is complete.
