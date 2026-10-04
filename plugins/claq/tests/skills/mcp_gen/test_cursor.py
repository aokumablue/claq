"""facade/cursor.py（署名付きの自己完結カーソルと、一覧の続きの cursor）のテスト。"""

from __future__ import annotations

import json
import logging
import types
from typing import Any

import pytest

from ._support import _audit_log, _Clock, _logged

_SECRET = b"s" * 32


_CURSOR_REJECTED = "この cursor は使えません。cursor を外して、同じ条件で検索し直してください。"
_BINDING = {"scope": "orders_search", "principal": "alice", "fingerprint": "q1"}


class TestCursor:
    """署名付きの自己完結カーソル。"""

    def test_round_trip(self, facade: types.SimpleNamespace) -> None:
        """発行したカーソルから、封じた位置を取り出せる。"""
        codec = facade.cursor.CursorCodec(_SECRET)
        token = codec.issue(**_BINDING, position={"page": "o:3", "skip": 1})
        assert codec.open(token, **_BINDING) == {"page": "o:3", "skip": 1}

    def test_payload_holds_only_position_and_expiry(self, facade: types.SimpleNamespace) -> None:
        """payload は版・位置・期限だけで、用途・主体・指紋は載せない（MAC の入力でだけ結びつける）。"""
        codec = facade.cursor.CursorCodec(_SECRET)
        binding = {"scope": "orders_search", "principal": "alice@example.com", "fingerprint": "f0" * 16}
        token = codec.issue(**binding, position={"page": "o:3"})
        payload = json.loads(facade.cursor._decode(token.partition(".")[0]))
        assert set(payload) == {"v", "pos", "exp"}
        assert payload["pos"] == {"page": "o:3"}
        for value in binding.values():
            assert value not in json.dumps(payload)

    def test_works_across_codecs_with_same_secret(self, facade: types.SimpleNamespace) -> None:
        """鍵が同じなら、別のインスタンスでも検証できる（状態を持たない）。"""
        token = facade.cursor.CursorCodec(_SECRET).issue(**_BINDING, position={"p": 1})
        assert facade.cursor.CursorCodec(_SECRET).open(token, **_BINDING) == {"p": 1}

    @pytest.mark.parametrize(
        "binding",
        [
            {**_BINDING, "principal": "mallory"},
            {**_BINDING, "fingerprint": "q2"},
            {**_BINDING, "scope": "invoices_search"},
            # 境目をずらしても同じ MAC にならない（単純な連結なら "aliceq1" で一致してしまう）
            {**_BINDING, "principal": "aliceq", "fingerprint": "1"},
        ],
    )
    def test_rejects_other_binding(self, facade: types.SimpleNamespace, binding: dict[str, str]) -> None:
        """別の主体・別の検索条件・別の用途のカーソルは、理由を区別しない同じ文面で拒否する。"""
        codec = facade.cursor.CursorCodec(_SECRET)
        token = codec.issue(**_BINDING, position={})
        with pytest.raises(facade.errors.CursorError) as rejected:
            codec.open(token, **binding)
        assert str(rejected.value) == _CURSOR_REJECTED

    def test_rejects_tampering_and_malformed(self, facade: types.SimpleNamespace) -> None:
        """別の鍵の署名・書き換え・形式の不正・ASCII でない文字を、例外を漏らさず同じ文面で拒否する。"""
        codec = facade.cursor.CursorCodec(_SECRET)
        token = codec.issue(**_BINDING, position={})
        other = facade.cursor.CursorCodec(b"t" * 32).issue(**_BINDING, position={})
        flipped = token[:-1] + ("A" if token[-1] != "A" else "B")
        for bad in (other, flipped, "no-dot", ".sig", "body.", token + "あ", "あ" + token, token.replace(".", "。")):
            with pytest.raises(facade.errors.CursorError) as rejected:
                codec.open(bad, **_BINDING)
            assert str(rejected.value) == _CURSOR_REJECTED, bad
        assert token.count(".") == 1

    @pytest.mark.parametrize(
        "payload",
        [
            b"",
            b"\xff\xfe\xfd",
            b"[1, 2]",
            b'{"v": 0, "pos": {}, "exp": 9999999999}',
            b'{"v": true, "pos": {}, "exp": 9999999999}',
            b'{"v": 1, "pos": [], "exp": 9999999999}',
            b'{"v": 1, "pos": {}, "exp": "9999999999"}',
            b'{"v": 1, "pos": {}, "exp": true}',
            b'{"v": 1, "pos": {}}',
        ],
    )
    def test_rejects_signed_but_malformed_payload(self, facade: types.SimpleNamespace, payload: bytes) -> None:
        """署名は合うが読めない・版が違う・形が違う payload も、例外を漏らさず同じ文面で拒否する。"""
        codec = facade.cursor.CursorCodec(_SECRET)
        body = facade.cursor._encode(payload)
        token = f"{body}.{codec._sign(body, **_BINDING)}"
        with pytest.raises(facade.errors.CursorError) as rejected:
            codec.open(token, **_BINDING)
        assert str(rejected.value) == _CURSOR_REJECTED

    def test_expires(self, facade: types.SimpleNamespace) -> None:
        """有効期間を過ぎたカーソルは、署名を確かめた後でだけ、期限切れと分かる文面で拒否する。"""
        clock = _Clock()
        codec = facade.cursor.CursorCodec(_SECRET, ttl_seconds=10, clock=clock)
        token = codec.issue(**_BINDING, position={})
        clock.now += 10
        assert codec.open(token, **_BINDING) == {}
        clock.now += 1
        with pytest.raises(facade.errors.CursorError, match="有効期限") as expired:
            codec.open(token, **_BINDING)
        assert "cursor" in str(expired.value)
        with pytest.raises(facade.errors.CursorError) as other:
            codec.open(token, **{**_BINDING, "principal": "mallory"})
        assert str(other.value) == _CURSOR_REJECTED

    def test_principal_with_lone_surrogate(self, facade: types.SimpleNamespace) -> None:
        """主体に対になっていないサロゲート（環境変数の不正なバイト）が混じっても、例外を出さずに結びつける。"""
        codec = facade.cursor.CursorCodec(_SECRET)
        binding = {**_BINDING, "principal": "user\udc80"}
        token = codec.issue(**binding, position={})
        assert codec.open(token, **binding) == {}

    def test_cursor_error_is_facade_error(self, facade: types.SimpleNamespace) -> None:
        """CursorError は FacadeError として model_errors() で ToolError に変換され、既定は拒否の文面。"""
        assert issubclass(facade.errors.CursorError, facade.errors.FacadeError)
        assert str(facade.errors.CursorError()) == _CURSOR_REJECTED

    def test_requires_long_secret(self, facade: types.SimpleNamespace) -> None:
        """32 バイト未満の鍵は受け付けない。"""
        with pytest.raises(ValueError, match="32"):
            facade.cursor.CursorCodec(b"short")

    def test_fingerprint(self, facade: types.SimpleNamespace) -> None:
        """指紋は cursor とキーの順序に依存せず、値が変われば変わる。"""
        fingerprint = facade.cursor.query_fingerprint
        assert fingerprint({"a": 1, "b": [1, 2], "cursor": "x"}) == fingerprint({"b": [1, 2], "a": 1})
        assert fingerprint({"a": 1}) != fingerprint({"a": 2})
        assert len(fingerprint({})) == 32


