#!/usr/bin/env bash
# プラグインを別名でコピー・一括リネームするスクリプト
# 使用法: ./rename.sh [--dry-run] [--out-dir PATH]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/rename-config.json"
SRC_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

DRY_RUN=false
OUT_DIR=""

# 引数解析
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# jq チェック
if ! command -v jq &>/dev/null; then
  echo "Error: jq is required. Install with: sudo apt install jq" >&2
  exit 1
fi

# 設定読み込み
PLUGIN_NAME="$(jq -r '.plugin_name' "${CONFIG_FILE}")"
PLUGIN_NAME_UPPER="$(jq -r '.plugin_name_upper' "${CONFIG_FILE}")"
AUTHOR_NAME="$(jq -r '.author_name' "${CONFIG_FILE}")"
AUTHOR_URL="$(jq -r '.author_url' "${CONFIG_FILE}")"
REPO_URL="$(jq -r '.repo_url' "${CONFIG_FILE}")"
REPO_URL_GIT="$(jq -r '.repo_url_git' "${CONFIG_FILE}")"
REPO_URL_ISSUES="$(jq -r '.repo_url_issues' "${CONFIG_FILE}")"
REPO_URL_README="$(jq -r '.repo_url_readme' "${CONFIG_FILE}")"

# 除外パス（プラグインルートからの相対パス）
mapfile -t EXCLUDE_PATHS < <(jq -r '.exclude // [] | .[]' "${CONFIG_FILE}")

# 出力先デフォルト
if [[ -z "${OUT_DIR}" ]]; then
  OUT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)/dist/${PLUGIN_NAME}"
fi

echo "=================================================="
echo "  deepblue → ${PLUGIN_NAME}"
echo "  出力先: ${OUT_DIR}"
echo "  dry-run: ${DRY_RUN}"
echo "=================================================="

