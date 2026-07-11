"""コーディングエージェントハーネス（Claude Code / Copilot CLI / Codex）の判定と差分吸収。

判定順はコスト昇順で、Claude Code では環境変数チェック 1 回で確定する。
すべて純 stdlib のみに依存する（venv 不在時のフォールバック実行を保証するため）。
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache

# Codex のツール名 → Claude Code 相当ツール名
_TOOL_NAME_MAP = {"apply_patch": "Edit"}

# Codex apply_patch パッチテキストのファイル操作マーカー
_PATCH_FILE_MARKERS = ("*** Add File: ", "*** Update File: ", "*** Delete File: ")


@lru_cache(maxsize=1)
def detect_harness() -> str:
    """実行中のコーディングエージェントハーネスを判定する。

    Args:
        引数はありません。

    Returns:
        "claude" / "codex" / "copilot" / "unknown" のいずれか。
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
    return "unknown"


def normalize_tool_name(tool_name: str) -> str:
    """ハーネス固有のツール名を Claude Code 相当のツール名へ正規化する。

    Codex の apply_patch は Edit に対応する。Claude Code に apply_patch という
    ツールは存在しないため、ハーネス判定なしの無条件マッピングで安全。

    Args:
        tool_name: フック stdin の tool_name フィールド値。

    Returns:
        正規化後のツール名。マッピング対象外はそのまま返す。

    Raises:
        例外は発生しません。
    """
    return _TOOL_NAME_MAP.get(tool_name, tool_name)


def _extract_apply_patch_text(tool_input: dict | str | None) -> str | None:
    """apply_patch 入力からパッチ本文を取り出す。

    Copilot CLI では生のパッチ文字列、他ハーネスでは {"input": "..."} の
    ような dict で渡ることがあるため、両方を吸収する。JSON 文字列化された
    dict が来た場合も input フィールドを復元する。
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


def extract_file_paths(tool_name: str, tool_input: dict | str | None) -> list[str] | None:
    """ツール入力から操作対象のファイルパス一覧を抽出する。

    Edit/Write/MultiEdit は file_path フィールド、Copilot/Codex の
    apply_patch はパッチテキストのファイル操作マーカー行をパースする。

    Args:
        tool_name: フック stdin の tool_name フィールド値（正規化前）。
        tool_input: フック stdin の tool_input フィールド値。dict / 文字列 / None。

    Returns:
        ファイルパスのリスト。判定不能（apply_patch でマーカーが 1 つも
        見つからない等）の場合は None を返す。呼び出し側は None を
        fail-closed として扱うこと。

    Raises:
        例外は発生しません。
    """
    if tool_name == "apply_patch":
        patch_text = _extract_apply_patch_text(tool_input)
        if not isinstance(patch_text, str):
            return None
        paths = [
            line[len(marker) :].strip()
            for line in patch_text.splitlines()
            for marker in _PATCH_FILE_MARKERS
            if line.startswith(marker)
        ]
        return paths or None

    if not isinstance(tool_input, dict):
        return []

    file_path = tool_input.get("file_path")
    if isinstance(file_path, str) and file_path:
        return [file_path]
    return []


# セッション ID として許容する形式（ファイル名に使われるため英数・._- のみ）
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def resolve_session_id(payload: dict) -> str:
    """フックペイロードと環境変数からセッション ID を解決する。

    ペイロードは信頼できない入力のため、ファイル名に安全な形式
    （英数・ピリオド・ハイフン・アンダースコア、128 文字以内）以外は
    fail-closed で "default" に倒す。

    Args:
        payload: フック stdin の JSON ペイロード。

    Returns:
        session_id フィールド値、無ければ CLAUDE_SESSION_ID、どちらも
        無ければ（または不正形式なら）"default"。

    Raises:
        例外は発生しません。
    """
    session_id = payload.get("session_id")
    if not (isinstance(session_id, str) and session_id):
        session_id = os.environ.get("CLAUDE_SESSION_ID") or ""
    if _SESSION_ID_PATTERN.fullmatch(session_id):
        return session_id
    return "default"


def resolve_project_dir(payload: dict) -> str:
    """フックペイロードと環境変数からプロジェクトディレクトリを解決する。

    Args:
        payload: フック stdin の JSON ペイロード。

    Returns:
        CLAUDE_PROJECT_DIR、無ければペイロードの cwd、どちらも無ければ
        カレントディレクトリ。

    Raises:
        例外は発生しません。
    """
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        return project_dir
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    return os.getcwd()
