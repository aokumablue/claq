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

FROM_PLUGIN_NAME="$(jq -r '.from_plugin_name' "${CONFIG_FILE}")"
FROM_AUTHOR_NAME="$(jq -r '.from_author_name' "${CONFIG_FILE}")"
FROM_PLUGIN_NAME_UPPER="${FROM_PLUGIN_NAME^^}"
FROM_AUTHOR_URL="https://github.com/${FROM_AUTHOR_NAME}"
FROM_REPO_URL="https://github.com/${FROM_AUTHOR_NAME}/${FROM_PLUGIN_NAME}"

PLUGIN_NAME="$(jq -r '.plugin_name' "${CONFIG_FILE}")"
PLUGIN_NAME_UPPER="${PLUGIN_NAME^^}"
AUTHOR_NAME="$(jq -r '.author_name' "${CONFIG_FILE}")"
AUTHOR_URL="https://github.com/${AUTHOR_NAME}"
REPO_URL="https://github.com/${AUTHOR_NAME}/${PLUGIN_NAME}"

echo "=================================================="
echo "  ${FROM_PLUGIN_NAME} → ${PLUGIN_NAME}"
echo "  対象: ${REPO_DIR}（直接置換）"
echo "  dry-run: ${DRY_RUN}"
echo "=================================================="

if [[ "${DRY_RUN}" == true ]]; then
  echo ""
  echo "[dry-run] 以下の変換を行います:"
  echo ""
  echo "  ディレクトリ・ファイル名（*${FROM_PLUGIN_NAME}* を動的検索）:"
  echo "    plugins/${FROM_PLUGIN_NAME}/**/*${FROM_PLUGIN_NAME}* → plugins/${PLUGIN_NAME}/...${PLUGIN_NAME}..."
  echo "    plugins/${FROM_PLUGIN_NAME}/              → plugins/${PLUGIN_NAME}/"
  echo ""
  echo "  テキスト置換（対象: .py .sh .md .toml .json .txt .html .in）:"
  echo "    ${FROM_PLUGIN_NAME_UPPER}_              → ${PLUGIN_NAME_UPPER}_"
  echo "    ~/.${FROM_PLUGIN_NAME}            → ~/.${PLUGIN_NAME}"
  echo "    \${HOME}/.${FROM_PLUGIN_NAME}     → \${HOME}/.${PLUGIN_NAME}"
  echo "    Path.home()/\".${FROM_PLUGIN_NAME}\" → Path.home()/\".${PLUGIN_NAME}\""
  echo "    from ${FROM_PLUGIN_NAME}          → from ${PLUGIN_NAME}"
  echo "    import ${FROM_PLUGIN_NAME}        → import ${PLUGIN_NAME}"
  echo "    ${FROM_PLUGIN_NAME}.              → ${PLUGIN_NAME}."
  echo "    -m ${FROM_PLUGIN_NAME}.           → -m ${PLUGIN_NAME}."
  echo "    src/${FROM_PLUGIN_NAME}           → src/${PLUGIN_NAME}"
  echo "    \"${FROM_PLUGIN_NAME}\"           → \"${PLUGIN_NAME}\""
  echo "    ${FROM_REPO_URL}/releases/...     → ${REPO_URL}/releases/..."
  echo "    ${FROM_REPO_URL}                  → ${REPO_URL}"
  echo "    ${FROM_AUTHOR_URL}                → ${AUTHOR_URL}"
  echo "    \"${FROM_AUTHOR_NAME}\"           → \"${AUTHOR_NAME}\""
  echo "    ${FROM_AUTHOR_NAME}               → ${AUTHOR_NAME}"
  echo "    ${FROM_PLUGIN_NAME} (残余)        → ${PLUGIN_NAME}"
  echo "    ${FROM_PLUGIN_NAME_UPPER} (残余)  → ${PLUGIN_NAME_UPPER}"
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

if [[ -d "${REPO_DIR}/plugins/${FROM_PLUGIN_NAME}" ]]; then
  # ファイルを先にリネーム（ディレクトリリネーム前）
  while IFS= read -r -d '' f; do
    new="${f//${FROM_PLUGIN_NAME}/${PLUGIN_NAME}}"
    mv "$f" "$new"
    echo "  ${f#${REPO_DIR}/} → ${new#${REPO_DIR}/}"
  done < <(find "${REPO_DIR}/plugins/${FROM_PLUGIN_NAME}" -type f \
    -name "*${FROM_PLUGIN_NAME}*" -print0 | sort -rz)

  # サブディレクトリをリネーム（深い順）
  while IFS= read -r -d '' d; do
    new="${d//${FROM_PLUGIN_NAME}/${PLUGIN_NAME}}"
    mv "$d" "$new"
    echo "  ${d#${REPO_DIR}/} → ${new#${REPO_DIR}/}"
  done < <(find "${REPO_DIR}/plugins/${FROM_PLUGIN_NAME}" -mindepth 1 -type d \
    -name "*${FROM_PLUGIN_NAME}*" -print0 | sort -rz)

  # トップレベルのプラグインディレクトリをリネーム
  mv "${REPO_DIR}/plugins/${FROM_PLUGIN_NAME}" "${REPO_DIR}/plugins/${PLUGIN_NAME}"
  echo "  plugins/${FROM_PLUGIN_NAME} → plugins/${PLUGIN_NAME}"
