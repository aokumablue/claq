"""署名付きの自己完結カーソル（upstream-protection-overlay）。

既定の `cursor.PageCursor` に、主体への結び付け・有効期限・偽造の防止を足す。カーソルは
「上流での位置と有効期限」を載せた本体に、「用途（ツール名）・主体・検索条件の指紋」を合わせて
HMAC で署名した文字列で、サーバ側に状態を持たない。鍵を共有すれば、どのインスタンスでも続きを返せる。
共有のリモート（複数の主体）で、他の主体の cursor を開かせないために重ねる。

- 用途・主体・指紋はカーソルに載せず、MAC の入力でだけ結びつける（カーソルはモデルの
  文脈に載るため、主体はその要約も出さない）。別の用途・主体・検索条件で開くと署名が合わない。
- 改ざん・形式の不正・別の用途・別の主体・別の検索条件・版違い・本体の形の不正は、理由を
  区別しない同じ文面の `CursorError` で拒否する。期限切れは署名を確かめた後にだけ分かるので、
  別の文面で知らせる。
- 署名はするが暗号化はしない。位置（`position`）に機微な値を入れない。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from .audit_log import AuditLog
from .cursor import _decode, _encode, _is_int, accepts_position
from .errors import CursorError, FacadeError

_VERSION = 1
_MIN_SECRET_BYTES = 32
# MAC の入力の先頭に置く領域名（同じ鍵で作る、他の用途の MAC と取り違えないため）
_MAC_DOMAIN = "facade.cursor"
_EXPIRED = "この cursor は有効期限が切れています。cursor を外して、同じ条件で検索し直してください。"
_PAGE_TOKEN_TOO_LONG = (
    "上流のページトークンが長すぎるため、続きの cursor を発行できません。"
    "サーバの運用者に MAX_PAGE_TOKEN_CHARS と MAX_CURSOR_CHARS の見直しを依頼してください。"
)


def _read_payload(body: str) -> dict[str, Any] | None:
    """署名を確かめた本体を読み、形が正しければ返す。

    Args:
        body: 符号化済みの本体。

    Returns:
        本体（版・位置・期限）。読めない・版が違う・形が違うときは None。
    """
    try:
        payload = json.loads(_decode(body))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    version, position, expires = payload.get("v"), payload.get("pos"), payload.get("exp")
    if not (_is_int(version) and version == _VERSION and isinstance(position, dict) and _is_int(expires)):
        return None
    return payload


class CursorCodec:
    """カーソルの発行と検証。"""

    def __init__(self, secret: bytes, *, ttl_seconds: int = 900, clock: Callable[[], float] = time.time) -> None:
        """鍵と有効期間を設定する。

        Args:
            secret: 署名の鍵。32 バイト以上。複数インスタンスでは全てで同じ値にする。
            ttl_seconds: カーソルの有効期間（秒）。
            clock: 現在時刻（UNIX 秒）を返す関数。

        Raises:
            ValueError: 鍵が短いとき。
        """
        if len(secret) < _MIN_SECRET_BYTES:
            raise ValueError(f"cursor の鍵は {_MIN_SECRET_BYTES} バイト以上にする")
        self._secret = secret
        self._ttl_seconds = ttl_seconds
        self._clock = clock

    def _sign(self, body: str, *, scope: str, principal: str, fingerprint: str) -> str:
        """本体と、本体に載せない結びつけの値（用途・主体・指紋）をまとめて署名する。

        MAC の入力は JSON の配列にする。単純に連結すると要素の境目がずれた組
        （主体 `ab` と指紋 `c`、主体 `a` と指紋 `bc`）が同じ入力になる。
        ASCII だけで符号化するので、主体に対になっていないサロゲートが混じっても失敗しない。

        Args:
            body: 符号化済みの本体。
            scope: カーソルの用途（ページングするツールの名前）。
            principal: 呼び出しの主体。
            fingerprint: 検索条件の指紋。

        Returns:
            符号化済みの署名。
        """
        message = json.dumps([_MAC_DOMAIN, scope, principal, fingerprint, body], separators=(",", ":"))
        return _encode(hmac.new(self._secret, message.encode("ascii"), hashlib.sha256).digest())

    def issue(self, *, scope: str, principal: str, fingerprint: str, position: Mapping[str, Any]) -> str:
        """続きの位置を封じたカーソルを発行する。

        Args:
            scope: カーソルの用途（ページングするツールの名前）。別のツールでは開けない。
            principal: 呼び出しの主体（検証済みのトークンや起動時の設定から導いた値）。
            fingerprint: `query_fingerprint` で作った検索条件の指紋。
            position: 上流での続きの位置（ページトークン・読み飛ばす件数など）。

        Returns:
            カーソル文字列。
        """
        payload = {"v": _VERSION, "pos": dict(position), "exp": int(self._clock()) + self._ttl_seconds}
        body = _encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        return f"{body}.{self._sign(body, scope=scope, principal=principal, fingerprint=fingerprint)}"

    def open(self, token: str, *, scope: str, principal: str, fingerprint: str) -> dict[str, Any]:
        """カーソルを検証し、封じた位置を返す。

        確かめる順は、文字種 → 署名 → 本体の形 → 期限。署名を確かめる前に
        本体を読まず、理由ごとに文面を変えない（期限切れだけは署名が合った後に分かる）。

        Args:
            token: クライアントから受け取ったカーソル。
            scope: 今回の用途（ページングするツールの名前）。
            principal: 今回の呼び出しの主体。
            fingerprint: 今回の検索条件の指紋。

        Returns:
            発行時に封じた位置。

        Raises:
            CursorError: ASCII でない・形式が不正・署名が合わない（改ざん・別の用途・主体・
                検索条件）・本体を読めないか版や形が違う・期限切れのとき。
        """
        # compare_digest は ASCII でない str を TypeError で拒むので、先に同じ文面で拒否する
        if not token.isascii():
            raise CursorError()
        # 区切りの無い・空の部分は、署名が合わないので下の照合で拒否される
        body, _, signature = token.partition(".")
        expected = self._sign(body, scope=scope, principal=principal, fingerprint=fingerprint)
        if not hmac.compare_digest(signature, expected):
            raise CursorError()
        payload = _read_payload(body)
        if payload is None:
            raise CursorError()
        if payload["exp"] < self._clock():
            raise CursorError(_EXPIRED)
        return payload["pos"]


class SignedPageCursor:
    """署名付きの一覧の続きの cursor。位置の形と長さは既定の `cursor.accepts_position` で確かめる。"""

    def __init__(
        self,
        codec: CursorCodec,
        *,
        scope: str,
        audit: AuditLog,
        max_page_token_chars: int,
        max_cursor_chars: int,
    ) -> None:
        """封じ方と上限を設定する。

        Args:
            codec: 署名付きカーソルの発行と検証。
            scope: cursor の用途（ページングするツールの名前）。
            audit: 拒否を残す監査ログ。
            max_page_token_chars: 上流のページトークンの長さの上限。
            max_cursor_chars: cursor の長さの上限（cursor 引数の max_length と同じ値）。
        """
        self._codec = codec
        self._scope = scope
        self._audit = audit
        self._max_page_token_chars = max_page_token_chars
        self._max_cursor_chars = max_cursor_chars

    def _accepts(self, token: str, position: Mapping[str, Any]) -> bool:
        """cursor の長さと位置の形を、このページングの上限で確かめる。

        Args:
            token: cursor 文字列。
            position: cursor に封じた位置。

        Returns:
            `accepts_position` の結果。
        """
        return accepts_position(
            token,
            position,
            max_page_token_chars=self._max_page_token_chars,
            max_cursor_chars=self._max_cursor_chars,
        )

    def issue(self, position: Mapping[str, Any], *, principal: str, fingerprint: str) -> str:
        """続きの位置を封じた cursor を発行する。

        Args:
            position: 続きの位置（page: 上流のページトークンか None、skip: そのページで読み飛ばす件数）。
            principal: 呼び出しの主体。
            fingerprint: 検索条件の指紋。

        Returns:
            cursor 文字列。

        Raises:
            FacadeError: 位置が形を満たさないか、cursor が上限を超えるとき（上流のページトークンが長すぎる）。
        """
        token = self._codec.issue(scope=self._scope, principal=principal, fingerprint=fingerprint, position=position)
        if not self._accepts(token, position):
            raise FacadeError(_PAGE_TOKEN_TOO_LONG)
        return token

    def open(self, token: str, *, principal: str, fingerprint: str) -> dict[str, Any]:
        """cursor を開き、発行と同じ述語で確かめる。拒否したら監査ログに残す。

        Args:
            token: クライアントから受け取った cursor。
            principal: 呼び出しの主体。
            fingerprint: 今回の検索条件の指紋。

        Returns:
            位置（page: 上流のページトークンか None、skip: そのページで読み飛ばす件数）。

        Raises:
            CursorError: cursor が使えない、または長さか位置の形がこのページングの発行するものと違うとき。
        """
        try:
            position = self._codec.open(token, scope=self._scope, principal=principal, fingerprint=fingerprint)
            if not self._accepts(token, position):
                raise CursorError()
        except CursorError:
            self._audit.cursor_rejected(self._scope, principal)
            raise
        return position
