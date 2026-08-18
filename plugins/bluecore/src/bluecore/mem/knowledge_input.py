"""知識カード入力の検証 — ``mem learn`` が使う唯一の入口。

``knowledge`` テーブルの CHECK 制約（``kind`` 6 値 / ``scope`` 2 値 /
``status`` 3 値 / ``source`` 3 値 / ``confidence`` 0.0〜1.0）を、DB へ到達する
前に Python 側で強制する。現在の入力経路は ``mem learn``（stdin の JSON。
人間・エージェントが明示的に書く）の1つのみ。

``source`` の許容値 ``observer`` は、観測ログから自動抽出して投入していた
session-observer エージェント（206585c で削除済み）の名残。既存 DB の
CHECK 制約との互換のため ``SOURCES``／schema からは削除していないが、
これを自動で書き込む経路は現在存在しない。

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

from bluecore.mem.models import Knowledge, utc_now_iso
from bluecore.mem.redaction import redact_knowledge_text
from bluecore.mem.repo_identity import slugify

KINDS: frozenset[str] = frozenset({"convention", "decision", "pitfall", "howto", "fact", "preference"})
"""``knowledge.kind`` の許容値。スキーマの CHECK 制約と 1 対 1 で対応する。"""

SCOPES: frozenset[str] = frozenset({"global", "repo"})
"""``knowledge.scope`` の許容値。"""

STATUSES: frozenset[str] = frozenset({"active", "pending", "archived"})
"""``knowledge.status`` の許容値。注入されるのは ``active`` のみ。"""

SOURCES: frozenset[str] = frozenset({"agent", "observer", "human"})
"""``knowledge.source`` の許容値。"""

DEFAULT_CONFIDENCE = 0.5
"""``confidence`` 未指定時の確信度。"""

_ASCII_ALNUM_RE = re.compile(r"[A-Za-z0-9]")
_KEY_HASH_LENGTH = 8


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
    override: str | None = None,
) -> str:
    """ペイロード（または上書き値）から列挙値を取り出して検証する。

    Args:
        payload: 知識カードを表す dict。
        name: 項目名。ペイロードのキーとエラーメッセージに使う。
        allowed: 許可される値の集合。
        default: ペイロードに値が無いときの既定値。
        override: ペイロードより優先する値。CLI フラグ（例: ``--status``）の固定値に使う。

    Returns:
        検証を通った列挙値。

    Raises:
        KnowledgeInputError: 値が *allowed* に含まれない場合。
    """
    return validate_choice(name, override or str(payload.get(name) or default), allowed)


def generate_key(raw_title: str, kind: str) -> str:
    """title から知識カードの key スラッグを生成する。

    key は **redact 後** の title から生成します（A-03 対応）。redact 前の
    生 title から生成すると、シークレットを含む title の断片が
    ``learned:`` 標準出力・``show <key>`` の入力履歴・ログへそのまま残る
    ためです（title/body 自体は redact 済みでも key 経由で漏れる）。

    ASCII 英数字を 1 文字でも含む redact 後 title は ``slugify`` で
    kebab-case 化します。``Keep [REDACTED] out of titles`` のような角括弧
    混じりの文字列も、``slugify`` の非英数字置換で安定したスラッグ
    （``keep-redacted-out-of-titles``）に落ちます。ASCII 英数字を全く
    含まない redact 後 title はスラッグ化すると空になるため、``kind`` と
    redact 後 title の SHA-1 先頭 8 桁で決定的な key を作ります。

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
    if _ASCII_ALNUM_RE.search(redacted_title):
        base = slugify(redacted_title)
    else:
        digest = hashlib.sha1(redacted_title.encode("utf-8")).hexdigest()[:_KEY_HASH_LENGTH]
        base = f"{kind}-{digest}"
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


def parse_knowledge_payload(
    payload: dict[str, Any],
    *,
    status_override: str | None = None,
) -> KnowledgeDraft:
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

    Args:
        payload: 知識カードを表す dict。
        status_override: ペイロードの ``status`` より優先する値。
            CLI の ``--status`` フラグに使う。

    Returns:
        全項目が CHECK 制約を満たす KnowledgeDraft。

    Raises:
        KnowledgeInputError: title 欠落や列挙値・数値の不正がある場合。
    """
    title = str(payload.get("title") or "").strip()
    if not title:
        raise KnowledgeInputError("learn: title は必須です")

    kind = _payload_choice(payload, "kind", KINDS)
    scope = _payload_choice(payload, "scope", SCOPES, default="repo")
    source = _payload_choice(payload, "source", SOURCES, default="agent")
    status = _payload_choice(payload, "status", STATUSES, default="active", override=status_override)

    raw_key = str(payload.get("key") or "").strip()
    # 明示 key もシークレット断片を持ち込みうるため、生成 key と同じく
    # redact してから slugify を通す（A-03 対応。ここが素通りする穴だった）。
    key = slugify(redact_knowledge_text(raw_key)) if raw_key else generate_key(title, kind)

    source_ref = optional_str(payload.get("source_ref"))
    domain = optional_str(payload.get("domain"))

    return KnowledgeDraft(
        key=key,
        scope=scope,
        kind=kind,
        title=redact_knowledge_text(title),
        body=redact_knowledge_text(str(payload.get("body") or "")),
        domain=redact_knowledge_text(domain) if domain else None,
        confidence=coerce_confidence(payload.get("confidence")),
        status=status,
        source=source,
        source_ref=redact_knowledge_text(source_ref) if source_ref else None,
    )
