"""facade/audit.py（呼び出しの主体の決定と、監査ログ）のテスト。"""

from __future__ import annotations

import hashlib
import hmac
import logging
import types
from collections.abc import Callable

import pytest

from ._support import _AUDIT_SECRET, _audit_log, _logged


def _never() -> str:
    """呼ばれたら落とす（引いてはいけない主体・OS の利用者名の代わり）。"""
    raise AssertionError("引いてはいけない主体を引きました")


class TestPrincipal:
    """呼び出しの主体の決定。"""

    @pytest.mark.parametrize(
        ("environ", "getuser", "expected"),
        [({"FACADE_PRINCIPAL": "alice"}, _never, "alice"), ({}, lambda: "bob", "bob"), ({"FACADE_PRINCIPAL": ""}, lambda: "bob", "bob")],
        ids=["configured-wins", "unset-uses-os-user", "empty-uses-os-user"],
    )
    def test_resolve_principal(self, facade: types.SimpleNamespace, environ: dict[str, str], getuser: Callable[[], str], expected: str) -> None:
        """FACADE_PRINCIPAL があれば OS の利用者名を引かずにその値を、無い・空なら OS の利用者名を返す。"""
        assert facade.audit.resolve_principal(environ, getuser=getuser) == expected

    @pytest.mark.parametrize("error", [OSError("no user"), KeyError("uid"), ImportError("pwd")])
    def test_resolve_principal_unknown_raises_facade_error(self, facade: types.SimpleNamespace, error: Exception) -> None:
        """OS の利用者名も引けなければ（3.12 の KeyError・ImportError を含む）、FACADE_PRINCIPAL の設定を促す FacadeError にする。"""

        def failing() -> str:
            """利用者名を引けない。"""
            raise error

        with pytest.raises(facade.errors.FacadeError, match="FACADE_PRINCIPAL") as unknown:
            facade.audit.resolve_principal({}, getuser=failing)
        assert unknown.value.__cause__ is error


class TestAuditLog:
    """監査ログ（主体の鍵付きの要約と、残す行）。"""

    @pytest.mark.parametrize("principal", ["alice", "user" + chr(0xDC80)], ids=["plain", "lone-surrogate"])
    def test_subject_is_domain_separated_hmac(self, facade: types.SimpleNamespace, caplog: pytest.LogCaptureFixture, principal: str) -> None:
        """主体の要約は、鍵の素材に用途名を前置した鍵の HMAC-SHA256 の先頭 32 桁（主体そのものはログに出ない）。対になっていないサロゲート（環境変数の不正なバイト）が混じっても、例外を出さずに要約する。"""
        audit = _audit_log(facade, caplog, "test-audit-subject")
        expected = hmac.new(b"audit-subject:" + _AUDIT_SECRET, principal.encode("utf-8", "surrogatepass"), hashlib.sha256)
        assert audit.subject(principal) == expected.hexdigest()[:32]

    def test_personal_returned_logs_one_info_line(self, facade: types.SimpleNamespace, caplog: pytest.LogCaptureFixture) -> None:
        """個人情報の項目を返したら、主体の要約・ツール・パス・件数を INFO で 1 行残す（値は残さない）。"""
        audit = _audit_log(facade, caplog, "test-audit-personal")
        audit.personal_returned("orders_get", ["a.b", "c"], 2, principal=lambda: "alice")
        assert _logged(caplog) == [
            (logging.INFO, f"personal fields returned: subject={audit.subject('alice')} tool=orders_get paths=a.b,c count=2")
        ]

    @pytest.mark.parametrize(("paths", "count"), [([], 3), (["a.b"], 0)])
    def test_personal_returned_nothing_returned_skips_principal(
        self, facade: types.SimpleNamespace, caplog: pytest.LogCaptureFixture, paths: list[str], count: int
    ) -> None:
        """個人情報の項目が無いか 0 件なら、記録せず、主体も引かない（主体を決められなくても失敗にしない）。"""
        audit = _audit_log(facade, caplog, "test-audit-skip")
        audit.personal_returned("orders_search", paths, count, principal=_never)
        assert _logged(caplog) == []

    def test_personal_returned_unknown_principal_fails_closed(self, facade: types.SimpleNamespace, caplog: pytest.LogCaptureFixture) -> None:
        """主体を決められなければ、記録できないまま個人情報を返さない（主体の失敗をそのまま投げる）。"""
        audit = _audit_log(facade, caplog, "test-audit-unknown")

        def unknown() -> str:
            """主体を決められない。"""
            raise facade.errors.FacadeError("主体を決められません")

        with pytest.raises(facade.errors.FacadeError, match="主体を決められません"):
            audit.personal_returned("orders_get", ["a.b"], 1, principal=unknown)

    def test_cursor_rejected_logs_one_warning(self, facade: types.SimpleNamespace, caplog: pytest.LogCaptureFixture) -> None:
        """cursor を拒否したら、主体の要約とツールを WARNING で 1 行残す。"""
        audit = _audit_log(facade, caplog, "test-audit-cursor")
        audit.cursor_rejected("orders_search", "alice")
        assert _logged(caplog) == [(logging.WARNING, f"cursor rejected: subject={audit.subject('alice')} tool=orders_search")]
