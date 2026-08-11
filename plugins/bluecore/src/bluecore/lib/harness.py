"""コーディングエージェントハーネス（Claude / Copilot / Codex / Grok）の判定と差分吸収。

判定順はコスト昇順で、Claude Code では環境変数チェック 1 回で確定する。
すべて純 stdlib のみに依存する（venv 不在時のフォールバック実行を保証するため）。
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import Any

# 各ハーネスのツール名 → Claude Code 相当ツール名。
#
# Codex の apply_patch は Edit に対応する。Copilot CLI はフックイベントに
# lowercase の runtime tool 名（write/edit/bash 等）を渡すため、大文字小文字を
# 区別しない照合で Claude Code 表記へ正規化する。
# shell は一部ハーネスが bash 相当として使う runtime 名。
_TOOL_NAME_MAP = {
    "apply_patch": "Edit",
    "agent": "Agent",
    "bash": "Bash",
    "shell": "Bash",
    "edit": "Edit",
    "glob": "Glob",
    "grep": "Grep",
    "multiedit": "MultiEdit",
    "notebookedit": "NotebookEdit",
    "read": "Read",
    "task": "Agent",
    "view": "Read",
    "write": "Write",
}

# 構造化パッチテキストのファイル操作マーカー（Codex apply_patch 形式）
_PATCH_FILE_MARKERS = ("*** Add File: ", "*** Update File: ", "*** Delete File: ")


@lru_cache(maxsize=1)
def detect_harness() -> str:
    """実行中のコーディングエージェントハーネスを判定する。

    Args:
        引数はありません。

    Returns:
        "claude" / "codex" / "copilot" / "grok" / "unknown" のいずれか。
        unknown は Claude 互換形式で出力する（最も安全側）。

    Raises:
        例外は発生しません。
    """
    if os.environ.get("CLAUDECODE"):
        return "claude"
    env = os.environ
    if "PLUGIN_DATA" in env or any(k.startswith("CODEX_") for k in env):
        return "codex"
    plugin_root = env.get("CLAUDE_PLUGIN_ROOT", "")
    if "/.copilot/installed-plugins/" in plugin_root or any(k.startswith("COPILOT_") for k in env):
        return "copilot"
    # Grok Build: installed-plugins/<name>-<hash> または plugins/<name>、GROK_* 環境変数
    if (
        "/.grok/installed-plugins/" in plugin_root
        or "/.grok/plugins/" in plugin_root
        or any(k.startswith("GROK_") for k in env)
    ):
        return "grok"
    return "unknown"


def extract_tool_input(payload: dict[str, Any]) -> Any:
    """フック payload から tool_input / toolArgs を正規化して返す。

    Claude / VS Code 互換は ``tool_input``、Copilot camelCase は ``toolArgs``
    （JSON 文字列のことが多い）。文字列で JSON オブジェクトに見える場合は
    パースして dict を返す。パース不能なら元の文字列を返す。

    Args:
        payload: フック stdin を JSON として読んだ dict。

    Returns:
        正規化後の tool 入力（dict / str / その他）、キーが無ければ None。

    Raises:
        例外は発生しません。
    """
    for key in ("tool_input", "toolArgs", "tool_args"):
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, str):
            stripped = value.lstrip()
            if stripped.startswith(("{", "[")):
                try:
                    return json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    return value
            return value
        return value
    return None


def extract_bash_command(payload: dict[str, Any]) -> str:
    """フック payload から Bash/shell の command 文字列を取り出す。

    ``tool_input.command`` / ``toolArgs``（JSON 文字列含む）/ 生文字列を吸収する。
    非 dict の tool_input に対して ``.get`` して AttributeError にならない。

    Args:
        payload: フック stdin を JSON として読んだ dict。

    Returns:
        コマンド文字列。取れなければ空文字列。

    Raises:
        例外は発生しません。
    """
    tool_input = extract_tool_input(payload)
    if isinstance(tool_input, dict):
        for key in ("command", "cmd"):
            cmd = tool_input.get(key)
            if isinstance(cmd, str):
                return cmd
        return ""
    if isinstance(tool_input, str):
        return tool_input
    return ""


def extract_tool_result_text(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """PostToolUse payload から LLM 向けテキストと置換用 dict を返す。

    吸収する形:
      - Claude: ``tool_response.stdout``
      - Copilot camelCase: ``toolResult.textResultForLlm``
      - VS Code: ``tool_result`` の同系フィールド

    Args:
        payload: フック stdin を JSON として読んだ dict。

    Returns:
        (テキスト, adapt_tool_output に渡す dict)。取れなければ ("", {})。

    Raises:
        例外は発生しません。
    """
    tool_response = payload.get("tool_response")
    if isinstance(tool_response, dict):
        stdout = tool_response.get("stdout")
        if isinstance(stdout, str) and stdout.strip():
            return stdout, tool_response

    for key in ("toolResult", "tool_result"):
        result = payload.get(key)
        if isinstance(result, dict):
            text = (
                result.get("textResultForLlm")
                or result.get("text_result_for_llm")
                or result.get("stdout")
            )
            if isinstance(text, str) and text.strip():
                merged = {**result, "stdout": text}
                return text, merged
        if isinstance(result, str) and result.strip():
            return result, {"stdout": result}

    return "", {}


def normalize_tool_name(tool_name: str) -> str:
    """ハーネス固有のツール名を Claude Code 相当のツール名へ正規化する。

    Codex の apply_patch は Edit に対応する。Copilot CLI はフックイベントに
    lowercase の runtime tool 名（write/edit/bash 等）を渡すため、大文字小文字を
    区別しない照合で Claude Code 表記へ正規化する。Claude Code に apply_patch
    というツールは存在せず、Claude Code 自身のツール名は既に正規形のため、
    ハーネス判定なしの無条件マッピングで安全。

    Args:
        tool_name: フック stdin の tool_name フィールド値。

    Returns:
        正規化後のツール名。マッピング対象外はそのまま返す。

    Raises:
        例外は発生しません。
    """
    return _TOOL_NAME_MAP.get(tool_name.lower(), tool_name)


def _extract_patch_text(tool_input: dict | str | None) -> str | None:
    """入力から構造化パッチ本文候補を取り出す。

    Copilot CLI では生のパッチ文字列、他ハーネスでは {"input": "..."} の
    ような dict で渡ることがあるため、両方を吸収する。JSON 文字列化された
    dict が来た場合も input フィールドを復元する。ツール名に関わらず、
    渡された入力の「形」だけから候補テキストを取り出す（判定は呼び出し側）。
    """
    if isinstance(tool_input, dict):
        patch_text = tool_input.get("input")
        return patch_text if isinstance(patch_text, str) else None

    if isinstance(tool_input, str):
        stripped = tool_input.lstrip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(tool_input)
            except (json.JSONDecodeError, TypeError):
                pass
            else:
                patch_text = parsed.get("input")
                if isinstance(patch_text, str):
                    return patch_text
        return tool_input

    return None


def _has_patch_markers(patch_text: str) -> bool:
    """テキストが構造化パッチのファイル操作マーカー行を 1 つ以上含むか判定する。"""
    return any(line.startswith(marker) for line in patch_text.splitlines() for marker in _PATCH_FILE_MARKERS)


def extract_file_paths(tool_name: str, tool_input: dict | str | None) -> list[str] | None:
    """ツール入力から操作対象のファイルパス一覧を抽出する。

    Edit/Write/MultiEdit は file_path フィールドを使う。構造化パッチ
    （Copilot/Codex の apply_patch 等）はパッチテキストのファイル操作
    マーカー行をパースする。パッチかどうかは tool_name の文字列一致
    だけに頼らず、生入力の内容（マーカー行の有無）でも判定する。
    ハーネスごとの命名差異でツール名が "apply_patch" と一致しない
    場合でも、構造化パッチの内容が検査対象から漏れないようにするため。

    Args:
        tool_name: フック stdin の tool_name フィールド値（正規化前）。
        tool_input: フック stdin の tool_input フィールド値。dict / 文字列 / None。

    Returns:
        ファイルパスのリスト。判定不能（パッチ本文と分かっているのに
        マーカーが 1 つも見つからない等）の場合は None を返す。呼び出し側は
        None を fail-closed として扱うこと。

    Raises:
        例外は発生しません。
    """
    patch_text = _extract_patch_text(tool_input)
    is_declared_patch_tool = tool_name == "apply_patch"
    if isinstance(patch_text, str) and (is_declared_patch_tool or _has_patch_markers(patch_text)):
        paths = [
            line[len(marker) :].strip()
            for line in patch_text.splitlines()
            for marker in _PATCH_FILE_MARKERS
            if line.startswith(marker)
        ]
        return paths or None

    if is_declared_patch_tool:
        # apply_patch と明示されているのにパッチ本文を取り出せない: 判定不能
        return None

    if not isinstance(tool_input, dict):
        return []

    file_path = tool_input.get("file_path")
    if isinstance(file_path, str) and file_path:
        return [file_path]
    return []


# セッション ID として許容する形式（ファイル名に使われるため英数・._- のみ）
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


