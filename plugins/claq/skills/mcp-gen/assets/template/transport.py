"""トランスポートを選んでサーバを起動する（全サーバ共通。公開するホスト名を足すとき以外は編集しない）。

stdio が既定で、`--http` で Streamable HTTP（既定は 127.0.0.1:8000/mcp）を使う。
どの server.py もここを通して起動し、HTTP の安全設定を 1 か所に保つ。
"""

from __future__ import annotations

import argparse
import logging

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

logger = logging.getLogger(__name__)

# HTTP でバインドしてよいホスト。認証を実装していないので、ループバックだけにする。
_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def run(server: MCPServer) -> None:
    """コマンドライン引数でトランスポートを選び、サーバを起動する。

    Args:
        server: 起動するサーバ。
    """
    parser = argparse.ArgumentParser(description=f"{server.name} MCP server")
    parser.add_argument("--http", action="store_true", help="Streamable HTTP で起動する（既定は stdio）")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=f"HTTP のバインド先。ループバック（{' / '.join(_LOOPBACK_HOSTS)}）だけを受け付ける（既定は 127.0.0.1）",
    )
    parser.add_argument("--port", type=int, default=8000, help="HTTP ポート")
    args = parser.parse_args()

    if not args.http:
        # トランスポート引数は run() に渡す。MCPServer() のコンストラクタには渡さない。
        server.run(transport="stdio")
        return

    # 仕様上、ローカル実行の HTTP サーバはループバックにだけバインドし、Origin を検証する
    # （DNS リバインディング対策）。認証の無いサーバをループバックの外へ公開する手段は置かない。
    if args.host not in _LOOPBACK_HOSTS:
        parser.error(f"--host {args.host} は使えません。認証を実装していないので、ループバック以外には公開しない")
    # SDK は Host ヘッダと Origin を許可リストとの完全一致で照合する。IPv6 の Host ヘッダは
    # ブラケット付き（[::1]:8000）で届くので、裸のホストとポート付きの両方をその形で挙げる
    # （挙げ忘れると全リクエストが 421 になる）。
    authority = f"[{args.host}]" if ":" in args.host else args.host
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[authority, f"{authority}:{args.port}"],
        allowed_origins=[f"http://{authority}:{args.port}"],
    )
    logger.info("starting streamable-http on %s:%s", args.host, args.port)
    server.run(
        transport="streamable-http",
        host=args.host,
        port=args.port,
        streamable_http_path="/mcp",
        transport_security=security,
    )
