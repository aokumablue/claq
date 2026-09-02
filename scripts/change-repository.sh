#!/usr/bin/env bash
# リポジトリ全体のプラグイン名・作者名を一括置換するスクリプト（直接書き換え）。
#
# 設計方針:
#   - 置換対象に除外リストを持たない。.git / .venv / 生成キャッシュ / バイナリ以外は
#     拡張子を問わず全ファイルを走査する（拡張子アローリストは置換漏れの温床のため廃止）。
#   - 大文字小文字は「一致した綴りに合わせて」変換する（全小文字・全大文字・混在を個別に列挙しない）。
#   - 検証（Step 5）は置換パスとは独立に列挙し直し、内容とパス名の両方を検査する。
#
# 使用法: ./scripts/change-repository.sh [--dry-run]
# 依存: bash 3.2+, python3 3.12+（jq 不要）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/change-repository-config.json"
LICENSE_TEMPLATE="${SCRIPT_DIR}/change-repository-license"

DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help)
      echo "Usage: ./scripts/change-repository.sh [--dry-run]"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if ! command -v python3 &>/dev/null; then
  echo "Error: python3 is required." >&2
  exit 1
fi

if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "Error: config not found: ${CONFIG_FILE}" >&2
  exit 1
fi

# ---- Python ワーカーを一時ファイルへ展開 ----
# 除外規則・大文字小文字規則・バイナリ判定を 1 か所に閉じ込め、
# 置換パスと検証パスが別々にドリフトするのを防ぐ。
WORKER="$(mktemp "${TMPDIR:-/tmp}/change-repository.XXXXXX.py")"

# 設定ファイル自身も置換対象（from_plugin_name が新名へ書き換わり、次回実行の起点になる）。
# ワーカーはサブコマンドごとに別プロセスで設定を読み直すため、置換後に verify が走ると
# 「新名の残留」を探してしまう。開始時点のスナップショットを全サブコマンドへ渡して固定する。
CONFIG_SNAPSHOT="$(mktemp "${TMPDIR:-/tmp}/change-repository-config.XXXXXX.json")"
cp "${CONFIG_FILE}" "${CONFIG_SNAPSHOT}"

trap 'rm -f "${WORKER}" "${CONFIG_SNAPSHOT}"' EXIT

cat > "${WORKER}" <<'PYEOF'
"""change-repository.sh のワーカー。列挙・改名・置換・検証を担う。

サブコマンド:
    config   設定を読み出し shell の eval 用に出力する
    delete   files_to_delete のパターンを削除する
    rename   パス名（ファイル・ディレクトリ）を改名する
    replace  ファイル内容を置換する
    meta     license / authors のメタデータを書き換える
    verify   旧名の残留を内容とパス名の両面から検査する
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
from glob import glob
from pathlib import Path

# 走査から外すディレクトリ。生成物・VCS メタデータのみで、リポジトリの原本は含めない。
EXCLUDED_DIR_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "node_modules",
    "dist",
    "build",
    ".idea",
    ".vscode",
}

# 前方一致で除外するディレクトリ名（.venv / .venv313 などの派生を含む）。
EXCLUDED_DIR_PREFIXES = (".venv",)

# 後方一致で除外するディレクトリ名。
EXCLUDED_DIR_SUFFIXES = (".egg-info",)


def is_excluded_dir(name: str) -> bool:
    """走査対象から外すディレクトリ名かどうかを返す。"""
    if name in EXCLUDED_DIR_NAMES:
        return True
    if name.startswith(EXCLUDED_DIR_PREFIXES):
        return True
    return name.endswith(EXCLUDED_DIR_SUFFIXES)


def walk(root: Path, topdown: bool = True):
    """除外ディレクトリを刈り込みながらリポジトリを走査する。

    os.walk は topdown=False のとき dirnames の書き換えによる刈り込みが効かない
    （降りきってから返すため）。両方向で確実に刈り込むため自前で再帰する。
    """
    dirnames: list[str] = []
    filenames: list[str] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    if not is_excluded_dir(entry.name):
                        dirnames.append(entry.name)
                else:
                    filenames.append(entry.name)
    except OSError:
        return
    if topdown:
        yield root, dirnames, filenames
    for name in list(dirnames):
        yield from walk(root / name, topdown=topdown)
    if not topdown:
        yield root, dirnames, filenames


def iter_files(root: Path):
    """走査対象の全ファイルパスを列挙する（拡張子で絞り込まない）。"""
    for dirpath, _dirnames, filenames in walk(root):
        for name in filenames:
            yield dirpath / name


def read_text(path: Path) -> str | None:
    """テキストファイルとして読める場合のみ内容を返す。バイナリなら None。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def match_case(matched: str, new: str) -> str:
    """一致した綴りの大文字小文字に合わせて置換後の綴りを決める。

    全小文字なら小文字、全大文字なら大文字、それ以外（先頭大文字・キャメルケース）は
    先頭のみ大文字にする。
    """
    if matched.islower():
        return new.lower()
    if matched.isupper():
        return new.upper()
    return new[:1].upper() + new[1:].lower()


