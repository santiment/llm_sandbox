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

**Recommended — `./run.sh`.** It builds the runtime image if missing, refuses to start when the
configured runtime is not registered with the daemon, and tells you plainly whether you are
actually sandboxed:

| command | what it does |
|---|---|
| `./run.sh` | check isolation, build if needed, run the service on `:8900` |
| `./run.sh --rebuild` | force-rebuild the runtime image first |
| `./run.sh doctor` | what isolation **this** machine can provide, and how to get gVisor if it can't |
| `./run.sh smoke` | create → run python → delete, against a running service |
| `./run.sh verify` | **prove** per-session isolation against a running service (below) |
| `./run.sh clean` | remove stray `llmsbx_*` containers left by a crashed run |

`verify` is the one worth knowing. It creates two sessions and empirically checks the claims
this README makes — one container each, the runtime actually backing them, that session B
cannot read session A's files, that A's files survive across `exec` calls, `NetworkMode=none`
plus a live outbound-connect probe, that `{"memory_mb":256,"cpus":0.5}` really became
`Memory=268435456`/`NanoCpus=500000000`/a pids cap, that no `LLM_SANDBOX_TOKEN`/`AWS_`/
`KUBERNETES_` vars are visible inside the session, and that `DELETE` removes the container.

It separates two failures that are easy to confuse. A ✗ means the **service** is broken and
exits non-zero. A ⚠ on the runtime line means the *plumbing* is right but your host gave you
`runc`, so there is no security boundary — every other guarantee still holds, and it exits 0.
See [gVisor / runsc](#gvisor--runsc) for why that is the normal outcome on a Mac.

Manual steps below.

```bash
# 1. Build the RUNTIME image (what code executes inside — python3 + pandas/numpy + shell tools)
docker build -f sandbox.Dockerfile -t llm-sandbox-runtime:latest .

# 2. Install the service and run it (uv)
uv sync --no-dev                # creates .venv + uv.lock from pyproject (drop the flag for tests)
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
pinned to a dedicated sandbox instance group. The cluster contract is three values —
RuntimeClass `gvisor`, nodeSelector `kops.k8s.io/instancegroup=ai-sandbox`, toleration
`dedicated=ai-sandbox:NoSchedule`. They are the provider's defaults and all three are
env-tunable via `SANDBOX_K8S_*`, so a cluster that names things differently needs no code
change.

**Requires Kubernetes ≥ 1.30.** The provider drives the apiserver directly and needs the
`v5.channel.k8s.io` exec subprotocol, whose stdin half-close is what lets `PUT .../files`
stream a payload and still read back an exit code. On older clusters writes fail with a
clear error instead of hanging; exec and reads still work.

```bash
# 1. Build & push BOTH images. Use an IMMUTABLE tag, never `latest`: session pods pull with
#    IfNotPresent, so a moving tag goes stale on warm nodes and a rollback stops being a
#    one-line edit. --platform matters when building on an Apple-silicon laptop for amd64 nodes.
docker build --platform linux/amd64 -t <registry>/llm-sandbox:v0.1.0 .                     # service
docker build --platform linux/amd64 -f sandbox.Dockerfile \
             -t <registry>/llm-sandbox-runtime:v0.1.0 .                                    # runtime
docker push <registry>/llm-sandbox:v0.1.0 && docker push <registry>/llm-sandbox-runtime:v0.1.0

# 2. Point the manifests at your images (the two CHANGEME lines in k8s/deployment.yaml)

# 3. Namespace and the token FIRST — the Deployment mounts that Secret and will not start
#    without it.
kubectl apply -f k8s/namespace.yaml
kubectl -n llm-sandbox create secret generic llm-sandbox \
        --from-literal=token=$(openssl rand -hex 32)

# 4. The rest. (`k8s/examples/` is deliberately outside this list — it holds a placeholder
#    Secret for reference, and applying it would overwrite the real token above.)
kubectl apply -f k8s/rbac.yaml   -f k8s/networkpolicy.yaml -f k8s/quota.yaml \
              -f k8s/pdb.yaml    -f k8s/service.yaml       -f k8s/deployment.yaml

# 5. Optional: kubectl apply -f k8s/hpa.yaml                        # CPU autoscale 2→6
#              kubectl apply -f k8s/networkpolicy-client-ingress.yaml
#    The latter restricts who may CALL the service to namespaces labelled
#    llm-sandbox/client=true — it will cut off unlabelled callers, so label them first.
```

**Verify on your cluster before trusting the egress policy** (both are called out inline in
`k8s/networkpolicy.yaml`): that the `except:` list covers your pod/service CIDRs — kops uses
`nonMasqueradeCIDR: 100.64.0.0/10`, which is *not* RFC1918 and is easy to miss — and that
CoreDNS actually carries the `k8s-app: kube-dns` label the DNS rule selects.

Operational notes:

- **Session create latency:** ~1–3 s when the runtime image is cached on the sandbox node;
  the first pull after a node rotation takes tens of seconds (image ≈ 240 MB). A pre-pull
  DaemonSet on the ai-sandbox group removes even that.
- **Memory:** session pods request 64 Mi (dense packing) and are hard-capped at the
  caller's `memory_mb` (default 512 Mi — enough for pandas), itself clamped to
  `SANDBOX_MAX_MEMORY_MB`. The service pod holds ~100 Mi steady-state: it speaks HTTP +
  WebSocket to the apiserver in-process and **ships no `kubectl`**, so it forks nothing per
  request. That is what keeps its limit at 512 Mi and its image at ~77 MB.
- **Egress:** `k8s/networkpolicy.yaml` is the analogue of docker's `--network none`:
  default-deny for all session pods; `network:true` sessions get DNS + public internet only
  (VPC ranges + cloud metadata blocked). **Requires a CNI that enforces NetworkPolicy** —
  verify on the cluster, otherwise it is silently inert.
- **Quotas:** `k8s/quota.yaml` caps the namespace at 50 pods and bounds per-pod cpu/memory.
  It is sized to match `SANDBOX_MAX_SESSIONS` × replicas (24 × 2 session pods + 2 service
  pods) — **raise both together**, or creates will start failing at the admission layer.
- **Pids:** unlike docker's `--pids-limit`, per-pod pid caps come from the kubelet
  (`podPidsLimit`) on the sandbox nodes.
- **Probes:** `/healthz` (liveness) is shallow on purpose; `/readyz` (readiness) checks the
  apiserver and the RBAC, so a missing Role drains the pod from the Service instead of
  restart-looping it. A failed preflight is visible in the `/readyz` body.
- The service's ServiceAccount can only manage pods in its own namespace (`k8s/rbac.yaml`);
  session pods themselves get **no** service-account token.

## gVisor / runsc

gVisor (`runsc`) is the security boundary for untrusted LLM-written code.

- **Kubernetes (prod):** the cluster provides it — RuntimeClass `gvisor` on the dedicated
  node group; the k8s provider sets `runtimeClassName` on every session pod.
- **Docker on Linux (local/EC2):** install `runsc`, register it as a Docker runtime, keep
  `SANDBOX_DOCKER_RUNTIME=runsc`.
- **Dev on macOS:** Docker Desktop **cannot** host `runsc` — its LinuxKit VM ships no `runsc`
  binary and offers no durable way to add one. Two honest options:
  - *Plumbing only:* `SANDBOX_DOCKER_RUNTIME=runc`. Everything works and nothing is isolated.
    `./run.sh` prints a NO ISOLATION banner and `./run.sh verify` ends on a ⚠. Never point
    untrusted LLM code at this.
  - *Real gVisor:* run Docker inside a Linux VM you control, then install `runsc` in it —
    `./run.sh doctor` prints the exact Colima commands. Same for a Linux host or EC2 box.
- **Stronger isolation (microVM):** a Kata/Firecracker provider on a `*.metal` host is the
  planned next backend for stronger per-workload isolation.

## Security posture

- No egress by default (`--network none` under docker; deny-all NetworkPolicy under k8s).
  Set `network:true` per session only when needed.
- Memory / CPU limits per session; pids capped (docker flag / kubelet `podPidsLimit`);
  output byte-capped (`SANDBOX_MAX_OUTPUT_BYTES`).
- Session **count** capped per replica (`SANDBOX_MAX_SESSIONS`, default 24 → 429 when full),
  so a create loop can't pin the node group; on k8s the namespace ResourceQuota
  (`k8s/quota.yaml`) enforces the same ceiling cluster-side.
- Ephemeral: a session is one container/pod, destroyed on `DELETE` or auto-reaped after
  `timeout_seconds`. Never reuse a session across users/tasks.
- Bearer auth (`LLM_SANDBOX_TOKEN`) between callers and the service.
- On k8s: session pods mount no ServiceAccount token and the service's RBAC is
  namespace-scoped.

## Repo layout

```
src/llm_sandbox/
  app.py              HTTP layer: auth, validation, session-slot accounting, logging
  config.py           every knob, read from env (Config.from_env)
  models.py           request/response schemas (pydantic)
  providers/
    base.py           SandboxProvider Protocol + SessionOpsMixin + clamp_resources
    gvisor.py         docker CLI + runsc — local/EC2
    k8s.py            one gVisor pod per session, direct kube-apiserver calls
Dockerfile            SERVICE image (alpine, multi-stage; targets: prod, dev)
sandbox.Dockerfile    RUNTIME image — what untrusted code executes in (debian slim)
k8s/                  manifests; k8s/examples/ is reference-only, never `kubectl apply -f k8s/`
tests/                pytest; no cluster, daemon, or network required
run.sh                dev bring-up, isolation doctor/verify, smoke check, cleanup
```

## Configuration

All config is env-driven (`config.py`); `.env.example` is the annotated template. On k8s these
are set in `k8s/deployment.yaml`, not in a `.env`.

| Variable | Default | What it does |
|---|---|---|
| `SANDBOX_PROVIDER` | `gvisor` | `gvisor` (docker) or `k8s` (pod-per-session) |
| `LLM_SANDBOX_TOKEN` | — | Bearer token callers must send. **Empty disables auth — dev only** |
| `SANDBOX_IMAGE` | `llm-sandbox-runtime:latest` | Runtime image. On k8s: registry ref, immutable tag |
| `SANDBOX_DOCKER_RUNTIME` | `runsc` | gvisor provider only. `runc` = no isolation, dev only |
| `SANDBOX_MAX_OUTPUT_BYTES` | `1000000` | Cap on any stdout/stderr/file payload returned |
| `SANDBOX_MAX_MEMORY_MB` | `4096` | Ceiling on a caller's `memory_mb` (clamped, not rejected) |
| `SANDBOX_MAX_CPUS` | `2` | Ceiling on a caller's `cpus` (clamped, not rejected) |
| `SANDBOX_MAX_CONCURRENCY` | `32` | In-flight backend ops across all sessions |
| `SANDBOX_MAX_SESSIONS` | `24` | Live sessions **per replica** before 429; `0` = unlimited |
| `SANDBOX_LOG_PAYLOADS` | `1` | Log command/code bodies. **Set `0` in prod** — untrusted content |
| `SANDBOX_K8S_NAMESPACE` | *(own)* | Namespace for session pods |
| `SANDBOX_K8S_RUNTIME_CLASS` | `gvisor` | Empty ⇒ service refuses to start (see below) |
| `SANDBOX_K8S_NODE_SELECTOR` | `kops.k8s.io/instancegroup=ai-sandbox` | `k=v[,k=v…]` |
| `SANDBOX_K8S_TOLERATION` | `dedicated=ai-sandbox:NoSchedule` | `key=value:Effect`; empty = none |
| `SANDBOX_K8S_CREATE_TIMEOUT` | `120` | Seconds to wait for a session pod to become Ready |
| `SANDBOX_K8S_IMAGE_PULL_SECRETS` | — | `name[,name…]` for a private registry |
| `SANDBOX_K8S_REAP_INTERVAL` | `120` | Seconds between sweeps for finished session pods |
| `SANDBOX_K8S_ALLOW_NO_RUNTIME_CLASS` | `0` | Opt-in to running sessions **without gVisor** |

Two settings fail loudly rather than degrading quietly:

- **Empty `SANDBOX_K8S_RUNTIME_CLASS`** would put untrusted code on the cluster's default
  runtime (runc) with no isolation boundary. The service refuses to start unless you set
  `SANDBOX_K8S_ALLOW_NO_RUNTIME_CLASS=1` to say you meant it.
- **`SANDBOX_PROVIDER=k8s` outside a cluster** exits with a message naming the cause rather
  than failing on the first request.

## Providers

`SANDBOX_PROVIDER` picks the backend: `gvisor` (docker CLI, local/EC2) or `k8s`
(pod-per-session, driven by direct kube-apiserver calls — `httpx` for the REST verbs, a
WebSocket for exec). The provider layer (`providers/`) is a `Protocol` (`base.py`) behind a
factory (`providers/__init__.py`), so a new backend is a single file + one factory branch —
the HTTP API and every caller stay the same. The four in-session primitives (`exec`,
`write_file`, `read_file`, `list_files`) are implemented once in `SessionOpsMixin` on top of
a single "run this argv in the session" hook, so file semantics can't drift between backends.

## Tests

```bash
uv sync && uv run pytest        # no cluster, no daemon, no network
```

`tests/test_k8s_provider.py` covers the k8s provider against local fakes: the REST verbs
against a scripted apiserver, and `exec` against a real local WebSocket server that speaks
the actual `v5.channel.k8s.io` framing (channel-prefixed frames, the stdin close frame, the
`Status` carrying the exit code). Those tests exist because that transport replaced
`kubectl exec` and a live cluster is the only other place it runs.

## Client integration

Clients reach the service over the HTTP API above and enable it **opt-in**, e.g. gated behind a
`LLM_SANDBOX_URL` env var. With that unset a client falls back to its own in-process execution
and leaves its `execute` tool disabled, so turning the sandbox on is a deliberate switch. The
API is client-agnostic — any backend or agent that speaks the endpoints above can use it.

The expected lifecycle, and what the agent repo's `run-stack.sh` brings up locally:

```
agent run starts
  └─ first execute/file op  → POST /sessions          → ONE container for this run
     every later call       → /sessions/<id>/{exec,run,files}   same container, /workspace persists
  run ends (cleanup hook)   → DELETE /sessions/<id>   → container gone
```

One session per run, created lazily and destroyed at the end — never shared across runs or
users. On k8s the same flow allocates a gVisor **pod** instead of a container; nothing in the
client changes.

`run-stack.sh` in the agent repo starts both halves wired together: it checks that
`LLM_SANDBOX_TOKEN` matches on both sides (a mismatch otherwise surfaces as a 401 on the
agent's first tool call, mid-run), starts this service, relays its isolation verdict, then
runs the agent — tearing the sandbox down and reaping stray session containers on exit.

## Status

`GvisorProvider` is complete and verified end-to-end against a local docker daemon — every
per-session guarantee in [Security posture](#security-posture) is machine-checked by
`./run.sh verify`. It has only ever been exercised under `runc`, though: the `runsc` code path
is one flag, but the gVisor **boundary** itself is untested here (see gVisor / runsc above).

`K8sProvider` is complete and covered by `tests/test_k8s_provider.py`, but its fakes are
written from the apiserver spec — it has **never run against a real cluster**. Treat the
first deploy as the real test:

```bash
kubectl -n llm-sandbox rollout status deploy/llm-sandbox      # readiness gates on /readyz,
kubectl -n llm-sandbox logs deploy/llm-sandbox                # so a stall here names the cause
kubectl -n llm-sandbox port-forward svc/llm-sandbox 8900:8900 &
LLM_SANDBOX_TOKEN=$(kubectl -n llm-sandbox get secret llm-sandbox \
  -o jsonpath='{.data.token}' | base64 -d) ./run.sh smoke     # create → run python → delete
```

(`run.sh smoke` talks to `localhost:$PORT`, hence the port-forward.)

The likeliest first failures are environmental, not logical — RBAC, an unpullable runtime
image, a cluster below 1.30, or a CNI that ignores NetworkPolicy. Each surfaces as a named
error rather than a generic 500.
