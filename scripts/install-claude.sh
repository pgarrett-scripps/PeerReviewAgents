#!/usr/bin/env bash
# Install PRA for Claude Code without preinstalled Python.
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]
then
  cat <<'HELP'
Usage: bash install-claude.sh [installer options]

Installs uv when needed, downloads a managed Python runtime, and installs
the PRA Claude Code plugin. No preinstalled Python or administrator access
is required. Install and sign in to the standalone Claude Code CLI first.

Options forwarded to the plugin installer:
  --claude PATH       Use a specific Claude Code executable
  --runtime PATH      Use an existing peerreview-mcp executable
  --data-dir PATH     Choose where the runtime and plugin are installed
  --configure-only    Prepare the plugin without registering it in Claude

Works on macOS, Linux, and Windows Git Bash. Platform setup is selected automatically.
Use the same command from a checkout or with a standalone downloaded script.
HELP
  exit 0
fi

download() {
  if command -v curl >/dev/null 2>&1
  then
    curl --fail --location --silent --show-error --retry 2 "$1" --output "$2"
  elif command -v wget >/dev/null 2>&1
  then
    wget --quiet "$1" --output-document="$2"
  else
    echo "PRA installation needs curl or wget to download its runtime." >&2
    return 1
  fi
}

scratch="$(mktemp -d "${TMPDIR:-/tmp}/pra-install.XXXXXXXX")"
trap 'rm -rf "$scratch"' EXIT

repo_root=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]
then
  candidate="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  if [[ -f "${candidate}/scripts/install-claude.py" && -f "${candidate}/pyproject.toml" ]]
  then
    repo_root="$candidate"
  fi
fi

if [[ -z "$repo_root" ]]
then
  echo "Downloading PeerReviewAgents..."
  download "https://github.com/pgarrett-scripps/PeerReviewAgents/archive/refs/heads/main.tar.gz" \
    "${scratch}/source.tar.gz"
  tar -xzf "${scratch}/source.tar.gz" -C "$scratch"
  repo_root="${scratch}/PeerReviewAgents-main"
fi

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    # Git Bash and native Windows programs use different path formats.
    powershell_command="$(command -v powershell.exe || true)"
    if [[ -z "$powershell_command" ]]
    then
      powershell_command="$(cygpath -u "${SYSTEMROOT:-C:/Windows}/System32/WindowsPowerShell/v1.0/powershell.exe")"
    fi
    windows_args=()
    while [[ $# -gt 0 ]]
    do
      case "$1" in
        --claude|--runtime|--data-dir)
          if [[ $# -lt 2 || -z "$2" ]]
          then
            echo "PRA installation requires a path after $1." >&2
            exit 2
          fi
          case "$1" in
            --claude) option="-Claude" ;;
            --runtime) option="-Runtime" ;;
            --data-dir) option="-DataDir" ;;
          esac
          windows_args+=("$option" "$(cygpath -wa "$2")")
          shift 2
          ;;
        --configure-only)
          windows_args+=("-ConfigureOnly")
          shift
          ;;
        *)
          echo "Unknown installer option: $1. Run with --help for supported options." >&2
          exit 2
          ;;
      esac
    done
    for path_variable in PEERREVIEW_BOOTSTRAP_DIR PEERREVIEW_CLAUDE_PATH UV_PYTHON_INSTALL_DIR UV_CACHE_DIR CLAUDE_CONFIG_DIR
    do
      if [[ -n "${!path_variable:-}" ]]
      then
        export "${path_variable}=$(cygpath -wa "${!path_variable}")"
      fi
    done
    MSYS2_ARG_CONV_EXCL='*' "$powershell_command" -NoProfile -ExecutionPolicy Bypass \
      -File "$(cygpath -wa "${repo_root}/scripts/install-claude.ps1")" "${windows_args[@]}"
    exit 0
    ;;
esac

bootstrap_root="${PEERREVIEW_BOOTSTRAP_DIR:-${HOME}/.local/share/peerreviewagents/bootstrap}"
uv_command="$(command -v uv || true)"
if [[ -z "$uv_command" ]]
then
  uv_command="${bootstrap_root}/bin/uv"
  if [[ ! -x "$uv_command" ]]
  then
    echo "Installing the PRA runtime manager..."
    download "https://astral.sh/uv/install.sh" "${scratch}/install-uv.sh"
    UV_UNMANAGED_INSTALL="${bootstrap_root}/bin" sh "${scratch}/install-uv.sh"
  fi
fi

# Keep the manager visible to the installer without changing shell profiles.
export PATH="$(dirname "$uv_command"):${PATH}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-${bootstrap_root}/python}"
echo "Preparing Python automatically and installing the Claude Code plugin..."
"$uv_command" run --no-project --no-config --managed-python --python 3.12 \
  "${repo_root}/scripts/install-claude.py" "$@"
