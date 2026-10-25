"""example-server: 計測値と項目の内容を返す MCP サーバ（見本。プロトコル 2026-07-28 / Python SDK `mcp` 2.x）。

実行:
    python3 server.py             # stdio
    python3 server.py --http      # Streamable HTTP (127.0.0.1:8000/mcp)
"""

from __future__ import annotations

import logging
from typing import Annotated

from guard import CallLimiter, install_guard
from mcp.server import CacheHint, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field
from transport import run

SERVER_NAME = "example-server"
SERVER_VERSION = "0.1.0"
# 受信側のレート制限: 主体 × ツールごとに、TOOL_CALL_WINDOW_SECONDS 秒に TOOL_CALLS_PER_WINDOW 回まで通す。
# smoke_check.py が 1 回の起動で送るツール呼び出しの数以上にする（下回ると smoke が上限で落ちる）。
TOOL_CALLS_PER_WINDOW = 30
TOOL_CALL_WINDOW_SECONDS = 60.0

# stdout は JSON-RPC 専用。ログは logging の既定の出力先（stderr）へ書き、print は使わない。
logger = logging.getLogger(__name__)

mcp = MCPServer(
    SERVER_NAME,
    version=SERVER_VERSION,
    instructions="計測値は measurements_compute、項目の内容は items_get で取る。項目 ID は 'item-' で始まる。",
    log_level="INFO",
    # 一覧と内容が全員に同じで安定しているので、共有のキャッシュを許す。
    cache_hints={
        "tools/list": CacheHint(ttl_ms=300_000, scope="public"),
        "prompts/list": CacheHint(ttl_ms=300_000, scope="public"),
        "resources/list": CacheHint(ttl_ms=60_000, scope="public"),
        "resources/read": CacheHint(ttl_ms=60_000, scope="public"),
    },
)


def _principal() -> str:
    """呼び出しの主体を返す（受信側のレート制限を主体ごとに数えるため）。

    stdio と、認証の無いループバックの HTTP では、呼び出し元は 1 人なので固定の値でよい。
    リモートで認証を入れたら、検証済みのアクセストークンの主体に置き換える（ツール引数から取らない）。

    Returns:
        主体の識別子。
    """
    return "local"


# 未知の引数の拒否・主体 × ツールの呼び出しの制限・公開する inputSchema の整形（guard.py）
install_guard(mcp, limiter=CallLimiter(TOOL_CALLS_PER_WINDOW, TOOL_CALL_WINDOW_SECONDS), principal=_principal)

# 注釈はヒントであり、読み取り専用の保証にはならない（保証は資格情報で行う）。
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


class Measurement(BaseModel):
    """計測値と単位。"""

    value: float = Field(description="計測値")
    unit: str = Field(description="単位")


class Item(BaseModel):
    """項目。"""

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
    samples: Annotated[int, Field(ge=1, le=100, description="サンプル数。1〜100（例: 10）", examples=[10])],
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
    item_id: Annotated[
        str, Field(min_length=1, max_length=100, description="'item-' で始まる項目 ID（例: item-1）", examples=["item-1"])
    ],
) -> Item:
    """項目 ID を引いて内容を返す。

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
