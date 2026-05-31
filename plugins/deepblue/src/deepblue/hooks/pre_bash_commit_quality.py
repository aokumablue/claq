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

from deepblue.hooks.hook_common import parse_json_object
from deepblue.lib.core_utils import log


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


_SECRET_PATTERNS = [
    (r"sk-[a-zA-Z0-9]{20,}", "OpenAI API key"),
    (r"ghp_[a-zA-Z0-9]{36}", "GitHub PAT"),
    (r"AKIA[A-Z0-9]{16}", "AWS Access Key"),
    (r"api[_-]?key\s*[=:]\s*['\"][^'\"]+['\"]", "API key"),
]


def _check_line_issues(line: str, line_num: int) -> list[dict]:
    """1行に対して console.log・debugger・TODO・シークレットを検出して返す。

    Args:
        line: 検査対象の行文字列。
        line_num: 1始まりの行番号。

    Returns:
        検出した問題の辞書リスト。
    """
    issues: list[dict] = []
    stripped = line.strip()

    if "console.log" in line and not stripped.startswith("//") and not stripped.startswith("*"):
        issues.append({"type": "console.log", "message": f"console.log found at line {line_num}", "line": line_num, "severity": "warning"})

    if re.search(r"\bdebugger\b", line) and not stripped.startswith("//"):
        issues.append({"type": "debugger", "message": f"debugger statement at line {line_num}", "line": line_num, "severity": "error"})

    todo_match = re.search(r"(?://|#)\s*(TODO|FIXME):?\s*(.+)", line)
    if todo_match and not re.search(r"#\d+|issue", todo_match.group(2), re.IGNORECASE):
        issues.append({"type": "todo", "message": f'TODO/FIXME without issue reference at line {line_num}: "{todo_match.group(2).strip()}"', "line": line_num, "severity": "info"})

    for pattern, name in _SECRET_PATTERNS:
        if re.search(pattern, line, re.IGNORECASE):
            issues.append({"type": "secret", "message": f"Potential {name} exposed at line {line_num}", "line": line_num, "severity": "error"})

    return issues


def find_file_issues(file_path: str) -> list[dict]:
    """ファイル内容から代表的な問題を検出します。

    Args:
        file_path: 調査対象のファイルパスです。

    Returns:
        検出した問題の辞書リストを返します。

    Raises:
        例外は発生しません。
    """
    issues: list[dict] = []
    try:
        content = get_staged_file_content(file_path)
        if content is None:
            return issues
        for index, line in enumerate(content.split("\n")):
            issues.extend(_check_line_issues(line, index + 1))
    except Exception:
        pass
    return issues


_CONVENTIONAL_COMMIT_RE = re.compile(
    r"^(feat|fix|docs|style|refactor|test|chore|build|ci|perf|revert)(\(.+\))?:\s*.+"
)


def _check_message_rules(message: str) -> list[dict]:
    """コミットメッセージに対してフォーマット・長さ・大文字・末尾ピリオドの4ルールを検査する。

    Args:
        message: 検査対象のコミットメッセージ。

    Returns:
        検出した問題の辞書リスト。
    """
    issues: list[dict] = []

    if not _CONVENTIONAL_COMMIT_RE.match(message):
        issues.append({"type": "format", "message": "Commit message does not follow conventional commit format", "suggestion": 'Use format: type(scope): description (e.g., "feat(auth): add login flow")'})

    if len(message) > 72:
        issues.append({"type": "length", "message": f"Commit message too long ({len(message)} chars, max 72)", "suggestion": "Keep the first line under 72 characters"})

    if _CONVENTIONAL_COMMIT_RE.match(message):
        after_colon = message.split(":", 1)[1] if ":" in message else ""
        if after_colon and re.match(r"^[A-Z]", after_colon.strip()):
            issues.append({"type": "capitalization", "message": "Subject should start with lowercase after type", "suggestion": "Use lowercase for the first letter of the subject"})

    if message.endswith("."):
        issues.append({"type": "punctuation", "message": "Commit message should not end with a period", "suggestion": "Remove the trailing period"})

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
    message_match = re.search(r"(?:-m|--message)[=\s]+[\"']?([^\"']+)[\"']?", command)
    if not message_match:
        return None
    message = message_match.group(1)
    return {"message": message, "issues": _check_message_rules(message)}


def _check_staged_files(files_to_check: list[str]) -> tuple[int, int, int, int]:
    """ステージング済みファイルを検査し、(total, errors, warnings, infos) の件数タプルを返す。

    Args:
        files_to_check: 検査対象のファイルパスリスト。

    Returns:
        (total_issues, error_count, warning_count, info_count) のタプル。
    """
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
    return total_issues, error_count, warning_count, info_count


def _check_commit_message(command: str) -> tuple[int, int]:
    """コミットメッセージを検証し、(追加 total_issues, 追加 warning_count) を返す。

    Args:
        command: git commit コマンド文字列。

    Returns:
        (delta_total, delta_warnings) のタプル。
    """
    message_validation = validate_commit_message(command)
    if not (message_validation and message_validation["issues"]):
        return 0, 0
    log("\nCommit Message Issues:")
    for issue in message_validation["issues"]:
        log(f"  WARNING {issue['message']}")
        if issue.get("suggestion"):
            log(f"     TIP {issue['suggestion']}")
    return len(message_validation["issues"]), len(message_validation["issues"])


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
        if "git commit" not in command or "--amend" in command:
            return {"output": raw_input, "exitCode": 0}

        staged_files = get_staged_files()
        if not staged_files:
            log('[Hook] No staged files found. Use "git add" to stage files first.')
            return {"output": raw_input, "exitCode": 0}
        log(f"[Hook] Checking {len(staged_files)} staged file(s)...")

        files_to_check = [f for f in staged_files if should_check_file(f)]
        total_issues, error_count, warning_count, info_count = _check_staged_files(files_to_check)
        delta_total, delta_warn = _check_commit_message(command)
        total_issues += delta_total
        warning_count += delta_warn

        if total_issues > 0:
            log(f"\nSummary: {total_issues} issue(s) found ({error_count} error(s), {warning_count} warning(s), {info_count} info)")
            if error_count > 0:
                log("\n[Hook] ERROR: Commit blocked due to critical issues. Fix them before committing.")
                return {"output": raw_input, "exitCode": 2}
            log("\n[Hook] WARNING: Warnings found. Consider fixing them, but commit is allowed.")
        else:
            log("\n[Hook] PASS: All checks passed!")
    except Exception as err:
        log(f"[Hook] Error: {err}")
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
    from deepblue.hooks.hook_common import read_raw_stdin

    try:
        raw = read_raw_stdin()
        result = evaluate(raw)
        return result["exitCode"]
    except Exception:
        return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
