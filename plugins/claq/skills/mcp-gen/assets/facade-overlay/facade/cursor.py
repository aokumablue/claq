"""一覧の続きの自己完結カーソル（署名なし）。

カーソルは「上流での位置」を載せた本体に、「用途（ツール名）・検索条件の指紋・本体」から作った
SHA-256 の照合値を付けた文字列で、サーバ側に状態を持たない。

- 別の用途・別の検索条件で開いたとき、本体を書き換えたときは照合値が合わず、理由を区別しない
  同じ文面の `CursorError` で拒否する。
- 鍵を使わないので、照合値は取り違えと書き換えを見つけるためのもので、偽造は止めない。主体への
  結び付け・有効期限・偽造の防止が要るとき（共有のリモート）は、upstream-protection-overlay の
  `signed_cursor` を重ねる。
- 暗号化はしない。位置（`position`）に機微な値を入れない。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

from .errors import CursorError, FacadeError

_VERSION = 1
# 照合値の入力の先頭に置く領域名（他の用途のハッシュと取り違えないため）
_CHECK_DOMAIN = "facade.cursor"
_PAGE_TOKEN_TOO_LONG = (
    "上流のページトークンが長すぎるため、続きの cursor を発行できません。"
    "サーバの運用者に MAX_PAGE_TOKEN_CHARS と MAX_CURSOR_CHARS の見直しを依頼してください。"
)


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


def _is_int(value: Any) -> bool:
    """bool を除く int か（JSON の true を 1 として通さない）。

    Args:
        value: 調べる値。

    Returns:
        int なら True。
    """
    return isinstance(value, int) and not isinstance(value, bool)


def accepts_position(token: str, position: Mapping[str, Any], *, max_page_token_chars: int, max_cursor_chars: int) -> bool:
    """発行と開封で共通の述語: cursor が上限の長さに収まり、位置がこのページングの形か。

    発行と開封で同じ述語を使う。開封側にだけ上限を置くと、上流のトークンが長いときに開けない
    cursor を発行し、モデルが 1 ページ目からやり直し続ける。

    Args:
        token: cursor 文字列。
        position: cursor に封じた位置。
        max_page_token_chars: 上流のページトークンの長さの上限。
        max_cursor_chars: cursor の長さの上限（cursor 引数の max_length と同じ値）。

    Returns:
        cursor が上限以下で、位置が page（None か上限以下の文字列）と skip（0 以上の int）だけを
        持てば True。
    """
    page, skip = position.get("page"), position.get("skip")
    return (
        len(token) <= max_cursor_chars
        and set(position) == {"page", "skip"}
        and _is_int(skip)
        and skip >= 0
        and (page is None or (isinstance(page, str) and len(page) <= max_page_token_chars))
    )


def _read_position(body: str) -> dict[str, Any] | None:
    """照合値を確かめた本体を読み、版と位置の形が正しければ位置を返す。

    Args:
        body: 符号化済みの本体。

    Returns:
        位置。読めない・版が違う・位置が dict でないときは None。
    """
    try:
        payload = json.loads(_decode(body))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    version, position = payload.get("v"), payload.get("pos")
    if not (_is_int(version) and version == _VERSION and isinstance(position, dict)):
        return None
    return position


class PageCursor:
    """一覧の続きの cursor。上流のページトークン（`page`）と、そのページで読み飛ばす件数（`skip`）を封じる。

    上限は server.py の `MAX_PAGE_TOKEN_CHARS` と `MAX_CURSOR_CHARS` から渡す（cursor 引数の
    max_length も `MAX_CURSOR_CHARS` にする）。
    """

    def __init__(self, *, scope: str, max_page_token_chars: int, max_cursor_chars: int) -> None:
        """用途と上限を設定する。

        Args:
            scope: cursor の用途（ページングするツールの名前）。別のツールでは開けない。
            max_page_token_chars: 上流のページトークンの長さの上限。
            max_cursor_chars: cursor の長さの上限（cursor 引数の max_length と同じ値）。
        """
        self._scope = scope
        self._max_page_token_chars = max_page_token_chars
        self._max_cursor_chars = max_cursor_chars

    def _check(self, body: str, fingerprint: str) -> str:
        """用途・検索条件の指紋・本体から照合値を作る。

        入力は JSON の配列にする（単純に連結すると要素の境目がずれた組が同じ入力になる）。

        Args:
            body: 符号化済みの本体。
            fingerprint: 検索条件の指紋。

        Returns:
            符号化済みの照合値。
        """
        message = json.dumps([_CHECK_DOMAIN, self._scope, fingerprint, body], separators=(",", ":"))
        return _encode(hashlib.sha256(message.encode("ascii")).digest())

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

    def issue(self, position: Mapping[str, Any], *, fingerprint: str) -> str:
        """続きの位置を封じた cursor を発行する。

        Args:
            position: 続きの位置（page: 上流のページトークンか None、skip: そのページで読み飛ばす件数）。
            fingerprint: 検索条件の指紋。

        Returns:
            cursor 文字列。

        Raises:
            FacadeError: 位置が形を満たさないか、cursor が上限を超えるとき（上流のページトークンが長すぎる）。
                開けない cursor は返さない。
        """
        payload = {"v": _VERSION, "pos": dict(position)}
        body = _encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        token = f"{body}.{self._check(body, fingerprint)}"
        if not self._accepts(token, position):
            raise FacadeError(_PAGE_TOKEN_TOO_LONG)
        return token

    def open(self, token: str, *, fingerprint: str) -> dict[str, Any]:
        """cursor を開き、照合値と、発行と同じ述語で確かめる。

        Args:
            token: クライアントから受け取った cursor。
            fingerprint: 今回の検索条件の指紋。

        Returns:
            位置（page: 上流のページトークンか None、skip: そのページで読み飛ばす件数）。

        Raises:
            CursorError: ASCII でない・照合値が合わない（書き換え・別の用途・別の検索条件）・本体を読めないか
                版や形が違う・長さか位置の形がこのページングの発行するものと違うとき。
        """
        # compare_digest は ASCII でない str を TypeError で拒むので、先に同じ文面で拒否する
        if not token.isascii():
            raise CursorError()
        body, _, check = token.partition(".")
        if not hmac.compare_digest(check, self._check(body, fingerprint)):
            raise CursorError()
        position = _read_position(body)
        if position is None or not self._accepts(token, position):
            raise CursorError()
        return position
