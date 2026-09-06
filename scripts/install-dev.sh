#!/usr/bin/env bash
# install-dev.sh
# リポジトリ直下 .venv を作り、plugins/ple4[dev] を editable install する。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT=""
SKIP_PYTHON="${PLE4_INSTALL_SKIP_PYTHON:-0}"

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

# 旧版が張った system-wide な ruff/vulture symlink を洗い出して警告する
#
# 以前の install-dev.sh は /usr/local/bin/{ruff,vulture} へ symlink を張って
# いた。張り先は checkout 固有の <repo>/.venv/bin/* で、checkout を消すと
# system-wide の名前が dangling になる。作成を止めただけでは既存の開発機に
# 残り続けるうえ、**生きている symlink は `command -v` を成功させる**ので
# 下の「venv の中だけにある」案内まで抑止され、開発者は別 checkout の ruff を
# 無自覚に実行し続ける。この隠蔽が問題の本体なので、案内とは独立に警告する。
#
# PATH の全エントリを走査するのは `command -v` が先頭 1 件しか返さないため。
# venv を有効化していると venv 側の ruff が先に当たり、/usr/local/bin に残った
# 旧 symlink が隠れて見えない。
#
# 自動削除はしない。張り先が /usr/local/bin なら削除に sudo が要り、非対話
# 環境ではパスワード待ちで止まる。`readlink` に `-f` は付けない（古い macOS の
# readlink は -f を持たない）。
#
# PATH の分割に配列を使わないのは、macOS の system bash が 3.2 で、そこでは
# `set -u` 下の `"${arr[@]}"` が**空配列で unbound variable になる**ため
# （実測）。`#!/usr/bin/env bash` は既定でその 3.2 に解決するので、PATH が空の
# 環境でインストーラごと落ちる。runtime/ple4-hook と同じ while ループで割る。
warn_stale_tool_symlinks() {
  local rest="${PATH}"
  local entry tool candidate target
  while [[ -n "${rest}" ]]; do
    if [[ "${rest}" == *:* ]]; then
      entry="${rest%%:*}"
      rest="${rest#*:}"
    else
      entry="${rest}"
      rest=""
    fi
    [[ -n "${entry}" ]] || continue
    for tool in ruff vulture; do
      candidate="${entry}/${tool}"
      [[ -L "${candidate}" ]] || continue
      target="$(readlink -- "${candidate}")"
      [[ "${target}" == */.venv/bin/* ]] || continue
      echo "[ple4] WARNING: ${candidate} is a leftover symlink into a checkout venv (-> ${target})."
      echo "[ple4]          An older install-dev.sh created it. It exposes a user-writable path under a system-wide name, shadows the venv copy, and dangles once that checkout is removed."
      echo "[ple4]          Remove it manually (sudo is required, so this script will not do it): sudo rm -- '${candidate}'"
    done
  done
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
PLUGIN_ROOT="${REPO_ROOT}/plugins/ple4"
VENV_DIR="${REPO_ROOT}/.venv"
VENV_PYTHON="${VENV_DIR}/bin/python3"

pip_install_quiet() {
  run_quietly "${VENV_PYTHON}" -m pip install --no-input --quiet --disable-pip-version-check "$@"
}

# ---- 開発者向け追加インストール ----

# 旧 symlink の回収案内は --skip-python でも出す。Python を触らない実行でも
# 「システム全体に残った旧 symlink」という事実は変わらず、ここより後ろに置くと
# 副作用の無い唯一の実行モードから到達できなくなる。
warn_stale_tool_symlinks

if [[ "${SKIP_PYTHON}" == "1" ]]; then
  echo "[ple4] Developer extras skipped because --skip-python was requested"
  echo "[ple4] OK"
  exit 0
fi

if ! PYTHON3="$(find_python3)"; then
  echo "Error: Python 3.12+ is required but not found." >&2
  echo "       Install python3.12 (e.g. brew install python@3.12) and retry." >&2
  exit 1
fi

# 旧共有 venv（~/.ple4/.venv）への symlink は開発用実体ではないので外す
if [[ -L "${VENV_DIR}" ]]; then
  echo "[ple4] Removing leftover .venv symlink at ${VENV_DIR}"
  rm -f -- "${VENV_DIR}"
fi

if [[ ! -x "${VENV_PYTHON}" ]]; then
  if ! "${PYTHON3}" -m venv --help >/dev/null 2>&1; then
    echo "Error: python3-venv is not available." >&2
    echo "       Install python3-venv manually and retry." >&2
    exit 1
  fi
  echo "[ple4] Creating Python virtual environment at ${VENV_DIR}"
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
  echo "[ple4] Bootstrapping pip via ensurepip"
  run_quietly "${VENV_PYTHON}" -m ensurepip --upgrade
fi

echo "[ple4] Installing developer-only Python extras"
pip_install_quiet -e "${PLUGIN_ROOT}[dev]"

# 開発ツールは venv の中だけに置く。
#
# 以前はここで /usr/local/bin/{ruff,vulture} へ symlink を張っていたが、
# リポジトリローカルの開発インストーラがシステム全体を書き換えるのは行き過ぎで、
# しかも張り先が checkout 固有の ${REPO_ROOT}/.venv/bin/* なので checkout を
# 消すと ruff がシステム全体で dangling symlink になる（change-repository.sh の
# 後始末もこの symlink を回収しない）。sudo も非対話環境ではパスワード待ちで
# 止まる。案内だけ出して、解決は venv の有効化に委ねる。
#
# 既に張られてしまった symlink の回収案内は warn_stale_tool_symlinks が上流で
# 出す。この案内は `command -v` が失敗したときにしか出ないため、生きた旧
# symlink がある機械では抑止される——だからこそ回収案内を別経路にしてある。
for tool in ruff vulture; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo "[ple4] ${tool} is available inside the venv only. Run 'source ${VENV_DIR}/bin/activate' (or add ${VENV_DIR}/bin to PATH) before invoking it."
  fi
done

echo "[ple4] OK"
