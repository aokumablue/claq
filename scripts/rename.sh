#!/usr/bin/env bash
# リポジトリ全体のプラグイン名を一括置換するスクリプト（直接書き換え）
# 使用法: ./scripts/rename.sh [--dry-run]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/rename-config.json"

DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if ! command -v jq &>/dev/null; then
  echo "Error: jq is required. Install with: sudo apt install jq" >&2
  exit 1
fi

PLUGIN_NAME="$(jq -r '.plugin_name' "${CONFIG_FILE}")"
PLUGIN_NAME_UPPER="${PLUGIN_NAME^^}"
AUTHOR_NAME="$(jq -r '.author_name' "${CONFIG_FILE}")"
AUTHOR_URL="$(jq -r '.author_url' "${CONFIG_FILE}")"
REPO_URL="$(jq -r '.repo_url' "${CONFIG_FILE}")"

echo "=================================================="
echo "  deepblue → ${PLUGIN_NAME}"
echo "  対象: ${REPO_DIR}（直接置換）"
echo "  dry-run: ${DRY_RUN}"
echo "=================================================="

if [[ "${DRY_RUN}" == true ]]; then
  echo ""
  echo "[dry-run] 以下の変換を行います:"
  echo ""
  echo "  ディレクトリ名（内側から順に）:"
  echo "    plugins/deepblue/src/deepblue/ → plugins/deepblue/src/${PLUGIN_NAME}/"
  echo "    plugins/deepblue/              → plugins/${PLUGIN_NAME}/"
  echo ""
  echo "  ファイル名:"
  echo "    plugins/deepblue/runtime/deepblue-helpers.sh"
  echo "      → plugins/deepblue/runtime/${PLUGIN_NAME}-helpers.sh"
  echo "    plugins/deepblue/tests/lib/test_resolve_deepblue_root.py"
  echo "      → .../test_resolve_${PLUGIN_NAME}_root.py"
  echo "    plugins/deepblue/tests/lib/test_deepblue_settings.py"
  echo "      → .../test_${PLUGIN_NAME}_settings.py"
  echo "    plugins/deepblue/tests/lib/test_deepblue_launcher.py"
  echo "      → .../test_${PLUGIN_NAME}_launcher.py"
  echo "    plugins/deepblue/tests/scripts/test_deepblue_helpers.py"
  echo "      → .../test_${PLUGIN_NAME}_helpers.py"
  echo ""
  echo "  テキスト置換（対象: .py .sh .md .toml .json .txt .html .in）:"
  echo "    DEEPBLUE_              → ${PLUGIN_NAME_UPPER}_"
  echo "    ~/.deepblue            → ~/.${PLUGIN_NAME}"
  echo "    \${HOME}/.deepblue     → \${HOME}/.${PLUGIN_NAME}"
  echo "    Path.home()/\".deepblue\" → Path.home()/\".${PLUGIN_NAME}\""
  echo "    from deepblue          → from ${PLUGIN_NAME}"
  echo "    import deepblue        → import ${PLUGIN_NAME}"
  echo "    deepblue.              → ${PLUGIN_NAME}."
  echo "    -m deepblue.           → -m ${PLUGIN_NAME}."
  echo "    deepblue_plugin_root   → ${PLUGIN_NAME}_plugin_root"
  echo "    deepblue_run           → ${PLUGIN_NAME}_run"
  echo "    deepblue_mem_json      → ${PLUGIN_NAME}_mem_json"
  echo "    deepblue_mem_search    → ${PLUGIN_NAME}_mem_search"
  echo "    src/deepblue           → src/${PLUGIN_NAME}"
  echo "    \"deepblue\"           → \"${PLUGIN_NAME}\""
  echo "    https://github.com/aokumablue/deepblue → ${REPO_URL}"
  echo "    https://github.com/aokumablue         → ${AUTHOR_URL}"
  echo "    \"aokumablue\"         → \"${AUTHOR_NAME}\""
  echo "    aokumablue             → ${AUTHOR_NAME}"
  echo "    deepblue (残余)        → ${PLUGIN_NAME}"
  echo ""
  echo "  除外: scripts/rename.sh と scripts/rename-config.json は置換対象外"
  echo ""
  echo "[dry-run] 実際の変換は --dry-run なしで実行してください"
  exit 0
