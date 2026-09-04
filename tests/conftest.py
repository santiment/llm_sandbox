"""Test env for the HTTP layer.

``llm_sandbox.app`` builds its Config and provider at import time (and Config loads the
repo's ``.env`` via setdefault), so the knobs the app tests rely on are pinned here — before
any test module imports the app — where real env vars win over the dotenv.
"""

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
