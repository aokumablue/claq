#!/usr/bin/env bash
# install.sh
# ~/.bluecore/settings.json の初回作成、キャッシュ等に残った .venv の削除、
# Grok plugin-root シンボリックリンクの更新、plugin_installed_version の記録。
# venv は作らない。
# 使い方:
#   bash install.sh
#   bash install.sh --repo-root /path/to/repo

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}"

usage() {
  cat <<'EOF'
Usage: bash plugins/bluecore/install.sh [options]

Options:
  --repo-root PATH   Repository root (default: script directory)
  --help             Show this help
EOF
}

ensure_settings_json() {
  if [[ -e "${SETTINGS_DIR}" && ! -d "${SETTINGS_DIR}" ]]; then
    echo "Error: ${SETTINGS_DIR} exists and is not a directory." >&2
    exit 1
  fi

  if [[ -e "${SETTINGS_PATH}" ]]; then
    if [[ -f "${SETTINGS_PATH}" ]]; then
      echo "[bluecore] Existing settings file found at ${SETTINGS_PATH}"
      return
    fi
    echo "Error: ${SETTINGS_PATH} exists and is not a regular file." >&2
    exit 1
  fi

  if [[ ! -f "${SETTINGS_TEMPLATE_PATH}" ]]; then
    echo "Error: settings template not found at ${SETTINGS_TEMPLATE_PATH}." >&2
    exit 1
  fi

  mkdir -p "${SETTINGS_DIR}"

  # 信頼鍵ストアを初期化する（BLUECORE_TRUSTED_KEY_FILE が設定されている場合のみ import）
  local trust_dir="${SETTINGS_DIR}/trust"
  mkdir -p "${trust_dir}"
  chmod 0700 "${trust_dir}"
  if [[ -n "${BLUECORE_TRUSTED_KEY_FILE:-}" && -f "${BLUECORE_TRUSTED_KEY_FILE}" ]]; then
    # symlink 経由攻撃と HOME 外参照を防ぐ
    if [[ -L "${BLUECORE_TRUSTED_KEY_FILE}" ]]; then
      echo "Error: BLUECORE_TRUSTED_KEY_FILE must not be a symlink" >&2
      exit 1
    fi
    local key_dir
    key_dir="$(cd "$(dirname "${BLUECORE_TRUSTED_KEY_FILE}")" 2>/dev/null && pwd)" || {
      echo "Error: invalid BLUECORE_TRUSTED_KEY_FILE (cannot resolve directory)" >&2
      exit 1
    }
    local resolved_key="${key_dir}/$(basename "${BLUECORE_TRUSTED_KEY_FILE}")"
    if [[ "${resolved_key}" != "${HOME}/"* ]]; then
      echo "Error: BLUECORE_TRUSTED_KEY_FILE must reside under HOME" >&2
      exit 1
    fi
    local gnupg_dir="${trust_dir}/gnupg"
    mkdir -p "${gnupg_dir}"
    chmod 0700 "${gnupg_dir}"
    cp -- "${BLUECORE_TRUSTED_KEY_FILE}" "${trust_dir}/maintainer.asc"
    chmod 0600 "${trust_dir}/maintainer.asc"
    GNUPGHOME="${gnupg_dir}" gpg --import "${trust_dir}/maintainer.asc" 2>/dev/null || true
    echo "[bluecore] Trust key imported: ${trust_dir}/gnupg"
  fi

  "${PYTHON3}" - "${SETTINGS_TEMPLATE_PATH}" "${SETTINGS_PATH}" <<'PY'
from pathlib import Path
import json
import sys

template_path = Path(sys.argv[1])
settings_path = Path(sys.argv[2])

data = json.loads(template_path.read_text(encoding="utf-8"))
settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
settings_path.chmod(0o600)
PY
  echo "[bluecore] Wrote full default settings file: ${SETTINGS_PATH}"
}

