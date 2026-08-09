"""SessionEnd で ``sessions.handoff`` へ書き込む引き継ぎ本文を組み立てる。

本文の出所は 2 つあり、優先度の高い順に:

1. フック stdin の ``handoff`` キー — エージェントが明示的に書いた引き継ぎ。
2. ``transcript_path`` のトランスクリプト — 直近のユーザー依頼・変更ファイル・
   使用ツールから機械的に組み立てた要約。SessionEnd のペイロードには
   引き継ぎ本文そのものは来ないため、こちらが既定の経路になる。

トランスクリプトは数十 MB に育ちうるため、読み取りバイト上限と走査の
ハードタイムアウトを持つ。引き継ぎは best-effort であり、フックを遅延
させてまで完全性を追わない。出力は ``mem context`` の注入予算
（``CONTEXT_HANDOFF_CHAR_BUDGET``）に収まるよう書き込み時点で切り詰め、
DB に読み出されないデータを溜めない。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from bluecore.lib.core_utils import strip_ansi
from bluecore.lib.harness import extract_file_paths, normalize_tool_name
from bluecore.lib.slim_text import compact_line
from bluecore.mem.logger import get as _get_logger
from bluecore.mem.redaction import redact
from bluecore.mem.settings import CONTEXT_HANDOFF_CHAR_BUDGET
from bluecore.mem.tag_stripping import strip_tags

log = _get_logger("HANDOFF")

TRANSCRIPT_TIMEOUT_SEC = 2.0
"""トランスクリプト走査のハードタイムアウト（秒）。超過分は捨てる。"""

TRANSCRIPT_MAX_BYTES = 2 * 1024 * 1024
"""トランスクリプトから読む末尾バイト数の上限。直近の作業ほど引き継ぎ価値が高い。"""

_USER_MESSAGE_COUNT = 3
"""引き継ぎに載せる直近ユーザー依頼の件数。"""

_USER_MESSAGE_CHAR_LIMIT = 200
"""ユーザー依頼 1 件を圧縮する上限文字数。"""

_FILE_LIST_LIMIT = 8
"""引き継ぎに載せる変更ファイルの件数。"""

_TOOL_LIST_LIMIT = 8
"""引き継ぎに載せる使用ツールの件数。"""

_EDIT_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "MultiEdit"})
"""ファイルパスを収集する対象となる正規化済みツール名。"""

_PATH_SEGMENT_LIMIT = 3
"""引き継ぎに載せるファイルパスの末尾セグメント数。絶対パス全体は予算の無駄。"""


def build_handoff(payload: dict[str, Any]) -> str:
    """フックの stdin JSON から引き継ぎ本文を組み立てる。

    ``handoff`` キーがあればエージェントの明示指定として優先し、
    無ければトランスクリプトから要約を組み立てる。

    シークレット除去は自由文（明示指定の本文とユーザー発話）にだけ掛ける。
    ファイルパスとツール名は機械生成の構造データであり、``redact`` の
    base64 検出が 40 文字超のパスを丸ごと ``[REDACTED]`` にしてしまうため
    通さない（引き継ぎで最も価値のある情報が消える）。

    Args:
        payload: SessionEnd フックが stdin へ渡した JSON。

    Returns:
        引き継ぎ本文。組み立てる材料が無ければ空文字列。
    """
    explicit = str(payload.get("handoff") or "").strip()
    text = redact(explicit) if explicit else _summarize_transcript(str(payload.get("transcript_path") or ""))
    if not text:
        return ""
    return _truncate(text)


def _truncate(text: str) -> str:
    """引き継ぎ本文を context 注入の予算内へ切り詰める。

    Args:
        text: 切り詰め前の本文。

    Returns:
        ``CONTEXT_HANDOFF_CHAR_BUDGET`` 文字以内の本文。切った場合は
        末尾を省略記号にする。
    """
    if len(text) <= CONTEXT_HANDOFF_CHAR_BUDGET:
        return text
    return text[: CONTEXT_HANDOFF_CHAR_BUDGET - 1] + "…"


def _summarize_transcript(transcript_path: str) -> str:
    """トランスクリプトを走査して引き継ぎ要約を組み立てる。

    Args:
        transcript_path: フックが渡した JSONL トランスクリプトのパス。

    Returns:
        要約の散文。パスが読めない・材料が無い場合は空文字列。
    """
    path = Path(transcript_path)
    if not path.is_file():
        return ""
    messages, files, tools = _scan(_read_tail(path), time.monotonic() + TRANSCRIPT_TIMEOUT_SEC)
    return _compose(messages, files, tools)


def _read_tail(path: Path) -> str:
    """トランスクリプトの末尾を上限バイトまで読む。

    先頭が途中で切れた行は JSON として壊れるが、走査側が解析不能行を
    捨てるため問題にならない。

    Args:
        path: トランスクリプトの絶対パス。

    Returns:
        デコード済みの末尾テキスト。
    """
    with path.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - TRANSCRIPT_MAX_BYTES))
        return stream.read().decode("utf-8", errors="replace")


def _scan(text: str, deadline: float) -> tuple[list[str], list[str], list[str]]:
    """トランスクリプト各行からユーザー依頼・変更ファイル・使用ツールを集める。

    Args:
        text: JSONL 形式のトランスクリプト本文。
        deadline: 走査を打ち切る ``time.monotonic()`` 基準の時刻。

    Returns:
        （直近のユーザー依頼、変更ファイル、使用ツール）の 3 つ組。
        いずれも件数上限で切ってある。
    """
    messages: list[str] = []
    files: set[str] = set()
    tools: set[str] = set()

    for line in text.splitlines():
        if time.monotonic() >= deadline:
            log.warning("トランスクリプト走査がタイムアウトしました: 残りの行を捨てます")
            break
        entry = _parse_entry(line)
        if entry is None:
            continue
        message = _user_message(entry)
        if message:
            messages.append(message)
        _collect_tools(entry, tools, files)

    return (
        messages[-_USER_MESSAGE_COUNT:],
        sorted(files)[:_FILE_LIST_LIMIT],
        sorted(tools)[:_TOOL_LIST_LIMIT],
    )


def _parse_entry(line: str) -> dict[str, Any] | None:
    """トランスクリプト 1 行を JSON オブジェクトへ解析する。

    Args:
        line: トランスクリプトの 1 行。

    Returns:
        解析できた dict。空行・不正 JSON・非 dict なら None。
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        entry = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return entry if isinstance(entry, dict) else None


