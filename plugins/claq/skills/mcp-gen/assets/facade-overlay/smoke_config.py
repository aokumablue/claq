"""smoke_check.py の検査対象（上流 API ファサードの見本向け）。生成したサーバに合わせて書き換える。

サンプルの値のままだと、検査が実際のサーバを見なくなる（未知ツールで落ちるか、
見本のツールだけを検査して合格する）。
"""

# 出力が決定的なツール。毎回新しい子プロセスで同じ順に呼んで同じ値が返ること。
# 取得時刻（as_of）を含む結果は落ちるので、項目カタログのような静的な結果を選ぶ。
HAPPY_TOOL = "fields_describe"
HAPPY_ARGS: dict = {"entity": "order", "query": "配送"}
HAPPY_EXPECTED_STRUCTURED: dict = {
    "entity": "order",
    "fields": [
        {
            "path": "shipping.address.city",
            "type": "string",
            "description": "配送先の市区町村",
            "views": ["detail"],
            "sensitivity": "internal",
            "in_search": True,
        },
        {
            "path": "shipping.address.postal_code",
            "type": "string",
            "description": "配送先の郵便番号（7 桁、ハイフンなし）",
            "views": [],
            "sensitivity": "personal",
            "in_search": False,
        },
        {
            "path": "shipments.tracking_number",
            "type": "string",
            "description": "配送の追跡番号（shipments は配列）",
            "views": ["detail"],
            "sensitivity": "internal",
            "in_search": False,
        },
        {
            "path": "shipments.state",
            "type": "string",
            "description": "配送の状態。preparing / in_transit / delivered",
            "views": ["detail"],
            "sensitivity": "internal",
            "in_search": False,
        },
    ],
    "total_matches": 4,
    "has_more": False,
}

# 型と範囲は正しいが業務的に失敗し、ツール本体から ToolError が投げられる呼び出し。
INVALID_TOOL = "orders_get"
INVALID_ARGS: dict = {"order_id": "ord_999999"}
INVALID_EXPECTED_MESSAGE = "見つからない"

# リソースとプロンプトは公開しない（SKIP として記録される）。
RESOURCE_URI = ""
RESOURCE_EXPECTED_SUBSTRING = ""
PROMPT_NAME = ""
PROMPT_ARGS: dict = {}
PROMPT_EXPECTED_SUBSTRING = ""

# 設計規則の上限。既定値であり、要件に合わせて変えてよい。
READ_ONLY_SERVER = True
MAX_TOOLS = 10
MAX_TOOL_NAME_LENGTH = 40
MAX_RESULT_CHARS = 20_000  # server.py の MAX_RESULT_CHARS 以上にする

# ページング: 2 ページ以上ある条件と、値を 1 つだけ変えた条件。
PAGED_TOOL = "orders_search"
PAGED_ARGS: dict = {"customer_id": "cus_0000000001", "limit": 3}
PAGED_CHANGED_ARGS: dict = {"customer_id": "cus_0000000001", "limit": 4}
# 2 つのプロセスで同じ鍵を使い、カーソルが自己完結していることを確かめる
PAGED_ENV: dict = {"FACADE_CURSOR_SECRET": "smoke-check-shared-secret-0123456789abcdef"}
CURSOR_ERROR_SUBSTRING = "cursor"

# 項目パス: 綴りを誤ったパスと、候補として示されるべき正しいパス。
FIELDS_TOOL = "orders_get"
FIELDS_BAD_ARGS: dict = {"order_id": "ord_000001", "fields": ["shiping.address.city"]}
FIELDS_EXPECTED_SUGGESTION = "shipping.address.city"
