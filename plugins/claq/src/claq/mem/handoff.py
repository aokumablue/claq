"""SessionEnd で ``sessions.handoff`` へ書き込む引き継ぎ本文を組み立てる。

本文の出所は 2 つあり、優先度の高い順に:

1. フック stdin の ``handoff`` キー — エージェントが明示的に書いた引き継ぎ。
2. ``transcript_path`` のトランスクリプト — 直近のユーザー依頼・変更ファイル・
   使用ツールから機械的に組み立てた要約。SessionEnd のペイロードには
   引き継ぎ本文そのものは来ないため、こちらが既定の経路になる。

トランスクリプトは数十 MB に育ちうるため、末尾から上限バイトずつの窓で読み、
走査のハードタイムアウトを持つ。引き継ぎは best-effort であり、フックを遅延
させてまで完全性を追わない。出力は ``mem context`` の注入予算
（``CONTEXT_HANDOFF_CHAR_BUDGET``）に収まるよう書き込み時点で切り詰め、
DB に読み出されないデータを溜めない。
"""

from __future__ import annotations

import contextlib
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from claq.lib.core_utils import get_home_dir, strip_ansi
from claq.lib.harness import (
    extract_file_paths,
    extract_raw_tool_name,
    extract_tool_input,
    normalize_tool_name,
    normalize_user_message,
)
from claq.lib.redaction import PLACEHOLDER, redact
from claq.lib.slim_text import TRUNCATION_MARKER, compact_line
from claq.lib.stdin_io import parse_json_object
from claq.lib.tag_stripping import keep_allowed_chars, strip_tags
from claq.mem.knowledge_input import optional_str
from claq.mem.logger import get as _get_logger
from claq.mem.repo_identity import PATH_SEPARATOR_RE, find_work_tree_root
from claq.mem.settings import CONTEXT_HANDOFF_CHAR_BUDGET

log = _get_logger("HANDOFF")

TRANSCRIPT_TIMEOUT_SEC = 2.0
"""トランスクリプト走査のハードタイムアウト（秒）。超過分は捨てる。"""

TRANSCRIPT_MAX_BYTES = 2 * 1024 * 1024
"""トランスクリプトを末尾から読む 1 窓のバイト数。直近の作業ほど引き継ぎ価値が高い。

変更ファイルと使用ツールは末尾の窓だけから集める。直近の依頼は、末尾の窓に無ければ
見つかるまで前の窓へ遡る（1 つの依頼から長く自走したセッションでは、依頼が末尾の窓より前にある）。
"""

_TRANSCRIPT_ROOTS_ENV = "CLAQ_TRANSCRIPT_ROOTS"
"""追加の trusted transcript root を ``os.pathsep`` 区切りで指定する環境変数。"""

_USER_MESSAGE_COUNT = 3
"""引き継ぎに載せる直近ユーザー依頼の件数。"""


_FILE_LIST_LIMIT = 8
"""引き継ぎに載せる変更ファイルの件数。"""

_TOOL_LIST_LIMIT = 8
"""引き継ぎに載せる使用ツールの件数。"""

_EDIT_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "MultiEdit"})
"""ファイルパスを収集する対象となる正規化済みツール名。"""

_NON_SPEECH_ENTRY_TYPES: frozenset[str] = frozenset({"api-request-blob"})
"""``message.role`` が ``user`` でも発話ではないエントリの ``type``（Claude Code の transcript）。

``api-request-blob`` は API へ送った過去の内容の控えで、圧縮サマリーや古い依頼を再び運んでくる。
"""

_HOST_META_FLAGS: tuple[str, ...] = ("isMeta", "isCompactSummary")
"""host がユーザー発話の形で記録する非依頼エントリの印（Claude Code の transcript）。

スキル本文の展開・他エージェントからの報告・圧縮サマリーはタグを持たず、足場タグの
除去では落とせない。印の付いたエントリは依頼として引き継がない。
"""

