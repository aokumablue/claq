"""hooks.json とフックエントリのルールを検証する。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from claq.ci.ci_common import (
    REPO_ROOT,
    emit_error,
    is_non_empty_string,
    is_non_empty_string_array,
    read_json,
)

# 宣言が失われたら保護が丸ごと無効化されるイベント。`{"hooks": {}}` を
# 「0 個のマッチャーを検証しました」として成功扱いしていたため、宣言の欠落を
# 一切検出できていなかった（F-03）。
#
# 「最低 N 個のマッチャー」という個数条件は置かない。個数は品質指標ではなく、
# 必要なイベントが宣言されているかどうかが実際の契約である（同じ理由で
# harness audit の固定件数 quota も撤去した）。
REQUIRED_EVENTS = frozenset({"PreToolUse", "PreCompact", "SessionStart", "SessionEnd"})
REQUIRED_HOOKS_VERSION = 1

VALID_EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PostToolUseFailure",
    "Notification",
    "SubagentStart",
    "Stop",
    "SubagentStop",
    "PreCompact",
    "InstructionsLoaded",
    "TeammateIdle",
    "TaskCompleted",
    "ConfigChange",
    "WorktreeCreate",
    "WorktreeRemove",
    "SessionEnd",
]
VALID_HOOK_TYPES = ["command", "http", "prompt", "agent"]
EVENTS_WITHOUT_MATCHER = {"UserPromptSubmit", "Notification", "Stop", "SubagentStop"}

DEFAULT_HOOKS_FILE = REPO_ROOT / "hooks" / "hooks.json"


def _validate_hook_type_and_timeout(hook: dict, label: str) -> bool:
    """フックの 'type' と 'timeout' フィールドを検証する。

    Args:
        hook: 検証するフックエントリ（辞書）
        label: エラーメッセージに使うラベル

    Returns:
        エラーがあれば True、なければ False

    Raises:
        例外は発生しません。
    """
    has_errors = False
    hook_type = hook.get("type")
    if not is_non_empty_string(hook_type):
        emit_error(f"{label} は 'type' フィールドが不足しているか無効です")
        has_errors = True
    elif hook_type not in VALID_HOOK_TYPES:
        emit_error(f"{label} はサポートされていないフックタイプ '{hook_type}' です")
        has_errors = True

    if "timeout" in hook:
        timeout = hook.get("timeout")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout < 0:
            emit_error(f"{label} の 'timeout' は 0 以上の数値である必要があります")
            has_errors = True

    return has_errors


def _validate_command_hook(hook: dict, label: str) -> bool:
    """command タイプのフック固有フィールドを検証する。

    Args:
        hook: 検証するフックエントリ（辞書）
        label: エラーメッセージに使うラベル

    Returns:
        エラーがあれば True、なければ False

    Raises:
        例外は発生しません。
    """
    has_errors = False
    if "async" in hook and not isinstance(hook.get("async"), bool):
        emit_error(f"{label} の 'async' は真偽値である必要があります")
        has_errors = True

    command = hook.get("command")
    if not is_non_empty_string(command) and not is_non_empty_string_array(command):
        emit_error(f"{label} は 'command' フィールドが不足しているか無効です")
        has_errors = True

    # Copilot CLI は Windows で `powershell` を優先する。空値や数値を許すと
    # POSIX 用 command へフォールバックせず、全ツール呼び出しが hook error になる。
    if "powershell" in hook and not is_non_empty_string(hook.get("powershell")):
        emit_error(f"{label} の 'powershell' は空でない文字列である必要があります")
        has_errors = True

    return has_errors


def _validate_http_hook(hook: dict, label: str) -> bool:
    """http タイプのフック固有フィールドを検証する。

    Args:
        hook: 検証するフックエントリ（辞書）
        label: エラーメッセージに使うラベル

    Returns:
        エラーがあれば True、なければ False

    Raises:
        例外は発生しません。
    """
    has_errors = False
    if not is_non_empty_string(hook.get("url")):
        emit_error(f"{label} は 'url' フィールドが不足しているか無効です")
        has_errors = True

    if "headers" in hook:
        headers = hook.get("headers")
        if not isinstance(headers, dict) or not all(isinstance(value, str) for value in headers.values()):
            emit_error(f"{label} の 'headers' は文字列値を持つオブジェクトである必要があります")
            has_errors = True

    if "allowedEnvVars" in hook:
        allowed_env_vars = hook.get("allowedEnvVars")
        if not isinstance(allowed_env_vars, list) or not all(
            is_non_empty_string(value) for value in allowed_env_vars
        ):
            emit_error(f"{label} の 'allowedEnvVars' は文字列の配列である必要があります")
            has_errors = True

    return has_errors


def _validate_prompt_hook(hook: dict, label: str) -> bool:
    """prompt タイプ（および非 command/http）のフック固有フィールドを検証する。

    Args:
        hook: 検証するフックエントリ（辞書）
        label: エラーメッセージに使うラベル

    Returns:
        エラーがあれば True、なければ False

    Raises:
        例外は発生しません。
    """
    has_errors = False
    if not is_non_empty_string(hook.get("prompt")):
        emit_error(f"{label} は 'prompt' フィールドが不足しているか無効です")
        has_errors = True

    if "model" in hook and not is_non_empty_string(hook.get("model")):
        emit_error(f"{label} の 'model' は空でない文字列である必要があります")
        has_errors = True

    return has_errors


def validate_hook_entry(hook: Any, label: str) -> bool:
    """単一のフックエントリを検証する。

    Args:
        hook: 検証対象のフック定義（dict 想定）。
        label: エラーメッセージに用いる表示ラベル。

    Returns:
        検証エラーがあれば True、無ければ False。
    """
    if not isinstance(hook, dict):
        emit_error(f"{label} は 'type' フィールドが不足しているか無効です")
        return True

    has_errors = _validate_hook_type_and_timeout(hook, label)
    hook_type = hook.get("type")

    if hook_type == "command":
        return _validate_command_hook(hook, label) or has_errors

    if "async" in hook:
        emit_error(f"{label} では 'async' は command フックでのみサポートされています")
        has_errors = True

    # `powershell` は command フックの Windows 側の別表現であり、http / prompt
    # フックには実行経路が無い。`async` と同じく明示的に拒否する。ここが無いと
    # 非 command フックに付いた `powershell` が無診断で捨てられ、Windows だけ
    # 静かに動かない — 本フィールドが解消した障害と同じ形が 1 階層下に残る。
    if "powershell" in hook:
        emit_error(f"{label} では 'powershell' は command フックでのみサポートされています")
        has_errors = True

    if hook_type == "http":
        return _validate_http_hook(hook, label) or has_errors

    return _validate_prompt_hook(hook, label) or has_errors


def _validate_matcher(event_type: str, index: int, matcher: Any) -> bool:
    """イベント配下の単一マッチャーエントリを検証する。

    Args:
        event_type: イベントタイプ名
        index: マッチャーのインデックス
        matcher: 検証するマッチャー（任意の値）

    Returns:
        エラーがあれば True、なければ False

    Raises:
        例外は発生しません。
    """
    if not isinstance(matcher, dict):
        emit_error(f"{event_type}[{index}] はオブジェクトではありません")
        return True

    has_errors = False
    matcher_value = matcher.get("matcher")
    if "matcher" not in matcher:
        if event_type not in EVENTS_WITHOUT_MATCHER:
            emit_error(f"{event_type}[{index}] は 'matcher' フィールドが不足しています")
            has_errors = True
    elif not (is_non_empty_string(matcher_value) or isinstance(matcher_value, (dict, list))):
        emit_error(f"{event_type}[{index}] の 'matcher' フィールドが無効です")
        has_errors = True

    if "hooks" not in matcher or not isinstance(matcher.get("hooks"), list):
        emit_error(f"{event_type}[{index}] は 'hooks' 配列が不足しています")
        has_errors = True
    else:
        for hook_index, hook in enumerate(matcher["hooks"]):
            if validate_hook_entry(hook, f"{event_type}[{index}].hooks[{hook_index}]"):
                has_errors = True

    return has_errors


def _validate_event(event_type: str, matchers: Any) -> tuple[bool, int]:
    """単一イベントタイプとその配下のマッチャー群を検証する。

    Args:
        event_type: イベントタイプ名
        matchers: イベントに紐づくマッチャーのリスト（任意の値）

    Returns:
        (エラー有無, 検証したマッチャー数) のタプル

    Raises:
        例外は発生しません。
    """
    if event_type not in VALID_EVENTS:
        emit_error(f"無効なイベントタイプ: {event_type}")
        return True, 0

    if not isinstance(matchers, list):
        emit_error(f"{event_type} は配列である必要があります")
        return True, 0

    has_errors = False
    total_matchers = 0
    for index, matcher in enumerate(matchers):
        if _validate_matcher(event_type, index, matcher):
            has_errors = True
        if isinstance(matcher, dict):
            total_matchers += 1
    return has_errors, total_matchers


def validate_hooks(
    hooks_file: str | Path = DEFAULT_HOOKS_FILE,
    *,
    optional: bool = False,
) -> int:
    """hooks.json を検証し、JS バリデータと同じメッセージを表示する。

    Args:
        hooks_file: 処理に渡す hooks_file の値です。
        optional: True なら対象パスが存在しない場合に検証をスキップして 0 を返す。
            既定の False では欠落を失敗として扱う（宣言がまるごと失われた破損を
            成功と報告しないため。F-03）。

    Returns:
        処理結果を返します。

    Raises:
        例外は発生しません。
    """
    hooks_path = Path(hooks_file)
    if not hooks_path.exists():
        if optional:
            print("hooks.json が見つかりません。--optional 指定のため検証をスキップします")
            return 0
        emit_error(f"hooks.json が見つかりません: {hooks_path}")
        return 1

    try:
        data = read_json(hooks_path, "hooks.json")
    except ValueError as error:
        emit_error(str(error))
        return 1

    if not isinstance(data, dict):
        emit_error("hooks.json はオブジェクトである必要があります")
        return 1

    version = data.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != REQUIRED_HOOKS_VERSION
    ):
        emit_error(f"hooks.json の 'version' は {REQUIRED_HOOKS_VERSION} である必要があります")
        return 1

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        emit_error("hooks.json の 'hooks' はイベント名をキーとするオブジェクトである必要があります")
        return 1

    has_errors = False
    total_matchers = 0
    for event_type, matchers in hooks.items():
        event_errors, matched_count = _validate_event(event_type, matchers)
        if event_errors:
            has_errors = True
        total_matchers += matched_count

    missing_events = sorted(REQUIRED_EVENTS - set(hooks))
    if missing_events:
        emit_error(f"必須イベントが宣言されていません: {' '.join(missing_events)}")
        has_errors = True

    if has_errors:
        return 1

    print(f"{total_matchers} 個のフックマッチャーを検証しました")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """CLI パーサーを構築する。

    Args:
        引数はありません。

    Returns:
        処理結果を返します。

    Raises:
        例外は発生しません。
    """
    parser = argparse.ArgumentParser(description="Validate hooks.json")
    parser.add_argument(
        "--optional",
        action="store_true",
        help="対象パスが存在しない場合に失敗ではなくスキップする",
    )
    parser.add_argument("--hooks-file", default=str(DEFAULT_HOOKS_FILE))
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI のエントリポイント。

    Args:
        argv: 処理に渡す argv の値です。

    Returns:
        処理結果を返します。

    Raises:
        例外は発生しません。
    """
    args = build_parser().parse_args(argv)
    return validate_hooks(args.hooks_file, optional=args.optional)


if __name__ == "__main__":
    raise SystemExit(main())
