# The SERVICE image — the FastAPI control plane that orchestrates sessions. NOT the sandbox
# runtime (that's sandbox.Dockerfile). Two targets:
#   prod (default) — for k8s: kubectl baked in, non-root. One gVisor pod per session.
#   dev            — for docker compose: + docker CLI, runs as root to use the mounted
#                    /var/run/docker.sock and spawn sibling sandbox containers. DEV ONLY.
#
#   docker build -t llm-sandbox:latest .            # prod
#   docker compose up --build                       # dev (compose picks target: dev)
FROM python:3.11-slim AS base

# Dependencies from the lockfile (uv), project last — keeps the dep layer cache-stable.
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

RUN useradd --system --uid 10001 app

EXPOSE 8900
CMD ["uvicorn", "llm_sandbox.app:app", "--host", "0.0.0.0", "--port", "8900"]

# --- dev: gvisor(docker) provider against the host daemon via the mounted socket ---
FROM base AS dev
COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/docker
# stays root: needs /var/run/docker.sock — acceptable ONLY for local dev

# --- prod (default): + kubectl for the k8s provider, non-root ---
FROM base AS prod
# Keep kubectl's minor version within ±1 of the cluster's; in-cluster service-account auth
# is automatic. Override: --build-arg KUBECTL_VERSION=…
ARG KUBECTL_VERSION=v1.33.4
ARG TARGETARCH
ADD --chmod=0755 \
    https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH:-amd64}/kubectl \
    /usr/local/bin/kubectl
USER app