_PAGE_BINDING = {"principal": "alice", "fingerprint": "q1"}


def _page_cursor(facade: types.SimpleNamespace, caplog: pytest.LogCaptureFixture, *, scope: str = "orders_search", max_cursor_chars: int = 2000) -> tuple[Any, Any]:
    """見本と同じ上限（ページトークン 512 文字、cursor 2000 文字）の PageCursor と、その監査ログを作る。

    Args:
        facade: 読み込んだ facade パッケージ。
        caplog: ログの捕捉。
        scope: cursor の用途。
        max_cursor_chars: cursor の長さの上限。

    Returns:
        （PageCursor、AuditLog）。
    """
    audit = _audit_log(facade, caplog, "test-page-cursor")
    codec = facade.cursor.CursorCodec(_SECRET)
    cursors = facade.cursor.PageCursor(codec, scope=scope, audit=audit, max_page_token_chars=512, max_cursor_chars=max_cursor_chars)
    return cursors, audit


class TestPageCursor:
    """一覧の続きの cursor（発行と開封で同じ述語と上限を確かめる）。"""

    def test_round_trip_at_page_token_limit(self, facade: types.SimpleNamespace, caplog: pytest.LogCaptureFixture) -> None:
        """上限ちょうど（ASCII 512 文字）のページトークンを封じた cursor は、発行でき、開けて、監査ログに何も残さない。"""
        cursors, _ = _page_cursor(facade, caplog)
        position = {"page": "o" * 512, "skip": 3}
        token = cursors.issue(position, **_PAGE_BINDING)
        assert (cursors.open(token, **_PAGE_BINDING), _logged(caplog)) == (position, [])

    @pytest.mark.parametrize("page", ["o" * 513, "あ" * 512], ids=["over-512-chars", "cursor-over-2000-chars"])
    def test_issue_unopenable_raises_facade_error(self, facade: types.SimpleNamespace, caplog: pytest.LogCaptureFixture, page: str) -> None:
        """開けない cursor は発行せず、MAX_PAGE_TOKEN_CHARS を名指しした FacadeError にする（cursor の拒否としては残さない）。

        "あ" は 512 文字に収まっても、本体の JSON で \\uXXXX の 6 文字に広がり、cursor が 2000 文字を超える。
        """
        cursors, _ = _page_cursor(facade, caplog)
        with pytest.raises(facade.errors.FacadeError, match="MAX_PAGE_TOKEN_CHARS") as refused:
            cursors.issue({"page": page, "skip": 0}, **_PAGE_BINDING)
        assert (type(refused.value), _logged(caplog)) == (facade.errors.FacadeError, [])

    @pytest.mark.parametrize(
        "position",
        [
            {"page": None},
            {"skip": 0},
            {"page": None, "skip": 0, "extra": 1},
            {"page": None, "skip": -1},
            {"page": None, "skip": True},
            {"page": None, "skip": 1.0},
            {"page": None, "skip": "1"},
            {"page": 5, "skip": 0},
            {"page": "o" * 513, "skip": 0},
        ],
    )
    def test_open_bad_position_rejected_and_audited(
        self, facade: types.SimpleNamespace, caplog: pytest.LogCaptureFixture, position: dict[str, Any]
    ) -> None:
        """署名は合うが位置の形が違う cursor は、理由を区別しない文面で拒否し、拒否を監査ログに残す。"""
        cursors, audit = _page_cursor(facade, caplog)
        token = facade.cursor.CursorCodec(_SECRET).issue(scope="orders_search", **_PAGE_BINDING, position=position)
        with pytest.raises(facade.errors.CursorError, match="使えません"):
            cursors.open(token, **_PAGE_BINDING)
        assert _logged(caplog) == [(logging.WARNING, f"cursor rejected: subject={audit.subject('alice')} tool=orders_search")]

    def test_open_tampered_rejected_and_audited(self, facade: types.SimpleNamespace, caplog: pytest.LogCaptureFixture) -> None:
        """改ざんした cursor は拒否し、拒否を監査ログに残す。"""
        cursors, audit = _page_cursor(facade, caplog)
        token = cursors.issue({"page": "o:3", "skip": 0}, **_PAGE_BINDING)
        with pytest.raises(facade.errors.CursorError):
            cursors.open(token[:-1] + ("A" if token[-1] != "A" else "B"), **_PAGE_BINDING)
        assert _logged(caplog) == [(logging.WARNING, f"cursor rejected: subject={audit.subject('alice')} tool=orders_search")]

    def test_open_over_cursor_limit_rejected(self, facade: types.SimpleNamespace, caplog: pytest.LogCaptureFixture) -> None:
        """cursor の長さの上限を超えるものは、署名が合っても拒否する（発行側と同じ上限）。"""
        cursors, _ = _page_cursor(facade, caplog, max_cursor_chars=60)
        token = facade.cursor.CursorCodec(_SECRET).issue(scope="orders_search", **_PAGE_BINDING, position={"page": None, "skip": 0})
        with pytest.raises(facade.errors.CursorError):
            cursors.open(token, **_PAGE_BINDING)
        assert len(caplog.records) == 1

    def test_open_other_scope_rejected(self, facade: types.SimpleNamespace, caplog: pytest.LogCaptureFixture) -> None:
        """別の用途（ツール）の PageCursor が発行した cursor は開けない。"""
        invoices, _ = _page_cursor(facade, caplog, scope="invoices_search")
        orders, _ = _page_cursor(facade, caplog)
        token = invoices.issue({"page": None, "skip": 0}, **_PAGE_BINDING)
        with pytest.raises(facade.errors.CursorError):
            orders.open(token, **_PAGE_BINDING)