fi

# git ステータス確認
if git -C "${REPO_DIR}" rev-parse --git-dir &>/dev/null 2>&1; then
  if git -C "${REPO_DIR}" status --porcelain | grep -q .; then
    echo "Warning: リポジトリに未コミットの変更があります。続行しますか? [y/N]"
    read -r answer
    if [[ "${answer}" != "y" && "${answer}" != "Y" ]]; then
      echo "中止しました"
      exit 0
    fi
  fi
fi

echo ""
echo "Step 1: ディレクトリ・ファイルをリネーム中..."

# 内側から順にリネーム
if [[ -d "${REPO_DIR}/plugins/deepblue/src/deepblue" ]]; then
  mv "${REPO_DIR}/plugins/deepblue/src/deepblue" \
     "${REPO_DIR}/plugins/deepblue/src/${PLUGIN_NAME}"
  echo "  plugins/deepblue/src/deepblue → plugins/deepblue/src/${PLUGIN_NAME}"
fi

if [[ -f "${REPO_DIR}/plugins/deepblue/runtime/deepblue-helpers.sh" ]]; then
  mv "${REPO_DIR}/plugins/deepblue/runtime/deepblue-helpers.sh" \
     "${REPO_DIR}/plugins/deepblue/runtime/${PLUGIN_NAME}-helpers.sh"
  echo "  runtime/deepblue-helpers.sh → runtime/${PLUGIN_NAME}-helpers.sh"
fi

for _old_new in \
  "plugins/deepblue/tests/lib/test_resolve_deepblue_root.py:plugins/deepblue/tests/lib/test_resolve_${PLUGIN_NAME}_root.py" \
  "plugins/deepblue/tests/lib/test_deepblue_settings.py:plugins/deepblue/tests/lib/test_${PLUGIN_NAME}_settings.py" \
  "plugins/deepblue/tests/lib/test_deepblue_launcher.py:plugins/deepblue/tests/lib/test_${PLUGIN_NAME}_launcher.py" \
  "plugins/deepblue/tests/scripts/test_deepblue_helpers.py:plugins/deepblue/tests/scripts/test_${PLUGIN_NAME}_helpers.py"
do
  _old="${REPO_DIR}/${_old_new%%:*}"
  _new="${REPO_DIR}/${_old_new##*:}"
  if [[ -f "${_old}" ]]; then
    mv "${_old}" "${_new}"
    echo "  ${_old_new%%:*} → ${_old_new##*:}"
  fi
done

# 外側をリネーム（最後）
if [[ -d "${REPO_DIR}/plugins/deepblue" ]]; then
  mv "${REPO_DIR}/plugins/deepblue" "${REPO_DIR}/plugins/${PLUGIN_NAME}"
  echo "  plugins/deepblue → plugins/${PLUGIN_NAME}"
fi

echo "Step 2: テキスト一括置換中..."

if sed --version &>/dev/null 2>&1; then
  SED_INPLACE=(-i)
else
  SED_INPLACE=(-i '')
fi

# 対象ファイルを収集（scripts/ と .git/ は除外）
mapfile -d '' TARGET_FILES < <(find "${REPO_DIR}" -type f \( \
  -name "*.py" -o -name "*.sh" -o -name "*.md" \
  -o -name "*.toml" -o -name "*.json" -o -name "*.txt" \
  -o -name "*.html" -o -name "*.in" \
\) \
  ! -path "${REPO_DIR}/.git/*" \
  ! -path "${SCRIPT_DIR}/rename.sh" \
  ! -path "${SCRIPT_DIR}/rename-config.json" \
  -print0)

