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
import os
import time
from pathlib import Path
from typing import Any

from bluecore.lib.core_utils import get_home_dir, strip_ansi
from bluecore.lib.harness import extract_file_paths, normalize_tool_name, normalize_user_message
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

_TRANSCRIPT_ROOTS_ENV = "BLUECORE_TRANSCRIPT_ROOTS"
"""追加の trusted transcript root をコロン区切りで指定する環境変数（§6.4 対応）。"""


def _default_trusted_transcript_roots() -> tuple[Path, ...]:
    """既知 host の transcript root を返す。

    ``plugins/bluecore/src`` 配下に host 検出コード（``COPILOT_*`` /
    ``CLAUDECODE`` / ``CODEX_*`` 等の環境変数参照）は一切存在しない
    （host 非依存の性質検査のみで防御する設計方針のため）。そのため
    「実行中の host を判定してそこから root を導出する」実装は取れず、
    既知 host の transcript 格納規約を静的な allowlist として列挙する。

    Args:
        なし

    Returns:
        既知 host の transcript root 絶対パスのタプル（存在確認はしない）。

    Raises:
        例外は発生しません。
    """
    home = get_home_dir()
    return (
        home / ".claude" / "projects",
        home / ".copilot",
        home / ".codex",
    )


def _trusted_transcript_roots() -> tuple[Path, ...]:
    """trusted transcript root 一覧（既知 host + 環境変数追加分）を返す。

    ``BLUECORE_TRANSCRIPT_ROOTS``（``os.pathsep`` 区切り）で追加・拡張できる
    （未知 host では transcript 要約が失われるトレードオフを、利用者が
    自分の host の root を教えることで解消できるようにする。§6.4 対応）。

    Args:
        なし

    Returns:
        trusted root 絶対パスのタプル。

    Raises:
        例外は発生しません。
    """
    roots = list(_default_trusted_transcript_roots())
    extra = os.environ.get(_TRANSCRIPT_ROOTS_ENV, "")
    for raw in extra.split(os.pathsep):
        stripped = raw.strip()
        if stripped:
            roots.append(Path(stripped).expanduser())
    return tuple(roots)


