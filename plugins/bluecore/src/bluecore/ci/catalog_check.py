"""リポジトリのカタログ件数を README.md と CLAUDE.md と照合する。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from bluecore.ci.ci_common import REPO_ROOT

DEFAULT_ROOT = REPO_ROOT
DEFAULT_README_PATH = REPO_ROOT / "README.md"
DEFAULT_CLAUDE_PATH = REPO_ROOT / "CLAUDE.md"
CATEGORY_LABELS = {
    "agents": "エージェント",
    "commands": "コマンド",
    "skills": "スキル",
}
SOURCE_LABELS = {
    "README.md quick-start summary": "README.md のクイックスタート要約",
    "README.md comparison table": "README.md の比較表",
    "CLAUDE.md project structure": "CLAUDE.md のプロジェクト構成",
}


def _normalize_path_segments(relative_path: str) -> str:
    """パスセグメントを正規化してスラッシュに統一する。

    Args:
        relative_path: 正規化する相対パス

    Returns:
        スラッシュに統一されたパス文字列

    Raises:
        例外は発生しません。
    """
    return relative_path.replace("\\", "/")


def list_matching_files(relative_dir: str, matcher: Any, root: str | Path = DEFAULT_ROOT) -> list[str]:
    """指定されたディレクトリからマッチャーに一致するファイルをリストアップする。

    Args:
        relative_dir: ルートからの相対ディレクトリパス
        matcher: ファイルエントリを判定する関数
        root: ルートディレクトリパス（デフォルト: REPO_ROOT）

    Returns:
        正規化された相対パスのソート済みリスト

    Raises:
        例外は発生しません（存在しないディレクトリは空リストを返す）。
    """
    directory = Path(root) / relative_dir
    if not directory.exists():
        return []

    files: list[str] = []
    for entry in os.scandir(directory):
        if matcher(entry):
            files.append(_normalize_path_segments(str(Path(relative_dir) / entry.name)))
    files.sort()
    return files


def build_catalog(root: str | Path = DEFAULT_ROOT) -> dict[str, Any]:
    """リポジトリのカタログを構築する。

    Args:
        root: リポジトリのルートディレクトリパス

    Returns:
        エージェント、コマンド、スキルのカウント、ファイルリスト、グロブパターンを含む辞書

    Raises:
        例外は発生しません。
    """
    root_path = Path(root)
    agents = list_matching_files(
        "agents",
        lambda entry: entry.is_file(follow_symlinks=False) and entry.name.endswith(".md"),
        root_path,
    )
    commands = list_matching_files(
        "commands",
        lambda entry: entry.is_file(follow_symlinks=False) and entry.name.endswith(".md"),
        root_path,
    )
    skills = list_matching_files(
        "skills",
        lambda entry: entry.is_dir(follow_symlinks=False) and (root_path / "skills" / entry.name / "SKILL.md").exists(),
        root_path,
    )
    skills = [f"{skill_dir}/SKILL.md" for skill_dir in skills]

    return {
        "agents": {"count": len(agents), "files": agents, "glob": "agents/*.md"},
        "commands": {"count": len(commands), "files": commands, "glob": "commands/*.md"},
        "skills": {"count": len(skills), "files": skills, "glob": "skills/*/SKILL.md"},
    }


def read_file_or_throw(file_path: str | Path) -> str:
    """ファイルを読み込み、失敗時は例外を発生させる。

    Args:
        file_path: 読み込むファイルのパス

    Returns:
        ファイルの内容（UTF-8エンコード）

    Raises:
        RuntimeError: ファイルの読み取りに失敗した場合
    """
    try:
        return Path(file_path).read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError(f"{Path(file_path).name} の読み取りに失敗しました: {error}") from error


def _parse_readme_quick_start(readme_content: str) -> list[dict[str, Any]]:
    """README.md のクイックスタート要約からカタログ件数を抽出する。

    Args:
        readme_content: README.md のテキスト内容

    Returns:
        agents/skills/commands の期待値辞書リスト

    Raises:
        RuntimeError: クイックスタート要約が見つからない場合
    """
    quick_start_match = re.search(
        r"access to\s+(\d+)\s+agents,\s+(\d+)\s+skills,\s+and\s+(\d+)\s+commands", readme_content, re.I
    )
    if not quick_start_match:
        raise RuntimeError("README.md にクイックスタートのカタログ要約がありません")

    source = "README.md quick-start summary"
    return [
        {"category": "agents", "mode": "exact", "expected": int(quick_start_match.group(1)), "source": source},
        {"category": "skills", "mode": "exact", "expected": int(quick_start_match.group(2)), "source": source},
        {"category": "commands", "mode": "exact", "expected": int(quick_start_match.group(3)), "source": source},
    ]


def _parse_readme_comparison_table(readme_content: str) -> list[dict[str, Any]]:
    """README.md の比較表からカテゴリ別のカタログ件数を抽出する。

    Args:
        readme_content: README.md のテキスト内容

    Returns:
        比較表の各行に対応する期待値辞書リスト

    Raises:
        RuntimeError: 比較表に必要な行が見つからない場合
    """
    table_patterns = [
        {
            "category": "agents",
            "regex": r"\|\s*(?:\*\*)?Agents(?:\*\*)?\s*\|\s*(?:(?:PASS:|\u2705)\s*)?(\d+)\s+agents\s*\|",
            "source": "README.md comparison table",
        },
        {
            "category": "commands",
            "regex": r"\|\s*(?:\*\*)?Commands(?:\*\*)?\s*\|\s*(?:(?:PASS:|\u2705)\s*)?(\d+)\s+commands\s*\|",
            "source": "README.md comparison table",
        },
        {
            "category": "skills",
            "regex": r"\|\s*(?:\*\*)?Skills(?:\*\*)?\s*\|\s*(?:(?:PASS:|\u2705)\s*)?(\d+)\s+skills\s*\|",
            "source": "README.md comparison table",
        },
    ]

    expectations: list[dict[str, Any]] = []
    for pattern in table_patterns:
        match = re.search(pattern["regex"], readme_content, re.I)
        if not match:
            raise RuntimeError(
                f"{SOURCE_LABELS.get(pattern['source'], pattern['source'])} に "
                f"{CATEGORY_LABELS.get(pattern['category'], pattern['category'])} 行がありません"
            )
        expectations.append(
            {
                "category": pattern["category"],
                "mode": "exact",
                "expected": int(match.group(1)),
                "source": f"{pattern['source']} ({pattern['category']})",
            }
        )

    return expectations


def parse_readme_expectations(readme_content: str) -> list[dict[str, Any]]:
    """README.md から期待されるカタログ件数を抽出する。

    Args:
        readme_content: README.md のテキスト内容

    Returns:
        期待値の辞書リスト（カテゴリ、モード、期待件数、ソースを含む）

    Raises:
        RuntimeError: 必要なカタログ情報が見つからない場合
    """
    return [
        *_parse_readme_quick_start(readme_content),
        *_parse_readme_comparison_table(readme_content),
    ]


def _match_claude_structure_pattern(claude_content: str, pattern: dict[str, str]) -> dict[str, Any]:
    """CLAUDE.md の構成パターン1件を照合して期待値辞書を生成する。

    Args:
        claude_content: CLAUDE.md のテキスト内容
        pattern: category/mode/regex/source を含むパターン定義

    Returns:
        マッチした件数を含む期待値辞書

    Raises:
        RuntimeError: パターンに一致するエントリが見つからない場合
    """
    match = re.search(pattern["regex"], claude_content, re.I | re.M)
    if not match:
        raise RuntimeError(
            f"{SOURCE_LABELS.get(pattern['source'], pattern['source'])} に "
            f"{CATEGORY_LABELS.get(pattern['category'], pattern['category'])} エントリがありません"
        )
    return {
        "category": pattern["category"],
        "mode": "exact",
        "expected": int(match.group(1)),
        "source": f"{pattern['source']} ({pattern['category']})",
    }


def parse_claude_doc_expectations(claude_content: str) -> list[dict[str, Any]]:
    """CLAUDE.md から期待されるカタログ件数を抽出する。

    Args:
        claude_content: CLAUDE.md のテキスト内容

    Returns:
        期待値の辞書リスト（カテゴリ、モード、期待件数、ソースを含む）

    Raises:
        RuntimeError: 必要なカタログ情報が見つからない場合
    """
    structure_patterns = [
        {
            "category": "agents",
            "mode": "exact",
            "regex": r"^\s*-\s+\*\*agents/\*\*\s*-\s*(\d+)\s+個の専門サブエージェント",
            "source": "CLAUDE.md project structure",
        },
        {
            "category": "skills",
            "mode": "exact",
            "regex": r"^\s*-\s+\*\*skills/\*\*\s*-\s*(\d+)\s+個のワークフロー定義とドメイン知識",
            "source": "CLAUDE.md project structure",
        },
        {
            "category": "commands",
            "mode": "exact",
            "regex": r"^\s*-\s+\*\*commands/\*\*\s*-\s*(\d+)\s+個のスラッシュコマンド",
            "source": "CLAUDE.md project structure",
        },
    ]

    return [_match_claude_structure_pattern(claude_content, pattern) for pattern in structure_patterns]


def evaluate_expectations(catalog: dict[str, Any], expectations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """カタログの実際の件数と期待値を照合して評価する。

    Args:
        catalog: build_catalog() から得られた実際のカタログ
        expectations: parse_readme_expectations() などから得られた期待値リスト

    Returns:
        各期待値に実際の件数とok判定を追加した結果リスト

    Raises:
        例外は発生しません。
    """
    results: list[dict[str, Any]] = []
    for expectation in expectations:
        actual = catalog[expectation["category"]]["count"]
        ok = (
            actual >= expectation["expected"] if expectation["mode"] == "minimum" else actual == expectation["expected"]
        )
        results.append({**expectation, "actual": actual, "ok": ok})
    return results


def format_expectation(expectation: dict[str, Any]) -> str:
    """期待値と実際の件数を日本語メッセージにフォーマットする。

    Args:
        expectation: 評価済みの期待値辞書

    Returns:
        日本語で整形されたメッセージ文字列

    Raises:
        例外は発生しません。
    """
    comparator = ">=" if expectation["mode"] == "minimum" else "="
    source = SOURCE_LABELS.get(expectation["source"], expectation["source"])
    category = CATEGORY_LABELS.get(expectation["category"], expectation["category"])
    return (
        f"{source}: {category} の文書化件数は {comparator} "
        f"{expectation['expected']}、実際は {expectation['actual']} です"
    )


def render_text(result: dict[str, Any]) -> None:
    """検証結果をプレーンテキスト形式で出力する。

    Args:
        result: カタログと検証結果を含む辞書

    Returns:
        戻り値はありません。

    Raises:
        例外は発生しません。
    """
    print("カタログ件数:")
    print(f"- エージェント: {result['catalog']['agents']['count']}")
    print(f"- コマンド: {result['catalog']['commands']['count']}")
    print(f"- スキル: {result['catalog']['skills']['count']}")
    print()

    mismatches = [check for check in result["checks"] if not check["ok"]]
    if not mismatches:
        print("ドキュメント件数はリポジトリのカタログと一致しています。")
        return

    print("ドキュメント件数の不一致が見つかりました:", file=sys.stderr)
    for mismatch in mismatches:
        print(f"- {format_expectation(mismatch)}", file=sys.stderr)


def render_markdown(result: dict[str, Any]) -> None:
    """検証結果をMarkdown形式で出力する。

    Args:
        result: カタログと検証結果を含む辞書

    Returns:
        戻り値はありません。

    Raises:
        例外は発生しません。
    """
    mismatches = [check for check in result["checks"] if not check["ok"]]
    print("# bluecore カタログ検証\n")
    print("| カテゴリ | 件数 | パターン |")
    print("| --- | ---: | --- |")
    print(f"| エージェント | {result['catalog']['agents']['count']} | `{result['catalog']['agents']['glob']}` |")
    print(f"| コマンド | {result['catalog']['commands']['count']} | `{result['catalog']['commands']['glob']}` |")
    print(f"| スキル | {result['catalog']['skills']['count']} | `{result['catalog']['skills']['glob']}` |")
    print()

    if not mismatches:
        print("ドキュメント件数はリポジトリのカタログと一致しています。")
        return

    print("## 不一致\n")
    for mismatch in mismatches:
        print(f"- {format_expectation(mismatch)}")


def build_parser() -> argparse.ArgumentParser:
    """CLI パーサーを構築する。

    Returns:
        --root / --readme-path / --claude-path / --md / --text を
        受け付ける argparse.ArgumentParser インスタンス（既定は JSON 出力）。
    """
    parser = argparse.ArgumentParser(
        description="Verify repo catalog counts against README.md and CLAUDE.md", add_help=False
    )
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--readme-path", default=str(DEFAULT_README_PATH))
    parser.add_argument("--claude-path", default=str(DEFAULT_CLAUDE_PATH))
    parser.add_argument("--md", action="store_true")
    parser.add_argument("--text", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI のエントリポイント。

    Args:
        argv: コマンドライン引数。None の場合は sys.argv を使う。

    Returns:
        全チェック合格なら 0、未合格のチェックがあるか例外発生時は 1。
    """
    args, _unknown = build_parser().parse_known_args(argv)
    try:
        catalog = build_catalog(args.root)
        readme_content = read_file_or_throw(args.readme_path)
        claude_content = read_file_or_throw(args.claude_path)
        expectations = [
            *parse_readme_expectations(readme_content),
            *parse_claude_doc_expectations(claude_content),
        ]
        checks = evaluate_expectations(catalog, expectations)
        result = {"catalog": catalog, "checks": checks}

        if args.md:
            render_markdown(result)
        elif args.text:
            render_text(result)
        else:
            print(json.dumps(result, indent=2, ensure_ascii=False))

        if any(not check["ok"] for check in checks):
            return 1
        return 0
    except (OSError, RuntimeError) as error:
        print(f"エラー: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
