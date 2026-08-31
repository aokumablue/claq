"""MCP サーバ テンプレート（プロトコル 2026-07-28 / Python SDK `mcp` 2.x）。

このファイルは**そのまま動く**最小完全サーバである。新規サーバはこれを複製し、
`SERVER_NAME` とツール・リソース・プロンプトの中身を置き換えて作る。ゼロから
書き起こしてはならない（学習データの MCP は 2025 年以前の形であり、古い形でも
import が通って起動するため誤りに気づけない）。

このテンプレートが実証している 2026-07-28 の要点:
  - 接続開始時のハンドシェイクは存在しない。プロトコルはステートレスで、
    各リクエストが `_meta` に自分のプロトコル版と capability を運ぶ。
  - `server/discover` は SDK が自動で応答する（自前実装は不要）。
  - 全 result に `resultType` が付く（SDK が自動付与）。
  - `tools/list` の `ttlMs` / `cacheScope` は既定が `0` / `private` = 即時陳腐化。
    キャッシュさせたいなら `cache_hints` で明示する。
  - stdio では stdout が JSON-RPC 専用。ログは必ず stderr（`logging`）へ出す。

実行:
    python3 server.py             # stdio
    python3 server.py --http      # Streamable HTTP (127.0.0.1:8000/mcp)
"""

from __future__ import annotations

import argparse
import logging

from mcp.server import CacheHint, MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings

SERVER_NAME = "example-server"
SERVER_VERSION = "0.1.0"

# stdio では stdout が JSON-RPC のフレームそのものなので、print() は禁止。
# logging は既定で stderr へ出るため安全。
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = MCPServer(
    SERVER_NAME,
    version=SERVER_VERSION,
    instructions="このサーバの使いどころをクライアントへ 1〜2 文で伝える。",
    # 既定は ttl_ms=0 / scope="private"（＝毎回取り直し）。一覧が安定しているなら
    # 明示してクライアント側キャッシュを効かせる。認可で内容が変わるなら "private"。
    cache_hints={
        "tools/list": CacheHint(ttl_ms=300_000, scope="public"),
        "prompts/list": CacheHint(ttl_ms=300_000, scope="public"),
        "resources/list": CacheHint(ttl_ms=60_000, scope="public"),
    },
)


@mcp.tool()
def add(a: float, b: float) -> float:
    """2 つの数値を加算して返す。

    Args:
        a: 加算する左辺。
        b: 加算する右辺。

    Returns:
        `a + b` の値。戻り値の型注釈から `outputSchema` と
        `structuredContent` が自動生成される。
    """
    return a + b


@mcp.tool()
async def fetch_items(count: int, ctx: Context) -> list[str]:
    """項目を `count` 件生成して返す（Context 利用例）。

    引数検証に失敗したら `ValueError` を投げる。SDK がそれを
    `isError: true` のツール実行エラーへ変換し、モデルが自己修正できる形で返す。
    プロトコルエラー（JSON-RPC error）にはならない点が重要。

    Args:
        count: 生成する件数。1〜100。
        ctx: SDK が注入するリクエストコンテキスト。ログと進捗通知に使う。

    Returns:
        生成した項目の一覧。

    Raises:
        ValueError: `count` が 1〜100 の範囲外のとき。
    """
    if not 1 <= count <= 100:
        raise ValueError(f"count は 1〜100 で指定してください（受領値: {count}）")

    await ctx.info(f"{count} 件を生成します")
    items: list[str] = []
    for index in range(count):
        items.append(f"item-{index}")
        # progressToken を送ってきたクライアントにのみ届く。送ってこなければ無視される。
        await ctx.report_progress(progress=index + 1, total=count)
    return items


@mcp.resource("config://runtime")
def runtime_config() -> str:
    """静的リソース。固定 URI に対して内容を返す。

    Returns:
        リソース本文（`str` はテキスト、`bytes` はバイナリとして扱われる）。
    """
    return f"{SERVER_NAME} {SERVER_VERSION}"


@mcp.resource("item://{item_id}")
def item_detail(item_id: str) -> str:
    """テンプレートリソース。URI 中の `{item_id}` が引数へ束縛される。

    `item_id` は外部入力である。既定の `resource_security` がパストラバーサル・
    絶対パス・NUL を弾くが、ファイルや DB を引くなら自前の検証も併せて行う。

    Args:
        item_id: URI から抽出された項目 ID。

    Returns:
        項目の内容。
    """
    return f"detail of {item_id}"


@mcp.prompt()
def review_request(target: str) -> str:
    """レビュー依頼のプロンプトを組み立てる。

    Args:
        target: レビュー対象の名前。

    Returns:
        クライアントへ渡すプロンプト本文。
    """
    return f"{target} をレビューし、修正すべき点を重要度順に挙げてください。"


def main() -> None:
    """トランスポートを選んでサーバを起動する。

    Raises:
        例外は発生しません。
    """
    parser = argparse.ArgumentParser(description=f"{SERVER_NAME} MCP server")
    parser.add_argument("--http", action="store_true", help="Streamable HTTP で起動する（既定は stdio）")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP バインド先（既定はループバックのみ）")
    parser.add_argument("--port", type=int, default=8000, help="HTTP ポート")
    args = parser.parse_args()

    if not args.http:
        mcp.run(transport="stdio")
        return

    # 仕様上、HTTP サーバは Origin を検証し（DNS リバインディング対策）、
    # ローカル実行ではループバックにのみバインドしなければならない。
    # allowed_origins は実際に許可するオリジンだけを列挙する（ワイルドカードを置かない）。
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{args.host}:{args.port}"],
        allowed_origins=[f"http://{args.host}:{args.port}"],
    )
    mcp.run(
        transport="streamable-http",
        host=args.host,
        port=args.port,
        streamable_http_path="/mcp",
        transport_security=security,
    )


if __name__ == "__main__":
    main()
