#!/usr/bin/env bash
# install-dev.sh
# リポジトリ直下 .venv を作り、plugins/claq[dev] を editable install する。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT=""
SKIP_PYTHON="${CLAQ_INSTALL_SKIP_PYTHON:-0}"

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
PLUGIN_ROOT="${REPO_ROOT}/plugins/claq"
VENV_DIR="${REPO_ROOT}/.venv"
VENV_PYTHON="${VENV_DIR}/bin/python3"

pip_install_quiet() {
  run_quietly "${VENV_PYTHON}" -m pip install --no-input --quiet --disable-pip-version-check "$@"
}

# ---- 開発者向け追加インストール ----

if [[ "${SKIP_PYTHON}" == "1" ]]; then
  echo "[claq] Developer extras skipped because --skip-python was requested"
  echo "[claq] OK"
  exit 0
fi

if ! PYTHON3="$(find_python3)"; then
  echo "Error: Python 3.12+ is required but not found." >&2
  echo "       Install python3.12 (e.g. brew install python@3.12) and retry." >&2
  exit 1
fi

if [[ ! -x "${VENV_PYTHON}" ]]; then
  if ! "${PYTHON3}" -m venv --help >/dev/null 2>&1; then
    echo "Error: python3-venv is not available." >&2
    echo "       Install python3-venv manually and retry." >&2
    exit 1
  fi
  echo "[claq] Creating Python virtual environment at ${VENV_DIR}"
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
  echo "[claq] Bootstrapping pip via ensurepip"
  run_quietly "${VENV_PYTHON}" -m ensurepip --upgrade
fi

echo "[claq] Installing developer-only Python extras"
pip_install_quiet -e "${PLUGIN_ROOT}[dev]"

# 開発ツールは venv の中だけに置く。システム全体（/usr/local/bin 等）へは張らない。
# 張り先が checkout 固有の venv になり、checkout を消すと dangling になるため。
for tool in ruff vulture; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo "[claq] ${tool} is available inside the venv only. Run 'source ${VENV_DIR}/bin/activate' (or add ${VENV_DIR}/bin to PATH) before invoking it."
  fi
done

echo "[claq] OK"
