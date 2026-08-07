#!/usr/bin/env bash
#
# One-command dev bring-up for the llm-sandbox service.
#   ./run.sh              build images if missing, check isolation, run the service
#   ./run.sh --rebuild    force-rebuild the runtime image, then run
#   ./run.sh doctor       report what isolation this machine can actually provide
#   ./run.sh smoke        create -> run python -> delete against a RUNNING service
#   ./run.sh verify       PROVE per-session isolation against a RUNNING service
#   ./run.sh clean        remove stray session containers left by crashed runs
#
# The service drives the HOST Docker daemon (it spawns one sandbox container per session), so
# it runs on the host, not in a container. Config comes from ./.env.
#
# ISOLATION: sessions are only sandboxed when the daemon runs them under gVisor (`runsc`).
# This script refuses to start with a runtime the daemon doesn't have, and warns loudly when
# you opt into plain `runc` — the same container plumbing with NO security boundary.
set -euo pipefail
cd "$(dirname "$0")"

# Load .env so this script and the service see the same config (the service also loads it).
if [ -f .env ]; then set -a; . ./.env || true; set +a; fi

IMAGE="${SANDBOX_IMAGE:-llm-sandbox-runtime:latest}"
RUNTIME="${SANDBOX_DOCKER_RUNTIME:-runsc}"
PORT="${PORT:-8900}"
BASE="http://localhost:${PORT}"
AUTH="Authorization: Bearer ${LLM_SANDBOX_TOKEN:-}"
CTYPE="Content-Type: application/json"

die()  { echo "error: $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

need_docker() {
  have docker || die "docker not found on PATH"
  docker info >/dev/null 2>&1 || die "Docker daemon not running — start Docker Desktop"
}

# Runtimes the daemon actually has registered, one per line.
# One registered runtime name per line. Ranging the map in the template beats parsing the
# JSON: the nested {"path":...} objects make a regex over keys report a runtime called "path".
runtimes() {
  docker info --format '{{range $k, $v := .Runtimes}}{{$k}}
{{end}}' 2>/dev/null | grep -v '^$'
}
has_runtime() { runtimes | grep -qx "$1"; }

gvisor_help() {
  cat <<'EOF'
  gVisor (runsc) is not registered with this Docker daemon.

  Docker Desktop on macOS cannot run it: the LinuxKit VM ships no runsc binary and gives you
  no persistent way to add one. For REAL gVisor on a Mac you need a Linux VM you control:

    brew install colima
    colima start --cpu 4 --memory 8
    colima ssh -- sudo sh -c '
      ARCH=$(uname -m); URL=https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}
      wget -q ${URL}/runsc ${URL}/containerd-shim-runsc-v1 -P /usr/local/bin
      chmod 755 /usr/local/bin/runsc /usr/local/bin/containerd-shim-runsc-v1
      runsc install && systemctl restart docker'
    docker context use colima

  On a Linux host/EC2: install runsc, register it, keep SANDBOX_DOCKER_RUNTIME=runsc.

  To develop WITHOUT isolation (plumbing only, never untrusted code):
    echo 'SANDBOX_DOCKER_RUNTIME=runc' >> .env
EOF
}

# Refuse to run under a runtime the daemon doesn't have; be loud about the insecure fallback.
check_isolation() {
  if ! has_runtime "$RUNTIME"; then
    echo "✗ SANDBOX_DOCKER_RUNTIME=$RUNTIME is not registered with the Docker daemon." >&2
    echo "  available: $(runtimes | tr '\n' ' ')" >&2
    echo >&2
    [ "$RUNTIME" = "runsc" ] && gvisor_help >&2
    exit 1
  fi
  if [ "$RUNTIME" = "runsc" ]; then
    echo "✓ isolation: gVisor (runsc) — sessions get a user-space kernel boundary"
  else
    cat <<EOF

  ┌─────────────────────────────────────────────────────────────────────────┐
  │  WARNING — NO ISOLATION                                                 │
  │  Sessions run under '$RUNTIME', not gVisor. That is ordinary container   │
  │  plumbing: a kernel exploit from sandboxed code reaches the host.       │
  │  Fine for developing the service. NEVER point untrusted LLM code at it. │
  │  './run.sh doctor' explains how to get real gVisor here.                │
  └─────────────────────────────────────────────────────────────────────────┘

EOF
  fi
}