_LOCAL_COMMAND_OUTPUT_RE = re.compile(r"\s*<local-command-(?:stdout|stderr)[\s>]")
"""ローカルコマンド（``/model`` ``/clear`` 等）の出力エントリの書き出し。

host はローカルコマンドの起動を ``<command-name>`` タグ付きで記録し、その出力を起動エントリを親に持つ
エントリ（``user`` 型または ``system`` 型）として続ける。host 自身の注意書きのとおり依頼ではないため、
出力が続いたタグ付きの起動は依頼から外す。``context: fork`` のスキルを slash で起動した依頼も結果が
この出力として続くが、起動はタグの無い平文で記録されるため外さない。
"""

_COMMAND_NAME_TAG = "<command-name>"
"""スラッシュコマンド起動をタグ付きで記録したエントリの印。"""

_PATH_SEGMENT_LIMIT = 3
"""引き継ぎに載せるファイルパスの末尾セグメント数。絶対パス全体は予算の無駄。"""

_STRUCTURED_VALUE_LIMIT = 120
"""パス・ツール名 1 件の上限文字数。"""

_MAX_TRANSCRIPT_LINE_CHARS = 1_000_000
"""走査する 1 行の上限文字数。超える行は捨てる。

``_scan`` の deadline は行と行の間でしか判定できず、1 行の処理中は割り込めない。
上限を置かないと、単一の巨大な行が「ハードタイムアウト」の宣言を無効化する。
"""


def _neutralize(text: str) -> str:
    """``keep_allowed_chars`` → ``normalize_user_message`` → ``strip_tags`` の 3 段で無害化する。

    ``redact`` の前までの合成で、順序の理由は ``_sanitize_freeform`` を参照。

    Args:
        text: 無害化前の文字列。

    Returns:
        制御文字・足場・信頼境界タグを落とした文字列。
    """
    return strip_tags(normalize_user_message(keep_allowed_chars(text)))


def _sanitize_freeform(text: str) -> str:
    """自由文（明示 handoff・ユーザー発話）を引き継ぎへ載せる前に無害化する。

    順序は ``keep_allowed_chars`` → ``normalize_user_message`` → ``strip_tags`` →
    ``redact`` で固定する。
    この 4 段は互いに順序依存があり、経路ごとに書き直すと片側だけずれる。
    たとえば ``strip_tags(redact(...))`` の順にすると、タグで分断された秘密
    （``sk-ant-<private>zz</private>api03-…``）が ``redact`` をすり抜けた後に
    ``strip_tags`` で 1 本へ再結合し、未マスクのまま ``sessions.handoff`` へ
    永続化されて以後の全 SessionStart へ注入される。そのため合成そのものを
    1 箇所に閉じる。

    - ``keep_allowed_chars`` が最初: 制御文字・不可視文字で分断した秘密・タグ
      （``sk-ant-…\\x00…``、``</claq\\u200b-memory>``）は、除去で 1 本へ戻して初めて後続の
      3 段が見える。除去を後ろに置くと、除去が再結合した形はどの段も見ずに
      ``sessions.handoff`` へ永続化される。タブと改行は残し、CR・NEL・LS・PS は LF に揃う
      （引き継ぎ本文が使う）。
    - ``normalize_user_message`` が次: docs/adr/untrusted-input-prompt-boundary.md の足場除去は生の足場タグを
      前提にしており、他の処理が先に走ると足場の形が崩れて検出できない。
    - ``strip_tags`` が ``redact`` より先: タグで分断された秘密は、タグを
      除いて 1 本へ戻して初めて ``redact`` のパターンに一致する。

    シークレットがタグで分断されている場合は ``strip_tags`` が本文ごと
    ``[REDACTED]`` へ倒す（判定の詳細は ``lib/tag_stripping``）。引き継ぎでは
    そこから更に一歩進めて**本文ごと捨てる**。断片も「秘密を隠す細工があった」
    という痕跡も次セッションへ渡す価値が無く、細工した側に第 2 の経路を与えない
    ためである。ペアブロックで分断された秘密は ``strip_tags`` が今も 1 本へ
    再結合するので、こちらは倒れずに ``[REDACTED]`` として残る（判定の対象は
    ``strip_tags`` の戻り値であって ``redact`` 後の本文ではない）。

    Args:
        text: 無害化前の自由文。

    Returns:
        足場を落とし、信頼境界タグを無害化し、シークレットをマスクした本文。
        足場しか含まれない、細工が検出された、またはタグで分断された秘密が
        見つかった場合は空文字列。
    """
    stripped = _neutralize(text)
    if stripped == PLACEHOLDER:
        log.warning("タグで分断されたシークレットを検出しました: 引き継ぎ本文を破棄します")
        return ""
    return redact(stripped)


