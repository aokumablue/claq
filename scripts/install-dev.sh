#!/usr/bin/env bash
# install-dev.sh
# リポジトリ直下 .venv を作り、plugins/bluecore[dev] を editable install する。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT=""
SKIP_PYTHON="${BLUECORE_INSTALL_SKIP_PYTHON:-0}"

usage() {
  cat <<'EOF'
Usage: bash scripts/install-dev.sh [options]

Options:
  --repo-root PATH   Git repository root (default: git toplevel of this script)
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

# Python 3.12+ のバイナリを探す
find_python3() {
  for candidate in python3.14 python3.13 python3.12 python3; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      local ver
      ver="$("${candidate}" -c 'import sys; print(sys.version_info.major * 100 + sys.version_info.minor)' 2>/dev/null || echo 0)"
      if [[ "${ver}" -ge 312 ]]; then
        echo "${candidate}"
        return 0
      fi
    fi
  done
  return 1
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

if [[ -z "${REPO_ROOT}" ]]; then
  REPO_ROOT="$(git -C "${SCRIPT_DIR}" rev-parse --show-toplevel)"
fi
PLUGIN_ROOT="${REPO_ROOT}/plugins/bluecore"
VENV_DIR="${REPO_ROOT}/.venv"
VENV_PYTHON="${VENV_DIR}/bin/python3"

pip_install_quiet() {
  run_quietly "${VENV_PYTHON}" -m pip install --no-input --quiet --disable-pip-version-check "$@"
}

# ---- 開発者向け追加インストール ----

if [[ "${SKIP_PYTHON}" == "1" ]]; then
  echo "[bluecore] Developer extras skipped because --skip-python was requested"
  echo "[bluecore] OK"
  exit 0
fi

if ! PYTHON3="$(find_python3)"; then
  echo "Error: Python 3.12+ is required but not found." >&2
  echo "       Install python3.12 (e.g. brew install python@3.12) and retry." >&2
  exit 1
fi

# 旧共有 venv（~/.bluecore/.venv）への symlink は開発用実体ではないので外す
if [[ -L "${VENV_DIR}" ]]; then
  echo "[bluecore] Removing leftover .venv symlink at ${VENV_DIR}"
  rm -f -- "${VENV_DIR}"
fi

if [[ ! -x "${VENV_PYTHON}" ]]; then
  if ! "${PYTHON3}" -m venv --help >/dev/null 2>&1; then
    echo "Error: python3-venv is not available." >&2
    echo "       Install python3-venv manually and retry." >&2
    exit 1
  fi
  echo "[bluecore] Creating Python virtual environment at ${VENV_DIR}"
  "${PYTHON3}" -m venv "${VENV_DIR}"
  if [[ ! -x "${VENV_PYTHON}" ]]; then
    echo "Error: failed to create virtual environment at ${VENV_DIR}." >&2
    exit 1
  fi
else
  venv_ver="$("${VENV_PYTHON}" -c 'import sys; print(sys.version_info.major * 100 + sys.version_info.minor)' 2>/dev/null || echo 0)"
  if [[ "${venv_ver}" -lt 312 ]]; then
    echo "Error: ${VENV_PYTHON} is older than Python 3.12." >&2
    echo "       Remove ${VENV_DIR} and re-run this script." >&2
    exit 1
  fi
fi

if ! "${VENV_PYTHON}" -m pip --version >/dev/null 2>&1; then
  echo "[bluecore] Bootstrapping pip via ensurepip"
  run_quietly "${VENV_PYTHON}" -m ensurepip --upgrade
fi

echo "[bluecore] Installing developer-only Python extras"
pip_install_quiet -e "${PLUGIN_ROOT}[dev]"

# PATH にシムリンクを作成 (venv 外から hook が呼べるように)
for tool in ruff vulture; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    if [[ -x "${VENV_DIR}/bin/${tool}" ]]; then
      echo "[bluecore] Symlinking ${tool} -> /usr/local/bin/${tool}"
      sudo ln -sf "${VENV_DIR}/bin/${tool}" "/usr/local/bin/${tool}" 2>/dev/null \
        || echo "[bluecore] Warning: could not symlink ${tool} to /usr/local/bin (no sudo?). Add ${VENV_DIR}/bin to PATH." >&2
    fi
  fi
done

echo "[bluecore] OK"