if [[ "${DRY_RUN}" == true ]]; then
  echo ""
  echo "[dry-run] 以下の変換を行います:"
  echo ""
  echo "  ディレクトリ名:"
  echo "    src/deepblue/              → src/${PLUGIN_NAME}/"
  echo "    runtime/deepblue-helpers.sh → runtime/${PLUGIN_NAME}-helpers.sh"
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
  echo "    https://github.com/aokumablue/deepblue#readme → ${REPO_URL_README}"
  echo "    https://github.com/aokumablue/deepblue.git    → ${REPO_URL_GIT}"
  echo "    https://github.com/aokumablue/deepblue/issues → ${REPO_URL_ISSUES}"
  echo "    https://github.com/aokumablue/deepblue        → ${REPO_URL}"
  echo "    https://github.com/aokumablue               → ${AUTHOR_URL}"
  echo "    \"aokumablue\"         → \"${AUTHOR_NAME}\""
  echo "    deepblue (残余)       → ${PLUGIN_NAME}"
  echo ""
  if [[ ${#EXCLUDE_PATHS[@]} -gt 0 ]]; then
    echo "  置換対象外ファイル:"
    for p in "${EXCLUDE_PATHS[@]}"; do
      echo "    ${p}"
    done
    echo ""
  fi
  echo "[dry-run] 実際の変換は --out-dir オプションを付けて ./rename.sh を実行してください"
  exit 0
fi

# 出力先が既存なら確認
if [[ -d "${OUT_DIR}" ]]; then
  echo "Warning: 出力先 ${OUT_DIR} は既に存在します。上書きしますか? [y/N]"
  read -r answer
  if [[ "${answer}" != "y" && "${answer}" != "Y" ]]; then
    echo "中止しました"
    exit 0
  fi
  rm -rf "${OUT_DIR}"
fi

echo ""
echo "Step 1: ソースをコピー中..."
cp -r "${SRC_DIR}" "${OUT_DIR}"

# export/ ディレクトリは出力先に不要なので削除
rm -rf "${OUT_DIR}/export"

echo "Step 2: ディレクトリ・ファイルをリネーム中..."

# src/deepblue → src/<plugin_name>
if [[ -d "${OUT_DIR}/src/deepblue" ]]; then
  mv "${OUT_DIR}/src/deepblue" "${OUT_DIR}/src/${PLUGIN_NAME}"
fi

# runtime/deepblue-helpers.sh → runtime/<plugin_name>-helpers.sh
if [[ -f "${OUT_DIR}/runtime/deepblue-helpers.sh" ]]; then
  mv "${OUT_DIR}/runtime/deepblue-helpers.sh" "${OUT_DIR}/runtime/${PLUGIN_NAME}-helpers.sh"
fi

# テストファイルのリネーム（deepblue ベース）
for _old_new in \
  "tests/lib/test_resolve_deepblue_root.py:tests/lib/test_resolve_${PLUGIN_NAME}_root.py" \
  "tests/lib/test_deepblue_settings.py:tests/lib/test_${PLUGIN_NAME}_settings.py" \
  "tests/lib/test_deepblue_launcher.py:tests/lib/test_${PLUGIN_NAME}_launcher.py" \
  "tests/scripts/test_deepblue_helpers.py:tests/scripts/test_${PLUGIN_NAME}_helpers.py"
do
  _old="${OUT_DIR}/${_old_new%%:*}"
  _new="${OUT_DIR}/${_old_new##*:}"
  [[ -f "${_old}" ]] && mv "${_old}" "${_new}"
done

echo "Step 3: テキスト一括置換中..."

# GNU sed かどうか確認（macOS/Linux 両対応）
if sed --version &>/dev/null 2>&1; then
  SED_INPLACE=(-i)
else
  SED_INPLACE=(-i '')
fi

# 対象ファイルを収集（export/ は除外済み）
mapfile -d '' _ALL_FILES < <(find "${OUT_DIR}" -type f \( \
  -name "*.py" -o -name "*.sh" -o -name "*.md" \
  -o -name "*.toml" -o -name "*.json" -o -name "*.txt" \
  -o -name "*.html" -o -name "*.in" \
\) -print0)

# 除外パスを絶対パスに変換してセット化
declare -A _EXCLUDE_SET
for _rel in "${EXCLUDE_PATHS[@]}"; do
  _EXCLUDE_SET["${OUT_DIR}/${_rel}"]=1
done

# 除外ファイルを除いた配列を構築
TARGET_FILES=()
for _f in "${_ALL_FILES[@]}"; do
  if [[ -z "${_EXCLUDE_SET[${_f}]+x}" ]]; then
    TARGET_FILES+=("${_f}")
  fi
done

if [[ ${#EXCLUDE_PATHS[@]} -gt 0 ]]; then
  echo "  除外ファイル: ${#EXCLUDE_PATHS[@]} 件"
  for _rel in "${EXCLUDE_PATHS[@]}"; do
    echo "    ${_rel}"
  done
fi

# URL は長い方から先に置換（部分マッチ上書き防止）
echo "  URL・作者情報..."
sed "${SED_INPLACE[@]}" \
  -e "s|https://github\.com/aokumablue/deepblue#readme|${REPO_URL_README}|g" \
  -e "s|https://github\.com/aokumablue/deepblue\.git|${REPO_URL_GIT}|g" \
  -e "s|https://github\.com/aokumablue/deepblue/issues|${REPO_URL_ISSUES}|g" \
  -e "s|https://github\.com/aokumablue/deepblue/releases/[^\"']*|${REPO_URL}/releases/download/v0.1.0/model.tar.gz|g" \
  -e "s|https://github\.com/aokumablue/deepblue|${REPO_URL}|g" \
  -e "s|https://github\.com/aokumablue|${AUTHOR_URL}|g" \
  -e "s|\"aokumablue\"|\"${AUTHOR_NAME}\"|g" \
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

echo ""
echo "Step 4: 残留チェック..."
RESIDUAL=$(grep -r "deepblue\|DEEPBLUE\|aokumablue" "${OUT_DIR}" \
  --include="*.py" --include="*.sh" --include="*.toml" --include="*.json" \
  -l 2>/dev/null || true)

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
echo "  完了: ${OUT_DIR}"
echo "=================================================="
