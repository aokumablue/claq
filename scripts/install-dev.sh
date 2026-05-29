#!/usr/bin/env bash
# install-dev.sh
# 開発者向け依存を追加する。事前に install.sh を実行しておくこと。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}"
SKIP_PYTHON="${DEEPBLUE_INSTALL_SKIP_PYTHON:-0}"

usage() {
  cat <<'EOF'
Usage: bash scripts/install-dev.sh [options]

Run bash scripts/install.sh first, then run this script.

Options:
  --repo-root PATH   Repository root (default: script parent)
  --skip-python      Skip Python package installation and venv setup
  --help             Show this help
EOF
}

run_quietly() {
  local output_file
  output_file="$(mktemp)"
  if "$@" >"${output_file}" 2>&1; then
    rm -f "${output_file}"
    return 0
  else
    local status=$?
    cat "${output_file}" >&2
    rm -f "${output_file}"
    return "${status}"
  fi
}

pip_install_quiet() {
  run_quietly "${VENV_PYTHON}" -m pip install --no-input --quiet --disable-pip-version-check "$@"
}

# ---- 引数パース ----

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root)
      REPO_ROOT="$2"
      shift 2
      ;;
    --skip-python)
      SKIP_PYTHON=1
      shift
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

# ---- 変数確定（引数パース後に設定） ----

VENV_DIR="${HOME}/.deepblue/.venv"
VENV_PYTHON="${VENV_DIR}/bin/python3"

# ---- 開発者向け追加インストール ----

if [[ "${SKIP_PYTHON}" == "1" ]]; then
  echo "[deepblue] Developer extras skipped because --skip-python was requested"
  echo "[deepblue] OK"
  exit 0
fi

if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "Error: failed to find virtual environment at ${VENV_DIR}." >&2
  exit 1
fi

if ! "${VENV_PYTHON}" -m pip --version >/dev/null 2>&1; then
  echo "[deepblue] Bootstrapping pip via ensurepip"
  run_quietly "${VENV_PYTHON}" -m ensurepip --upgrade
fi

echo "[deepblue] Installing developer-only Python extras"
pip_install_quiet -e "${REPO_ROOT}[dev]"

# PATH にシムリンクを作成 (venv 外から hook が呼べるように)
for tool in ruff vulture; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    if [[ -x "${VENV_DIR}/bin/${tool}" ]]; then
      echo "[deepblue] Symlinking ${tool} -> /usr/local/bin/${tool}"
      sudo ln -sf "${VENV_DIR}/bin/${tool}" "/usr/local/bin/${tool}" 2>/dev/null \
        || echo "[deepblue] Warning: could not symlink ${tool} to /usr/local/bin (no sudo?). Add ${VENV_DIR}/bin to PATH." >&2
    fi
  fi
done

echo "[deepblue] OK"