def _sanitize_structured(value: str) -> str:
    """パス・ツール名を引き継ぎ本文へ載せる前に無害化する。

    これらは ``redact`` を通さない（base64 検出が 40 文字超のパスを丸ごと
    ``[REDACTED]`` にしてしまうため）が、無検査でよい理由にはならない。値の
    出所はエージェントが呼んだツールの入力であり、prompt injection を受けた
    エージェントが ``Write(file_path="<system-reminder>…")`` のような呼び出しを
    すれば、失敗した呼び出しでも transcript に残り、``変更ファイル:`` 行として
    次セッションへ注入される。

    Args:
        value: transcript から拾った生のパスまたはツール名。

    Returns:
        タグ・制御文字・改行・過剰な長さを落とした値。無害化の結果が空なら空文字列。
    """
    cleaned = _neutralize(value)
    return " ".join(cleaned.split())[:_STRUCTURED_VALUE_LIMIT]


def build_handoff(payload: dict[str, Any]) -> str:
    """フックの stdin JSON から引き継ぎ本文を組み立てる。

    ``handoff`` キーがあればエージェントの明示指定として優先し、無ければ
    トランスクリプトから要約を組み立てる。

    シークレット除去は自由文（明示指定の本文とユーザー発話）にだけ掛ける。
    ファイルパスとツール名は、``redact`` の base64 検出が 40 文字超のパスを
    丸ごと ``[REDACTED]`` にしてしまうため通さない（最も価値のある情報が消える）。

    明示指定の無害化結果が空になっても transcript へフォールバックしない —
    空になるのは足場の細工が検出されたときであり、細工した側に第 2 の経路を
    与えないため。

    Args:
        payload: SessionEnd フックが stdin へ渡した JSON。

    Returns:
        ``CONTEXT_HANDOFF_CHAR_BUDGET`` 文字以内の引き継ぎ本文（切った場合は
        末尾を省略記号にする）。組み立てる材料が無ければ空文字列。
    """
    explicit = str(payload.get("handoff") or "").strip()
    if explicit:
        return truncate_handoff(_sanitize_freeform(explicit))
    transcript_path = str(payload.get("transcript_path") or "")
    return truncate_handoff(_summarize_transcript(transcript_path, optional_str(payload.get("cwd"))))


def truncate_handoff(text: str) -> str:
    """引き継ぎ本文を ``CONTEXT_HANDOFF_CHAR_BUDGET`` 文字に収める。

    書き込み側（``build_handoff``）だけでは足りない。注入側の無害化は ``<`` を ``&lt;`` へ
    広げて本文を伸ばし、DB へ直接書かれた行はこの関数を通っていないため、注入側
    （``cmd_context._handoff_section``）でも同じ規則で切る。

    Args:
        text: 切り詰める前の本文。

    Returns:
        予算内ならそのままの本文。超えた場合は末尾を省略記号にした本文。
    """
    if len(text) <= CONTEXT_HANDOFF_CHAR_BUDGET:
        return text
    return text[: CONTEXT_HANDOFF_CHAR_BUDGET - 1] + "…"


