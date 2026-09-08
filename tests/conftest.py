"""Pin the env the app reads at import time (real env vars win over the repo's .env)."""

import os

os.environ.update({
    "SANDBOX_PROVIDER": "gvisor",          # constructing GvisorProvider touches no daemon
    "LLM_SANDBOX_TOKEN": "test-token",
    "SANDBOX_MAX_SESSIONS": "2",
    "SANDBOX_MAX_SESSION_SECONDS": "100",
    "SANDBOX_MAX_EXEC_SECONDS": "30",
    "SANDBOX_MAX_REQUEST_BYTES": "4096",
    "SANDBOX_MAX_MEMORY_MB": "1024",
    "SANDBOX_MAX_CPUS": "2",
    "SANDBOX_LOG_PAYLOADS": "0",
    "SANDBOX_EXPOSE_DOCS": "0",
})
