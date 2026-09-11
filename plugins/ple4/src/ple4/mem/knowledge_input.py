"""知識カード入力の検証 — ``mem learn`` が使う唯一の入口。

``knowledge`` テーブルの CHECK 制約（``kind`` 6 値 / ``scope`` 2 値 /
``status`` 3 値 / ``source`` 3 値 / ``confidence`` 0.0〜1.0）を、DB へ到達する
前に Python 側で強制する。現在の入力経路は ``mem learn``（stdin の JSON。
人間・エージェントが明示的に書く）の1つのみ。

``source``/``status`` は generic learn の authority としては扱わない
（H-01 対応）。``mem learn`` の呼び出し元は agent・human・agent が処理した
外部入力のいずれもあり得て CLI 側には区別する手段が無いため、caller が
JSON へ ``source: "human"`` や ``status: "active"`` と書くだけで、永続
SessionStart context への注入（``status='active'``）を自己承認できていた。
本モジュールは常に ``source="agent"`` / ``status="pending"`` を書き込み、
payload がそれ以外の値を明示した場合は ``KnowledgeInputError`` にする
（黙って上書きすると「指定したのに効いていない」という別の事故を招くため）。
``status='active'`` への昇格は ``mem promote <key>`` による人間承認のみで
行う（詳細は ``docs/adr/04-untrusted-input-prompt-boundary.md``）。

検証を素通しする経路が生まれると、CHECK 制約違反が
``sqlite3.IntegrityError`` として遅れて表面化し、どのフィールドが悪いのか
利用者に伝わらなくなる。

``repo_id`` の解決には DB が要るため、本モジュールは ``KnowledgeDraft``
（``repo_id`` 未確定の検証済み入力）までを組み立て、``repos`` の解決は
呼び出し側に任せる。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from ple4.mem.models import Knowledge, utc_now_iso
from ple4.mem.redaction import redact_knowledge_text
from ple4.mem.repo_identity import slugify

KINDS: frozenset[str] = frozenset({"convention", "decision", "pitfall", "howto", "fact", "preference"})
"""``knowledge.kind`` の許容値。スキーマの CHECK 制約と 1 対 1 で対応する。"""

SCOPES: frozenset[str] = frozenset({"global", "repo"})
"""``knowledge.scope`` の許容値。"""

STATUSES: frozenset[str] = frozenset({"active", "pending", "archived"})
"""``knowledge.status`` の許容値。``list``/``search`` の ``--status`` 絞り込み
フラグが参照する（generic learn からは常に ``pending`` 固定。H-01 対応）。"""

# generic learn 経路が常に書き込む固定値（H-01 対応）。payload/CLI からの
# 上書きは authority として扱わず、非既定値は KnowledgeInputError にする。
_FIXED_SOURCE = "agent"
_FIXED_STATUS = "pending"

DEFAULT_CONFIDENCE = 0.5

#: title / body に許すバイト数の上限。
#:
#: 注入側は `strip_tags` を**全長**に対して走らせてから 200 文字判定で捨てるため、
#: 巨大な body は SessionStart（同期フック）の遅延に直結する。
#:
#: **先に切り詰めてはならない。** `handoff._sanitize_compact` が明記するとおり、
#: `redact` より先に切るとシークレットが分断され、断片がマスクされずに残る。
#: よって入口で受け付けない側で塞ぐ。知識カードは人間が読む要約なので、この上限が
#: 正当な入力を落とすことは実務上ない。
MAX_TITLE_BYTES = 4 * 1024
MAX_BODY_BYTES = 64 * 1024

"""``confidence`` 未指定時の確信度。"""

_ASCII_ALNUM_RE = re.compile(r"[A-Za-z0-9]")
_KEY_HASH_LENGTH = 8
_MIN_KEY_SLUG_LENGTH = 3


class KnowledgeInputError(ValueError):
    """知識カード入力が CHECK 制約を満たさないときに送出する。

    メッセージは利用者へ 1 行でそのまま提示できる粒度にする。
    """


def optional_str(value: Any) -> str | None:
    """空でない値だけを文字列にして返す。

    Args:
        value: JSON から得た任意の値。

    Returns:
        文字列化した値。None・空文字・空コンテナなら None。
    """
    return str(value) if value else None


def validate_choice(name: str, value: str, allowed: frozenset[str]) -> str:
    """列挙値を検証して返す。

    Args:
        name: エラーメッセージに出す項目名。
        value: 検証したい値。
        allowed: 許可される値の集合。

    Returns:
        検証を通った *value*。

    Raises:
        KnowledgeInputError: *value* が *allowed* に含まれない場合。
    """
    if value not in allowed:
        raise KnowledgeInputError(f"{name} は {'/'.join(sorted(allowed))} のいずれか: {value!r}")
    return value


def coerce_confidence(value: Any) -> float:
    """confidence を 0.0〜1.0 の float に変換する。

    Args:
        value: JSON から得た確信度。None なら ``DEFAULT_CONFIDENCE``。

    Returns:
        変換済みの確信度。

    Raises:
        KnowledgeInputError: 数値に変換できない、または範囲外の場合。
    """
    if value is None:
        return DEFAULT_CONFIDENCE
    try:
        confidence = float(value)
    except (TypeError, ValueError) as e:
        raise KnowledgeInputError(f"confidence は数値で指定してください: {value!r}") from e
    if not 0.0 <= confidence <= 1.0:
        raise KnowledgeInputError(f"confidence は 0.0〜1.0 の範囲で指定してください: {confidence}")
    return confidence


def _payload_choice(
    payload: dict[str, Any],
    name: str,
    allowed: frozenset[str],
    *,
    default: str = "",
) -> str:
    """ペイロードから列挙値を取り出して検証する。

    Args:
        payload: 知識カードを表す dict。
        name: 項目名。ペイロードのキーとエラーメッセージに使う。
        allowed: 許可される値の集合。
        default: ペイロードに値が無いときの既定値。

    Returns:
        検証を通った列挙値。

    Raises:
        KnowledgeInputError: 値が *allowed* に含まれない場合。
    """
    return validate_choice(name, str(payload.get(name) or default), allowed)


def _fixed_choice(payload: dict[str, Any], name: str, fixed_value: str) -> str:
    """generic learn 経路で常に固定値を返し、payload の非既定値を authority として拒否する（H-01 対応）。

    payload が *name* を明示的に指定していて、かつその値が *fixed_value* と
    異なる場合は ``KnowledgeInputError`` にする。未指定、または *fixed_value*
    と同じ値の明示は許可する（既定値の明示は無害なため）。

    Args:
        payload: 知識カードを表す dict。
        name: 項目名。ペイロードのキーとエラーメッセージに使う。
        fixed_value: この経路が常に使う固定値。

    Returns:
        常に *fixed_value*。

    Raises:
        KnowledgeInputError: payload が *fixed_value* と異なる値を明示した場合。
    """
    raw = payload.get(name)
    if raw is not None and str(raw) != fixed_value:
        raise KnowledgeInputError(
            f"{name} は generic learn からは指定できません（常に {fixed_value!r} 固定）: {raw!r}"
        )
    return fixed_value


def slug_with_hash_fallback(redacted: str, prefix: str) -> str:
    """redact 済みテキストを slug 化し、ASCII が足りなければ決定的ハッシュへ倒す。

    ``slugify`` は ASCII 英数字を 1 つも含まない入力に対して共通の
    ``FALLBACK_SLUG`` を返すため、``日本語`` と ``別`` のように異なる入力が
    同じ key へ潰れ、upsert で既存カードを黙って上書きしてしまう。ASCII が
    ``_MIN_KEY_SLUG_LENGTH`` 未満しか残らない入力は、``prefix`` と SHA-1
    先頭 8 桁による決定的な key へ倒してこの衝突を避ける。

    同じ入力からは常に同じ key が出るため、意図的な再 learn による upsert
    （同じ key へ上書きして知識を更新する正規の使い方）は壊さない。

    Args:
        redacted: redact 済みのテキスト（title または明示 key）。
        prefix: ハッシュ由来 key の接頭辞。

    Returns:
        ``[a-z0-9-]`` のみからなる slug。

    Raises:
        例外は発生しません。
    """
    base = slugify(redacted) if _ASCII_ALNUM_RE.search(redacted) else ""
    if len(base) < _MIN_KEY_SLUG_LENGTH:
        digest = hashlib.sha1(redacted.encode("utf-8")).hexdigest()[:_KEY_HASH_LENGTH]
        return f"{prefix}-{digest}"
    return base


def generate_key(raw_title: str, kind: str) -> str:
    """title から知識カードの key スラッグを生成する。

    key は **redact 後** の title から生成します（A-03 対応）。redact 前の
    生 title から生成すると、シークレットを含む title の断片が
    ``learned:`` 標準出力・``show <key>`` の入力履歴・ログへそのまま残る
    ためです（title/body 自体は redact 済みでも key 経由で漏れる）。

    ASCII 英数字を十分に含む redact 後 title は ``slugify`` で
    kebab-case 化します。``Keep [REDACTED] out of titles`` のような角括弧
    混じりの文字列も、``slugify`` の非英数字置換で安定したスラッグ
    （``keep-redacted-out-of-titles``）に落ちます。

    スラッグが ``_MIN_KEY_SLUG_LENGTH`` 未満になる title は、``kind`` と
    redact 後 title の SHA-1 先頭 8 桁で決定的な key を作ります。ASCII を
    全く含まない title（スラッグが空）に加え、日本語 title に ASCII が
    1〜2 文字だけ混ざる場合もこちらに倒します。``指示文書の列挙を『上記 N
    種』と個数で参照しない`` は ``n`` に、``md の構造テストで…`` は ``md``
    に潰れ、次に ASCII を 1〜2 文字だけ含む別の title が同じ key へ衝突して
    **既存カードを upsert で上書きする**（＝別の知識が失われる）ためです。

    redaction が title を書き換えた場合（＝シークレットを含んでいた場合）
    のみ、生 title の SHA-1 先頭 8 桁を key の末尾に付します。これは
    redact 後に同一の title へ潰れる 2 枚のカード（例:
    ``Keep sk-aaa... safe`` と ``Keep sk-bbb... safe`` はどちらも
    ``Keep [REDACTED] safe`` になる）が同じ key へ衝突するのを避けるため
    です。切り詰めた SHA-1 は一方向ハッシュであり生 title（＝シークレット
    本体）を復元できないため、これ自体が新たな漏えい経路にはなりません。
    同じ生 title を 2 度 learn した場合はハッシュも同じになるため同一 key
    に落ち、upsert され重複行は作られません。

    Args:
        raw_title: 知識カードの 1 行タイトル（redact 前）。
        kind: 知識の種別。ハッシュ由来 key の接頭辞に使う。

    Returns:
        ``[a-z0-9-]`` のみからなる key。

    Raises:
        例外は発生しません。
    """
    redacted_title = redact_knowledge_text(raw_title)
    base = slug_with_hash_fallback(redacted_title, kind)
    if redacted_title == raw_title:
        return base
    collision_guard = hashlib.sha1(raw_title.encode("utf-8")).hexdigest()[:_KEY_HASH_LENGTH]
    return f"{base}-{collision_guard}"


@dataclass(frozen=True)
class KnowledgeDraft:
    """``repo_id`` 解決前の検証済み知識カード入力。

    全フィールドが ``knowledge`` の CHECK 制約を満たすことを保証済み。
    ``repo_id`` だけは ``repos`` の解決（= DB アクセス）が要るため持たず、
    ``to_knowledge`` の引数で受け取る。

    Attributes:
        key: kebab-case スラッグ。重複投入の防止キー。
        scope: ``global`` または ``repo``。
        kind: ``convention`` / ``decision`` / ``pitfall`` / ``howto`` /
            ``fact`` / ``preference``。
        title: 1 行。これ単体で意味が通ること。
        body: why / how の補足。空でよい。
        domain: ``testing`` ``git`` ``build`` 等。任意。
        confidence: 0.0〜1.0 の確信度。
        status: ``active`` / ``pending`` / ``archived``。
        source: ``agent`` / ``observer`` / ``human``。
        source_ref: 出所の自由記述（ファイルパス等）。
    """

    key: str
    scope: str
    kind: str
    title: str
    body: str
    domain: str | None
    confidence: float
    status: str
    source: str
    source_ref: str | None

    def to_knowledge(self, repo_id: str | None) -> Knowledge:
        """解決済みの ``repo_id`` を与えて DB へ書ける Knowledge にする。

        Args:
            repo_id: ``scope='repo'`` のとき所属リポジトリの ``repos.id``、
                ``scope='global'`` のとき None。

        Returns:
            作成・更新時刻を現在時刻で埋めた Knowledge。

        Raises:
            KnowledgeInputError: scope と repo_id の組み合わせが
                スキーマの CHECK 制約と矛盾する場合。
        """
        if (self.scope == "repo") != (repo_id is not None):
            raise KnowledgeInputError(f"scope={self.scope!r} と repo_id={repo_id!r} が矛盾しています")
        now = utc_now_iso()
        return Knowledge(
            key=self.key,
            scope=self.scope,
            kind=self.kind,
            title=self.title,
            source=self.source,
            repo_id=repo_id,
            body=self.body,
            domain=self.domain,
            confidence=self.confidence,
            status=self.status,
            source_ref=self.source_ref,
            created_at=now,
            updated_at=now,
        )


def _reject_oversized(text: str, field: str, limit: int) -> None:
    """文字列が上限バイト数を超えていたら ``KnowledgeInputError`` を送出する。

    Args:
        text: 検査対象
        field: エラーメッセージへ出すフィールド名
        limit: 許すバイト数

    Raises:
        KnowledgeInputError: 上限を超えた場合。
    """
    size = len(text.encode("utf-8", "surrogatepass"))
    if size > limit:
        raise KnowledgeInputError(f"learn: {field} が大きすぎます（{size:,} バイト > {limit:,}）")


def _checked_body(payload: dict[str, Any]) -> str:
    """body を取り出し、上限バイト数を超えていないことを確かめて返す。

    Args:
        payload: learn の入力

    Returns:
        検査を通った body 文字列

    Raises:
        KnowledgeInputError: 上限を超えた場合。
    """
    body = str(payload.get("body") or "")
    _reject_oversized(body, "body", MAX_BODY_BYTES)
    return body


def parse_knowledge_payload(payload: dict[str, Any]) -> KnowledgeDraft:
    """JSON ペイロードを検証済みの ``KnowledgeDraft`` へ変換する。

    ``title`` / ``body`` / ``source_ref`` / ``domain`` は DB へ書く前に
    ``redact_knowledge_text`` で既知プレフィックス系シークレットを
    マスクする（agent/外部入力由来の未検証データが SessionStart context
    へそのまま昇格するのを防ぐ）。key は **redact 後**の title から
    `generate_key` で生成する（A-03 対応。redact 前の生 title から生成する
    と、title/body 自体は redact 済みでも key・``learned:`` 標準出力・
    ``show <key>`` の入力履歴経由でシークレットが残ってしまうため）。
    ペイロードが ``key`` を明示指定した場合も、生の値をそのまま使わず
    redact + slugify を通してから使う（同じ理由。明示 key だけが検証・
    redaction を素通りする穴になっていた）。

    ``source``/``status`` は常に ``agent``/``pending`` に固定する（H-01
    対応）。payload がそれ以外の値を明示した場合は拒否する（`_fixed_choice`）。

    Args:
        payload: 知識カードを表す dict。

    Returns:
        全項目が CHECK 制約を満たす KnowledgeDraft。

    Raises:
        KnowledgeInputError: title 欠落、列挙値・数値の不正、または
            ``source``/``status`` に非既定値を明示した場合。
    """
    # `.strip()` は前後しか落とさず、内部の改行は DB へ入る。title は注入時に
    # `- [kind] title` の 1 行として描かれる契約なので、改行が残ると 1 枚のカードが
    # 複数行に割れ、偽の知識カード行を注入枠の内側に作れる（実測済み）。
    title = " ".join(str(payload.get("title") or "").split())
    _reject_oversized(title, "title", MAX_TITLE_BYTES)
    if not title:
        raise KnowledgeInputError("learn: title は必須です")

    kind = _payload_choice(payload, "kind", KINDS)
    scope = _payload_choice(payload, "scope", SCOPES, default="repo")
    source = _fixed_choice(payload, "source", _FIXED_SOURCE)
    status = _fixed_choice(payload, "status", _FIXED_STATUS)

    raw_key = str(payload.get("key") or "").strip()
    # 明示 key もシークレット断片を持ち込みうるため、生成 key と同じく
    # redact してから slugify を通す（A-03 対応。ここが素通りする穴だった）。
    # ハッシュ fallback も title 由来 key と同じく適用する。適用しないと
    # `日本語` と `別` のような ASCII を含まない明示 key が同じ slug へ潰れ、
    # upsert で別の知識を黙って上書きする（F-24）。
    key = slug_with_hash_fallback(redact_knowledge_text(raw_key), kind) if raw_key else generate_key(title, kind)

    source_ref = optional_str(payload.get("source_ref"))
    domain = optional_str(payload.get("domain"))

    return KnowledgeDraft(
        key=key,
        scope=scope,
        kind=kind,
        title=redact_knowledge_text(title),
        body=redact_knowledge_text(_checked_body(payload)),
        domain=redact_knowledge_text(domain) if domain else None,
        confidence=coerce_confidence(payload.get("confidence")),
        status=status,
        source=source,
        source_ref=redact_knowledge_text(source_ref) if source_ref else None,
    )