build_if_missing() {
  if [ "${1:-}" = "force" ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "▶ building $IMAGE (python3 + pandas/numpy + shell tools)…"
    docker build -f sandbox.Dockerfile -t "$IMAGE" .
  else
    echo "▶ image $IMAGE present (./run.sh --rebuild to rebuild)"
  fi
}

wait_healthy() {
  for _ in $(seq 1 "${2:-40}"); do
    curl -fsS "$1/healthz" >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  return 1
}

# api METHOD PATH [json-body]
api() {
  if [ -n "${3:-}" ]; then
    curl -fsS -X "$1" "$BASE$2" -H "$AUTH" -H "$CTYPE" -d "$3"
  else
    curl -fsS -X "$1" "$BASE$2" -H "$AUTH"
  fi
}

case "${1:-up}" in

  doctor)
    echo "── llm-sandbox doctor ───────────────────────────────────────────"
    have docker && echo "✓ docker CLI     $(docker --version | cut -d, -f1)" \
                || echo "✗ docker CLI     missing"
    if docker info >/dev/null 2>&1; then
      echo "✓ docker daemon  $(docker info --format '{{.OperatingSystem}} / {{.Architecture}}')"
      echo "  runtimes:      $(runtimes | tr '\n' ' ')"
    else
      echo "✗ docker daemon  not running"
    fi
    have uv && echo "✓ uv             $(uv --version)" || echo "✗ uv             missing (https://docs.astral.sh/uv/)"
    have jq && echo "✓ jq" || echo "  jq             missing (needed by smoke/verify)"
    if docker image inspect "$IMAGE" >/dev/null 2>&1; then
      echo "✓ runtime image  $IMAGE ($(docker image inspect "$IMAGE" --format '{{.Size}}' | awk '{printf "%.0f MB", $1/1048576}'))"
    else
      echo "  runtime image  $IMAGE not built yet (./run.sh builds it)"
    fi
    [ -f .env ] && echo "✓ .env           present" || echo "  .env           missing (cp .env.example .env)"
    echo
    echo "  configured runtime: $RUNTIME"
    if has_runtime "$RUNTIME"; then
      if [ "$RUNTIME" = "runsc" ]; then
        echo "  VERDICT: REAL ISOLATION — sessions run under gVisor."
      else
        echo "  VERDICT: NO ISOLATION — '$RUNTIME' is plumbing only, dev use exclusively."
        echo
        gvisor_help
      fi
    else
      echo "  VERDICT: WILL NOT START — '$RUNTIME' is not registered."
      echo
      [ "$RUNTIME" = "runsc" ] && gvisor_help
    fi
    ;;

  smoke)
    have jq || die "jq not found (brew install jq)"
    wait_healthy "$BASE" 4 || die "service not responding at $BASE (start it: ./run.sh)"
    sid=$(api POST /sessions '{}' | jq -r .session_id)
    echo "session: $sid"
    api POST "/sessions/$sid/run" '{"language":"python","code":"print(6*7)"}'; echo
    api DELETE "/sessions/$sid" >/dev/null
    echo "✓ smoke ok"
    ;;

  verify)
    # Proves the properties a sandbox is supposed to have, against a RUNNING service. Every
    # check reads real container state or runs real code inside the session — nothing is
    # inferred from config.
    have jq || die "jq not found (brew install jq)"
    need_docker
    wait_healthy "$BASE" 4 || die "service not responding at $BASE (start it: ./run.sh)"
    fail=0; weak=0
    ok()  { echo "  ✓ $*"; }
    bad() { echo "  ✗ $*"; fail=1; }
    # Distinct from bad(): the plumbing is correct, the HOST just can't provide a boundary.
    # Conflating the two would make every Mac dev see a red "broken" that isn't about the code.
    warn(){ echo "  ⚠ $*"; weak=1; }

    echo "▶ creating two sessions…"
    a=$(api POST /sessions '{"memory_mb":256,"cpus":0.5}' | jq -r .session_id)
    b=$(api POST /sessions '{}' | jq -r .session_id)
    ca="llmsbx_$a"; cb="llmsbx_$b"
    echo "  A=$a  B=$b"

    echo "▶ one container per session"
    [ -n "$(docker ps -q -f "name=^${ca}$")" ] && ok "$ca is a live container" || bad "$ca missing"
    [ -n "$(docker ps -q -f "name=^${cb}$")" ] && ok "$cb is a live container" || bad "$cb missing"

    echo "▶ runtime actually backing the session"
    rt=$(docker inspect "$ca" --format '{{.HostConfig.Runtime}}')
    if [ "$rt" = "runsc" ]; then
      ok "runtime=$rt (gVisor boundary)"
    else
      warn "runtime=$rt — NOT gVisor: no security boundary, dev plumbing only"
    fi

    echo "▶ filesystem isolation between sessions"
    api PUT "/sessions/$a/files" '{"path":"/workspace/secret.txt","content":"session-A-only"}' >/dev/null
    leak=$(api POST "/sessions/$b/exec" '{"command":"cat /workspace/secret.txt 2>/dev/null || echo ABSENT"}' | jq -r .stdout | tr -d '[:space:]')
    [ "$leak" = "ABSENT" ] && ok "B cannot see A's file" || bad "LEAK: B read A's file ($leak)"
    mine=$(api POST "/sessions/$a/exec" '{"command":"cat /workspace/secret.txt"}' | jq -r .stdout | tr -d '[:space:]')
    [ "$mine" = "session-A-only" ] && ok "A's file persists across exec calls" || bad "A lost its own file"

    echo "▶ egress blocked by default (network:false)"
    netmode=$(docker inspect "$ca" --format '{{.HostConfig.NetworkMode}}')
    [ "$netmode" = "none" ] && ok "NetworkMode=none" || bad "NetworkMode=$netmode (expected none)"
    probe='python3 -c "import socket;socket.setdefaulttimeout(4);socket.create_connection((\"1.1.1.1\",53));print(\"REACHED\")" 2>&1 | tail -1'
    net=$(api POST "/sessions/$a/exec" "$(jq -nc --arg c "$probe" '{command:$c}')" | jq -r '.stdout + .stderr')
    case "$net" in
      *REACHED*) bad "network reachable from a network:false session!" ;;
      *)         ok "outbound connection refused" ;;
    esac

    echo "▶ resource caps applied from the request"
    mem=$(docker inspect "$ca" --format '{{.HostConfig.Memory}}')
    cpu=$(docker inspect "$ca" --format '{{.HostConfig.NanoCpus}}')
    pids=$(docker inspect "$ca" --format '{{.HostConfig.PidsLimit}}')
    [ "$mem" = "268435456" ] && ok "memory=256Mi as requested" || bad "memory=$mem bytes (expected 268435456)"
    [ "$cpu" = "500000000" ] && ok "cpus=0.5 as requested"      || bad "cpus=$cpu nanocpus (expected 500000000)"
    [ "${pids:-0}" -gt 0 ] 2>/dev/null && ok "pids-limit=$pids" || bad "pids-limit unset"

    echo "▶ no host/service credentials inside the session"
    envprobe='env | grep -ciE "LLM_SANDBOX_TOKEN|AWS_|KUBERNETES_" || true'
    envleak=$(api POST "/sessions/$a/exec" "$(jq -nc --arg c "$envprobe" '{command:$c}')" | jq -r .stdout | tr -d '[:space:]')
    [ "$envleak" = "0" ] && ok "no secrets in session env" || bad "$envleak secret-ish env vars visible"

    echo "▶ teardown removes the container"
    api DELETE "/sessions/$a" >/dev/null; api DELETE "/sessions/$b" >/dev/null
    sleep 1
    [ -z "$(docker ps -aq -f "name=^${ca}$")" ] && ok "$ca gone after DELETE" || bad "$ca survived DELETE"

    echo
    if [ "$fail" -ne 0 ]; then
      echo "✗ some checks FAILED — see above. This is a bug in the service, not your host."
      exit 1
    elif [ "$weak" -ne 0 ]; then
      echo "✓ every per-session guarantee holds — but runtime=$rt, so there is NO SANDBOX."
      echo "  The plumbing is right: separate containers, private filesystems, no egress,"
      echo "  enforced caps, no leaked credentials, clean teardown. Under '$rt' all of that"
      echo "  is enforced by the host kernel, which the sandboxed code is talking to directly."
      echo "  Run './run.sh doctor' for how to get real gVisor. Never point untrusted code here."
    else
      echo "✓ all isolation checks passed under gVisor (runtime=$rt) — this is the prod posture."
    fi
    ;;

  clean)
    need_docker
    ids=$(docker ps -aq -f "name=^llmsbx_" || true)
    if [ -z "$ids" ]; then
      echo "no stray session containers"
    else
      echo "$ids" | xargs docker rm -f >/dev/null
      echo "removed $(echo "$ids" | wc -l | tr -d ' ') stray session container(s)"
    fi
    ;;

  up|--rebuild)
    need_docker
    have uv || die "uv not found (https://docs.astral.sh/uv/)"
    check_isolation
    if [ "${1:-up}" = "--rebuild" ]; then build_if_missing force; else build_if_missing; fi
    echo "▶ starting llm-sandbox on ${BASE} (provider=${SANDBOX_PROVIDER:-gvisor}, runtime=${RUNTIME})…"
    echo "  each session = one '$IMAGE' container; './run.sh verify' proves it"
    exec uv run uvicorn llm_sandbox.app:app --host 0.0.0.0 --port "$PORT"
    ;;

  *)
    die "usage: ./run.sh [--rebuild|doctor|smoke|verify|clean]"
    ;;
esac
