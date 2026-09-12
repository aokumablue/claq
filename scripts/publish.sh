#!/bin/bash
# claq（ユーザー向け公開リポジトリ）へ dev スナップショットを線形履歴で公開する。
#
# 使い方:
#   ./scripts/publish.sh [--no-push]
#
#   --no-push   版アップ・コミット・publish コミット構築までローカルで実施し、
#               dev / publish 両 origin への push を省略する（検証用）。
#
# 動作:
#   1. dev 作業ツリーが clean か確認（未コミット変更があれば中断）
#   2. 公開先 origin/main の版を末尾桁 +1 した新版を算出
#   3. version-up.sh で dev の版ファイルを更新し pytest+ruff を通してコミット
#   4. dev スナップショット（dev 専用ファイル除外）を公開先 origin/main の上に
#      線形リリースコミットとして構築
#   5. dev origin → publish origin の順に push
#
# 次版は「公開先 origin/main の公開版 +1」から導出するため、push 失敗後に
# 再実行しても版が飛ばず安全に再開できる。
set -euo pipefail

PUBLISH_REMOTE="${CLAQ_PUBLISH_REMOTE:-$HOME/dev/claq}"
VENV="${CLAQ_VENV:-}"
NO_PUSH=false
TMPDIR=""
BOOTSTRAP=false

usage() {
  cat <<'EOF'
Usage: ./scripts/publish.sh [--no-push]

  --no-push   ローカル構築のみ実施し dev / publish への push を省略（検証用）
  --help      このヘルプを表示
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-push)
      NO_PUSH=true
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

trap 'if [[ -n "${TMPDIR}" ]]; then rm -rf "${TMPDIR}"; fi' EXIT

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "${REPO_ROOT}"
# gate 用 venv 既定は repo ローカル .venv（CLAQ_VENV で上書き可）
: "${VENV:=${REPO_ROOT}/.venv}"

PLUGIN_JSON="plugins/claq/.claude-plugin/plugin.json"
VERSION_FILES=(
  "plugins/claq/pyproject.toml"
  "${PLUGIN_JSON}"
  ".claude-plugin/marketplace.json"
  "plugins/claq/src/claq/mem/__init__.py"
)

# プラグイン版を JSON から読む（引数: plugin.json のパス）
read_version() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$1"
}

# 末尾桁 +1（引数: X.Y.Z → X.Y.(Z+1)）
bump_patch() {
  python3 -c '
import sys
parts = sys.argv[1].split(".")
if len(parts) != 3 or not all(p.isdigit() for p in parts):
    sys.exit(f"invalid version: {sys.argv[1]}")
parts[2] = str(int(parts[2]) + 1)
print(".".join(parts))
' "$1"
}

# ホスト component inventory ゲート（docs/adr/verification-scope-release-gates.md）。
#
# manifest も公式 schema も validator も「正しい」と答えるのに、ホストの
# loader が component をまるごと登録できていない、という非互換が実際に起きた
# （v0.9.41 の F-01: manifest の agents 宣言により 9 体全てが ENOTDIR）。
# 静的検証では原理的に検出できないため、ツリーをホストに読ませて登録結果を
# 1 回問い合わせる。docs/adr/verification-scope-release-gates.md はこれをリリース条件と定めている。
#
# `claude` が PATH に無い環境では実行できない。その場合は警告のうえ続行する
# （manifest 形式の退行自体は tests/test_plugin_manifest.py が常時検出する）。
run_host_inventory_gate() {
  if ! command -v claude >/dev/null 2>&1; then
    echo "WARNING: claude が PATH にありません。host component inventory ゲート（docs/adr/verification-scope-release-gates.md）を実行できません。" >&2
    return 0
  fi

  echo "Running host component inventory gate (docs/adr/verification-scope-release-gates.md)..."
  claude plugin validate --strict plugins/claq || return 1

  local details expected_agents expected_surfaces
  details="$(claude --plugin-dir plugins/claq plugin details claq@inline 2>&1)" || return 1

  if grep -q "Failed to read plugin components" <<<"${details}"; then
    echo "ERROR: ホストが component を読めていません（docs/adr/verification-scope-release-gates.md ゲート 3）。" >&2
    echo "${details}" >&2
    return 1
  fi

  # ディスク上の実体数と、ホストが報告した登録数を突き合わせる。
  # ホストは skills/ と commands/ を合算して Skills として数える。
  expected_agents="$(find plugins/claq/agents -maxdepth 1 -name '*.md' | wc -l | tr -d ' ')"
  expected_surfaces=$((
    $(find plugins/claq/skills -maxdepth 2 -name 'SKILL.md' | wc -l | tr -d ' ') +
    $(find plugins/claq/commands -maxdepth 1 -name '*.md' | wc -l | tr -d ' ')
  ))

  grep -qE "Agents \(${expected_agents}\)" <<<"${details}" || {
    echo "ERROR: Agents 登録数がディスク上の ${expected_agents} 件と一致しません（docs/adr/verification-scope-release-gates.md ゲート 2）。" >&2
    echo "${details}" >&2
    return 1
  }
  grep -qE "Skills \(${expected_surfaces}\)" <<<"${details}" || {
    echo "ERROR: Skills 登録数がディスク上の ${expected_surfaces} 件と一致しません（docs/adr/verification-scope-release-gates.md ゲート 2）。" >&2
    echo "${details}" >&2
    return 1
  }
  return 0
}