else
  echo "  plugins/${FROM_PLUGIN_NAME} が見つかりません。スキップ"
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
    -e "s|${FROM_REPO_URL}/releases/[^\"']*|${REPO_URL}/releases/download/v0.1.0/model.tar.gz|g" \
    -e "s|${FROM_REPO_URL}|${REPO_URL}|g" \
    -e "s|${FROM_AUTHOR_URL}|${AUTHOR_URL}|g" \
    -e "s|\"${FROM_AUTHOR_NAME}\"|\"${AUTHOR_NAME}\"|g" \
    -e "s|${FROM_AUTHOR_NAME}|${AUTHOR_NAME}|g" \
    "${TARGET_FILES[@]}"

  echo "  環境変数・パス..."
  sed "${SED_INPLACE[@]}" \
    -e "s|${FROM_PLUGIN_NAME_UPPER}_|${PLUGIN_NAME_UPPER}_|g" \
    -e "s|~/\.${FROM_PLUGIN_NAME}|~/.${PLUGIN_NAME}|g" \
    -e "s|\${HOME}/\.${FROM_PLUGIN_NAME}|\${HOME}/.${PLUGIN_NAME}|g" \
    -e "s|Path\.home() / \"\.${FROM_PLUGIN_NAME}\"|Path.home() / \".${PLUGIN_NAME}\"|g" \
    "${TARGET_FILES[@]}"

  echo "  Python インポート・モジュール..."
  sed "${SED_INPLACE[@]}" \
    -e "s|from ${FROM_PLUGIN_NAME}|from ${PLUGIN_NAME}|g" \
    -e "s|import ${FROM_PLUGIN_NAME}\b|import ${PLUGIN_NAME}|g" \
    -e "s|${FROM_PLUGIN_NAME}\.|${PLUGIN_NAME}.|g" \
    -e "s|-m ${FROM_PLUGIN_NAME}\.|-m ${PLUGIN_NAME}.|g" \
    "${TARGET_FILES[@]}"

  echo "  パッケージ・ディレクトリ参照..."
  sed "${SED_INPLACE[@]}" \
    -e "s|src/${FROM_PLUGIN_NAME}|src/${PLUGIN_NAME}|g" \
    -e "s|\"${FROM_PLUGIN_NAME}\"|\"${PLUGIN_NAME}\"|g" \
    "${TARGET_FILES[@]}"

  echo "  残余..."
  sed "${SED_INPLACE[@]}" \
    -e "s|${FROM_PLUGIN_NAME}|${PLUGIN_NAME}|g" \
    -e "s|${FROM_PLUGIN_NAME_UPPER}|${PLUGIN_NAME_UPPER}|g" \
    "${TARGET_FILES[@]}"
fi

echo ""
echo "Step 3: 残留チェック..."
RESIDUAL=$(grep -r "${FROM_PLUGIN_NAME}\|${FROM_PLUGIN_NAME_UPPER}\|${FROM_AUTHOR_NAME}" \
  "${REPO_DIR}" \
  --include="*.py" --include="*.sh" --include="*.md" \
  --include="*.toml" --include="*.json" --include="*.txt" \
  --include="*.html" --include="*.in" \
  --exclude-dir=".git" \
  -l 2>/dev/null \
  | grep -v -e "^${SCRIPT_DIR}/rename\.sh$" -e "^${SCRIPT_DIR}/rename-config\.json$" || true)

if [[ -n "${RESIDUAL}" ]]; then
  echo "  [Warning] 以下のファイルに置換漏れの可能性があります:"
  echo "${RESIDUAL}" | while read -r f; do
    echo "    ${f}"
    grep -n "${FROM_PLUGIN_NAME}\|${FROM_PLUGIN_NAME_UPPER}\|${FROM_AUTHOR_NAME}" "${f}" | head -5 | \
      sed 's/^/      /'
  done
else
  echo "  残留なし"
fi

echo ""
echo "=================================================="
echo "  完了"
echo "=================================================="
