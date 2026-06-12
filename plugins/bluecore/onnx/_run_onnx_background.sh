#!/usr/bin/env bash
# _run_onnx_background.sh — ONNX モデル取得を排他制御付きでバックグラウンド実行する。
# 外部配布ダウンロードを先に試行し、config disabled（exit 3）のときのみビルドへ
# フォールバックする。install.sh（BLUECORE_INSTALL_ONNX_ASYNC=1）と SessionStart の
# session_install から nohup setsid / detached で起動される。
# 直接実行しない。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK_FILE="${HOME}/.bluecore/onnx_build.lock"
LOG_DIR="${HOME}/.bluecore/logs"
LOG_FILE="${LOG_DIR}/modelbuild.log"
MODEL_TARGET="${HOME}/.bluecore/models"

# ~/.bluecore とログディレクトリを事前確認（env -i で HOME が汚染されていないか検証）
mkdir -p "${HOME}/.bluecore" "${LOG_DIR}"
chmod 0700 "${HOME}/.bluecore"
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
# 事前チェックと exec の間に symlink を差し込まれた場合（TOCTOU）を検出する
if [[ -L "${LOCK_FILE}" ]]; then
  echo "[onnx-bg] lock file became a symlink, aborting" >> "${LOG_FILE}"
  exit 1
fi
if ! flock -n 200; then
  echo "[onnx-bg] another build is in progress, exiting" >> "${LOG_FILE}"
  exit 0
fi

# 試行時刻を記録する（SessionStart の session_install がリトライ間隔の判定に使う）
date +%s > "${HOME}/.bluecore/onnx_last_attempt"

if [[ -f "${MODEL_TARGET}/model.onnx" ]]; then
  echo "[onnx-bg] model already present, exiting" >> "${LOG_FILE}"
  exit 0
fi

# 第 1 段: 外部配布ダウンロード（exit 3 = config disabled → ビルドへフォールバック）
VENV_PYTHON="${HOME}/.bluecore/.venv/bin/python3"
ONNX_CONFIG="${SCRIPT_DIR}/../onnx.json"
download_status=3
if [[ -x "${VENV_PYTHON}" && -f "${ONNX_CONFIG}" ]]; then
  # `if cmd; then ... fi` 直後の $? は if 文の終了コード（条件 false 時は 0）に
  # なるため、|| で失敗コードを直接捕捉する
  download_status=0
  "${VENV_PYTHON}" -m bluecore.onnx_download --config "${ONNX_CONFIG}" --out "${MODEL_TARGET}" \
      >> "${LOG_FILE}" 2>&1 || download_status=$?
  if [[ "${download_status}" -eq 0 ]]; then
    echo "[onnx-bg] model downloaded: ${MODEL_TARGET}/model.onnx" >> "${LOG_FILE}"
    exit 0
  fi
fi

if [[ "${download_status}" != "3" ]]; then
  # ネットワーク等の一時障害: ビルド（巨大依存の取得）も失敗する公算が高いので
  # ここでは終了し、次回 SessionStart のリトライに委ねる
  echo "[onnx-bg] download failed (exit ${download_status}), will retry on next session" >> "${LOG_FILE}"
  exit 1
fi

# 第 2 段: config disabled / venv 未準備時は現行のローカルビルドへフォールバック
# set -e による無記録の異常終了を避けるため、source 前に存在を確認する
if [[ ! -f "${SCRIPT_DIR}/_build_onnx_lib.sh" ]]; then
  echo "[onnx-bg] build lib not found, aborting" >> "${LOG_FILE}"
  exit 1
fi
# shellcheck source=_build_onnx_lib.sh
source "${SCRIPT_DIR}/_build_onnx_lib.sh"
build_onnx_if_missing "${MODEL_TARGET}" "fp16" >> "${LOG_FILE}" 2>&1