def build_replacer(pairs: list[tuple[str, str]]):
    """(旧名, 新名) の組から大文字小文字非依存の置換関数を作る。"""
    compiled = [
        (re.compile(re.escape(old), re.IGNORECASE), new)
        for old, new in pairs
        if old and new and old.lower() != new.lower()
    ]

    def replace(text: str) -> str:
        for pattern, new in compiled:
            text = pattern.sub(lambda m, _n=new: match_case(m.group(0), _n), text)
        return text

    return replace


def load_config(path: Path) -> dict:
    """設定 JSON を読み込み、必須キーの欠落を検出する。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("from_plugin_name", "plugin_name"):
        if not data.get(key):
            raise SystemExit(f"Error: config key '{key}' is missing or empty")
    return data


def cmd_config(cfg: dict) -> None:
    """shell が eval できる形で設定値を出力する。"""
    files = cfg.get("files_to_delete") or []
    out = {
        "FROM_PLUGIN_NAME": cfg["from_plugin_name"],
        "FROM_AUTHOR_NAME": cfg.get("from_author_name", ""),
        "PLUGIN_NAME": cfg["plugin_name"],
        "AUTHOR_NAME": cfg.get("author_name", ""),
        "LICENSE_NAME": cfg.get("license", "") or "",
        "REMOVE_AUTHORS": "true" if cfg.get("remove_authors") else "false",
        "DELETE_COUNT": str(len(files)),
    }
    for key, value in out.items():
        print(f"{key}={shlex.quote(str(value))}")


def cmd_delete(root: Path, cfg: dict, dry_run: bool) -> None:
    """files_to_delete のパターンにマッチするファイル・ディレクトリを削除する。"""
    patterns = cfg.get("files_to_delete") or []
    if not patterns:
        print("  削除対象なし")
        return
    for pattern in patterns:
        matches = sorted(glob(str(root / pattern), recursive=True))
        if not matches:
            print(f"  スキップ（マッチなし）: {pattern}")
            continue
        for target in matches:
            rel = os.path.relpath(target, root)
            if not dry_run:
                if os.path.isdir(target) and not os.path.islink(target):
                    shutil.rmtree(target)
                else:
                    os.remove(target)
            print(f"  削除: {rel}")


def cmd_rename(root: Path, pairs: list[tuple[str, str]], dry_run: bool) -> int:
    """パス名に旧名を含むファイル・ディレクトリを改名する。

    os.walk(topdown=False) で深い側から処理するため、plugins/<name>/src/<name> の
    ような入れ子も 1 パスで解決する。
    """
    replace = build_replacer(pairs)
    renamed = 0
    for dirpath, dirnames, filenames in walk(root, topdown=False):
        for name in sorted(filenames) + sorted(dirnames):
            new_name = replace(name)
            if new_name == name:
                continue
            src = dirpath / name
            dst = dirpath / new_name
            if dst.exists():
                raise SystemExit(f"Error: 改名先が既に存在します: {dst}")
            if not dry_run:
                src.rename(dst)
            print(f"  {src.relative_to(root)} → {dst.relative_to(root)}")
            renamed += 1
    return renamed


def cmd_replace(root: Path, pairs: list[tuple[str, str]], dry_run: bool) -> int:
    """全テキストファイルの内容を置換する。"""
    replace = build_replacer(pairs)
    changed = 0
    skipped_binary = []
    for path in sorted(iter_files(root)):
        text = read_text(path)
        if text is None:
            if any(
                re.search(re.escape(old), path.name, re.IGNORECASE)
                for old, _new in pairs
                if old
            ):
                skipped_binary.append(path.relative_to(root))
            continue
        new_text = replace(text)
        if new_text == text:
            continue
        if not dry_run:
            path.write_text(new_text, encoding="utf-8")
        print(f"  {path.relative_to(root)}")
        changed += 1
    for path in skipped_binary:
        print(f"  [skip] バイナリのため内容は未置換: {path}")
    return changed


def cmd_meta(root: Path, plugin_name: str, license_name: str, remove_authors: bool, dry_run: bool) -> None:
    """license / authors のメタデータを設定に従って書き換える。"""
    marketplace = root / ".claude-plugin" / "marketplace.json"
    plugin_json = root / "plugins" / plugin_name / ".claude-plugin" / "plugin.json"
    pyproject = root / "plugins" / plugin_name / "pyproject.toml"

    def edit_json(path: Path, mutate) -> None:
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        mutate(data)
        if not dry_run:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"  更新: {path.relative_to(root)}")

    if license_name:
        def _set_marketplace_license(data: dict) -> None:
            for entry in data.get("plugins", []):
                entry["license"] = license_name

        edit_json(marketplace, _set_marketplace_license)
        edit_json(plugin_json, lambda data: data.__setitem__("license", license_name))
        if pyproject.exists():
            text = pyproject.read_text(encoding="utf-8")
            new_text = re.sub(
                r'^license = \{ text = "[^"]*" \}',
                f'license = {{ text = "{license_name}" }}',
                text,
                flags=re.MULTILINE,
            )
            if new_text != text:
                if not dry_run:
                    pyproject.write_text(new_text, encoding="utf-8")
                print(f"  更新: {pyproject.relative_to(root)}")

    if remove_authors:
        edit_json(plugin_json, lambda data: data.pop("author", None))
        if pyproject.exists():
            text = pyproject.read_text(encoding="utf-8")
            new_text = re.sub(r"authors = \[[^\]]*\]\n", "", text, flags=re.DOTALL)
            if new_text != text:
                if not dry_run:
                    pyproject.write_text(new_text, encoding="utf-8")
                print(f"  更新: {pyproject.relative_to(root)}")
        print("  ※ marketplace.json の owner は仕様上必須のため削除しません")


def cmd_verify(root: Path, olds: list[str]) -> int:
    """旧名の残留を内容とパス名の両面から検査する。

    置換パスとは独立にリポジトリを列挙し直す。検査対象を置換対象と同じ絞り込みで
    導くと「絞り込み漏れが原因の残留」を検査自身が見逃すため。
    """
    needles = [old for old in olds if old]
    if not needles:
        return 0
    patterns = [re.compile(re.escape(old), re.IGNORECASE) for old in needles]
    hits = 0

    path_hits: list[str] = []
    for dirpath, dirnames, filenames in walk(root):
        for name in sorted(dirnames) + sorted(filenames):
            if any(p.search(name) for p in patterns):
                path_hits.append(str((dirpath / name).relative_to(root)))
    for rel in sorted(set(path_hits)):
        print(f"  [パス名] {rel}")
        hits += 1

    for path in sorted(iter_files(root)):
        text = read_text(path)
        if text is None:
            continue
        lines = [
            f"    {no}: {line.strip()[:160]}"
            for no, line in enumerate(text.splitlines(), 1)
            if any(p.search(line) for p in patterns)
        ]
        if lines:
            print(f"  [内容] {path.relative_to(root)}")
            for line in lines[:5]:
                print(line)
            if len(lines) > 5:
                print(f"    ... 他 {len(lines) - 5} 行")
            hits += 1
    return hits


def main() -> int:
    """サブコマンドを振り分ける。"""
    command = sys.argv[1]
    root = Path(sys.argv[2]).resolve()
    config_path = Path(sys.argv[3])
    dry_run = len(sys.argv) > 4 and sys.argv[4] == "--dry-run"
    cfg = load_config(config_path)

    pairs = [
        (cfg.get("from_author_name", ""), cfg.get("author_name", "")),
        (cfg["from_plugin_name"], cfg["plugin_name"]),
    ]

    if command == "config":
        cmd_config(cfg)
        return 0
    if command == "delete":
        cmd_delete(root, cfg, dry_run)
        return 0
    if command == "rename":
        count = cmd_rename(root, pairs, dry_run)
        if count == 0:
            print("  改名対象なし")
        return 0
    if command == "replace":
        count = cmd_replace(root, pairs, dry_run)
        if count == 0:
            print("  置換対象なし")
        return 0
    if command == "meta":
        cmd_meta(
            root,
            cfg["plugin_name"],
            cfg.get("license", "") or "",
            bool(cfg.get("remove_authors")),
            dry_run,
        )
        return 0
    if command == "verify":
        olds = [cfg["from_plugin_name"]]
        from_author = cfg.get("from_author_name", "")
        if from_author and from_author.lower() != cfg.get("author_name", "").lower():
            olds.append(from_author)
        hits = cmd_verify(root, olds)
        if hits:
            print(f"  残留 {hits} 件")
            return 1
        print("  残留なし")
        return 0
    raise SystemExit(f"Error: unknown command: {command}")


if __name__ == "__main__":
    sys.exit(main())
PYEOF

run_worker() {
  python3 "${WORKER}" "$1" "${REPO_DIR}" "${CONFIG_SNAPSHOT}" ${2:-}
}

eval "$(run_worker config)"

DRY_FLAG=""
if [[ "${DRY_RUN}" == true ]]; then
  DRY_FLAG="--dry-run"
fi

echo "=================================================="
echo "  プラグイン名: ${FROM_PLUGIN_NAME} → ${PLUGIN_NAME}"
if [[ -n "${FROM_AUTHOR_NAME}" && "${FROM_AUTHOR_NAME}" != "${AUTHOR_NAME}" ]]; then
  echo "  作者名      : ${FROM_AUTHOR_NAME} → ${AUTHOR_NAME}"
else
  echo "  作者名      : ${FROM_AUTHOR_NAME}（変更なし）"
fi
echo "  対象        : ${REPO_DIR}（直接置換）"
echo "  削除パターン: ${DELETE_COUNT} 件"
echo "  ライセンス  : ${LICENSE_NAME:-（変更しない）}"
echo "  著者情報削除: ${REMOVE_AUTHORS}"
echo "  dry-run     : ${DRY_RUN}"
echo "=================================================="

if [[ "${DRY_RUN}" != true ]]; then
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
fi

echo ""
echo "Step 1: 不要ファイルを削除中..."
run_worker delete "${DRY_FLAG}"

echo ""
echo "Step 1.5: ライセンスファイルを配置中..."
if [[ -z "${LICENSE_NAME}" ]]; then
  echo "  スキップ（config の license が空。既存 LICENSE を保持します）"
elif [[ ! -f "${LICENSE_TEMPLATE}" ]]; then
  echo "  スキップ（scripts/change-repository-license が見つかりません）"
else
  if [[ "${DRY_RUN}" != true ]]; then
    cat "${LICENSE_TEMPLATE}" > "${REPO_DIR}/LICENSE"
  fi
  echo "  配置: LICENSE"
fi

echo ""
echo "Step 2: パス名（ファイル・ディレクトリ）を改名中..."
run_worker rename "${DRY_FLAG}"

echo ""
echo "Step 3: ファイル内容を置換中..."
run_worker replace "${DRY_FLAG}"

echo ""
echo "Step 3.5: import 順を整形中..."
# 識別子の改名（get_<旧名>_dir → get_<新名>_dir など）でアルファベット順の
# import ブロックが崩れる。置換の副作用なので lint 規則 I（isort）だけを当てて直す。
RUFF_BIN=""
if [[ -x "${REPO_DIR}/.venv/bin/ruff" ]]; then
  RUFF_BIN="${REPO_DIR}/.venv/bin/ruff"
elif command -v ruff &>/dev/null; then
  RUFF_BIN="ruff"
fi
if [[ -z "${RUFF_BIN}" ]]; then
  echo "  スキップ（ruff が見つかりません。'ruff check --fix --select I' を手動で実行してください）"
elif [[ "${DRY_RUN}" == true ]]; then
  echo "  ruff check --fix --select I ${REPO_DIR}"
elif "${RUFF_BIN}" check --fix --select I --quiet "${REPO_DIR}"; then
  echo "  整形完了"
else
  echo "  [Warning] ruff が自動修正しきれない import 順の問題を報告しました" >&2
fi

echo ""
echo "Step 4: メタデータ（license / authors）を更新中..."
if [[ -z "${LICENSE_NAME}" && "${REMOVE_AUTHORS}" != "true" ]]; then
  echo "  スキップ（config で無効）"
else
  run_worker meta "${DRY_FLAG}"
fi

echo ""
echo "Step 5: 残留チェック（内容 + パス名を独立に再列挙）..."
VERIFY_STATUS=0
run_worker verify || VERIFY_STATUS=$?

if [[ "${DRY_RUN}" == true ]]; then
  echo ""
  echo "[dry-run] 実際の変換は --dry-run なしで実行してください"
  echo "  ※ dry-run では改名・置換を行わないため Step 5 は必ず残留を報告します"
  exit 0
fi

echo ""
echo "Step 6: 開発環境の後始末..."
VENV_DIR="${REPO_DIR}/.venv"
if [[ -d "${VENV_DIR}" ]]; then
  STALE_FOUND=false
  while IFS= read -r stale; do
    [[ -e "${stale}" ]] || continue
    rm -rf "${stale}"
    echo "  削除: ${stale#${REPO_DIR}/}"
    STALE_FOUND=true
  done < <(find "${VENV_DIR}" -maxdepth 4 \
    \( -name "*editable*${FROM_PLUGIN_NAME}*" \
       -o -name "${FROM_PLUGIN_NAME}-*.dist-info" \
       -o -name "${FROM_PLUGIN_NAME}.egg-link" \
       -o -name "${FROM_PLUGIN_NAME}" -type d \) 2>/dev/null)
  if [[ "${STALE_FOUND}" == false ]]; then
    echo "  .venv に旧名の editable install 残骸はありません"
  fi
  echo "  → 'bash scripts/install-dev.sh' を再実行して editable install を張り直してください"
else
  echo "  .venv なし（スキップ）"
fi

echo ""
echo "=================================================="
if [[ ${VERIFY_STATUS} -ne 0 ]]; then
  echo "  完了（ただし残留あり — Step 5 の一覧を確認してください）"
  echo "=================================================="
  exit 1
fi
echo "  完了（残留なし）"
echo "=================================================="
echo ""
echo "リポジトリ外の手作業（本スクリプトの対象外）:"
echo "  1. git remote: git remote set-url origin <新 URL>"
echo "  2. ランタイムデータ: mv ~/.${FROM_PLUGIN_NAME} ~/.${PLUGIN_NAME}"
echo "  3. チェックアウト先ディレクトリ名の変更（必要なら）"
echo "  4. bash scripts/install-dev.sh"