def _user_message(entry: dict[str, Any]) -> str:
    """エントリがユーザー発話ならその本文を 1 行へ圧縮して返す。

    注入済みの ``<bluecore-memory>`` 等のタグは ``strip_tags`` で落とす。
    落とさないと前セッションへ注入した記憶をそのまま引き継ぎとして
    記録し直すエコーが起きる。シークレット除去は圧縮より前に掛ける
    （後だと 200 文字での打ち切りがシークレットを分断し、断片が残る）。

    Args:
        entry: トランスクリプトの 1 エントリ。

    Returns:
        圧縮済みのユーザー発話。ユーザー発話でない、または中身が空なら空文字列。
    """
    message = entry.get("message")
    message = message if isinstance(message, dict) else {}
    if "user" not in (entry.get("type"), entry.get("role"), message.get("role")):
        return ""

    raw = message.get("content") or entry.get("content")
    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, list):
        text = " ".join(str(part.get("text", "")) for part in raw if isinstance(part, dict))
    else:
        return ""
    return compact_line(redact(strip_tags(strip_ansi(text))), _USER_MESSAGE_CHAR_LIMIT)


def _collect_tools(entry: dict[str, Any], tools: set[str], files: set[str]) -> None:
    """エントリから使用ツール名と編集対象ファイルを収集する。

    単独の ``tool_use`` エントリと、assistant メッセージ内の ``tool_use``
    ブロックの両方を見る（ハーネスによって形が異なるため）。

    Args:
        entry: トランスクリプトの 1 エントリ。
        tools: 使用ツール名の収集先。
        files: 変更ファイルパスの収集先。
    """
    if entry.get("type") == "tool_use" or entry.get("tool_name"):
        name = str(entry.get("tool_name") or entry.get("name") or "")
        _record_tool(name, entry.get("tool_input") or entry.get("input"), tools, files)

    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            _record_tool(str(block.get("name") or ""), block.get("input"), tools, files)


def _record_tool(tool_name: str, tool_input: object, tools: set[str], files: set[str]) -> None:
    """ツール名を正規化して記録し、編集系なら対象ファイルも集める。

    Args:
        tool_name: トランスクリプト上のツール名（正規化前）。
        tool_input: ツール入力。dict / パッチ文字列以外は入力なしとして扱う。
        tools: 使用ツール名の収集先。
        files: 変更ファイルパスの収集先。
    """
    if not tool_name:
        return
    normalized = normalize_tool_name(tool_name)
    tools.add(normalized)
    if normalized not in _EDIT_TOOLS:
        return
    payload = tool_input if isinstance(tool_input, dict | str) else None
    files.update(extract_file_paths(tool_name, payload) or ())


def _shorten_path(path: str) -> str:
    """ファイルパスを末尾数セグメントへ縮める。

    絶対パスを丸ごと載せると数本で予算を食い潰すうえ、次セッションが
    知りたいのは「どのファイルを触っていたか」だけで先頭のディレクトリ
    階層は不要なため。

    Args:
        path: トランスクリプトから拾った生のパス。

    Returns:
        末尾 ``_PATH_SEGMENT_LIMIT`` セグメント。省略した場合は ``…/`` を付ける。
    """
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) <= _PATH_SEGMENT_LIMIT:
        return path
    return "…/" + "/".join(segments[-_PATH_SEGMENT_LIMIT:])


def _compose(messages: list[str], files: list[str], tools: list[str]) -> str:
    """収集結果を人間可読の引き継ぎ散文へ組み立てる。

    ユーザー依頼も変更ファイルも無い場合は次セッションへ渡す内容が無いと
    みなす（使用ツールだけでは何をしていたか分からない）。

    Args:
        messages: 直近のユーザー依頼。
        files: 変更ファイルパス。
        tools: 使用ツール名。

    Returns:
        引き継ぎの散文。材料が足りなければ空文字列。
    """
    if not messages and not files:
        return ""

    lines: list[str] = []
    if messages:
        lines.append("直近の依頼:")
        lines.extend(f"- {message}" for message in messages)
    if files:
        lines.append(f"変更ファイル: {', '.join(_shorten_path(path) for path in files)}")
    if tools:
        lines.append(f"使用ツール: {', '.join(tools)}")
    return "\n".join(lines)
