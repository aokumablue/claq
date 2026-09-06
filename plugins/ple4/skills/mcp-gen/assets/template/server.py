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
  - ログはプロトコル機能としては非推奨（SEP-2577）。標準 `logging` で stderr へ出す。
    stdio では stdout が JSON-RPC 専用なので、そこへ書くとフレームが壊れる。
  - モデルに読ませたいエラーは `ToolError` で投げる。素の例外はメッセージが
    伏せられ、モデルには "Error executing tool <name>" しか届かない。
  - ツール実行中に確認・追加入力を取るなら MRTR（`Resolve` + `Elicit`）を使う。
    `Context` の `elicit` メソッドは旧経路で、ステートレスな transport では失敗する。

この docstring の最後の行が**旧 API 名の書き方の見本**である。`Context` の
`elicit` メソッドのように**受け手と名前を分けて書く**こと。`ctx` に続けて
ドットとメソッド名を並べて書くと、それが説明文であっても禁止パターン検査に
当たる（正規表現は呼び出しと散文を区別しない）。

実行:
    python3 server.py             # stdio
    python3 server.py --http      # Streamable HTTP (127.0.0.1:8000/mcp)
"""

from __future__ import annotations

import argparse
import logging
from typing import Annotated

from mcp.server import CacheHint, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

SERVER_NAME = "example-server"
SERVER_VERSION = "0.1.0"

# プロトコルのログ機能は非推奨。標準ライブラリの logging は既定で stderr へ出るため、
# stdio の stdout（JSON-RPC フレーム）を汚さない。`print` は使わない。
logger = logging.getLogger(__name__)

mcp = MCPServer(
    SERVER_NAME,
    version=SERVER_VERSION,
    instructions="このサーバの使いどころをクライアントへ 1〜2 文で伝える。",
    log_level="INFO",
    # 既定は ttl_ms=0 / scope="private"（＝毎回取り直し）。一覧が安定しているなら
    # 明示してクライアント側キャッシュを効かせる。認可で内容が変わるなら "private"。
    # ヒントを置ける先は 6 つ: tools/list, prompts/list, resources/list,
    # resources/templates/list, resources/read, server/discover。
    # 公開しない面のキーは消す。逆に公開する面のキーを書き忘れると、
    # その応答だけ ttlMs=0 のまま（＝毎回取り直し）になる。
    cache_hints={
        "tools/list": CacheHint(ttl_ms=300_000, scope="public"),
        "prompts/list": CacheHint(ttl_ms=300_000, scope="public"),
        "resources/list": CacheHint(ttl_ms=60_000, scope="public"),
        "resources/read": CacheHint(ttl_ms=60_000, scope="public"),
    },
)


class Measurement(BaseModel):
    """構造化出力の例。戻り値の型から `outputSchema` が生成される。"""

    value: float = Field(description="計測値")
    unit: str = Field(description="単位")


@mcp.tool(
    # クライアントは注釈を見て確認プロンプトの要否を判断する。
    # 仕様上これは「信頼できないヒント」であり、認可の代わりにはならない。
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True)
)
def measure(
    # 制約は Annotated + Field で宣言する。inputSchema の minimum/maximum に載るので
    # モデルが呼ぶ前に範囲を知れて、違反時は読めるエラーが返る。
    # 自前の if 文 + 素の ValueError にすると、どちらの利点も失われる。
    samples: Annotated[int, Field(ge=1, le=100, description="サンプル数。1〜100")],
) -> Measurement:
    """サンプル数から計測値を算出する。

    Args:
        samples: サンプル数。1〜100。

    Returns:
        計測値と単位。
    """
    return Measurement(value=float(samples) * 1.5, unit="ms")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def lookup(item_id: str) -> str:
    """項目 ID を引いて内容を返す。

    見つからない場合は `ToolError` を投げる。SDK がメッセージ込みで
    `isError: true` に変換するため、モデルが読んで別の ID で再試行できる。
    素の例外（`ValueError` など）だとメッセージが伏せられ、モデルは
    何が悪かったのか分からないまま同じ失敗を繰り返す。

    Args:
        item_id: 引く項目の ID。

    Returns:
        項目の内容。

    Raises:
        ToolError: 該当する項目が存在しないとき。
    """
    if not item_id.startswith("item-"):
        raise ToolError(f"item_id は 'item-' で始まる必要があります（受領値: {item_id!r}）")
    return f"content of {item_id}"


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
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="ループバック以外へのバインドを許可する（DNS リバインディング検査が実質無効になる）",
    )
    parser.add_argument("--port", type=int, default=8000, help="HTTP ポート")
    args = parser.parse_args()

    if not args.http:
        # トランスポート引数は run() に渡す。MCPServer() のコンストラクタには渡さない。
        mcp.run(transport="stdio")
        return

    # 仕様上、HTTP サーバは Origin を検証し（DNS リバインディング対策）、
    # ローカル実行ではループバックにのみバインドしなければならない。
    # 許可リストは args.host から導出するため、非ループバックへ広げた瞬間に
    # 「バインド先と同じ値を照合する」形になり検査が実質無効化する。明示フラグを要求する。
    if args.host not in ("127.0.0.1", "::1", "localhost") and not args.allow_remote:
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
    mcp.run(
        transport="streamable-http",
        host=args.host,
        port=args.port,
        streamable_http_path="/mcp",
        transport_security=security,
    )


if __name__ == "__main__":
    main()