# Grok Build: ${CLAUDE_PLUGIN_ROOT} は ~/.grok/plugins/bluecore に展開されるが、
# 実体は ~/.grok/installed-plugins/bluecore-<hash>/ のみ。リンク先に開発用
# リポジトリ（bluecore-dev 等）は使わない。
update_grok_plugin_root_symlink() {
  local grok_plugins="${HOME}/.grok/plugins"
  local installed="${HOME}/.grok/installed-plugins"
  local target=""

  # 実行中の install.sh が installed-plugins/bluecore-* 配下にある場合のみ採用
  case "${SCRIPT_DIR}" in
    *"/installed-plugins/bluecore-"*)
      if [[ -f "${SCRIPT_DIR}/src/bluecore/launcher.py" ]]; then
        target="${SCRIPT_DIR}"
      fi
      ;;
  esac

  # それ以外は ~/.grok/installed-plugins/bluecore-* の最新のみ（開発ツリーは無視）
  if [[ -z "${target}" && -d "${installed}" ]]; then
    target="$(ls -1dt "${installed}"/bluecore-* 2>/dev/null | while read -r d; do
      if [[ -f "${d}/src/bluecore/launcher.py" ]]; then
        printf '%s\n' "${d}"
        break
      fi
    done)"
  fi

  [[ -n "${target}" && -d "${target}" ]] || return 0
  [[ -f "${target}/src/bluecore/launcher.py" ]] || return 0

  mkdir -p "${grok_plugins}"
  local link="${grok_plugins}/bluecore"
  if [[ -L "${link}" ]]; then
    local current
    current="$(readlink "${link}" 2>/dev/null || true)"
    if [[ "${current}" == "${target}" ]]; then
      return 0
    fi
    rm -f "${link}"
  elif [[ -e "${link}" ]]; then
    if [[ -f "${link}/src/bluecore/launcher.py" ]]; then
      return 0
    fi
    # 壊れた配置のみ除去（中身がある通常ディレクトリは触らない）
    if [[ -d "${link}" ]] && [[ -z "$(ls -A "${link}" 2>/dev/null)" ]]; then
      rmdir "${link}" 2>/dev/null || return 0
    else
      return 0
    fi
  fi
  ln -sfn "${target}" "${link}"
  echo "[bluecore] Grok plugin root symlink: ${link} -> ${target}"
}

# basename が .venv のパスだけを消す。symlink はリンクのみ、実体は rm -rf。
# --repo-root/.venv と SCRIPT_DIR/.venv（git checkout 側）は絶対に消さない。
remove_leftover_venv() {
  local path="$1"
  [[ "$(basename "${path}")" == ".venv" ]] || return 0
  if [[ "${path}" == "${REPO_ROOT}/.venv" || "${path}" == "${SCRIPT_DIR}/.venv" ]]; then
    return 0
  fi
  if [[ -L "${path}" ]]; then
    echo "[bluecore] Removing leftover .venv symlink: ${path}"
    rm -f -- "${path}"
  elif [[ -e "${path}" ]]; then
    echo "[bluecore] Removing leftover .venv: ${path}"
    rm -rf -- "${path}"
  fi
}

remove_leftover_venvs() {
  remove_leftover_venv "${HOME}/.bluecore/.venv"

  if [[ -d "${HOME}/.claude/plugins/cache/bluecore" ]]; then
    local org_dir ver_dir
    for org_dir in "${HOME}/.claude/plugins/cache/bluecore"/*; do
      [[ -L "${org_dir}" ]] && continue
      [[ -d "${org_dir}" ]] || continue
      for ver_dir in "${org_dir}"/*; do
        [[ -L "${ver_dir}" ]] && continue
        [[ -d "${ver_dir}" ]] || continue
        remove_leftover_venv "${ver_dir}/.venv"
      done
    done
  fi

  remove_leftover_venv "${HOME}/.copilot/installed-plugins/bluecore/bluecore/.venv"

  local grok_plugin
  for grok_plugin in "${HOME}/.grok/installed-plugins"/bluecore-*; do
    remove_leftover_venv "${grok_plugin}/.venv"
  done
}

# ---- 引数パース ----

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root)
      REPO_ROOT="$2"
      shift 2
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

: "${HOME:?Error: HOME must be set.}"
SETTINGS_DIR="${HOME}/.bluecore"
SETTINGS_PATH="${SETTINGS_DIR}/settings.json"
SETTINGS_TEMPLATE_PATH="${REPO_ROOT}/settings.json"

# ---- 前提条件チェック ----

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 is required but not found." >&2
  exit 1
fi
PYTHON3="$(command -v python3)"

if ! command -v git >/dev/null 2>&1; then
  echo "Error: git is required." >&2
  exit 1
fi

# ---- メイン処理 ----

ensure_settings_json
remove_leftover_venvs
update_grok_plugin_root_symlink

# mem.db の作成はインストーラでは行わない。
# SessionStart の `bluecore.mem.cli context` が Database() 経由で
# 親ディレクトリ作成とスキーマ初期化を毎セッション冪等に済ませるため。

# インストール済みバージョンを記録する。
# SessionStart の session_install フックが参照する
# ヒアドキュメント + 引数渡しでパスをシェルから分離してインジェクションを防ぐ
PLUGIN_VERSION="$(${PYTHON3} - "${SCRIPT_DIR}/.claude-plugin/plugin.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["version"])
PY
)"
chmod 0700 "${SETTINGS_DIR}"
# mktemp + mv でアトミック書き込みし、並行プロセスによる部分読み取りを防ぐ
_ver_tmp="$(mktemp "${SETTINGS_DIR}/plugin_installed_version.XXXXXX")"
printf '%s\n' "${PLUGIN_VERSION}" > "${_ver_tmp}"
chmod 0600 "${_ver_tmp}"
mv -f "${_ver_tmp}" "${SETTINGS_DIR}/plugin_installed_version"
echo "[bluecore] Recorded installed version: ${PLUGIN_VERSION}"

echo "[bluecore] OK"
