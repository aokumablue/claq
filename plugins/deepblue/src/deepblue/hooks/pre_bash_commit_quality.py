#!/usr/bin/env python3
"""
コミット前にステージ済みファイルの品質を確認します。

pre:bash で `git commit` を検出したときだけ、lint や簡易静的チェックを実行します。
問題が見つかった場合はコミットを止め、それ以外は入力をそのまま通過させます。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from devgear.hooks.hook_common import parse_json_object
from devgear.lib.core_utils import log


def get_staged_files() -> list[str]:
    """ステージング済みファイルの一覧を取得します。

    Returns:
        ステージングされたファイルパスのリストを返します。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return []
        return [f for f in result.stdout.strip().split("\n") if f]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def get_staged_file_content(file_path: str) -> str | None:
    """ステージング済みファイルの内容を取得します。

    Args:
        file_path: 対象ファイルのパスです。

    Returns:
        ファイル内容、または取得できない場合は None を返します。

    Raises:
        例外は発生しません。
    """
    try:
        result = subprocess.run(
            ["git", "show", f":{file_path}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def should_check_file(file_path: str) -> bool:
    """対象ファイルかどうかを判定します。

    Args:
        file_path: 判定対象のファイルパスです。

    Returns:
        品質チェック対象なら True を返します。

    Raises:
        例外は発生しません。
    """
    checkable_extensions = {".js", ".jsx", ".ts", ".tsx", ".py", ".go", ".rs"}
    return Path(file_path).suffix in checkable_extensions


def find_file_issues(file_path: str) -> list[dict]:
    """ファイル内容から代表的な問題を検出します。

    Args:
        file_path: 調査対象のファイルパスです。

    Returns:
        検出した問題の辞書リストを返します。

    Raises:
        例外は発生しません。
    """
    issues = []

    try:
        content = get_staged_file_content(file_path)
        if content is None:
            return issues

        lines = content.split("\n")

        for index, line in enumerate(lines):
            line_num = index + 1

            # console.log をチェック
            if "console.log" in line and not line.strip().startswith("//") and not line.strip().startswith("*"):
                issues.append(
                    {
                        "type": "console.log",
                        "message": f"console.log found at line {line_num}",
                        "line": line_num,
                        "severity": "warning",
                    }
                )

            # debugger 文をチェック
            if re.search(r"\bdebugger\b", line) and not line.strip().startswith("//"):
                issues.append(
                    {
                        "type": "debugger",
                        "message": f"debugger statement at line {line_num}",
                        "line": line_num,
                        "severity": "error",
                    }
                )

            # Issue 参照のない TODO/FIXME をチェック
            todo_match = re.search(r"(?://|#)\s*(TODO|FIXME):?\s*(.+)", line)
            if todo_match and not re.search(r"#\d+|issue", todo_match.group(2), re.IGNORECASE):
                issues.append(
                    {
                        "type": "todo",
                        "message": f'TODO/FIXME without issue reference at line {line_num}: "{todo_match.group(2).strip()}"',
                        "line": line_num,
                        "severity": "info",
                    }
                )

            # ハードコードされたシークレットをチェック（基本パターン）
            secret_patterns = [
                (r"sk-[a-zA-Z0-9]{20,}", "OpenAI API key"),
                (r"ghp_[a-zA-Z0-9]{36}", "GitHub PAT"),
                (r"AKIA[A-Z0-9]{16}", "AWS Access Key"),
                (r"api[_-]?key\s*[=:]\s*['\"][^'\"]+['\"]", "API key"),
            ]

            for pattern, name in secret_patterns:
                if re.search(pattern, line, re.IGNORECASE):
                    issues.append(
                        {
                            "type": "secret",
                            "message": f"Potential {name} exposed at line {line_num}",
                            "line": line_num,
                            "severity": "error",
                        }
                    )

    except Exception:
        # ファイルが読めない場合はスキップ
        pass

    return issues


def validate_commit_message(command: str) -> dict | None:
    """コミットメッセージの形式を検証します。

    Args:
        command: `git commit` コマンド文字列です。

    Returns:
        メッセージと問題一覧を含む辞書、またはメッセージがない場合は None を返します。

    Raises:
        例外は発生しません。
    """
    # コマンドからコミットメッセージを抽出
    message_match = re.search(r"(?:-m|--message)[=\s]+[\"']?([^\"']+)[\"']?", command)
    if not message_match:
        return None

    message = message_match.group(1)
    issues = []

    # コンベンショナルコミット形式をチェック
    conventional_commit = re.compile(r"^(feat|fix|docs|style|refactor|test|chore|build|ci|perf|revert)(\(.+\))?:\s*.+")
    if not conventional_commit.match(message):
        issues.append(
            {
                "type": "format",
                "message": "Commit message does not follow conventional commit format",
                "suggestion": 'Use format: type(scope): description (e.g., "feat(auth): add login flow")',
            }
        )

    # メッセージの長さをチェック
    if len(message) > 72:
        issues.append(
            {
                "type": "length",
                "message": f"Commit message too long ({len(message)} chars, max 72)",
                "suggestion": "Keep the first line under 72 characters",
            }
        )

    # 先頭の小文字をチェック（規約）
    if conventional_commit.match(message):
        after_colon = message.split(":", 1)[1] if ":" in message else ""
        if after_colon and re.match(r"^[A-Z]", after_colon.strip()):
            issues.append(
                {
                    "type": "capitalization",
                    "message": "Subject should start with lowercase after type",
                    "suggestion": "Use lowercase for the first letter of the subject",
                }
            )

    # 末尾のピリオドをチェック
    if message.endswith("."):
        issues.append(
            {
                "type": "punctuation",
                "message": "Commit message should not end with a period",
                "suggestion": "Remove the trailing period",
            }
        )

    return {"message": message, "issues": issues}


def evaluate(raw_input: str) -> dict:
    """入力を評価し、出力内容と終了コードを返します。

    Args:
        raw_input: フックに渡された生の入力文字列です。

    Returns:
        output と exitCode を含む辞書を返します。

    Raises:
        例外は発生しません。
    """
    try:
        input_data = parse_json_object(raw_input)
        if not input_data:
            return {"output": raw_input, "exitCode": 0}

        command = input_data.get("tool_input", {}).get("command", "")

        # git commit コマンドの場合のみ実行
        if "git commit" not in command:
            return {"output": raw_input, "exitCode": 0}

        # --amend の場合はチェックをスキップ（ブロックを避けるため）
        if "--amend" in command:
            return {"output": raw_input, "exitCode": 0}

        # ステージングされたファイルを取得
        staged_files = get_staged_files()

        if not staged_files:
            log('[Hook] No staged files found. Use "git add" to stage files first.')
            return {"output": raw_input, "exitCode": 0}

        log(f"[Hook] Checking {len(staged_files)} staged file(s)...")

        # 各ステージングファイルをチェック
        files_to_check = [f for f in staged_files if should_check_file(f)]
        total_issues = 0
        error_count = 0
        warning_count = 0
        info_count = 0

        for file_path in files_to_check:
            file_issues = find_file_issues(file_path)
            if file_issues:
                log(f"\n[FILE] {file_path}")
                for issue in file_issues:
                    label = {"error": "ERROR", "warning": "WARNING", "info": "INFO"}.get(issue["severity"], "INFO")
                    log(f"  {label} Line {issue['line']}: {issue['message']}")
                    total_issues += 1
                    if issue["severity"] == "error":
                        error_count += 1
                    elif issue["severity"] == "warning":
                        warning_count += 1
                    elif issue["severity"] == "info":
                        info_count += 1

        # コミットメッセージが提供されている場合は検証
        message_validation = validate_commit_message(command)
        if message_validation and message_validation["issues"]:
            log("\nCommit Message Issues:")
            for issue in message_validation["issues"]:
                log(f"  WARNING {issue['message']}")
                if issue.get("suggestion"):
                    log(f"     TIP {issue['suggestion']}")
                total_issues += 1
                warning_count += 1

        # サマリー
        if total_issues > 0:
            log(
                f"\nSummary: {total_issues} issue(s) found "
                f"({error_count} error(s), {warning_count} warning(s), {info_count} info)"
            )

            if error_count > 0:
                log("\n[Hook] ERROR: Commit blocked due to critical issues. Fix them before committing.")
                return {"output": raw_input, "exitCode": 2}
            else:
                log("\n[Hook] WARNING: Warnings found. Consider fixing them, but commit is allowed.")
                log("[Hook] To bypass these checks, use: git commit --no-verify")
        else:
            log("\n[Hook] PASS: All checks passed!")

    except Exception as err:
        log(f"[Hook] Error: {err}")
        # エラー時はノンブロッキング

    return {"output": raw_input, "exitCode": 0}


def run(raw_input: str) -> dict:
    """フックを実行し、run_with_flags 用の結果を返します。

    Args:
        raw_input: フックに渡された生の入力文字列です。

    Returns:
        output と exitCode を含む辞書を返します。

    Raises:
        例外は発生しません。
    """
    return evaluate(raw_input)


def main() -> int:
    """スクリプト実行時に入力を読み取り、品質チェックを行います。

    Returns:
        コミットを許可する場合は 0、ブロックする場合は 2 を返します。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    from devgear.hooks.hook_common import read_raw_stdin

    try:
        raw = read_raw_stdin()
        result = evaluate(raw)
        return result["exitCode"]
    except Exception:
        return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
