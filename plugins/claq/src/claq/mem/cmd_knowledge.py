"""知識カードの CRUD と全文検索のサブコマンドハンドラ。

``learn`` / ``list`` / ``show`` / ``promote`` / ``forget`` / ``search`` を持つ。
出力は最小トークンに絞る: ``body`` を返すのは ``show`` のみで、``list`` / ``search`` は
``title`` を 1 件 1 行で出し、``LIST_CHAR_BUDGET`` / ``SEARCH_CHAR_BUDGET`` 文字で
打ち切る。``search`` は全件をロードして Python 側でスコアリングする（転置インデックスも
仮想テーブルも持たない）。このモジュールは ``cli`` を import しない。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

from claq.lib.core_utils import actor_identity
from claq.mem import logger as mem_logger
from claq.mem.command_support import CommandArgs, CommandError, _coerce_limit, _find_knowledge, _fit_budget
from claq.mem.database import Database
from claq.mem.knowledge_input import (
    KINDS,
    STATUSES,
    KnowledgeInputError,
    parse_knowledge_payload,
    validate_choice,
)
from claq.mem.models import Knowledge, utc_now_iso
from claq.mem.repo_identity import resolve_repo
from claq.mem.settings import Settings

log = mem_logger.get("CLI")


LIST_DEFAULT_LIMIT = 20
"""``list`` の既定表示件数。増やすには ``--limit`` の明示が要る。"""

LIST_CHAR_BUDGET = 1400
"""``list`` の出力予算（文字数）。400 トークン相当で打ち切る。"""

SEARCH_DEFAULT_LIMIT = 5
"""``search`` の既定表示件数。上位だけ返し、詳細は ``show`` に任せる。"""

SEARCH_CHAR_BUDGET = 700
"""``search`` の出力予算（文字数）。200 トークン相当で打ち切る。"""

_SEARCH_FIELD_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("title", 3.0),
    ("key", 1.5),
    ("domain", 1.0),
    ("body", 0.5),
)
"""検索語がどの項目に当たったかの重み。

``title`` は人が書いた 1 行要約で、語が出るならそれが主題そのものなので最重量。
``key`` は title 由来のスラッグで意味は同じだが機械生成で語が落ちるため半減。
``domain`` は分類語で、当たれば主題を示すが粒度が粗い。``body`` は長文ゆえに
無関係な語が偶然含まれる確率が高く、``title`` の 1/6 まで下げる。
"""

_SEARCH_RECENCY_SCALE_DAYS = 90.0
"""新しさ係数の減衰スケール（日）。この日数で係数が 1.0 → 0.75 に落ちる。"""

_SEARCH_MULTIPLIER_FLOOR = 0.5
"""confidence / 新しさ係数の下限。