def _summarize_transcript(transcript_path: str, cwd: str | None) -> str:
    """トランスクリプトを走査して引き継ぎ要約を組み立てる。

    ``transcript_path`` は host 供給値なので ``is_trusted_transcript`` で検査し、
    通らなければ要約を諦める（handoff は壊れずに劣化する。未知 host では
    transcript 要約が失われる）。

    Args:
        transcript_path: フックが渡した JSONL トランスクリプトのパス。
        cwd: フックが渡したセッションの作業ディレクトリ。変更ファイルはこの作業ツリーの
            中だけを集める。None なら現在の作業ディレクトリ。

    Returns:
        要約の散文。信頼できないパス・材料が無い場合は空文字列。
    """
    path = Path(transcript_path)
    if not is_trusted_transcript(path):
        return ""
    work_tree = find_work_tree_root(cwd)
    deadline = time.monotonic() + TRANSCRIPT_TIMEOUT_SEC
    with contextlib.closing(_windows_newest_first(path)) as windows:
        messages, files, tools = _scan(next(windows, ""), deadline, work_tree)
        while not messages and time.monotonic() < deadline and (window := next(windows, None)) is not None:
            messages = _scan(window, deadline, work_tree)[0]
    return _compose(messages, files, tools)


def _windows_newest_first(path: Path) -> Iterator[str]:
    """ファイルを末尾から ``TRANSCRIPT_MAX_BYTES`` バイトずつの窓で読み、新しい窓から順に返す。

    窓の先頭に掛かった行の断片は捨て、次の（前の）窓をその行の終わりまで伸ばして行ごと読む。
    窓より長い行は、どの窓でも断片になるため読まない（走査が 1 行の上限で捨てる規模の行）。
    不正な UTF-8 は置換文字にする。

    Args:
        path: 読み取り対象のファイル。

    Yields:
        完全な行だけからなる窓のテキスト。窓全体が 1 行の断片なら空文字列。

    Raises:
        OSError: ファイルを開けない・読めない場合（扱いは呼び出し側が決める）。
    """
    with path.open("rb") as stream:
        end = stream.seek(0, os.SEEK_END)
        while end > 0:
            start = max(0, end - TRANSCRIPT_MAX_BYTES)
            stream.seek(start)
            data = stream.read(end - start)
            if start == 0:
                yield data.decode("utf-8", errors="replace")
                return
            newline = data.find(b"\n")
            if 0 <= newline < len(data) - 1:
                yield data[newline + 1 :].decode("utf-8", errors="replace")
                end = start + newline + 1
            else:
                yield ""
                end = start


def trusted_transcript_roots() -> tuple[Path, ...]:
    """走査対象にしてよい transcript root 一覧を返す。

    src 配下は host 検出（``COPILOT_*`` / ``CLAUDECODE`` / ``CODEX_*`` 等の参照）を
    持たない設計のため、実行中の host から root を導出できない。既知 host の
    格納規約を静的な allowlist として列挙し、未知 host の利用者は
    ``CLAQ_TRANSCRIPT_ROOTS`` で自分の host の root を追加できる。

    Returns:
        既知 host の root と ``CLAQ_TRANSCRIPT_ROOTS`` で追加された root
        （存在確認はしない）。
    """
    home = get_home_dir()
    extra_roots = (raw.strip() for raw in os.environ.get(_TRANSCRIPT_ROOTS_ENV, "").split(os.pathsep))
    return (
        home / ".claude" / "projects",
        home / ".copilot",
        home / ".codex",
        *(Path(root).expanduser() for root in extra_roots if root),
    )


def is_trusted_transcript(path: Path) -> bool:
    """transcript として読んでよいファイルかを判定する。

    所有者一致の任意 regular file を無条件で読むと、ユーザーが書ける任意
    ファイルを prompt injection の入力にできてしまう。host 非依存の性質検査
    （symlink 拒否・通常ファイル・所有者一致）に加えて、既知 host の transcript
    root allowlist 包含も要求する。同じ判定を別モジュールで書き直すと片側だけ
    強化されて非対称になるため、走査側もこの関数を使う。

    所有者一致は ``os.getuid`` の有無という capability で分岐し、uid が意味を
    持つ環境でだけ検査する。Windows では ``os.getuid`` が無く ``st_uid`` も常に
    0 で、比較は「検査したふり」か一律拒否にしかならない。そこで残る防御は
    他の 3 つで、trusted root はユーザーのホーム配下＝既定でそのユーザーの
    ACL に閉じている。

    Args:
        path: 判定対象のパス。

    Returns:
        symlink でなく、通常ファイルで、（uid が意味を持つ環境では）所有者が
        自分で、trusted root 配下なら True。``stat`` / ``resolve`` の失敗は False。
    """
    if path.is_symlink() or not path.is_file():
        return False
    getuid = getattr(os, "getuid", None)
    try:
        if getuid is not None and path.stat().st_uid != getuid():
            return False
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved.is_relative_to(root.resolve()) for root in trusted_transcript_roots())