if [[ ${#TARGET_FILES[@]} -eq 0 ]]; then
  echo "  対象ファイルが見つかりませんでした"
else
  echo "  URL・作者情報..."
  sed "${SED_INPLACE[@]}" \
    -e "s|https://github\.com/aokumablue/deepblue/releases/[^\"']*|${REPO_URL}/releases/download/v0.1.0/model.tar.gz|g" \
    -e "s|https://github\.com/aokumablue/deepblue|${REPO_URL}|g" \
    -e "s|https://github\.com/aokumablue|${AUTHOR_URL}|g" \
    -e "s|\"aokumablue\"|\"${AUTHOR_NAME}\"|g" \
    -e "s|aokumablue|${AUTHOR_NAME}|g" \
    "${TARGET_FILES[@]}"

  echo "  環境変数・パス..."
  sed "${SED_INPLACE[@]}" \
    -e "s|DEEPBLUE_|${PLUGIN_NAME_UPPER}_|g" \
    -e "s|~/\.deepblue|~/.${PLUGIN_NAME}|g" \
    -e "s|\${HOME}/\.deepblue|\${HOME}/.${PLUGIN_NAME}|g" \
    -e "s|Path\.home() / \"\.deepblue\"|Path.home() / \".${PLUGIN_NAME}\"|g" \
    "${TARGET_FILES[@]}"

  echo "  Python インポート・モジュール..."
  sed "${SED_INPLACE[@]}" \
    -e "s|from deepblue|from ${PLUGIN_NAME}|g" \
    -e "s|import deepblue\b|import ${PLUGIN_NAME}|g" \
    -e "s|deepblue\.|${PLUGIN_NAME}.|g" \
    -e "s|-m deepblue\.|-m ${PLUGIN_NAME}.|g" \
    "${TARGET_FILES[@]}"

  echo "  ヘルパー関数名..."
  sed "${SED_INPLACE[@]}" \
    -e "s|deepblue_plugin_root|${PLUGIN_NAME}_plugin_root|g" \
    -e "s|deepblue_run\b|${PLUGIN_NAME}_run|g" \
    -e "s|deepblue_mem_json|${PLUGIN_NAME}_mem_json|g" \
    -e "s|deepblue_mem_search|${PLUGIN_NAME}_mem_search|g" \
    "${TARGET_FILES[@]}"

  echo "  パッケージ・ディレクトリ参照..."
  sed "${SED_INPLACE[@]}" \
    -e "s|src/deepblue|src/${PLUGIN_NAME}|g" \
    -e "s|\"deepblue\"|\"${PLUGIN_NAME}\"|g" \
    "${TARGET_FILES[@]}"

  echo "  残余 deepblue..."
  sed "${SED_INPLACE[@]}" \
    -e "s|deepblue|${PLUGIN_NAME}|g" \
    -e "s|DEEPBLUE|${PLUGIN_NAME_UPPER}|g" \
    "${TARGET_FILES[@]}"
fi

echo ""
echo "Step 3: 残留チェック..."
RESIDUAL=$(grep -r "deepblue\|DEEPBLUE\|aokumablue" "${REPO_DIR}" \
  --include="*.py" --include="*.sh" --include="*.toml" --include="*.json" \
  --exclude-dir=".git" \
  -l 2>/dev/null \
  | grep -v -e "^${SCRIPT_DIR}/rename\.sh$" -e "^${SCRIPT_DIR}/rename-config\.json$" || true)

if [[ -n "${RESIDUAL}" ]]; then
  echo "  [Warning] 以下のファイルに置換漏れの可能性があります:"
  echo "${RESIDUAL}" | while read -r f; do
    echo "    ${f}"
    grep -n "deepblue\|DEEPBLUE\|aokumablue" "${f}" | head -5 | \
      sed 's/^/      /'
  done
else
  echo "  残留なし"
fi

echo ""
echo "=================================================="
echo "  完了"
echo "=================================================="
