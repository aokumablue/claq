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

from bluecore.hooks.hook_common import parse_json_object
from bluecore.lib.core_utils import log


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

    `# nosec` を含む行は、意図的にパターンを含む行（検出器自身のテスト
    フィクスチャ等）として検出対象から除外します。

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

            # 抑制マーカー付き行（検出器自身のテストフィクスチャ等、意図的に
            # パターンを含む行）はスキップする
            if "# nosec" in line:
                continue

            # ログ出力呼び出しをチェック
            if "console.log" in line and not line.strip().startswith(("//", "*")):  # nosec
                issues.append(
                    {
                        "type": "console.log",  # nosec
                        "message": f"console.log found at line {line_num}",  # nosec
                        "line": line_num,
                        "severity": "warning",
                    }
                )

            # デバッガ文をチェック
            if re.search(r"\bdebugger\b", line) and not line.strip().startswith("//"):
                issues.append(
                    {
                        "type": "debugger",  # nosec
                        "message": f"debugger statement at line {line_num}",  # nosec
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


def _count_file_issues(files_to_check: list[str]) -> tuple[int, int, int, int]:
    """チェック対象ファイルの問題数を集計します。

    各ファイルに対して find_file_issues を呼び出し、severity 別に問題数を返します。
    ログ出力も行います。

    Args:
        files_to_check: チェック対象のファイルパスリストです。

    Returns:
        (total_issues, error_count, warning_count, info_count) のタプルを返します。

    Raises:
        例外は発生しません。
    """
    total_issues = 0
    error_count = 0
    warning_count = 0
    info_count = 0
    severity_label = {"error": "ERROR", "warning": "WARNING", "info": "INFO"}

    for file_path in files_to_check:
        file_issues = find_file_issues(file_path)
        if not file_issues:
            continue
        log(f"\n[FILE] {file_path}")
        for issue in file_issues:
            label = severity_label.get(issue["severity"], "INFO")
            log(f"  {label} Line {issue['line']}: {issue['message']}")
            total_issues += 1
            if issue["severity"] == "error":
                error_count += 1
            elif issue["severity"] == "warning":
                warning_count += 1
            elif issue["severity"] == "info":
                info_count += 1

    return total_issues, error_count, warning_count, info_count


def _apply_commit_message_issues(
    command: str,
    total_issues: int,
    warning_count: int,
) -> tuple[int, int]:
    """コミットメッセージの問題を検証してカウントに加算します。

    validate_commit_message を呼び出し、問題があればログ出力して
    更新後の (total_issues, warning_count) を返します。

    Args:
        command: `git commit` コマンド文字列です。
        total_issues: 現在の問題総数です。
        warning_count: 現在の警告数です。

    Returns:
        (total_issues, warning_count) の更新後タプルを返します。

    Raises:
        例外は発生しません。
    """
    message_validation = validate_commit_message(command)
    if not (message_validation and message_validation["issues"]):
        return total_issues, warning_count

    log("\nCommit Message Issues:")
    for issue in message_validation["issues"]:
        log(f"  WARNING {issue['message']}")
        if issue.get("suggestion"):
            log(f"     TIP {issue['suggestion']}")
        total_issues += 1
        warning_count += 1

    return total_issues, warning_count


def _finalize_result(
    total_issues: int,
    error_count: int,
    warning_count: int,
    info_count: int,
    raw_input: str,
) -> dict:
    """問題集計結果をログに記録し、終了コードを含む結果辞書を返します。

    error_count > 0 の場合は exitCode=2（コミットブロック）、
    それ以外は exitCode=0 を返します。

    Args:
        total_issues: 検出された問題の総数です。
        error_count: エラー severity の問題数です。
        warning_count: 警告 severity の問題数です。
        info_count: info severity の問題数です。
        raw_input: そのまま output に返す生の入力文字列です。

    Returns:
        output と exitCode を含む辞書を返します。

    Raises:
        例外は発生しません。
    """
    if total_issues > 0:
        log(
            f"\nSummary: {total_issues} issue(s) found "
            f"({error_count} error(s), {warning_count} warning(s), {info_count} info)"
        )
        if error_count > 0:
            log("\n[Hook] ERROR: Commit blocked due to critical issues. Fix them before committing.")
            return {"output": raw_input, "exitCode": 2}
        log("\n[Hook] WARNING: Warnings found. Consider fixing them, but commit is allowed.")
        log("[Hook] To bypass these checks, use: git commit --no-verify")
    else:
        log("\n[Hook] PASS: All checks passed!")

    return {"output": raw_input, "exitCode": 0}


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

        files_to_check = [f for f in staged_files if should_check_file(f)]
        total_issues, error_count, warning_count, info_count = _count_file_issues(files_to_check)
        total_issues, warning_count = _apply_commit_message_issues(command, total_issues, warning_count)

        return _finalize_result(total_issues, error_count, warning_count, info_count, raw_input)

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
    from bluecore.hooks.hook_common import read_raw_stdin

    try:
        raw = read_raw_stdin()
        result = evaluate(raw)
        return result["exitCode"]
    except Exception:
        return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