# pytest + ruff + host inventory ゲート（venv のバイナリを直接実行）
run_gate() {
  echo "Running test gate (pytest + ruff)..."
  if [[ ! -x "${VENV}/bin/python" ]]; then
    echo "ERROR: venv が見つかりません: ${VENV}" >&2
    return 1
  fi
  "${VENV}/bin/python" -m pytest -q || return 1
  "${VENV}/bin/ruff" check plugins/claq || return 1
  run_host_inventory_gate || return 1
  return 0
}

# ── Preflight（検証のみ・無変更）──
if [[ -n "$(git status --porcelain)" ]]; then
  echo "ERROR: 作業ツリーに未コミット変更があります。コミットまたは退避してから再実行してください。" >&2
  git status --short >&2
  exit 1
fi

if [[ ! -d "${PUBLISH_REMOTE}/.git" ]]; then
  echo "ERROR: 公開先リポジトリが見つかりません: ${PUBLISH_REMOTE}" >&2
  exit 1
fi

echo "Fetching publish origin..."
# 空リポジトリでは fetch が失敗し得るので許容する
git -C "${PUBLISH_REMOTE}" fetch --quiet origin || true

DEVVER="$(read_version "${PLUGIN_JSON}")"

if ! git -C "${PUBLISH_REMOTE}" rev-parse --verify --quiet origin/main >/dev/null; then
  # origin/main 不在 → 初回公開（bootstrap）として続行
  BOOTSTRAP=true
  PUBLISHED="(none)"
  # 冪等性: dev HEAD が既に release: v${DEVVER} なら bump 済みとみなし再開
  if [[ "$(git log -1 --pretty=%s)" == "release: v${DEVVER}" ]]; then
    NEXT="${DEVVER}"
    MODE="resume"
  else
    NEXT="$(bump_patch "${DEVVER}")"
    MODE="normal"
  fi
  echo "公開版: ${PUBLISHED} / dev版: ${DEVVER} / 次版: ${NEXT}"
  echo "Bootstrap モード: 公開先に origin/main がありません。初回公開として v${NEXT} を公開します。"
else
  PUBLISHED="$(git -C "${PUBLISH_REMOTE}" show "origin/main:${PLUGIN_JSON}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
  NEXT="$(bump_patch "${PUBLISHED}")"
  echo "公開版: ${PUBLISHED} / dev版: ${DEVVER} / 次版: ${NEXT}"

  # ── モード判定（冪等再開）──
  if [[ "${DEVVER}" == "${PUBLISHED}" ]]; then
    MODE="normal"
  elif [[ "${DEVVER}" == "${NEXT}" ]]; then
    MODE="resume"
    echo "再開モード: dev は既に v${NEXT}。版アップをスキップします。"
  else
    echo "ERROR: 版が不整合です (dev=${DEVVER}, published=${PUBLISHED})。手動で確認してください。" >&2
    exit 1
  fi
fi

# ── 版アップ + テストゲート + コミット ──
if [[ "${MODE}" == "normal" ]]; then
  echo "Bumping version ${DEVVER} -> ${NEXT}..."
  bash scripts/version-up.sh --version "${NEXT}"
  if ! run_gate; then
    git restore -- "${VERSION_FILES[@]}"
    echo "ERROR: テストゲート失敗。版アップを巻き戻しました（push なし）。" >&2
    exit 1
  fi
  git add -- "${VERSION_FILES[@]}"
  git commit -m "release: v${NEXT}"
