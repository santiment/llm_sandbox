# The SERVICE image — the FastAPI control plane that orchestrates sessions. NOT the sandbox
# runtime (that's sandbox.Dockerfile). Two targets:
#   prod (default) — for k8s: non-root, nothing but the app. One gVisor pod per session.
#   dev            — for docker compose: + docker CLI, runs as root to use the mounted
#                    /var/run/docker.sock and spawn sibling sandbox containers. DEV ONLY.
#
#   docker build --platform linux/amd64 -t llm-sandbox:v0.1.0 .   # prod — see README on arch
#   docker compose up --build                                     # dev (compose picks target: dev)
#
# Build/runtime split: the builder carries uv, the wheel cache and the project metadata; the
# final stage receives only the resolved virtualenv. Alpine over slim because nothing here
# needs glibc — every dependency is pure Python except pydantic-core, which ships musl wheels.
FROM python:3.11-alpine AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
# uv from PyPI (musl wheel); the ghcr.io/astral-sh/uv binary is glibc-linked and will not
# execute on Alpine.
RUN pip install --no-cache-dir uv

WORKDIR /app
# Dependencies from the lockfile (uv), project last — keeps the dep layer cache-stable.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev


# --- test: CI only (Jenkinsfile) — dev deps + the suite. Never shipped; `prod` stays the
# default target because it is the last stage in the file.
FROM build AS test
RUN uv sync --frozen
COPY tests ./tests
# The build stage never puts the venv on PATH (uv addresses it directly); pytest needs it.
ENV PATH="/app/.venv/bin:$PATH"
CMD ["pytest"]


# --- base: the venv and the app, no build tooling ------------------------------------------
FROM python:3.11-alpine AS base
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/src /app/src
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Explicit uid AND gid so the Deployment can pin runAsUser/runAsGroup to matching numbers.
RUN addgroup -g 10001 -S app && adduser -u 10001 -S -G app app

EXPOSE 8900
CMD ["uvicorn", "llm_sandbox.app:app", "--host", "0.0.0.0", "--port", "8900"]

# --- dev: gvisor(docker) provider against the host daemon via the mounted socket ---
FROM base AS dev
COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/docker
# stays root: needs /var/run/docker.sock — acceptable ONLY for local dev

# --- prod (default): non-root, no extra binaries ---
# The k8s provider speaks to the apiserver over HTTPS/websockets with the pod's
# service-account credentials (providers/k8s.py), so NO kubectl is baked in: no ~57 MB Go
# binary, no version skew to track against the cluster, and no build-time fetch of an
# unpinned upstream release into the image. Keep it that way — if some future provider
# needs a downloaded binary, verify its checksum in the same RUN that installs it.
FROM base AS prod
USER app
