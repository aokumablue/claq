#!/usr/bin/env bash
# build_onnx_model.sh — ruri-v3 ONNX ビルド・量子化・分割を 1 発で実行するメンテナ向けスクリプト。
#
# 使い方:
#   ./plugins/devgear/onnx/build_onnx_model.sh                        # FP16 デフォルト（推奨）
#   ./plugins/devgear/onnx/build_onnx_model.sh --quant fp32           # FP32（品質劣化ゼロ、約 1.2 GB）
#   ./plugins/devgear/onnx/build_onnx_model.sh --quant int8           # INT8（動的量子化、約 300 MB）
#   ./plugins/devgear/onnx/build_onnx_model.sh --quant fp16 --revision <SHA>
#
# 出力先: ~/.devgear/models/（install.sh と共有）
# 使用する venv: ~/.devgear/.venv-modelbuild（初回のみ自動作成）
#
# 量子化方式:
#   FP16: onnxruntime.transformers.optimizer を使用（CPU 対応）。約 600 MB。
#   FP32: 量子化なし。品質劣化ゼロ。約 1.2 GB。
#   INT8: onnxruntime 動的量子化。約 300 MB。精度低下 < 0.5%（要検証）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

QUANT="fp16"
REVISION=""
OUT_DIR="${HOME}/.devgear/models"

usage() {
  cat <<'EOF'
Usage: ./plugins/devgear/onnx/build_onnx_model.sh [options]

Options:
  --quant fp16|fp32|int8   量子化レベル (default: fp16)
                            fp16: ort optimizer 経由（CPU 対応、約 600 MB）★推奨
                            fp32: 量子化なし（約 1.2 GB）
                            int8: 動的量子化（約 300 MB）
  --revision SHA           HF Hub commit SHA (default: build_config.json の hf_revision)
  --out DIR                出力先ディレクトリ (default: ~/.devgear/models)
  --help                   このヘルプを表示
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --quant)
      QUANT="$2"
      shift 2
      ;;
    --revision)
      REVISION="$2"
      if [[ ! "${REVISION}" =~ ^[0-9a-f]{40}$|^[0-9a-f]{64}$ ]]; then
        echo "Error: --revision は 40 または 64 文字の16進数 SHA を指定してください: '${REVISION}'" >&2
        exit 1
      fi
      shift 2
      ;;
    --out)
      OUT_DIR="$2"
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

# 量子化レベルのバリデーション
case "${QUANT}" in
  fp16|fp32|int8) ;;
  *)
    echo "Error: --quant は fp16 / fp32 / int8 のいずれかを指定してください。" >&2
    exit 1
    ;;
esac

echo "[build] Starting ONNX model build (quant=${QUANT})"
echo "[build] Output: ${OUT_DIR}"

# shellcheck source=_build_onnx_lib.sh
source "${SCRIPT_DIR}/_build_onnx_lib.sh"
build_onnx_always "${OUT_DIR}" "${QUANT}" "${REVISION}"