else
  if ! run_gate; then
    echo "ERROR: テストゲート失敗（再開モード、push なし）。" >&2
    exit 1
  fi
fi

# ── publish スナップショット構築（ローカル）──
TMPDIR="$(mktemp -d)"

# 配布ツリーから外す開発専用アーティファクト。
# pyproject.toml を外すのは 2 つの理由による:
#   1. testpaths=["tests"] と fail_under=100 を持つ pyproject が、tests/ を
#      除去したツリーへ同梱されると、配布ツリーで pytest を叩いたときに
#      「coverage 0% で FAIL」という回帰そっくりの失敗が出る。過去 3 回の
#      実機監査がこれを「テストが消失した」と誤報告した（docs/adr/verification-scope-release-gates.md）。
#   2. wheel は src/claq しか含まず plugin assets も entry point も持たない
#      ため、配布ツリーに build 設定を残すと非機能 artifact を公開できてしまう。
# ランタイムは launcher.py が sys.path へ src/ を挿すだけで、パッケージ
# メタデータを一切参照しないので配布ツリーに pyproject は不要。
echo "Cloning and filtering dev snapshot..."
git clone --quiet --local --no-hardlinks . "${TMPDIR}/repo"
(
  cd "${TMPDIR}/repo"
  git filter-repo \
    --invert-paths \
    --path plugins/claq/tests/ \
    --path plugins/claq/pyproject.toml \
    --path scripts/ \
    --path CLAUDE.md \
    --path conftest.py \
    --force
)

echo "Building linear release commit on publish repo..."
git -C "${PUBLISH_REMOTE}" fetch --no-tags --quiet "${TMPDIR}/repo" HEAD
TREE="$(git -C "${PUBLISH_REMOTE}" rev-parse 'FETCH_HEAD^{tree}')"
if [[ "${BOOTSTRAP}" == true ]]; then
  # bootstrap: 親なしの root リリースコミット（ancestor チェック不要）
  NEW="$(git -C "${PUBLISH_REMOTE}" commit-tree "${TREE}" -m "release: v${NEXT}")"
  git -C "${PUBLISH_REMOTE}" update-ref refs/heads/main "${NEW}"
  git -C "${PUBLISH_REMOTE}" reset --quiet --hard main
else
  PARENT="$(git -C "${PUBLISH_REMOTE}" rev-parse origin/main)"
  NEW="$(git -C "${PUBLISH_REMOTE}" commit-tree "${TREE}" -p "${PARENT}" -m "release: v${NEXT}")"
  git -C "${PUBLISH_REMOTE}" update-ref refs/heads/main "${NEW}"
  git -C "${PUBLISH_REMOTE}" reset --quiet --hard main

  if ! git -C "${PUBLISH_REMOTE}" merge-base --is-ancestor "${PARENT}" "${NEW}"; then
    echo "ERROR: 構築したコミットが origin/main の子孫ではありません（fast-forward 不可）。" >&2
    exit 1
  fi
fi

# ── push（最後・不可逆）──
if [[ "${NO_PUSH}" == true ]]; then
  echo ""
  echo "=== --no-push: ローカル構築完了（push なし）==="
  echo "  dev    : $(git rev-parse --short HEAD)  release: v${NEXT}"
  if [[ "${BOOTSTRAP}" == true ]]; then
    PARENT_DISP="(none / bootstrap)"
  else
    PARENT_DISP="$(git -C "${PUBLISH_REMOTE}" rev-parse --short origin/main)"
  fi
  echo "  publish: $(git -C "${PUBLISH_REMOTE}" rev-parse --short main)  (parent: ${PARENT_DISP})"
  echo "  push するには --no-push なしで再実行してください。"
  exit 0
fi

echo "Pushing dev to origin..."
if ! git push origin HEAD:main; then
  echo "ERROR: dev の push に失敗しました。再実行で再開できます（版は飛びません）。" >&2
  exit 1
fi

echo "Pushing publish to origin..."
if ! git -C "${PUBLISH_REMOTE}" push origin main; then
  echo "ERROR: publish の push に失敗しました。再実行で再開できます（版は飛びません）。" >&2
  exit 1
fi

echo ""
echo "Done: v${NEXT} を公開しました。"
echo "  dev    : $(git rev-parse --short HEAD)"
echo "  publish: $(git -C "${PUBLISH_REMOTE}" rev-parse --short main)"
