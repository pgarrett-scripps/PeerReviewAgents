#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target="${1:-runtime}"

install_runtime() {
  if command -v uv >/dev/null 2>&1
  then
    uv tool install --python ">=3.10,<3.14" --force "${repo_root}[mcp]"
  else
    python3 - <<'PY'
import sys

if not (sys.version_info >= (3, 10) and sys.version_info < (3, 14)):
    raise SystemExit("PeerReviewAgents requires Python 3.10 through 3.13")
PY
    python3 -m pip install --user --upgrade "${repo_root}[mcp]"
  fi
}

install_runtime

if [[ "$target" == "claude" ]]
then
  claude plugin marketplace add "$repo_root"
  claude plugin install peer-review-agents@peer-review-agents-local
elif [[ "$target" == "codex" ]]
then
  codex plugin marketplace add "$repo_root"
  codex plugin add peer-review-agents@peer-review-agents-local
elif [[ "$target" == "droid" ]]
then
  droid plugin marketplace add "$repo_root"
  droid plugin install peer-review-agents@peer-review-agents-local --scope user
elif [[ "$target" == "pi" ]]
then
  pi install "${repo_root}/integrations/pi"
elif [[ "$target" != "runtime" ]]
then
  echo "Usage: scripts/install-local.sh [runtime|claude|codex|droid|pi]" >&2
  exit 2
fi

echo "PeerReviewAgents local runtime installed for: ${target}"
