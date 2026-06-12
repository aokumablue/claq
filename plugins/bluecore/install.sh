#!/usr/bin/env bash
# install.sh
# bluecore の Python 依存を ~/.bluecore/.venv に導入し、初回の ~/.bluecore/settings.json を作成する。
# Claude / Copilot どちらか片方で実行すれば両方から共有できる。
# Ubuntu と macOS に対応。
# 使い方:
#   bash install.sh
#   bash install.sh --repo-root /path/to/repo
#   BLUECORE_INSTALL_SKIP_PYTHON=1 bash install.sh
#   BLUECORE_INSTALL_ASSUME_YES=1 bash install.sh  # sudo パッケージインストールを自動許可

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}"
SKIP_PYTHON="${BLUECORE_INSTALL_SKIP_PYTHON:-0}"
# sudo インストールの明示オプトイン（BLUECORE_INSTALL_ASSUME_YES=1 または --assume-yes）
ASSUME_YES="${BLUECORE_INSTALL_ASSUME_YES:-0}"

usage() {
  cat <<'EOF'
Usage: bash plugins/bluecore/install.sh [options]

Options:
  --repo-root PATH   Repository root (default: script directory)
  --skip-python      Skip Python package installation and venv setup
  --assume-yes       Allow sudo package installation without confirmation
  --help             Show this help

Environment:
  BLUECORE_INSTALL_SKIP_PYTHON=1  Skip Python package installation and venv setup
  BLUECORE_INSTALL_ASSUME_YES=1   Allow sudo package installation without confirmation
EOF
}

run_quietly() {
  local output_file
  output_file="$(mktemp)"
  local status=0
  if "$@" >"${output_file}" 2>&1; then
    rm -f "${output_file}"
    return 0
  else
    status=$?
    cat "${output_file}" >&2
    rm -f "${output_file}"
    return "${status}"
  fi
}

