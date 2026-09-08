# The sandbox RUNTIME image — what untrusted agent code actually executes inside (under
# gVisor/runsc in prod). NOT the service image (that's ./Dockerfile). Reference it via
# SANDBOX_IMAGE; on k8s push it to a registry the cluster can pull from.
#
# ############################################################################################
# #  CHANGED THIS FILE? THE CHANGE DOES *NOT* DEPLOY ITSELF.                                 #
# #                                                                                          #
# #  Session pods pull SANDBOX_IMAGE by a PINNED COMMIT SHA with imagePullPolicy             #
# #  IfNotPresent — zero registry round-trips on session create, but nothing ever            #
# #  auto-updates. After your change merges to main (Jenkins pushes                          #
# #  llm-sandbox-runtime:<commit-sha> to ECR), you MUST manually bump the sha in:            #
# #                                                                                          #
# #    devops repo → stage/k8s-apps/llm_sandbox/deployment.yaml → env SANDBOX_IMAGE          #
# #                                                                                          #
# #  and `kubectl apply` it. Until then the cluster keeps running the OLD runtime image,     #
# #  silently.                                                                               #
# ############################################################################################
#
#   docker build --platform linux/amd64 -f sandbox.Dockerfile -t llm-sandbox-runtime:v0.1.0 .
#
# Python-only by design: python3 + pandas/numpy + the shell toolchain (bash/awk/sed/grep/
# coreutils) so the agent can store files and manipulate them with any shell command.
# Libraries are preinstalled because sandboxes default to NO network (no PyPI at runtime).
#
# Debian slim, NOT Alpine, unlike the service image: agent-written shell commands are
# arbitrary, and busybox's applets differ from GNU coreutils/awk in ways that silently
# change results. Fidelity beats the ~80 MB Alpine would save here.
#
# This image is on the session-create hot path — every cold node pull is latency a caller
# waits for — so it installs only what the base does NOT already ship. python:3.11-slim
# already has bash, sed, grep, coreutils, findutils and mawk; only gawk, jq and tini are new.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        gawk jq tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Run as root INSIDE the sandbox so agent-written files can land at any path (/home/user,
# /large_tool_results, /workspace, …). The isolation boundary is gVisor AROUND the
# container, not the in-container uid — the same model E2B/Firecracker sandboxes use.
RUN mkdir -p /workspace
WORKDIR /workspace

# Preinstall the default analysis libs so LLM-written code works offline. openpyxl is the
# engine pandas needs for read_excel/to_excel — without it every xlsx touch is an
# ImportError. Deliberately NO network clients (requests/curl): sandboxes must not make web
# calls. The test suites ship ~40 MB of fixtures that nothing here can use; pandas.testing
# lives in `_testing` and is untouched by this prune.
#
# Versions AND hashes come from sandbox-requirements.txt (compiled by uv under the same
# exclude-newer window as the service's own lockfile): an unpinned `pip install pandas` made
# this image the one artefact in the repo whose contents depended on the day it was built.
# --require-hashes refuses anything whose digest differs; --only-binary refuses to build from
# an sdist. Re-lock with ./update_safe_deps_date.sh --lock.
COPY sandbox-requirements.txt /tmp/sandbox-requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: \
        -r /tmp/sandbox-requirements.txt \
    && rm /tmp/sandbox-requirements.txt \
    && find /usr/local/lib/python3.11/site-packages \
         \( -type d -name tests -o -type d -name __pycache__ \) -prune -exec rm -rf {} + \
    && find /usr/local/lib/python3.11/site-packages -name '*.pyx' -delete

# tini reaps orphans. PID 1 here is otherwise `sleep`, which never wait()s, so every process
# an agent leaves behind becomes a zombie holding a slot against the kubelet's podPidsLimit.
# Kept as ENTRYPOINT (not baked into CMD) so both providers can pass `sleep <timeout>` as
# plain args and still get a real init — see the `args` field in providers/k8s.py.
ENTRYPOINT ["/usr/bin/tini", "--"]

# Containers are started with `sleep <timeout>` by the provider; this is just a safe default.
CMD ["sleep", "3600"]
