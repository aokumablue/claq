#!/usr/bin/env bash
# _run_onnx_background.sh — ONNX ビルドを排他制御付きでバックグラウンド実行する。
# install.sh から DEVGEAR_INSTALL_ONNX_ASYNC=1 のとき nohup setsid で起動される。
# 直接実行しない（install.sh から使用）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK_FILE="${HOME}/.devgear/onnx_build.lock"
LOG_DIR="${HOME}/.devgear/logs"
LOG_FILE="${LOG_DIR}/modelbuild.log"
MODEL_TARGET="${HOME}/.devgear/models"

# ~/.devgear とログディレクトリを事前確認（env -i で HOME が汚染されていないか検証）
mkdir -p "${HOME}/.devgear" "${LOG_DIR}"
chmod 0700 "${HOME}/.devgear"
# ログファイルが 10MB 超なら truncate（無制限肥大化の防止）

if [[ -f "${LOG_FILE}" ]] && \
   [[ $(stat -c%s "${LOG_FILE}" 2>/dev/null || stat -f%z "${LOG_FILE}" 2>/dev/null || echo 0) -gt 10485760 ]]; then
  : > "${LOG_FILE}"
fi

# symlink 攻撃: ロックファイルがシンボリックリンクなら拒否する
if [[ -L "${LOCK_FILE}" ]]; then
  echo "[onnx-bg] lock file is a symlink, aborting" >> "${LOG_FILE}"
  exit 1
fi

# flock で重複起動を防ぐ（非ブロッキング: 既に走っていれば即終了）
exec 200>"${LOCK_FILE}"
if ! flock -n 200; then
  echo "[onnx-bg] another build is in progress, exiting" >> "${LOG_FILE}"
  exit 0
fi

# shellcheck source=_build_onnx_lib.sh
source "${SCRIPT_DIR}/_build_onnx_lib.sh"
build_onnx_if_missing "${MODEL_TARGET}" "fp16" >> "${LOG_FILE}" 2>&1
