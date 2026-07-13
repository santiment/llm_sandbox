# llm-sandbox

A **provider-agnostic code-execution sandbox** sidecar. The backend and the research agent
call **one HTTP interface** to store files and run shell / Python on them; underneath, a
swappable provider runs the code in **gVisor**. The provider layer is pluggable, so switching
the isolation backend is a server-side env change — callers don't change.

```
 backend ─┐
          ├──HTTP──▶  llm-sandbox service  ──▶  SandboxProvider
 agent  ──┘            (this app, FastAPI)        ├─ GvisorProvider  (docker + runsc, local/EC2)
                                                  └─ K8sProvider     (one gVisor pod per session)
                                                            │
                                                   one container/pod per session
                                                   (files persist across calls)
```

## Capabilities → endpoints

| You want to… | Endpoint |
|---|---|
| **store files** | `PUT /sessions/{id}/files` `{path, content, encoding}` |
| **read / manipulate with awk/sed/bash/any shell** | `GET /sessions/{id}/files` + `POST /sessions/{id}/exec` `{command}` |
| **run python** | `POST /sessions/{id}/run` `{language:"python", code}` |
| list files | `GET /sessions/{id}/files/list?path=` |
| lifecycle | `POST /sessions`, `DELETE /sessions/{id}` |

The sandbox is **python-only by design** (python3 + pandas/numpy preinstalled + the shell
toolchain); there is no node runtime in the image.

A **session = one persistent workspace** (`/workspace`): files you write survive across
`exec`/`run` until you `DELETE` the session. `run` is composed on `write_file`+`exec`, so it
behaves identically on every provider.

## Quickstart (local)

**Easiest — docker compose:**

```bash
docker compose up --build     # builds both images, service on :8900
```

Then point your agent/backend at `http://localhost:8900` with
`Authorization: Bearer change-me` (override via `LLM_SANDBOX_TOKEN` env). The service drives
the host docker daemon through the mounted socket and spawns one sibling container per
session — same plumbing as prod, but under `runc`: **no isolation on a dev box**.

**Alternative — on the host:** `./run.sh` builds the runtime image (if missing) and starts the service;
`./run.sh --rebuild` forces an image rebuild; `./run.sh smoke` runs a create→run→delete check
against a running service. Manual steps below.

```bash
# 1. Build the RUNTIME image (what code executes inside — python3 + pandas/numpy + shell tools)
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
     -d '{"language":"python","code":"import pandas as pd;print(pd.read_csv(\"data.csv\").b.sum())"}'
curl -s -XDELETE localhost:8900/sessions/$SID -H "$T"   # no body → no Content-Type needed
```

## Deploy on Kubernetes (production)

On k8s the service runs as a normal Deployment (trusted, ordinary nodes) and
`SANDBOX_PROVIDER=k8s` makes every session **one pod under the `gvisor` RuntimeClass**,
pinned to the dedicated `ai-sandbox` instance group (see the devops doc *"Add gVisor to
stage kubernetes cluster"* — RuntimeClass `gvisor`, nodeSelector
`kops.k8s.io/instancegroup=ai-sandbox`, toleration `dedicated=ai-sandbox:NoSchedule`; all
three are the provider's defaults and env-tunable via `SANDBOX_K8S_*`).

```bash
# 1. Build & push BOTH images (pick your registry)
docker build -t  <registry>/llm-sandbox:<tag> .                              # service
docker build -f sandbox.Dockerfile -t <registry>/llm-sandbox-runtime:<tag> . # runtime
docker push <registry>/llm-sandbox:<tag> && docker push <registry>/llm-sandbox-runtime:<tag>

# 2. Point the manifests at your images (the two CHANGEME lines in k8s/deployment.yaml)

# 3. Apply
kubectl apply -f k8s/namespace.yaml -f k8s/rbac.yaml -f k8s/networkpolicy.yaml \
              -f k8s/service.yaml   -f k8s/deployment.yaml
kubectl -n llm-sandbox create secret generic llm-sandbox \
        --from-literal=token=$(openssl rand -hex 32)
```

Operational notes:

- **Session create latency:** ~1–3 s when the runtime image is cached on the sandbox node;
  the first pull after a node rotation takes tens of seconds (image ≈ 350 MB). A pre-pull
  DaemonSet on the ai-sandbox group removes even that.
- **Memory:** session pods request 64 Mi (dense packing) and are hard-capped at the
  caller's `memory_mb` (default 512 Mi — enough for pandas).
- **Egress:** `k8s/networkpolicy.yaml` is the analogue of docker's `--network none`:
  default-deny for all session pods; `network:true` sessions get DNS + public internet only
  (VPC ranges + cloud metadata blocked). **Requires a CNI that enforces NetworkPolicy** —
  verify on the cluster, otherwise it is silently inert.
- **Pids:** unlike docker's `--pids-limit`, per-pod pid caps come from the kubelet
  (`podPidsLimit`) on the sandbox nodes.
- The service's ServiceAccount can only manage pods in its own namespace (`k8s/rbac.yaml`);
  session pods themselves get **no** service-account token.

## gVisor / runsc

gVisor (`runsc`) is the security boundary for untrusted LLM-written code.

- **Kubernetes (prod):** the cluster provides it — RuntimeClass `gvisor` on the dedicated
  node group; the k8s provider sets `runtimeClassName` on every session pod.
- **Docker on Linux (local/EC2):** install `runsc`, register it as a Docker runtime, keep
  `SANDBOX_DOCKER_RUNTIME=runsc`.
- **Dev on macOS:** you can't run `runsc` natively — set `SANDBOX_DOCKER_RUNTIME=runc` to test
  the plumbing (this gives **no isolation** — never for untrusted code in prod).
- **Stronger isolation (microVM):** a Kata/Firecracker provider on a `*.metal` host is the
  planned next backend for stronger per-workload isolation.

## Security posture

- No egress by default (`--network none` under docker; deny-all NetworkPolicy under k8s).
  Set `network:true` per session only when needed.
- Memory / CPU limits per session; pids capped (docker flag / kubelet `podPidsLimit`);
  output byte-capped (`SANDBOX_MAX_OUTPUT_BYTES`).
- Ephemeral: a session is one container/pod, destroyed on `DELETE` or auto-reaped after
  `timeout_seconds`. Never reuse a session across users/tasks.
- Bearer auth (`LLM_SANDBOX_TOKEN`) between callers and the service.
- On k8s: session pods mount no ServiceAccount token and the service's RBAC is
  namespace-scoped.

## Providers

`SANDBOX_PROVIDER` picks the backend: `gvisor` (docker CLI, local/EC2) or `k8s`
(pod-per-session via kubectl). The provider layer (`providers/`) is a `Protocol`
(`base.py`) behind a factory (`providers/__init__.py`), so a new backend is a single file +
one factory branch — the HTTP API and every caller stay the same.

## Client integration

Clients reach the service over the HTTP API above and enable it **opt-in**, e.g. gated behind a
`LLM_SANDBOX_URL` env var. With that unset a client falls back to its own in-process execution
and leaves its `execute` tool disabled, so turning the sandbox on is a deliberate switch. The
API is client-agnostic — any backend or agent that speaks the endpoints above can use it.

## Status

`GvisorProvider` is complete; `K8sProvider` is complete and syntax-checked but **not yet run
against a cluster** — first deploy should smoke-test with `./run.sh smoke` pointed at the
in-cluster URL.
