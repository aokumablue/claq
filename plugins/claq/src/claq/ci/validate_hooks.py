"""hooks.json とフックエントリのルールを検証する。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from claq.ci.ci_common import (
    REPO_ROOT,
    emit_error,
    is_non_empty_string,
    is_non_empty_string_array,
    parse_validator_args,
    read_json,
    report_errors,
    report_missing,
)

# 宣言が失われたら保護が丸ごと無効化されるイベント。`{"hooks": {}}` を
# 「0 個のマッチャーを検証しました」として成功扱いすると、宣言の欠落を
# 一切検出できない。
#
# 「最低 N 個のマッチャー」という個数条件は置かない。個数は品質指標ではなく、
# 必要なイベントが宣言されているかどうかが実際の契約である。
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


def _type_and_timeout_errors(hook: dict, label: str) -> Iterator[str]:
    """フックの 'type' と 'timeout' フィールドの誤りを返す。"""
    hook_type = hook.get("type")
    if not is_non_empty_string(hook_type):
        yield f"{label} は 'type' フィールドが不足しているか無効です"
    elif hook_type not in VALID_HOOK_TYPES:
        yield f"{label} はサポートされていないフックタイプ '{hook_type}' です"
    if "timeout" in hook:
        timeout = hook["timeout"]
        if not isinstance(timeout, int | float) or isinstance(timeout, bool) or timeout < 0:
            yield f"{label} の 'timeout' は 0 以上の数値である必要があります"


def _command_hook_errors(hook: dict, label: str) -> Iterator[str]:
    """command タイプのフック固有フィールドの誤りを返す。"""
    if "async" in hook and not isinstance(hook["async"], bool):
        yield f"{label} の 'async' は真偽値である必要があります"
    command = hook.get("command")
    if not is_non_empty_string(command) and not is_non_empty_string_array(command):
        yield f"{label} は 'command' フィールドが不足しているか無効です"
    # Copilot CLI は Windows で `powershell` を優先する。空値や数値を許すと
    # POSIX 用 command へフォールバックせず、全ツール呼び出しが hook error になる。
    if "powershell" in hook and not is_non_empty_string(hook["powershell"]):
        yield f"{label} の 'powershell' は空でない文字列である必要があります"


def _http_hook_errors(hook: dict, label: str) -> Iterator[str]:
    """http タイプのフック固有フィールドの誤りを返す。"""
    if not is_non_empty_string(hook.get("url")):
        yield f"{label} は 'url' フィールドが不足しているか無効です"
    if "headers" in hook:
        headers = hook["headers"]
        if not isinstance(headers, dict) or not all(isinstance(value, str) for value in headers.values()):
            yield f"{label} の 'headers' は文字列値を持つオブジェクトである必要があります"
    if "allowedEnvVars" in hook:
        allowed_env_vars = hook["allowedEnvVars"]
        if not isinstance(allowed_env_vars, list) or not all(is_non_empty_string(value) for value in allowed_env_vars):
            yield f"{label} の 'allowedEnvVars' は文字列の配列である必要があります"


def _prompt_hook_errors(hook: dict, label: str) -> Iterator[str]:
    """prompt タイプ（および非 command/http）のフック固有フィールドの誤りを返す。"""
    if not is_non_empty_string(hook.get("prompt")):
        yield f"{label} は 'prompt' フィールドが不足しているか無効です"
    if "model" in hook and not is_non_empty_string(hook["model"]):
        yield f"{label} の 'model' は空でない文字列である必要があります"


def _hook_entry_errors(hook: Any, label: str) -> Iterator[str]:
    """単一のフックエントリの誤りを返す。"""
    if not isinstance(hook, dict):
        yield f"{label} は 'type' フィールドが不足しているか無効です"
        return
    yield from _type_and_timeout_errors(hook, label)
    hook_type = hook.get("type")
    if hook_type == "command":
        yield from _command_hook_errors(hook, label)
        return
    if "async" in hook:
        yield f"{label} では 'async' は command フックでのみサポートされています"
    # `powershell` は command フックの Windows 側の別表現であり、http / prompt
    # フックには実行経路が無い。拒否しないと非 command フックに付いた
    # `powershell` が無診断で捨てられ、Windows だけ静かに動かない。
    if "powershell" in hook:
        yield f"{label} では 'powershell' は command フックでのみサポートされています"
    yield from (_http_hook_errors if hook_type == "http" else _prompt_hook_errors)(hook, label)


def validate_hook_entry(hook: Any, label: str) -> bool:
    """単一のフックエントリを検証し、誤りを stderr へ書き出す。誤りがあれば True。"""
    return report_errors(_hook_entry_errors(hook, label))


def _matcher_errors(event_type: str, index: int, matcher: Any) -> Iterator[str]:
    """イベント配下の単一マッチャーエントリとその hooks の誤りを返す。"""
    prefix = f"{event_type}[{index}]"
    if not isinstance(matcher, dict):
        yield f"{prefix} はオブジェクトではありません"
        return
    if "matcher" not in matcher:
        if event_type not in EVENTS_WITHOUT_MATCHER:
            yield f"{prefix} は 'matcher' フィールドが不足しています"
    elif not (is_non_empty_string(matcher["matcher"]) or isinstance(matcher["matcher"], dict | list)):
        yield f"{prefix} の 'matcher' フィールドが無効です"
    hooks = matcher.get("hooks")
    if not isinstance(hooks, list):
        yield f"{prefix} は 'hooks' 配列が不足しています"
        return
    for hook_index, hook in enumerate(hooks):
        yield from _hook_entry_errors(hook, f"{prefix}.hooks[{hook_index}]")


def _hooks_errors(hooks: dict[str, Any]) -> Iterator[str]:
    """イベント名をキーとする hooks オブジェクト全体の誤りを返す。"""
    for event_type, matchers in hooks.items():
        if event_type not in VALID_EVENTS:
            yield f"無効なイベントタイプ: {event_type}"
        elif not isinstance(matchers, list):
            yield f"{event_type} は配列である必要があります"
        else:
            for index, matcher in enumerate(matchers):
                yield from _matcher_errors(event_type, index, matcher)
    missing_events = sorted(REQUIRED_EVENTS - set(hooks))
    if missing_events:
        yield f"必須イベントが宣言されていません: {' '.join(missing_events)}"


def validate_hooks(hooks_file: str | Path = DEFAULT_HOOKS_FILE, *, optional: bool = False) -> int:
    """hooks.json を検証し、JS バリデータと同じメッセージを表示する。

    Args:
        hooks_file: 検証する hooks.json のパス。
        optional: True なら対象パスが存在しない場合に検証をスキップして 0 を返す。

    Returns:
        終了コード（0 = 合格、1 = 不合格）。
    """
    hooks_path = Path(hooks_file)
    if not hooks_path.exists():
        return report_missing("hooks.json ", hooks_path, optional)
    try:
        data = read_json(hooks_path, "hooks.json")
    except ValueError as error:
        emit_error(str(error))
        return 1
    if not isinstance(data, dict):
        emit_error("hooks.json はオブジェクトである必要があります")
        return 1
    # bool（True == 1）と float（1.0 == 1）を通さないため型を厳密に見る。
    version = data.get("version")
    if type(version) is not int or version != REQUIRED_HOOKS_VERSION:
        emit_error(f"hooks.json の 'version' は {REQUIRED_HOOKS_VERSION} である必要があります")
        return 1
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        emit_error("hooks.json の 'hooks' はイベント名をキーとするオブジェクトである必要があります")
        return 1
    if report_errors(_hooks_errors(hooks)):
        return 1
    # 誤りが無ければ、全イベントが配列で全マッチャーがオブジェクトである。
    print(f"{sum(len(matchers) for matchers in hooks.values())} 個のフックマッチャーを検証しました")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI のエントリポイント。"""
    args = parse_validator_args("Validate hooks.json", argv, hooks_file=DEFAULT_HOOKS_FILE)
    return validate_hooks(args.hooks_file, optional=args.optional)


if __name__ == "__main__":
    raise SystemExit(main())
