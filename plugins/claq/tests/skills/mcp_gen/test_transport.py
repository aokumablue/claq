"""transport.py（起動のトランスポートと、HTTP の公開範囲）のテスト。"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from ._support import _TEMPLATE, _load


class _RecordingServer:
    """transport.run が起動するサーバの代わり（run の引数を記録する）。"""

    name = "fake"

    def __init__(self) -> None:
        """記録を空にする。"""
        self.runs: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> None:
        """起動の代わりに引数を記録する。"""
        self.runs.append(kwargs)


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """transport.py を、mcp の代わりのスタブで読み込む（スタブはテストの後で外す）。

    TransportSecuritySettings は受け取った引数を属性に持つだけの SimpleNamespace にする。
    """
    server_module = types.ModuleType("mcp.server")
    server_module.MCPServer = object
    security_module = types.ModuleType("mcp.server.transport_security")
    security_module.TransportSecuritySettings = types.SimpleNamespace
    monkeypatch.setitem(sys.modules, "mcp", types.ModuleType("mcp"))
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.server.transport_security", security_module)
    return _load("mcp_gen_transport", _TEMPLATE / "transport.py")


class TestTransport:
    """起動のトランスポートと、HTTP の公開範囲。"""

    @staticmethod
    def _run(transport: types.ModuleType, monkeypatch: pytest.MonkeyPatch, *argv: str) -> _RecordingServer:
        """コマンドライン引数を与えて run を呼び、記録したサーバを返す。"""
        monkeypatch.setattr(sys, "argv", ["server.py", *argv])
        server = _RecordingServer()
        transport.run(server)
        return server

    def test_stdio_is_default(self, transport: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        """引数なしなら stdio で起動する。"""
        assert self._run(transport, monkeypatch).runs == [{"transport": "stdio"}]

    @pytest.mark.parametrize(
        ("host", "hosts", "origins"),
        [
            ("127.0.0.1", ["127.0.0.1", "127.0.0.1:8000"], ["http://127.0.0.1:8000"]),
            ("localhost", ["localhost", "localhost:8000"], ["http://localhost:8000"]),
            ("::1", ["[::1]", "[::1]:8000"], ["http://[::1]:8000"]),
        ],
    )
    def test_http_on_loopback(
        self, transport: types.ModuleType, monkeypatch: pytest.MonkeyPatch, host: str, hosts: list[str], origins: list[str]
    ) -> None:
        """ループバックでは、Host ヘッダと Origin に現れる形（IPv6 はブラケット付き）で許可リストを作る。"""
        (run,) = self._run(transport, monkeypatch, "--http", "--host", host).runs
        assert (run["transport"], run["host"], run["port"], run["streamable_http_path"]) == ("streamable-http", host, 8000, "/mcp")
        security = run["transport_security"]
        assert security.enable_dns_rebinding_protection is True
        assert (security.allowed_hosts, security.allowed_origins) == (hosts, origins)

    @pytest.mark.parametrize(
        ("argv", "message"),
        [
            (("--http", "--host", "0.0.0.0"), "認証を実装していないので、ループバック以外には公開しない"),
            (("--http", "--host", "mcp.example.com"), "認証を実装していないので、ループバック以外には公開しない"),
            (("--http", "--host", "0.0.0.0", "--allow-remote"), "--allow-remote"),
        ],
        ids=["any-address", "public-name", "allow-remote-flag"],
    )
    def test_rejects_non_loopback_host(
        self,
        transport: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        argv: tuple[str, ...],
        message: str,
    ) -> None:
        """ループバック以外のホストは、認証を実装していないので起動せずに拒否し、ループバック以外へ広げる抜け道（--allow-remote）も持たない。"""
        with pytest.raises(SystemExit) as exited:
            self._run(transport, monkeypatch, *argv)
        assert exited.value.code == 2
        assert message in capsys.readouterr().err
