"""コーディングエージェントハーネスの入力形式差分を吸収する汎用パーサ。

host 判定は一切行わない。フィールド名の union を無条件に受理する寛容パーサ
のみを提供する（tool_input / toolArgs、tool_name / toolName 等の複数命名を
同一の意味へ正規化する）。すべて純 stdlib のみに依存する（venv 不在時の
フォールバック実行を保証するため）。
"""

from __future__ import annotations

import json
import re
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
    # Grok の runtime tool 名。read_file/list_dir は書き込み系ゲート
    # （config_protection の _WRITE_TOOL_NAMES）の対象外だが、
    # observe.py の観測レコード（tool フィールド）がハーネス横断で
    # 正規化名を記録するために正規化する。
    "search_replace": "Edit",
    "run_terminal_command": "Bash",
    "spawn_subagent": "Agent",
    "read_file": "Read",
    "list_dir": "Glob",
}

# 構造化パッチテキストのファイル操作マーカー（Codex apply_patch 形式）
_PATCH_FILE_MARKERS = ("*** Add File: ", "*** Update File: ", "*** Delete File: ")


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
        if not isinstance(value, str):
            return value
        stripped = value.lstrip()
        if not stripped.startswith(("{", "[")):
            return value
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return None


def extract_raw_tool_name(payload: dict[str, Any]) -> str:
    """フック payload から正規化前の生ツール名を返す。

    非空の ``tool_name`` を優先し、無ければ非空の ``toolName`` を使う。
    空文字・非文字列は無効として次候補へ倒す。どちらも無効なら空文字。

    Args:
        payload: フック stdin を JSON として読んだ dict。

    Returns:
        ``normalize_tool_name()`` 適用前の生文字列。取れなければ ``""``。

    Raises:
        例外は発生しません。
    """
    tool_name = payload.get("tool_name")
    if isinstance(tool_name, str) and tool_name:
        return tool_name
    camel_name = payload.get("toolName")
    if isinstance(camel_name, str) and camel_name:
        return camel_name
    return ""


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


def normalize_tool_name(tool_name: str) -> str:
    """ハーネス固有のツール名を Claude Code 相当のツール名へ正規化する。

    Codex の apply_patch は Edit に対応する。Copilot CLI はフックイベントに
    lowercase の runtime tool 名（write/edit/bash 等）を渡すため、大文字小文字を
    区別しない照合で Claude Code 表記へ正規化する。Grok は search_replace /
    run_terminal_command / spawn_subagent / read_file / list_dir という
    固有名を使うため、同様に Claude Code 表記へ正規化する。Claude Code に apply_patch
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

    if not isinstance(tool_input, str):
        return None

    stripped = tool_input.lstrip()
    if not stripped.startswith("{"):
        return tool_input
    try:
        parsed = json.loads(tool_input)
    except (json.JSONDecodeError, TypeError):
        return tool_input
    patch_text = parsed.get("input")
    return patch_text if isinstance(patch_text, str) else tool_input


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


# ユーザー発話として transcript に載るが、実際にはハーネスが生成した足場で
# あって依頼ではないタグ。中身ごと捨てる。
#
# - local-command-caveat: ローカルコマンド実行時の定型注意書き。「DO NOT
#   respond to these messages」という指示文を含むため、依頼として引き継ぐと
#   次セッションへ疑似ユーザー指示として再注入されてしまう。
# - local-command-stdout / local-command-stderr: コマンドの出力であって依頼ではない。
# - task-notification: サブエージェント完了通知。ユーザーの発話ではない。
_SCAFFOLD_TAGS = (
    "local-command-caveat",
    "local-command-stdout",
    "local-command-stderr",
    "task-notification",
)

# ReDoS 保護: タグ出現回数の上限（tag_stripping._MAX_TAG_COUNT と同方針）。
_MAX_SCAFFOLD_TAG_COUNT = 100

_SCAFFOLD_BLOCK_PATTERNS = [
    re.compile(rf"<{tag}[^>]*>.*?</{tag}>", re.DOTALL | re.IGNORECASE) for tag in _SCAFFOLD_TAGS
]

# 対を成さずに残った足場タグ。切り詰め等で片側だけが残った場合に、タグ表記
# そのものが依頼本文として引き継がれるのを防ぐ。
_SCAFFOLD_ORPHAN_PATTERNS = [re.compile(rf"</?{tag}[^>]*>", re.IGNORECASE) for tag in _SCAFFOLD_TAGS]

# スラッシュコマンド起動の足場。`<command-name>` と `<command-args>` の中身は
# ユーザーが実際に入力した依頼そのものなので、捨てずに `/name args` へ畳む。
# `<command-message>` はコマンド名の再掲であり情報を持たないため捨てる。
_COMMAND_NAME_PATTERN = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.DOTALL | re.IGNORECASE)
_COMMAND_ARGS_PATTERN = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.DOTALL | re.IGNORECASE)
_COMMAND_SCAFFOLD_PATTERN = re.compile(r"</?command-(?:name|message|args)[^>]*>", re.IGNORECASE)


def _drop_scaffold_blocks(text: str) -> str:
    """依頼ではないハーネス足場タグを中身ごと除去する。

    Args:
        text: ユーザー発話として transcript に載っていた生テキスト。

    Returns:
        足場タグとその中身を除いたテキスト。

    Raises:
        例外は発生しません。
    """
    for pattern in _SCAFFOLD_BLOCK_PATTERNS:
        if len(pattern.findall(text)) > _MAX_SCAFFOLD_TAG_COUNT:
            continue
        text = pattern.sub("", text)
    for pattern in _SCAFFOLD_ORPHAN_PATTERNS:
        text = pattern.sub("", text)
    return text


def _fold_command_invocation(text: str) -> str:
    """スラッシュコマンド起動の足場を ``/name args`` の 1 行へ畳む。

    Args:
        text: ``<command-name>`` を含みうるテキスト。

    Returns:
        コマンド起動が含まれていれば ``/name args`` 形式の文字列。含まれて
        いなければ入力をそのまま返す。

    Raises:
        例外は発生しません。
    """
    name_match = _COMMAND_NAME_PATTERN.search(text)
    if name_match is None:
        return text
    name = name_match.group(1).strip().lstrip("/")
    if not name:
        return _COMMAND_SCAFFOLD_PATTERN.sub("", text)
    args_match = _COMMAND_ARGS_PATTERN.search(text)
    args = args_match.group(1).strip() if args_match else ""
    return f"/{name} {args}".strip()


def normalize_user_message(text: str) -> str:
    """ユーザー発話からハーネス生成の足場を落として依頼本文だけを残す。

    transcript の ``user`` エントリにはユーザーの依頼だけでなく、ハーネスが
    自分で生成した足場（ローカルコマンドの注意書き・その stdout・サブ
    エージェント完了通知・スラッシュコマンドの起動タグ）も同じ形で載る。
    これらを依頼として引き継ぐと、引き継ぎ本文が足場だけで埋まって実際の
    依頼が押し出されるうえ、注意書きに含まれる指示文が次セッションへ疑似
    ユーザー指示として再注入される。

    足場のうち中身が無価値なものは丸ごと捨て、スラッシュコマンド起動だけは
    ``/name args`` へ畳んで依頼としての情報を残す。

    Args:
        text: transcript の ``user`` エントリから取り出した生テキスト。

    Returns:
        依頼本文。足場しか含まれていなければ空文字列。

    Raises:
        例外は発生しません。
    """
    if not text:
        return text
    return _fold_command_invocation(_drop_scaffold_blocks(text)).strip()



