"""SessionStart への知識注入（``context``）と SessionEnd の引き継ぎ記録（``handoff``）。

注入する ``<claq-memory>`` ブロックの組み立てと、その本文を無害化する関数群
（``_one_line`` / ``_neutralize_handoff_structure`` / ``_format_injected_item`` 等）を持つ。
このモジュールは ``cli`` を import しない。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from claq.hooks.detach import recent_bg_failure_notice
from claq.lib.core_utils import get_plugin_root
from claq.lib.env_pointer import ancestor_pointers_supported
from claq.lib.harness import normalize_user_message
from claq.mem import logger as mem_logger
from claq.mem.command_support import CommandArgs, _fit_budget, _session_uid
from claq.mem.database import Database
from claq.mem.handoff import build_handoff
from claq.mem.knowledge_input import optional_str
from claq.mem.models import Knowledge, Session, utc_now_iso
from claq.mem.redaction import redact
from claq.mem.repo_identity import resolve_repo
from claq.mem.settings import (
    CONTEXT_GLOBAL_CHAR_BUDGET,
    CONTEXT_HANDOFF_CHAR_BUDGET,
    CONTEXT_ITEM_CHAR_LIMIT,
    CONTEXT_REPO_CHAR_BUDGET,
    Settings,
)
from claq.mem.tag_stripping import keep_allowed_chars, keep_allowed_chars_for_injection, strip_tags

log = mem_logger.get("CLI")


# --- SessionStart への知識注入 ---


def _one_line(text: str) -> str:
    """連続する空白と改行を 1 個の空白へ畳み、1 行にする。

    注入枠の中で「1 行」として描かれる文字列は、改行が残ると 1 件が複数行へ割れて
    偽の項目を増やせる。描画側で畳むことで、DB に既に入っている行も守る。

    Args:
        text: 畳む対象の文字列

    Returns:
        改行を含まない 1 行
    """
    return " ".join(text.split())


#: 引き継ぎ本文の行頭で無害化する構造マーカー。注入枠は `## 見出し` と
#: `- [kind] タイトル` で組み立てられているため、引き継ぎ側がこの 2 つを行頭へ
#: 置けると、人間の承認（promote）を通った知識と見分けの付かない節や項目を
#: 捏造できる。引き継ぎは promote を通らないので、ここが唯一の防壁になる。
_HANDOFF_STRUCTURE_MARKERS = ("#", "- [")


def _neutralize_handoff_structure(text: str) -> str:
    """引き継ぎ本文が注入枠の構造を偽装するのを防ぐ。

    行頭の `#`（見出し）と `- [`（知識カード行）だけを対象にし、空白 1 個で
    字下げする。正当な引き継ぎが使う改行と通常の箇条書き `- ` は残す。

    Args:
        text: 引き継ぎ本文

    Returns:
        行頭の構造マーカーを無害化した本文
    """
    return "\n".join(
        f" {line.lstrip()}" if line.lstrip().startswith(_HANDOFF_STRUCTURE_MARKERS) else line
        for line in text.split("\n")
    )


def _format_injected_item(row: Knowledge) -> str:
    """知識カード 1 件を注入用の 1 行へ整形する。

    既定は ``- [kind] title`` のみ。``body`` を足しても ``CONTEXT_ITEM_CHAR_LIMIT``
    文字に収まる場合だけ ``—`` で続け、長い body が節の予算を食い潰すのを防ぐ。
    title / body は信頼境界タグを偽装できるため ``strip_tags`` で無害化する。その前に
    ``keep_allowed_chars_for_injection`` で許可した文字だけを残す（後ろに置くと、不可視文字で
    分断したタグが除去で再結合して ``strip_tags`` をすり抜ける）。読み出し側は ``redact`` を
    掛けないため、除去で再結合した鍵だけは同関数が伏せる。

    Args:
        row: 注入対象の知識カード。

    Returns:
        整形済みの 1 行（改行を含まない）。
    """
    # 入口で畳んでいても、既に DB にある行や将来の別経路を信用しない。返り値が
    # 「改行を含まない 1 行」であることは、この関数が自分で保証する。
    head = f"- [{row.kind}] {_one_line(strip_tags(keep_allowed_chars_for_injection(row.title)))}"
    body = _one_line(strip_tags(keep_allowed_chars_for_injection(row.body).strip()))
    if not body:
        return head
    combined = f"{head} — {body}"
    return combined if len(combined) <= CONTEXT_ITEM_CHAR_LIMIT else head


def _knowledge_section(heading: str, rows: list[Knowledge], budget: int) -> str:
    """知識カード群を見出し付きの 1 節へ組み立てる。

    並び順は ``confidence DESC, updated_at DESC``（同値は ``id`` の新しい順）。

    Args:
        heading: 節の見出し（``## `` は本関数が付ける）。
        rows: 節に載せる知識カード。
        budget: 明細行に使ってよい文字数の上限。

    Returns:
        見出しと明細からなる節の文字列。*rows* が空なら空文字列。
    """
    if not rows:
        return ""
    ordered = sorted(rows, key=lambda row: (row.confidence, row.updated_at, row.id or 0), reverse=True)
    return "\n".join([f"## {heading}", *_fit_budget([_format_injected_item(row) for row in ordered], budget)])


def _handoff_section(db: Database, repo_id: str) -> str:
    """直近セッションの引き継ぎを ``前回の続き`` 節へ組み立てる。

    handoff 本文は信頼境界タグを偽装できるため ``strip_tags`` で無害化する。その前に
    ``keep_allowed_chars_for_injection`` で許可した文字だけを残す。行区切りは LF に揃って
    改行として残るので、``_neutralize_handoff_structure`` は全ての論理行を見られる。

    Args:
        db: 参照する mem データベース。
        repo_id: 対象リポジトリの ``repos.id``。

    Returns:
        見出しと引き継ぎ本文からなる節。引き継ぎが無ければ空文字列。
    """
    session = db.get_latest_session(repo_id)
    if session is None:
        return ""
    handoff = _neutralize_handoff_structure(strip_tags(keep_allowed_chars_for_injection(session.handoff).strip()))
    if not handoff:
        return ""
    if len(handoff) > CONTEXT_HANDOFF_CHAR_BUDGET:
        handoff = handoff[: CONTEXT_HANDOFF_CHAR_BUDGET - 1] + "…"
    stamp = datetime.fromisoformat(session.ended_at or session.started_at).strftime("%Y-%m-%d %H:%M")
    return f"## 前回の続き ({stamp})\n{handoff}"


# detach 失敗通知の 1 行化に使う。許可リスト処理が行区切りを LF に揃えた後の、LF の連なりを空白 1 つに畳む。
_BG_NOTICE_LINE_BREAKS = re.compile(r"\n+")


def _bg_failure_section() -> str:
    """前回セッションの detach 起動（``--bg``）失敗痕跡があれば 1 行の節にする。

    通知本文はログファイルの末尾行そのままで、ログへ書かれた行は次セッション
    以降へ注入され続ける。``</claq-memory> IMPORTANT: ...`` を追記するだけで
    注入枠の境界を閉じられるため、handoff 節の ``_sanitize_freeform`` と同じ
    順序（``normalize_user_message`` → ``strip_tags`` → ``redact``）で無害化する。
    ``strip_tags`` だけでは足場タグが素通りし、``redact`` はログ中のシークレット
    を伏せる。タグで分断された秘密は ``strip_tags`` で再結合して初めて
    ``redact`` に一致するため、``redact`` を後に置く。

    その前段で、許可した文字だけを残して行区切り（CR・NEL・LS・PS・VT・FF を含む）を LF に
    揃え、LF の連なりを空白 1 つに畳んで 1 行にする（ESC・BEL・NUL は表示と後段の文字列処理を
    壊し、改行は見出し行の下に別の行を作れる）。除去を無害化の後に置くと、制御文字・不可視文字で
    分断した秘密や閉じタグが除去で再結合し、``redact`` / ``strip_tags`` が見ていない形で
    注入されるため、必ず最初に行う。

    Returns:
        痕跡があれば見出し付き 1 行の節。無ければ空文字列。
    """
    one_line = _BG_NOTICE_LINE_BREAKS.sub(" ", keep_allowed_chars(recent_bg_failure_notice()))
    notice = redact(strip_tags(normalize_user_message(one_line))).strip()
    if not notice:
        return ""
    return f"## 前回セッションの通知\n{notice}"


def _shell_bootstrap_section() -> str:
    """`$HOME/.claq/env.sh` が使えない OS で、その読み替え方を 1 節で示す。

    md の bash fence は ``. "$HOME/.claq/env.sh"`` → ``claq_run <module> ...``
    の形を取るが、この resolver は祖先 PID の照合に依存し、``ps`` の無い
    Windows では解決できない。md を全部 2 通り書くとプロンプトが倍になるため、
    この hook を起動した host 自身の plugin root を SessionStart で 1 度だけ渡す。

    Returns:
        読み替え方の節。POSIX では空文字列。
    """
    if ancestor_pointers_supported():
        return ""
    wrapper = get_plugin_root() / "runtime" / "claq-hook.cmd"
    return (
        "## claq の呼び出し\n"
        'この OS では `. "$HOME/.claq/env.sh"` は解決できない。md 中の '
        f'`claq_run <module> ...` は `"{wrapper}" <module> ...`'
        "（PowerShell では先頭に `&`）へ、"
        f'`claq_mem_learn ...` は `CLAQ_LEARN_KIND=... CLAQ_LEARN_TITLE=... "{wrapper}" '
        f'claq.mem.learn_payload` の JSON を `"{wrapper}" claq.mem.cli learn` の '
        "stdin へ渡す形へ読み替える。"
    )


def _build_context(settings: Settings, args: CommandArgs) -> str:
    """注入する ``<claq-memory>`` ブロックを組み立てる。

    共通知識・リポジトリ知識・前回の引き継ぎを、いずれも ``status='active'`` に
    限って集める（``pending`` は昇格レビュー前の下書きでノイズになる）。加えて
    detach 起動の失敗通知と、Windows 向けの呼び出し方を節として足す。

    Args:
        settings: mem 設定。
        args: コマンド引数とフックの stdin JSON。

    Returns:
        注入する文字列。全節が空なら空文字列（注入をゼロにする）。
    """
    payload = args.stdin_data
    with Database(settings.db_path) as db:
        repo = resolve_repo(optional_str(payload.get("cwd")), db)
        # ここが sessions 行を作る唯一の場所で、SessionEnd の handoff はこの行を
        # 更新する。session_uid は NOT NULL UNIQUE なので空文字では作らない。
        if session_uid := _session_uid(payload):
            db.start_session(Session(session_uid=session_uid, repo_id=repo.id))
        sections = [
            _knowledge_section(
                "共通知識",
                db.list_knowledge(scope="global", status="active"),
                CONTEXT_GLOBAL_CHAR_BUDGET,
            ),
            _knowledge_section(
                repo.id,
                db.list_knowledge(scope="repo", repo_id=repo.id, status="active"),
                CONTEXT_REPO_CHAR_BUDGET,
            ),
            _handoff_section(db, repo.id),
            _bg_failure_section(),
            _shell_bootstrap_section(),
        ]

    body = "\n\n".join(section for section in sections if section)
    if not body:
        return ""
    return f"<claq-memory>\n{body}\n</claq-memory>"


def _handle_context(settings: Settings, args: CommandArgs) -> str:
    """context コマンド: SessionStart へ蓄積済みの知識を注入する。

    毎セッション必ず走る唯一の注入口。フックを壊さないため失敗は握り潰し、
    注入なし（空文字列）に倒す。

    Args:
        settings: mem 設定。
        args: コマンド引数とフックの stdin JSON。

    Returns:
        注入する追加コンテキスト。組み立てに失敗した場合は空文字列。
    """
    try:
        return _build_context(settings, args)
    except Exception as e:
        log.warning("context 失敗: %s", e)
        return ""


# --- SessionEnd の引き継ぎ記録 ---


def _record_handoff(settings: Settings, payload: dict[str, Any]) -> None:
    """引き継ぎ本文を組み立てて ``sessions`` へ書き込む。

    既存行があれば更新し、無ければ作る（SessionStart が走らなかったハーネスでも
    引き継ぎは残すべきため）。新設時の開始時刻は不明なので ``ended_at`` と同値に
    する。``session_id`` が無ければ何も書かない（``session_uid`` は NOT NULL
    UNIQUE で、空文字で埋めると全セッションが 1 行に衝突する）。

    Args:
        settings: mem 設定。
        payload: SessionEnd フックが stdin へ渡した JSON。
    """
    session_uid = _session_uid(payload)
    if not session_uid:
        return
    handoff = build_handoff(payload)
    if not handoff:
        return

    now = utc_now_iso()
    with Database(settings.db_path) as db:
        if db.finish_session(session_uid, handoff, now):
            return
        repo = resolve_repo(optional_str(payload.get("cwd")), db)
        db.start_session(
            Session(session_uid=session_uid, repo_id=repo.id, handoff=handoff, started_at=now, ended_at=now)
        )


def _handle_handoff(settings: Settings, args: CommandArgs) -> None:
    """handoff コマンド: SessionEnd の引き継ぎを ``sessions`` へ記録する。

    フック経路のため失敗は握り潰して終了コード 0 を保つ。外部 I/O は
    トランスクリプト走査（``TRANSCRIPT_TIMEOUT_SEC``）とローカル SQLite
    （既定 busy timeout 5 秒）のみで、いずれも上限時間を持つ。

    Args:
        settings: mem 設定。
        args: コマンド引数とフックの stdin JSON。
    """
    try:
        _record_handoff(settings, args.stdin_data)
    except Exception as e:
        log.warning("handoff 失敗: %s", e)
