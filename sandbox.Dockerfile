# The sandbox RUNTIME image — what untrusted agent code actually executes inside (under
# gVisor/runsc in prod). NOT the service image. Build once and reference it via SANDBOX_IMAGE.
#
#   docker build -f sandbox.Dockerfile -t llm-sandbox-runtime:latest .
#
# Ships: python3 + node + the shell toolchain (bash/awk/sed/grep/coreutils) so the agent can
# store files and manipulate them with any shell command. Common data libs are preinstalled
# so code runs with NO network (sandboxes default to --network none).
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash gawk sed grep coreutils findutils jq ca-certificates curl \
        nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Run as root INSIDE the sandbox so agent-written files can land at any path (/home/user,
# /large_tool_results, /workspace, …). The isolation boundary is gVisor/runc AROUND the
# container, not the in-container uid — the same model E2B/Firecracker sandboxes use.
RUN mkdir -p /workspace
WORKDIR /workspace

# Preinstall common analysis libs so LLM-written code works offline (no PyPI at runtime).
RUN pip install --no-cache-dir pandas numpy duckdb pyarrow

# Containers are started with `sleep <timeout>` by the provider; this is just a safe default.
CMD ["sleep", "3600"]