def _is_under_trusted_root(resolved_path: Path) -> bool:
    """解決済み絶対パスが trusted transcript root のいずれか配下かを判定する。

    Args:
        resolved_path: ``Path.resolve()`` 済みの絶対パス。

    Returns:
        既知 host の trusted root 配下、または環境変数で追加された root
        配下なら True。

    Raises:
        例外は発生しません。
    """
    return any(resolved_path.is_relative_to(root.resolve()) for root in _trusted_transcript_roots())


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
    text = strip_tags(redact(explicit)) if explicit else _summarize_transcript(str(payload.get("transcript_path") or ""))
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

    ``transcript_path`` は SessionEnd payload の host 供給値であり、
    Claude Code は ``~/.claude/projects/...``、Copilot CLI は
    ``~/.copilot/...`` と host ごとに置き場所が異なる。まず host 非依存の
    性質検査（symlink 拒否・通常ファイル・所有者一致）で防御し（F-08a
    対応）、その後段に既知 host transcript root の allowlist 包含チェックを
    追加する（``_trusted_transcript_roots``。§6.4 対応）。所有者一致の
    任意 regular file を無条件で読むと、ユーザーが書ける任意ファイルを
    prompt injection の入力にできてしまうため、性質検査だけでは不十分
    という監査指摘に対応する。allowlist 外なら要約を諦めて空文字列を返す
    （handoff は明示テキスト・構造化事実で継続するため、壊れずに劣化する。
    トレードオフ: 未知 host では transcript 要約が失われる）。

    Args:
        transcript_path: フックが渡した JSONL トランスクリプトのパス。

    Returns:
        要約の散文。パスが読めない・シンボリックリンク・所有者不一致・
        trusted root 外・材料が無い場合は空文字列。
    """
    path = Path(transcript_path)
    if path.is_symlink():
        return ""
    if not path.is_file():
        return ""
    try:
        owner_uid = path.stat().st_uid
    except OSError:
        return ""
    if owner_uid != os.getuid():
        return ""
    try:
        resolved = path.resolve()
    except OSError:
        return ""
    if not _is_under_trusted_root(resolved):
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
        _dedupe_keeping_latest(messages)[-_USER_MESSAGE_COUNT:],
        sorted(files)[:_FILE_LIST_LIMIT],
        sorted(tools)[:_TOOL_LIST_LIMIT],
    )


def _dedupe_keeping_latest(messages: list[str]) -> list[str]:
    """同一内容の依頼を 1 件に畳み、最後に現れた位置を残す。

    引き継ぎ枠は ``_USER_MESSAGE_COUNT`` 件しかない。同じ依頼が繰り返されても
    新しい情報は増えないのに枠だけを消費し、古い実依頼を押し出してしまう
    （実測: `mainにマージせよ` の後に同じスラッシュコマンドを 3 回叩くと
    実依頼が枠外へ落ちる）。重複を畳めば、繰り返し実行が何回あっても実依頼が
    残る。位置は最後の出現に寄せる（「直近の依頼」なので新しいほうが正しい）。

    Args:
        messages: 出現順のユーザー依頼。

    Returns:
        重複を除いた出現順の依頼。
    """
    seen: set[str] = set()
    kept: list[str] = []
    for message in reversed(messages):
        if message in seen:
            continue
        seen.add(message)
        kept.append(message)
    kept.reverse()
    return kept


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


# user ロールのエントリに混ざるが、ユーザーの発話ではないブロック種別。
# ツール結果・ツール呼び出し・画像・思考は依頼ではないため本文に採らない。
# 未知の種別は通す（host ごとにテキストブロックの type 名が異なりうるため、
# allowlist にすると未知 host で実依頼を落とす）。
_NON_SPEECH_BLOCK_TYPES = frozenset({"tool_result", "tool_use", "image", "thinking"})


def _text_content(raw: object) -> str:
    """ユーザー発話の content をプレーンテキストへ畳む。

    Args:
        raw: 文字列本文、または ``{"text": ...}`` ブロックのリスト。

    Returns:
        連結した本文。文字列でもブロック列でもなければ空文字列。
        ツール結果等の非発話ブロックは除外する。
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return " ".join(
            str(part.get("text", ""))
            for part in raw
            if isinstance(part, dict) and part.get("type") not in _NON_SPEECH_BLOCK_TYPES
        )
    return ""


def _user_message(entry: dict[str, Any]) -> str:
    """エントリがユーザー発話ならその本文を 1 行へ圧縮して返す。

    ハーネスが生成した足場（ローカルコマンドの注意書き・その stdout・
    サブエージェント完了通知）は ``normalize_user_message`` で落とし、
    スラッシュコマンド起動は ``/name args`` へ畳む。落とさないと引き継ぎが
    足場だけで埋まり、実際の依頼が押し出される。

    注入済みの ``<bluecore-memory>`` 等のタグは ``strip_tags`` で落とす。
    落とさないと前セッションへ注入した記憶をそのまま引き継ぎとして
    記録し直すエコーが起きる。シークレット除去は圧縮より前に掛ける
    （後だと 200 文字での打ち切りがシークレットを分断し、断片が残る）。

    Args:
        entry: トランスクリプトの 1 エントリ。

    Returns:
        圧縮済みのユーザー発話。ユーザー発話でない、中身が空、または
        ハーネス足場しか含まれていなければ空文字列。
    """
    message = entry.get("message")
    message = message if isinstance(message, dict) else {}
    if "user" not in (entry.get("type"), entry.get("role"), message.get("role")):
        return ""

    text = normalize_user_message(strip_ansi(_text_content(message.get("content") or entry.get("content"))))
    if not text:
        return ""
    return compact_line(redact(strip_tags(text)), _USER_MESSAGE_CHAR_LIMIT)


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
