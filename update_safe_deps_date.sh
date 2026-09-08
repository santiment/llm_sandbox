#!/usr/bin/env bash
# Move the dependency-freshness window forward.
#
# pyproject.toml pins [tool.uv].exclude-newer — uv never resolves or installs a
# distribution PUBLISHED after that timestamp. The policy: stay ~2 weeks behind
# today, so a freshly-hijacked package release can't reach this project before
# the ecosystem has had time to notice. uv only accepts a static timestamp (no
# "now - 14 days" expression), hence this script: it rewrites the date to
# (today - SAFE_DEPS_AGE_DAYS, default 14) and you re-lock.
#
# The date is an INPUT to uv's version resolution and uv.lock records which date it
# was resolved under — so committing a moved date WITHOUT the re-locked uv.lock fails
# CI (`uv lock --check`). Use --lock, and commit pyproject.toml + uv.lock together.
#
# The same window governs sandbox-requirements.txt (the runtime image's pandas/numpy/
# openpyxl, pinned with hashes). --lock re-compiles it too; CI diffs it.
#
# Usage:
#   ./update_safe_deps_date.sh --lock     # move the date, re-lock, sync (the normal path)
#   ./update_safe_deps_date.sh            # move the date only — you MUST `uv lock` after
#   SAFE_DEPS_AGE_DAYS=30 ./update_safe_deps_date.sh --lock   # wider window
set -euo pipefail
cd "$(dirname "$0")"

DAYS="${SAFE_DEPS_AGE_DAYS:-14}"
if date -u -v -1d >/dev/null 2>&1; then
    SAFE="$(date -u -v -"${DAYS}"d +%Y-%m-%dT00:00:00Z)"   # BSD/macOS date
else
    SAFE="$(date -u -d "${DAYS} days ago" +%Y-%m-%dT00:00:00Z)"  # GNU date
fi

python3 - "$SAFE" <<'PYEOF'
import re
import sys
from pathlib import Path

safe = sys.argv[1]
p = Path("pyproject.toml")
text = p.read_text()
new, n = re.subn(r'exclude-newer = "[^"]*"', f'exclude-newer = "{safe}"', text)
if n != 1:
    sys.exit(f"expected exactly one exclude-newer line in pyproject.toml, found {n}")
if new == text:
    print(f"exclude-newer already at {safe}")
else:
    p.write_text(new)
    print(f"exclude-newer -> {safe}")
PYEOF

lock_sandbox_requirements() {
    uv pip compile --quiet --no-header --generate-hashes --python-platform linux --python-version 3.11 \
        --exclude-newer "$SAFE" sandbox-requirements.in -o sandbox-requirements.txt
}

if [[ "${1:-}" == "--lock" ]]; then
    uv lock
    uv sync
    lock_sandbox_requirements
    echo "Re-locked uv.lock + sandbox-requirements.txt. Run the tests: uv run pytest -q"
else
    echo "Date updated. Now run: uv lock && uv sync  (then the tests), and re-compile"
    echo "sandbox-requirements.txt (see lock_sandbox_requirements in this script)."
fi
