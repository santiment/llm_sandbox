# The sandbox RUNTIME image — what untrusted agent code actually executes inside (under
# gVisor/runsc in prod). NOT the service image (that's ./Dockerfile). Reference it via
# SANDBOX_IMAGE; on k8s push it to a registry the cluster can pull from.
#
#   docker build -f sandbox.Dockerfile -t llm-sandbox-runtime:latest .
#
# Python-only by design: python3 + pandas/numpy + the shell toolchain (bash/awk/sed/grep/
# coreutils) so the agent can store files and manipulate them with any shell command.
# Libraries are preinstalled because sandboxes default to NO network (no PyPI at runtime).
# No node/npm and no extra data libs — keeps the image small (faster cold pull on a node).
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash gawk sed grep coreutils findutils jq ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Run as root INSIDE the sandbox so agent-written files can land at any path (/home/user,
# /large_tool_results, /workspace, …). The isolation boundary is gVisor AROUND the
# container, not the in-container uid — the same model E2B/Firecracker sandboxes use.
RUN mkdir -p /workspace
WORKDIR /workspace

# Preinstall the default analysis libs so LLM-written code works offline.
RUN pip install --no-cache-dir pandas numpy

# Containers are started with `sleep <timeout>` by the provider; this is just a safe default.
CMD ["sleep", "3600"]
