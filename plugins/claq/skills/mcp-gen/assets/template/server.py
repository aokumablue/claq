"""MCP サーバの共通スケルトン（プロトコル 2026-07-28 / Python SDK `mcp` 2.x）。

このファイルは**そのまま動く**最小完全サーバである。新規サーバはこれを複製し、
`SERVER_NAME` とツール・リソース・プロンプトの中身を置き換えて作る。ゼロから
書き起こしてはならない（学習データの MCP は 2025 年以前の形であり、古い形でも
import が通って起動するため誤りに気づけない）。

このスケルトンが実証している 2026-07-28 の要点:
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

設計規則のうち、このファイルで見本を示しているもの:
  - モデルに見せる説明は `@mcp.tool(description=...)` で書く。docstring は開発者向け。
    `description` を省くと docstring 全文（Args: や実装の注記まで）がそのままモデルへ届く。
  - ツール名は `<resource>_<verb>`（英小文字・数字・`_`）。
  - 引数は平坦に並べ、制約と例は `Annotated` + `Field` で宣言する。入れ子のモデルを
    引数にしない（`$defs` / `$ref` が公開スキーマに出る）。
  - `install_guard(mcp)` を外さない。未知の引数の拒否と公開スキーマの整形を担う。
  - 起動は `transport.run(mcp)` に任せる。HTTP の安全設定はそちらに集めてある。

この docstring の要点の最後の行が**旧 API 名の書き方の見本**である。`Context` の
`elicit` メソッドのように**受け手と名前を分けて書く**こと。`ctx` に続けて
ドットとメソッド名を並べて書くと、それが説明文であっても禁止パターン検査に
当たる（正規表現は呼び出しと散文を区別しない）。

実行:
    python3 server.py             # stdio
    python3 server.py --http      # Streamable HTTP (127.0.0.1:8000/mcp)
"""

from __future__ import annotations

import logging
from typing import Annotated

from guard import install_guard
from mcp.server import CacheHint, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field
from transport import run

SERVER_NAME = "example-server"
SERVER_VERSION = "0.1.0"

# プロトコルのログ機能は非推奨。標準ライブラリの logging は既定で stderr へ出るため、
# stdio の stdout（JSON-RPC フレーム）を汚さない。`print` は使わない。
logger = logging.getLogger(__name__)

mcp = MCPServer(
    SERVER_NAME,
    version=SERVER_VERSION,
    # ツールをまたぐ流れ・ID の規約・鮮度・上限だけを書く。ツールの説明を繰り返さない。
    instructions="計測値は measurements_compute、項目の内容は items_get で取る。項目 ID は 'item-' で始まる。",
    log_level="INFO",
    # 既定は ttl_ms=0 / scope="private"（＝毎回取り直し）。一覧が安定しているなら
    # 明示してクライアント側キャッシュを効かせる。認可（スコープ）で一覧や内容が
    # 変わるなら "private" にする。
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
install_guard(mcp)

# 注釈は「信頼できないヒント」であり、認可の代わりにはならない（読み取り専用の保証は
# 上流の資格情報で行う）。`idempotentHint` / `destructiveHint` は `readOnlyHint=False`
# のときにだけ意味を持つので、読み取り専用のツールには付けない。
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


class Measurement(BaseModel):
    """構造化出力の例。戻り値の型から `outputSchema` が生成される。"""

    value: float = Field(description="計測値")
    unit: str = Field(description="単位")


class Item(BaseModel):
    """項目。`-> str` で返すと text が生の文字列になり、structuredContent の写しにならない。"""

    item_id: str = Field(description="項目 ID")
    content: str = Field(description="項目の内容")


@mcp.tool(
    description=(
        "サンプル数から計測値を算出して返す。\n"
        "使う場面: 計測値が必要なとき。\n"
        "応答: value（計測値）と unit（単位）。"
    ),
    annotations=READ_ONLY,
)
def measurements_compute(
    # 制約は Annotated + Field で宣言する。inputSchema の minimum/maximum に載るので
    # モデルが呼ぶ前に範囲を知れて、違反時は読めるエラーが返る。
    # 自前の if 文 + 素の ValueError にすると、どちらの利点も失われる。
    samples: Annotated[int, Field(ge=1, le=100, description="サンプル数。1〜100", examples=[10])],
    # 省略可能な引数は `X | None = None` でよい（公開スキーマの anyOf はガードが外す）。
    round_digits: Annotated[int | None, Field(ge=0, le=6, description="小数点以下の桁数。省略時は丸めない")] = None,
) -> Measurement:
    """サンプル数から計測値を算出する。

    Args:
        samples: サンプル数。1〜100。
        round_digits: 小数点以下の桁数。None なら丸めない。

    Returns:
        計測値と単位。
    """
    value = float(samples) * 1.5
    return Measurement(value=value if round_digits is None else round(value, round_digits), unit="ms")


@mcp.tool(
    description=(
        "項目 ID で 1 件の内容を返す。\n"
        "使う場面: 項目 ID が分かっているとき。\n"
        "主な引数: item_id = 'item-' で始まる ID。\n"
        "応答: item_id と content。"
    ),
    annotations=READ_ONLY,
)
def items_get(
    item_id: Annotated[str, Field(min_length=1, max_length=100, description="'item-' で始まる項目 ID", examples=["item-1"])],
) -> Item:
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
        raise ToolError(f"item_id は 'item-' で始まる必要があります（受領値: {item_id!r}）。正しい ID で呼び直してください。")
    return Item(item_id=item_id, content=f"content of {item_id}")


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
    """トランスポートを選んでサーバを起動する（安全設定は transport.py に集めてある）。"""
    run(mcp)


if __name__ == "__main__":
    main()