どちらも下限 0.5・上限 1.0 に収めるため、両方が最悪でもスコアは 1/4 までしか
下がらない。``title`` ヒット（3.0）が ``body`` ヒット（0.5）を下回ることはなく、
確度と鮮度は僅差の順位を決める補正に留まる。confidence=0.0 のカードが
スコア 0 に潰れて「ヒットしなかった」扱いになるのも防ぐ。
"""


# 人間が promote 済みのカードへ再 learn したときの拒否理由。
# 「失敗」ではなく「その知識は既に有効なので何もしなくてよい」と読めるよう、
# 次に取るべき行動まで書く。ここを曖昧にすると、agent が別 key で作り直して
# 重複カードを量産する（この経路が防ごうとしている事故そのもの）。
_ACTIVE_CARD_IS_IMMUTABLE_MESSAGE = (
    "learn: {key} は既に active（人間が promote 済み）です。"
    "agent からの更新は受け付けません —— 更新できてしまうと、人間が承認した内容と"
    "違うものが注入され続けるためです。この知識は既に SessionStart へ注入されている"
    "ので、記録としては完了しています。**別の key で作り直さないでください**"
    "（重複カードになります）。本文を変えたい場合は人間へ依頼してください。"
)


_Ranker = Callable[[Knowledge], float]
"""知識カード 1 件に検索スコアを与える関数。0.0 は「ヒットなし」を意味する。"""


# --- 知識 CRUD ---


def _handle_learn(settings: Settings, args: CommandArgs) -> None:
    """learn コマンド: stdin の JSON から知識カードを 1 件登録する。

    出力は ``learned: <key>`` の 1 行のみ。``source``/``status`` は常に
    ``agent``/``pending`` に固定され、有効化は ``promote <key>`` のみ。

    **active カードは learn 経路から不変にする**。``upsert`` は status を無条件に
    上書きするため、そのまま通すと人間が ``promote`` したカードへの再 learn で
    承認が黙って取り消され、本文まで差し替わる。既存 status を維持して本文だけ
    更新する案は「agent が承認済みの内容を書き換えられる」穴になるため採らず、
    拒否する。``archived`` / ``pending`` は更新でき、``archived`` は ``pending``
    へ戻るだけで注入はされない。

    Args:
        settings: mem 設定。
        args: コマンド引数と stdin JSON。

    Raises:
        CommandError: payload が不正な場合、または同じ key の既存カードが
            既に ``active`` の場合。
    """
    try:
        draft = parse_knowledge_payload(args.stdin_data)
    except KnowledgeInputError as e:
        raise CommandError(str(e)) from e

    with Database(settings.db_path) as db:
        repo_id = resolve_repo(None, db).id if draft.scope == "repo" else None
        existing = db.get_knowledge_by_key(draft.key, repo_id)
        if existing is not None and existing.status == "active":
            raise CommandError(_ACTIVE_CARD_IS_IMMUTABLE_MESSAGE.format(key=draft.key))
        db.upsert_knowledge(draft.to_knowledge(repo_id))
    print(f"learned: {draft.key}")


def _collect_list_rows(
    settings: Settings,
    args: CommandArgs,
    *,
    default_limit: int = LIST_DEFAULT_LIMIT,
    rank: _Ranker | None = None,
) -> list[Knowledge]:
    """``list`` / ``search`` の対象行を DB から集めて整列する。

    既定は cwd の repo スコープと global スコープの両方。``--global`` /
    ``--repo`` でどちらか一方に絞る（両者の排他は dispatch 層で検証済み）。

    Args:
        settings: mem 設定。
        args: コマンド引数。
        default_limit: ``--limit`` 未指定時の件数。
        rank: 各行のスコアを返す関数。None なら更新の新しい順に並べる。
            指定した場合はスコアの高い順（同点は更新の新しい順）に並べ、
            スコアが 0 以下の行は落とす。

    Returns:
        整列して ``--limit`` 件で切った Knowledge のリスト。

    Raises:
        CommandError: 列挙値・件数が不正な場合。
    """
    try:
        status = validate_choice("status", args.values.get("--status", "active"), STATUSES)
        kind = args.values.get("--kind")
        if kind is not None:
            validate_choice("kind", kind, KINDS)
    except KnowledgeInputError as e:
        raise CommandError(str(e)) from e
    limit = _coerce_limit(args.values.get("--limit"), default_limit)

    rows: list[Knowledge] = []
    with Database(settings.db_path) as db:
        if "--global" not in args.flags:
            rows += db.list_knowledge(scope="repo", repo_id=resolve_repo(None, db).id, status=status)
        if "--repo" not in args.flags:
            rows += db.list_knowledge(scope="global", status=status)
    if kind is not None:
        rows = [row for row in rows if row.kind == kind]

    if rank is None:
        rows.sort(key=lambda row: (row.updated_at, row.id or 0), reverse=True)
        return rows[:limit]
    hits = [(score, row) for row in rows if (score := rank(row)) > 0.0]
    hits.sort(key=lambda hit: (hit[0], hit[1].updated_at, hit[1].id or 0), reverse=True)
    return [row for _score, row in hits[:limit]]


def _emit_rows(rows: list[Knowledge], args: CommandArgs, budget: int) -> None:
    """知識カード群を 1 件 1 行で出力する。

    ``body`` は決して出さない。0 件なら 1 文字も出さない（「見つかりません」も
    出さず、エージェントの空振りコストを 0 トークンに落とす）。

    Args:
        rows: 出力する知識カード。
        args: コマンド引数。``--json`` の有無だけを見る。
        budget: 素テキスト出力に使ってよい文字数の上限。
    """
    if not rows:
        return
    if "--json" in args.flags:
        payload = [{"key": row.key, "scope": row.scope, "kind": row.kind, "title": row.title} for row in rows]
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return
    for line in _fit_budget([f"- [{row.kind}] {row.title} ({row.key})" for row in rows], budget):
        print(line)


def _handle_list(settings: Settings, args: CommandArgs) -> None:
    """list コマンド: 知識カードの一覧を ``- [kind] title (key)`` で 1 件 1 行に出す。

    Args:
        settings: mem 設定。
        args: コマンド引数。
    """
    _emit_rows(_collect_list_rows(settings, args), args, LIST_CHAR_BUDGET)


def _query_terms(query: str) -> tuple[str, ...]:
    """検索クエリを小文字の検索語へ分解する。

    語が 2 つ以上に割れた場合は、空白を取り除いたクエリ全体も検索語に足す。
    ``型 ヒント`` のように空白を入れたクエリでも本文の ``型ヒント`` を拾うため。

    Args:
        query: 利用者が渡したクエリ文字列。

    Returns:
        重複を除いた検索語のタプル。

    Raises:
        CommandError: 空白のみで検索語が 1 つも取れない場合。
    """
    terms = [term.lower() for term in query.split()]
    if not terms:
        raise CommandError("search: 検索クエリを指定してください")
    if len(terms) > 1:
        terms.append("".join(terms))
    return tuple(dict.fromkeys(terms))


def _match_score(row: Knowledge, terms: tuple[str, ...]) -> float:
    """検索語が知識カードのどの項目に当たったかを重み付きで合算する。

    1 語が複数項目に当たればその分だけ加算し、語ごとの合算で「何語を
    カバーしたか」も効く。

    Args:
        row: 採点対象の知識カード。
        terms: 小文字化済みの検索語。

    Returns:
        一致の重み合計。1 語も当たらなければ 0.0。
    """
    haystacks = {
        "title": row.title.lower(),
        "key": row.key.lower(),
        "domain": (row.domain or "").lower(),
        "body": row.body.lower(),
    }
    return sum(
        (weight for term in terms for name, weight in _SEARCH_FIELD_WEIGHTS if term in haystacks[name]),
        0.0,
    )


def _scaled_factor(value: float) -> float:
    """0.0〜1.0 を ``_SEARCH_MULTIPLIER_FLOOR``〜1.0 へ線形写像する。"""
    return _SEARCH_MULTIPLIER_FLOOR + (1.0 - _SEARCH_MULTIPLIER_FLOOR) * value


def _recency_factor(updated_at: str, now: datetime) -> float:
    """更新時刻の新しさを ``_SEARCH_MULTIPLIER_FLOOR``〜1.0 の係数へ写す。

    Args:
        updated_at: 知識カードの ISO8601 更新時刻。
        now: 現在時刻（tz-aware）。

    Returns:
        新しいほど 1.0 に近く、古いほど下限へ漸近する係数。
    """
    age_days = max((now - datetime.fromisoformat(updated_at)).total_seconds(), 0.0) / 86400.0
    return _scaled_factor(1.0 / (1.0 + age_days / _SEARCH_RECENCY_SCALE_DAYS))


def _score_knowledge(row: Knowledge, terms: tuple[str, ...], now: datetime) -> float:
    """知識カード 1 件の検索スコアを返す。

    一致の重み合計に確度と新しさの係数を掛ける。係数に下限があるので、一度でも
    一致した行のスコアは必ず正になり「ヒットなし」と混ざらない。

    Args:
        row: 採点対象の知識カード。
        terms: 小文字化済みの検索語。
        now: 現在時刻（tz-aware）。

    Returns:
        検索スコア。1 語も当たらなければ 0.0。
    """
    matched = _match_score(row, terms)
    if matched == 0.0:
        return 0.0
    return matched * _scaled_factor(row.confidence) * _recency_factor(row.updated_at, now)


def _handle_search(settings: Settings, args: CommandArgs) -> None:
    """search コマンド: クエリに一致する知識カードを 1 件 1 行で出す。

    ``body`` はスコア計算にだけ使い、出力しない。本文は上位の key を ``show``
    に渡して読む二段構えにする。

    Args:
        settings: mem 設定。
        args: コマンド引数。位置引数の連結が検索クエリ。

    Raises:
        CommandError: 検索クエリが空、またはオプション値が不正な場合。
    """
    terms = _query_terms(" ".join(args.positionals))
    now = datetime.now(UTC)
    rows = _collect_list_rows(
        settings,
        args,
        default_limit=SEARCH_DEFAULT_LIMIT,
        rank=lambda row: _score_knowledge(row, terms, now),
    )
    _emit_rows(rows, args, SEARCH_CHAR_BUDGET)


def _handle_show(settings: Settings, args: CommandArgs) -> None:
    """show コマンド: 知識カード 1 件を全項目表示する。``body`` を返す唯一の口。

    Args:
        settings: mem 設定。
        args: コマンド引数。

    Raises:
        CommandError: 該当が無い場合。
    """
    with Database(settings.db_path) as db:
        found = _find_knowledge(db, args.positionals[0])
    for label, value in (
        ("key", found.key),
        ("scope", found.scope),
        ("repo", found.repo_id or "-"),
        ("kind", found.kind),
        ("title", found.title),
        ("body", found.body or "-"),
        ("domain", found.domain or "-"),
        ("confidence", f"{found.confidence:.2f}"),
        ("status", found.status),
        ("source", found.source),
        ("updated_at", found.updated_at),
    ):
        print(f"{label}: {value}")


def _handle_promote(settings: Settings, args: CommandArgs) -> None:
    """promote コマンド: 知識カードの status を active にする。

    昇格は以後の全セッションへ自動注入される状態への遷移なので、誰が・いつ・
    どのカードを昇格させたかをログへ残す。これは境界ではなく事後追跡である
    （同一 UID から mem.db を直接更新できる以上、人間の実行を技術的に強制
    できず、Bash を持つ agent からも到達できる）。

    Args:
        settings: mem 設定。
        args: コマンド引数。

    Raises:
        CommandError: 該当が無い場合。
    """
    key = args.positionals[0]
    # actor は DB 更新の前に確定させる。更新後だと、識別子の取得失敗が
    # 「active になったのに監査ログが残らず非 0 終了」という部分成功になる。
    actor = actor_identity()
    with Database(settings.db_path) as db:
        found = _find_knowledge(db, key)
        db.set_knowledge_status(found.id, "active")
    log.info(
        "promote: key=%s scope=%s source=%s previous_status=%s actor=%s",
        found.key,
        found.scope,
        found.source,
        found.status,
        actor,
    )
    print(f"promoted: {key}")


def _handle_forget(settings: Settings, args: CommandArgs) -> None:
    """forget コマンド: 知識カードを archived にする。

    ``--superseded-by KEY`` を渡すと、その key の ``knowledge.id`` を
    ``superseded_by`` に記録して置き換え関係を残す。

    Args:
        settings: mem 設定。
        args: コマンド引数。

    Raises:
        CommandError: 該当が無い場合。
    """
    key = args.positionals[0]
    successor_key = args.values.get("--superseded-by")
    with Database(settings.db_path) as db:
        found = _find_knowledge(db, key)
        found.status = "archived"
        found.updated_at = utc_now_iso()
        if successor_key is not None:
            found.superseded_by = _find_knowledge(db, successor_key).id
        db.upsert_knowledge(found)
    suffix = f" (superseded by {successor_key})" if successor_key is not None else ""
    print(f"forgot: {key}{suffix}")