def _scan(text: str, deadline: float, work_tree: str) -> tuple[list[str], list[str], list[str]]:
    """トランスクリプト各行からユーザー依頼・変更ファイル・使用ツールを集める。

    Args:
        text: JSONL 形式のトランスクリプト本文。
        deadline: 走査を打ち切る ``time.monotonic()`` 基準の時刻。
        work_tree: 変更ファイルを集める作業ツリーのルート絶対パス。

    Returns:
        （直近のユーザー依頼、変更ファイル、使用ツール）の 3 つ組。
        いずれも件数上限で切ってある。
    """
    messages: list[str] = []
    files: set[str] = set()
    tools: set[str] = set()
    # 直前に依頼として採ったタグ付きの起動の uuid。続く出力エントリがこれを親に持てば、その起動はローカルコマンド。
    last_command_uuid: object = None

    for line in text.splitlines():
        if len(line) > _MAX_TRANSCRIPT_LINE_CHARS:
            # 画像・巨大なツール結果の行で、捨てても引き継ぎは欠けない。WARNING は bg ログ経由で次の SessionStart に失敗として出る。
            log.info("トランスクリプトの 1 行が上限を超えました: その行を捨てます")
            continue
        if time.monotonic() >= deadline:
            log.warning("トランスクリプト走査がタイムアウトしました: 残りの行を捨てます")
            break
        entry = parse_entry(line)
        if entry is None:
            continue
        if last_command_uuid is not None and entry.get("parentUuid") == last_command_uuid and _is_local_command_output(entry):
            messages.pop()
            last_command_uuid = None
            continue
        message = _user_message(entry)
        if message:
            messages.append(message)
            last_command_uuid = entry.get("uuid") if _is_tagged_command(entry) else None
        _collect_tools(entry, tools, files, work_tree)

    return (
        _dedupe_keeping_latest(messages)[-_USER_MESSAGE_COUNT:],
        sorted(files)[:_FILE_LIST_LIMIT],
        sorted(tools)[:_TOOL_LIST_LIMIT],
    )


def _is_tagged_command(entry: dict[str, Any]) -> bool:
    """エントリがスラッシュコマンド起動をタグ付きで記録したものかを判定する。

    Args:
        entry: 依頼として採ったトランスクリプトの 1 エントリ。

    Returns:
        本文が文字列で ``<command-name>`` を含めば True。
    """
    content = user_entry_content(entry)
    return isinstance(content, str) and _COMMAND_NAME_TAG in content


def _is_local_command_output(entry: dict[str, Any]) -> bool:
    """エントリがローカルコマンドの出力（``<local-command-stdout>`` 等）かを判定する。

    Args:
        entry: トランスクリプトの 1 エントリ。

    Returns:
        本文が文字列で、ローカルコマンドの出力タグで始まれば True。
    """
    content = _message_of(entry).get("content") or entry.get("content")
    return isinstance(content, str) and _LOCAL_COMMAND_OUTPUT_RE.match(content) is not None


def _dedupe_keeping_latest(messages: list[str]) -> list[str]:
    """同一内容の依頼を 1 件に畳み、最後に現れた位置を残す。

    引き継ぎ枠は ``_USER_MESSAGE_COUNT`` 件しかない。同じ依頼が繰り返されても
    新しい情報は増えないのに枠だけを消費し、古い実依頼を押し出してしまう。
    重複を畳めば、繰り返し実行が何回あっても実依頼が残る。位置は最後の出現に寄せる（「直近の依頼」なので新しいほうが正しい）。

    Args:
        messages: 出現順のユーザー依頼。

    Returns:
        重複を除いた出現順の依頼。
    """
    seen: set[str] = set()
    kept: list[str] = []
    for message in reversed(messages):
        # 切り詰められた行は先頭 COMPACT_LINE_MAX_CHARS 文字しか残っておらず、
        # 一致しても元の依頼が同一とは限らない。畳むと別依頼が片方消えるため対象外。
        if message in seen and not message.endswith(TRUNCATION_MARKER):
            continue
        seen.add(message)
        kept.append(message)
    return kept[::-1]


