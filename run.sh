#!/usr/bin/env bash
#
# One-command dev bring-up for the llm-sandbox service.
#   ./run.sh             build the runtime image if missing, then run the service
#   ./run.sh --rebuild   force-rebuild the runtime image, then run
#   ./run.sh smoke       quick create -> run python -> delete against a running service
#
# The service drives the HOST Docker daemon (it spawns sandbox containers), so it runs on the
# host, not in a container. It reads ./.env (set SANDBOX_DOCKER_RUNTIME=runc on macOS).
set -euo pipefail
cd "$(dirname "$0")"

# Load .env so this script and the service see the same config (the service also loads it).
if [ -f .env ]; then set -a; . ./.env || true; set +a; fi

IMAGE="${SANDBOX_IMAGE:-llm-sandbox-runtime:latest}"
PORT="${PORT:-8900}"

die() { echo "error: $*" >&2; exit 1; }

case "${1:-up}" in
  smoke)
    command -v jq >/dev/null || die "jq not found (brew install jq)"
    base="http://localhost:${PORT}"
    auth="Authorization: Bearer ${LLM_SANDBOX_TOKEN:-}"
    ctype="Content-Type: application/json"
    sid=$(curl -fsS -XPOST "$base/sessions" -H "$auth" -H "$ctype" -d '{}' | jq -r .session_id)
    echo "session: $sid"
    curl -fsS -XPOST "$base/sessions/$sid/run" -H "$auth" -H "$ctype" \
      -d '{"language":"python","code":"print(6*7)"}'; echo
    curl -fsS -XDELETE "$base/sessions/$sid" -H "$auth" >/dev/null
    echo "✓ smoke ok"
    ;;
  up|--rebuild)
    command -v docker >/dev/null || die "docker not found on PATH"
    docker info >/dev/null 2>&1 || die "Docker daemon not running — start Docker Desktop"
    command -v uv >/dev/null || die "uv not found (https://docs.astral.sh/uv/)"
    if [ "${1:-up}" = "--rebuild" ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
      echo "▶ building $IMAGE (python3 + node + shell tools)…"
      docker build -f sandbox.Dockerfile -t "$IMAGE" .
    else
      echo "▶ image $IMAGE present (./run.sh --rebuild to rebuild)"
    fi
    echo "▶ starting llm-sandbox on :$PORT (provider=${SANDBOX_PROVIDER:-gvisor}, runtime=${SANDBOX_DOCKER_RUNTIME:-runsc})…"
    exec uv run uvicorn llm_sandbox.app:app --host 0.0.0.0 --port "$PORT"
    ;;
  *)
    die "usage: ./run.sh [--rebuild|smoke]"
    ;;
esac
