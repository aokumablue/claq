"""署名付きの自己完結カーソル。

カーソルは「上流での位置・検索条件の指紋・主体・有効期限」を HMAC で署名した文字列で、
サーバ側に状態を持たない。鍵を共有すれば、どのインスタンスでも続きを返せる。

- 改ざん・期限切れ・別の主体・別の検索条件のカーソルは `FacadeError` で拒否する
  （ハンドルを持っていることを認証とみなさない）。
- 署名はするが暗号化はしない。主体はそのまま入れず、鍵付きの要約にして封じる
  （カーソルはモデルの文脈に載るため、利用者 ID やメールアドレスを出さない）。
  位置（`position`）にも機微な値を入れない。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from .errors import FacadeError

_VERSION = 1
_MIN_SECRET_BYTES = 32
_RESTART = "cursor を外して、同じ条件で検索し直してください。"


def query_fingerprint(arguments: Mapping[str, Any]) -> str:
    """ページをまたいで変わってはいけない検索条件の指紋を作る。

    `cursor` 以外の全ての引数（既定値を補った実効値）から作る。キーの順序には依存しない。

    Args:
        arguments: ツールの引数（実効値）。

    Returns:
        16 進数 32 文字の指紋。
    """
    canonical = json.dumps(
        {key: value for key, value in arguments.items() if key != "cursor"},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _encode(raw: bytes) -> str:
    """URL に安全な base64（パディングなし）にする。

    Args:
        raw: 符号化するバイト列。

    Returns:
        符号化した文字列。
    """
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(text: str) -> bytes:
    """`_encode` の逆変換。

    Args:
        text: 符号化された文字列。

    Returns:
        元のバイト列。

    Raises:
        ValueError: base64 として解釈できないとき。
    """
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


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

    def _sign(self, body: str) -> str:
        """本体の署名を作る。

        Args:
            body: 符号化済みの本体。

        Returns:
            符号化済みの署名。
        """
        return _encode(hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest())

    def _principal_digest(self, principal: str) -> str:
        """主体を、鍵付きの要約にする（カーソルから主体を読み取らせない）。

        Args:
            principal: 呼び出しの主体。

        Returns:
            符号化済みの要約。
        """
        return _encode(hmac.new(self._secret, b"sub:" + principal.encode("utf-8"), hashlib.sha256).digest()[:16])

    def issue(self, *, principal: str, fingerprint: str, position: Mapping[str, Any]) -> str:
        """続きの位置を封じたカーソルを発行する。

        Args:
            principal: 呼び出しの主体（検証済みのトークンや起動時の設定から導いた値）。
            fingerprint: `query_fingerprint` で作った検索条件の指紋。
            position: 上流での続きの位置（ページトークン・読み飛ばす件数など）。

        Returns:
            カーソル文字列。
        """
        payload = {
            "v": _VERSION,
            "sub": self._principal_digest(principal),
            "q": fingerprint,
            "pos": dict(position),
            "exp": int(self._clock()) + self._ttl_seconds,
        }
        body = _encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        return f"{body}.{self._sign(body)}"

    def open(self, token: str, *, principal: str, fingerprint: str) -> dict[str, Any]:
        """カーソルを検証し、封じた位置を返す。

        Args:
            token: クライアントから受け取ったカーソル。
            principal: 今回の呼び出しの主体。
            fingerprint: 今回の検索条件の指紋。

        Returns:
            発行時に封じた位置。

        Raises:
            FacadeError: 形式が不正・署名が合わない・期限切れ・主体か検索条件が違うとき。
        """
        body, _, signature = token.partition(".")
        if not body or not signature:
            raise FacadeError(f"cursor の形式が不正です。{_RESTART}")
        if not hmac.compare_digest(signature, self._sign(body)):
            raise FacadeError(f"cursor を検証できません。{_RESTART}")
        try:
            payload = json.loads(_decode(body))
        except ValueError as error:
            raise FacadeError(f"cursor を読み取れません。{_RESTART}") from error
        if payload.get("v") != _VERSION or not isinstance(payload.get("pos"), dict):
            raise FacadeError(f"cursor の版が古いか壊れています。{_RESTART}")
        if payload.get("exp", 0) < self._clock():
            raise FacadeError(f"cursor の有効期限が切れています。{_RESTART}")
        # 主体の違いと条件の違いは区別しない（他の主体のカーソルかどうかを漏らさない）
        if payload.get("sub") != self._principal_digest(principal) or payload.get("q") != fingerprint:
            raise FacadeError(f"この cursor は今回の検索条件には使えません。{_RESTART}")
        return payload["pos"]