def parse_entry(line: str) -> dict[str, Any] | None:
    """トランスクリプト 1 行を JSON オブジェクトへ解析する。

    Args:
        line: トランスクリプトの 1 行。

    Returns:
        解析できた dict。空行・不正 JSON・非 dict なら None。
    """
    return parse_json_object(line.strip())


# ユーザー発話として本文に採ってよいブロック種別（allowlist）。
#
# ここは **allowlist** にする。既知の非発話種別だけを除外する denylist では
# 「知らない種別＝通す」なので、transcript に載る外部由来テキスト（ツール出力・
# ファイル内容・web 取得結果）が **人間の承認ゲートを一切通らずに恒久注入へ
# 届く**。handoff は `promote` を通らないため、ここが唯一の防壁である。
# denylist では次の 3 形が注入まで到達する:
#
#     {"type": "function_result", "text": ...}
#     {"type": "tool_output",     "text": ...}
#     {"text": ...}（type キー無し）
#
# allowlist を選ぶ根拠は、両方向の失敗のコストが非対称なこと。allowlist が外すと
# 「引き継ぎが薄くなる」＝**見えて直せる**劣化で済むが、denylist が漏らすと
# 「攻撃者が書いた文字列が依頼として注入される」＝**黙って通る**。docs/adr/shell-analysis-boundary.md の
# 「誤検出 > 誤通過」と `schema.py` の「最も危険な値を既定に据えない」も同じ向き。
# 生文字列の content は別経路（`isinstance(raw, str)`）で拾うため、この allowlist
# が実際に選別しているのは `text` と `tool_result` だけである。
_SPEECH_BLOCK_TYPES = frozenset({"text"})

# 発話でないと**分かっている**種別。allowlist から外れた種別のうちこれに載る物は
# 想定内の除外なので黙って落とす。載っていない種別を落としたときだけ記録して、
# 「未知 host の発話を取りこぼしている」状態を観測可能にする（allowlist の失敗を
# 見える劣化に留めるための仕掛け）。
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
        return " ".join(str(part.get("text", "")) for part in raw if _is_speech_block(part))
    return ""


def _is_speech_block(part: object) -> bool:
    """content ブロックがユーザー発話として採ってよいものか判定する。

    allowlist 判定。未知の種別は**採らない**（理由は `_SPEECH_BLOCK_TYPES`）。
    想定外の種別を落としたときだけ記録し、未知 host の発話を取りこぼしている
    状態を観測できるようにする。

    Args:
        part: content 配列の要素。

    Returns:
        発話ブロックなら True。
    """
    if not isinstance(part, dict):
        return False
    block_type = part.get("type")
    if block_type in _SPEECH_BLOCK_TYPES:
        return True
    if block_type not in _NON_SPEECH_BLOCK_TYPES:
        log.info("transcript の未知ブロック種別を発話として採らなかった: %r", block_type)
    return False


def _message_of(entry: dict[str, Any]) -> dict[str, Any]:
    """エントリの ``message`` を返す。

    Args:
        entry: トランスクリプトの 1 エントリ。

    Returns:
        ``message`` の dict。無い・dict でなければ空 dict。
    """
    message = entry.get("message")
    return message if isinstance(message, dict) else {}