pip_install_quiet() {
  run_quietly "${VENV_PYTHON}" -m pip install --no-input --quiet --disable-pip-version-check "$@"
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

# python3 -m venv が使えるか確認し、なければ OS パッケージでインストールする
ensure_venv_module() {
  if "${PYTHON3}" -m venv --help >/dev/null 2>&1; then
    return
  fi

  echo "[bluecore] python3-venv not found. Attempting to install..."

  # sudo インストールに明示的な許可が必要
  if [[ "${ASSUME_YES}" != "1" ]]; then
    echo "[bluecore] Will install python3-venv using sudo." >&2
    echo "[bluecore] To allow, set BLUECORE_INSTALL_ASSUME_YES=1 or specify --assume-yes." >&2
    exit 1
  fi

  local py_ver
  py_ver="$("${PYTHON3}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  # インタープリタ出力をパッケージ名に埋め込むため、想定形式以外を拒否する
  if [[ ! "${py_ver}" =~ ^[0-9]+\.[0-9]+$ ]]; then
    echo "Error: unexpected python version string: ${py_ver}" >&2
    exit 1
  fi

  if command -v apt-get >/dev/null 2>&1; then
    echo "[bluecore] Running: sudo apt-get install python${py_ver}-venv"
    sudo apt-get update -qq
    sudo apt-get install -y "python${py_ver}-venv" \
      || sudo apt-get install -y python3-venv
  elif command -v dnf >/dev/null 2>&1; then
    echo "[bluecore] Running: sudo dnf install python${py_ver}-devel python3-virtualenv"
    sudo dnf install -y "python${py_ver}-devel" python3-virtualenv \
      || sudo dnf install -y python3-virtualenv
  elif command -v yum >/dev/null 2>&1; then
    echo "[bluecore] Running: sudo yum install python3-virtualenv"
    sudo yum install -y python3-virtualenv
  elif command -v brew >/dev/null 2>&1; then
    echo "[bluecore] Running: brew install python@${py_ver}"
    brew install "python@${py_ver}" || brew install python3
  else
    echo "Error: python3-venv not found and automatic installation failed." >&2
    echo "       Please install python3-venv manually and try again." >&2
    exit 1
  fi

  if ! "${PYTHON3}" -m venv --help >/dev/null 2>&1; then
    echo "Error: python3-venv installation completed but venv module is still not available." >&2
    exit 1
  fi
  echo "[bluecore] python3-venv installation complete"
}

# 既存 venv の stale symlink を削除する
check_and_reset_venv() {
  # 壊れたシンボリックリンク（自己参照含む）は即削除
  if [[ -L "${VENV_DIR}" ]]; then
    echo "[bluecore] Removing stale .venv symlink at ${VENV_DIR}"
    rm -f -- "${VENV_DIR}"
  fi
}

ensure_virtualenv() {
  if [[ -x "${VENV_PYTHON}" ]]; then
    return
  fi

  ensure_venv_module

  echo "[bluecore] Creating Python virtual environment at ${VENV_DIR}"
  "${PYTHON3}" -m venv "${VENV_DIR}"
  if [[ ! -x "${VENV_PYTHON}" ]]; then
    echo "Error: failed to create virtual environment at ${VENV_DIR}." >&2
    exit 1
  fi
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

install_user_python() {
  # 旧パス（REPO_ROOT/.venv）に実体 venv が残っていれば削除する
  # シンボリックリンクを先に判定し、正しいリンクは触らない
  if [[ -L "${_LEGACY_VENV}" ]]; then
    if [[ "$(readlink "${_LEGACY_VENV}")" != "${VENV_DIR}" ]]; then
      # VENV_DIR 以外を指す stale symlink のみ削除
      echo "[bluecore] Removing stale symlink at ${_LEGACY_VENV}"
      rm -f -- "${_LEGACY_VENV}"
    fi
  elif [[ -d "${_LEGACY_VENV}/bin" && -f "${_LEGACY_VENV}/pyvenv.cfg" ]]; then
    echo "[bluecore] Removing legacy venv at ${_LEGACY_VENV} (migrated to ${VENV_DIR})"
    rm -rf -- "${_LEGACY_VENV}"
  fi

  check_and_reset_venv
  ensure_virtualenv

  if ! "${VENV_PYTHON}" -m pip --version >/dev/null 2>&1; then
    echo "[bluecore] Bootstrapping pip via ensurepip"
    run_quietly "${VENV_PYTHON}" -m ensurepip --upgrade
  fi

  echo "[bluecore] Installing Python package dependencies into ${VENV_DIR}"
  pip_install_quiet --upgrade pip wheel

  # ハッシュロックで PyPI レジストリ側改ざんを検知する（LS-1）。
  # 再生成: pip-compile --generate-hashes plugins/bluecore/requirements.in -o plugins/bluecore/requirements.txt
  run_quietly "${VENV_PYTHON}" -m pip install --no-input --quiet --disable-pip-version-check \
    --require-hashes -r "${SCRIPT_DIR}/requirements.txt"

  # --no-deps: pyproject.toml の依存解決をスキップして上で固定したバージョンを維持する
  # editable install は --require-hashes と排他のため別途実行する
  pip_install_quiet --no-deps -e "${REPO_ROOT}"

  # 静的埋め込みモデルは既存ファイルを最優先で使い、未生成なら DL → テーブル抽出で解決する。
  # DL は数十 MB・抽出は数秒のため同期実行で完結する（torch ビルド venv は不要）。
  local model_target="${HOME}/.bluecore/models"
  local model_npy="${model_target}/embeddings.npy"
  local model_config="${SCRIPT_DIR}/model.json"

  if [[ -f "${model_npy}" ]]; then
    echo "[bluecore] Embedding model already present (skipping): ${model_npy}"
  elif "${VENV_PYTHON}" -m bluecore.model_download --config "${model_config}" --out "${model_target}"; then
    "${VENV_PYTHON}" -m bluecore.model_build build --out "${model_target}"
    echo "[bluecore] Embedding model built: ${model_npy}"
  else
    local download_status=$?
    if [[ "${download_status}" != "3" ]]; then
      exit "${download_status}"
    fi
    echo "[bluecore] Model download disabled in ${model_config}. Embedding features will be unavailable." >&2
  fi

  # 既存 settings.json のセキュリティ移行（パスワードを <data_dir>/.pgpass に分離・sslmode 未指定時のみ require 付与）
  if [[ -f "${SETTINGS_PATH}" ]]; then
    echo "[bluecore] Migrating existing settings.json to hardened format"
    "${VENV_PYTHON}" -m bluecore.mem migrate-settings || echo "[bluecore] Note: settings migration skipped."
  fi
}

# キャッシュディレクトリ内の .venv を VENV_DIR へのシンボリックリンクに差し替える共通処理。
# venv 実体は ~/.bluecore/.venv に一元化したため、キャッシュ内の旧実体 venv もレガシーとして削除する。
_replace_with_symlink() {
  local target_venv="$1"
  local quiet="${2:-0}"
  # 既に正しいリンクが張られている場合はスキップ
  if [[ -L "${target_venv}" && "$(readlink "${target_venv}")" == "${VENV_DIR}" ]]; then
    [[ "${quiet}" != "1" ]] && echo "[bluecore] .venv already linked (skipping): ${target_venv} -> ${VENV_DIR}"
    return 0
  fi
  # キャッシュ内の旧実体 venv は削除して symlink に置換する
  if [[ -d "${target_venv}" && -f "${target_venv}/pyvenv.cfg" ]]; then
    echo "[bluecore] Removing legacy venv at ${target_venv} (replacing with symlink)"
    rm -rf -- "${target_venv}"
  elif [[ -L "${target_venv}" ]]; then
    rm -f -- "${target_venv}"
  elif [[ -e "${target_venv}" ]]; then
    echo "[bluecore] Warning: ${target_venv} is unexpected file type, skipping" >&2
    return 0
  fi
  echo "[bluecore] Symlinking .venv: ${target_venv} -> ${VENV_DIR}"
  ln -sfn -- "${VENV_DIR}" "${target_venv}"
}

# Claude Code キャッシュに .venv シンボリックリンクを張る
update_claude_cache_symlinks() {
  [[ -d "${HOME}/.claude/plugins/cache/bluecore" ]] || return 0

  # 現バージョンを取得（plugin.json が読めない場合は空文字 → 全バージョン静音なし）
  local current_ver=""
  if [[ -f "${SCRIPT_DIR}/.claude-plugin/plugin.json" ]]; then
    current_ver="$(${PYTHON3} - "${SCRIPT_DIR}/.claude-plugin/plugin.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["version"])
PY
)"
  fi

  for org_dir in "${HOME}/.claude/plugins/cache/bluecore"/*; do
    [[ -L "${org_dir}" ]] && continue
    [[ -d "${org_dir}" ]] || continue
    for ver_dir in "${org_dir}"/*; do
      [[ -L "${ver_dir}" ]] && continue
      [[ -d "${ver_dir}" ]] || continue
      # 現バージョン以外の already linked ログは出力しない
      local is_current=0
      [[ -n "${current_ver}" && "$(basename "${ver_dir}")" == "${current_ver}" ]] && is_current=1
      _replace_with_symlink "${ver_dir}/.venv" "$((1 - is_current))"
    done
  done
}

# Copilot キャッシュに .venv シンボリックリンクを張る
update_copilot_cache_symlink() {
  local copilot_plugin_dir="${HOME}/.copilot/installed-plugins/bluecore/bluecore"
  [[ -d "${copilot_plugin_dir}" ]] || return 0

  _replace_with_symlink "${copilot_plugin_dir}/.venv"
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
    --assume-yes)
      ASSUME_YES=1
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

: "${HOME:?Error: HOME must be set.}"
SETTINGS_DIR="${HOME}/.bluecore"
VENV_DIR="${SETTINGS_DIR}/.venv"
VENV_PYTHON="${VENV_DIR}/bin/python3"
# 旧パスに実体 venv が残っていれば削除する（SETTINGS_DIR/.venv に移行済み）
_LEGACY_VENV="${REPO_ROOT}/.venv"
SETTINGS_PATH="${SETTINGS_DIR}/settings.json"
SETTINGS_TEMPLATE_PATH="${REPO_ROOT}/settings.json"

# ---- 前提条件チェック ----

if ! PYTHON3="$(find_python3)"; then
  echo "Error: Python 3.12+ is required but not found." >&2
  echo "       Install python3.12 (e.g. brew install python@3.12) and retry." >&2
  exit 1
fi
echo "[bluecore] Using ${PYTHON3} ($("${PYTHON3}" --version))"

if ! command -v git >/dev/null 2>&1; then
  echo "Error: git is required." >&2
  exit 1
fi

# ---- メイン処理 ----

ensure_settings_json

if [[ "${SKIP_PYTHON}" != "1" ]]; then
  install_user_python
fi

update_claude_cache_symlinks
update_copilot_cache_symlink

if ! command -v psql >/dev/null 2>&1; then
  echo "[bluecore] Note: PostgreSQL client (psql) is required for mem sync features."
fi

# ~/.bluecore/mem.db スキーマを初期化する（べき等: 既存DBは変更しない）
if [[ "${SKIP_PYTHON}" != "1" ]]; then
  echo "[bluecore] Initializing mem database at ${SETTINGS_DIR}/mem.db"
  "${VENV_PYTHON}" -m bluecore.mem setup
fi

# インストール済みバージョンを記録する（SKIP_PYTHON=1 のときは Python 未インストールなので記録しない）
# SessionStart の session_install フックが参照する
if [[ "${SKIP_PYTHON}" != "1" ]]; then
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
fi

echo "[bluecore] OK"
