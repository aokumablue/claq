"""トランスポートを選んでサーバを起動する（全サーバ共通。編集しない）。

stdio が既定で、`--http` で Streamable HTTP（既定は 127.0.0.1:8000/mcp）を使う。
どの server.py もここを通して起動し、HTTP の安全設定を 1 か所に保つ。
"""

from __future__ import annotations

import argparse
import logging

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def run(server: MCPServer) -> None:
    """コマンドライン引数でトランスポートを選び、サーバを起動する。

    Args:
        server: 起動するサーバ。
    """
    parser = argparse.ArgumentParser(description=f"{server.name} MCP server")
    parser.add_argument("--http", action="store_true", help="Streamable HTTP で起動する（既定は stdio）")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP バインド先（既定はループバックのみ）")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="ループバック以外へのバインドを許可する（DNS リバインディング検査が実質無効になる）",
    )
    parser.add_argument("--port", type=int, default=8000, help="HTTP ポート")
    args = parser.parse_args()

    if not args.http:
        # トランスポート引数は run() に渡す。MCPServer() のコンストラクタには渡さない。
        server.run(transport="stdio")
        return

    # 仕様上、HTTP サーバは Origin を検証し（DNS リバインディング対策）、
    # ローカル実行ではループバックにのみバインドしなければならない。
    # 許可リストは args.host から導出するため、非ループバックへ広げた瞬間に
    # 「バインド先と同じ値を照合する」形になり検査が実質無効化する。明示フラグを要求する。
    if args.host not in _LOOPBACK_HOSTS and not args.allow_remote:
        parser.error(f"--host {args.host} は非ループバック。--allow-remote を明示すること")
    # 既定の許可リストは空で localhost しか通らないため、実ホスト名で公開するときは
    # 裸のホスト名とポート付きの両方を挙げる（挙げ忘れると全リクエストが 421 になる）。
    # リバースプロキシが Host/Origin を制御しているなら
    # enable_dns_rebinding_protection=False にする。
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[args.host, f"{args.host}:{args.port}"],
        allowed_origins=[f"http://{args.host}:{args.port}"],
    )
    logger.info("starting streamable-http on %s:%s", args.host, args.port)
    server.run(
        transport="streamable-http",
        host=args.host,
        port=args.port,
        streamable_http_path="/mcp",
        transport_security=security,
    )