def user_entry_content(entry: dict[str, Any]) -> object:
    """ユーザー発話のエントリから ``content`` を取り出す。

    host ごとに role の置き場所が違う（``type`` / ``role`` / ``message.role``）ため、どれかが
    ``user`` ならユーザー発話とみなす。ただし ``_NON_SPEECH_ENTRY_TYPES`` の ``type`` を持つ
    エントリは、``message.role`` が ``user`` でも発話ではない。この知識の唯一の置き場所で、
    足場診断（`scan_scaffold_drift`）も同じ判定を使う。

    Args:
        entry: トランスクリプトの 1 エントリ。

    Returns:
        ``content``（文字列・ブロック列など生の値）。ユーザー発話でない、または content が無ければ None。
    """
    entry_type = entry.get("type")
    if entry_type in _NON_SPEECH_ENTRY_TYPES:
        return None
    message = _message_of(entry)
    if "user" not in (entry_type, entry.get("role"), message.get("role")):
        return None
    return message.get("content") or entry.get("content")


def _user_message(entry: dict[str, Any]) -> str:
    """エントリがユーザー発話ならその本文を 1 行へ圧縮して返す。

    ハーネスが生成した足場（ローカルコマンドの注意書き・その stdout・
    サブエージェント完了通知）は ``normalize_user_message`` で落とし、
    スラッシュコマンド起動は ``/name args`` へ畳む。落とさないと引き継ぎが
    足場だけで埋まり、実際の依頼が押し出される。

    注入済みの ``<claq-memory>`` 等のタグは ``strip_tags`` で落とす。
    落とさないと前セッションへ注入した記憶をそのまま引き継ぎとして
    記録し直すエコーが起きる。無害化の合成と順序は ``_sanitize_freeform``
    が単一の情報源で、明示 handoff 経路と共有する。

    圧縮は無害化の**後**に掛ける。前に掛けると ``redact`` より先に 200 文字で切って
    シークレットが分断され、断片がマスクされずに残る。``compact_line`` は空白と行頭記号を
    整えるだけで行の途中の文字を消さないため、後に掛けても前後が接着してタグに戻らない。

    Args:
        entry: トランスクリプトの 1 エントリ。

    Returns:
        圧縮済みのユーザー発話。ユーザー発話でない、host が非依頼の印
        （``_HOST_META_FLAGS``）を付けた、中身が空、またはハーネス足場しか
        含まれていなければ空文字列。
    """
    if any(entry.get(flag) for flag in _HOST_META_FLAGS):
        return ""
    content = user_entry_content(entry)
    if content is None:
        return ""

    raw = strip_ansi(_text_content(content))
    return compact_line(_sanitize_freeform(raw))


def _collect_tools(entry: dict[str, Any], tools: set[str], files: set[str], work_tree: str) -> None:
    """エントリから使用ツール名と編集対象ファイルを収集する。

    単独の ``tool_use`` エントリと、assistant メッセージ内の ``tool_use``
    ブロックの両方を見る（ハーネスによって形が異なるため）。

    単独エントリのツール名・入力コンテナの抽出は ``lib/harness`` に委ねる。
    ここで ``tool_name`` / ``tool_input`` だけを直接読むと camelCase
    （``toolName`` / ``toolInput``）・``toolArgs``・JSON 文字列化コンテナを
    取りこぼす。コンテナキー一覧を独自に持つと同じ取りこぼしが起きるため、
    共有層に寄せる。

    assistant メッセージ内のブロックは Anthropic の content block スキーマ
    （``{"type": "tool_use", "name": ..., "input": ...}``）で別物なので、
    そちらは ``input`` を直接読む。

    Args:
        entry: トランスクリプトの 1 エントリ。
        tools: 使用ツール名の収集先。
        files: 変更ファイルパスの収集先。
        work_tree: 変更ファイルを集める作業ツリーのルート絶対パス。
    """
    raw_name = extract_raw_tool_name(entry) or str(entry.get("name") or "")
    if raw_name:
        _record_tool(raw_name, extract_tool_input(entry) or entry.get("input"), tools, files, work_tree)

    content = _message_of(entry).get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            _record_tool(str(block.get("name") or ""), block.get("input"), tools, files, work_tree)


def _record_tool(tool_name: str, tool_input: object, tools: set[str], files: set[str], work_tree: str) -> None:
    """ツール名を正規化して記録し、編集系なら作業ツリー内の対象ファイルも集める。

    Args:
        tool_name: トランスクリプト上のツール名（正規化前）。
        tool_input: ツール入力。dict / パッチ文字列以外は入力なしとして扱う。
        tools: 使用ツール名の収集先。
        files: 変更ファイルパスの収集先。
        work_tree: 変更ファイルを集める作業ツリーのルート絶対パス。
    """
    if not tool_name:
        return
    normalized = normalize_tool_name(tool_name)
    _add_sanitized(tools, normalized)
    if normalized not in _EDIT_TOOLS:
        return
    usable_input = tool_input if isinstance(tool_input, dict | str) else None
    for path in extract_file_paths(tool_name, usable_input) or ():
        if _is_in_work_tree(path, work_tree):
            _add_sanitized(files, _shorten_path(path))


def _is_in_work_tree(path: str, work_tree: str) -> bool:
    """編集対象のパスが作業ツリーの中かを判定する。

    引き継ぎはリポジトリ単位で注入されるため、scratchpad など作業ツリー外の編集を
    載せると枠を消費するうえ、次セッションが開けないパスを渡すことになる。相対パスは
    作業ツリーからの相対として中に数える。NUL を含む絶対パスは ``realpath`` が
    ``ValueError`` を送出するため、実在しないパスとして外に数える。

    Args:
        path: transcript から拾った生のパス。
        work_tree: 作業ツリーのルート絶対パス（シンボリックリンク解決済み）。

    Returns:
        相対パス、または解決後に作業ツリー配下になる絶対パスなら True。
    """
    if not os.path.isabs(path):
        return True
    try:
        resolved = os.path.realpath(path)
    except ValueError:
        return False
    return Path(resolved).is_relative_to(work_tree)


def _add_sanitized(collected: set[str], value: str) -> None:
    """パス・ツール名を無害化し、空にならなければ収集先へ加える。

    Args:
        collected: 収集先。
        value: transcript から拾った生の値。
    """
    sanitized = _sanitize_structured(value)
    if sanitized:
        collected.add(sanitized)


def _shorten_path(path: str) -> str:
    """ファイルパスを末尾数セグメントへ縮める。

    絶対パスを丸ごと載せると数本で予算を食い潰すうえ、次セッションが
    知りたいのは「どのファイルを触っていたか」だけで先頭のディレクトリ
    階層は不要なため。無害化（``_STRUCTURED_VALUE_LIMIT`` 文字で切る）より
    前に掛ける。後に掛けると、上限を超える深いパスは末尾のファイル名が
    切り落とされた先頭部分から縮めることになる。

    区切りは ``/`` と ``\\`` の両方を見る。``/`` だけで分割すると
    ``C:\\Users\\<name>\\proj\\src\\app.py`` が 1 セグメント扱いになり、
    ユーザー名を含む絶対パスがそのまま handoff と DB に残る。
    プラットフォーム判定はしない
    — POSIX のパスに ``\\`` が現れることは事実上なく、両方を区切りとして
    扱う 1 本のロジックで 3 プラットフォームを賄える。

    Args:
        path: トランスクリプトから拾った生のパス。

    Returns:
        末尾 ``_PATH_SEGMENT_LIMIT`` セグメント。省略した場合は ``…/`` を付ける。
    """
    segments = [segment for segment in PATH_SEPARATOR_RE.split(path) if segment]
    if len(segments) <= _PATH_SEGMENT_LIMIT:
        return path
    return "…/" + "/".join(segments[-_PATH_SEGMENT_LIMIT:])


def _compose(messages: list[str], files: list[str], tools: list[str]) -> str:
    """収集結果を人間可読の引き継ぎ散文へ組み立てる。

    ユーザー依頼も変更ファイルも無い場合は次セッションへ渡す内容が無いと
    みなす（使用ツールだけでは何をしていたか分からない）。

    Args:
        messages: 直近のユーザー依頼。
        files: 末尾セグメントへ縮めた変更ファイルパス。
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
        lines.append(f"変更ファイル: {', '.join(files)}")
    if tools:
        lines.append(f"使用ツール: {', '.join(tools)}")
    return "\n".join(lines)
