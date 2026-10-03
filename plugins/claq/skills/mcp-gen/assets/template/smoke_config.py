"""smoke_check.py の検査対象。生成したサーバに合わせて書き換える。

サンプルの値のままだと、検査が実際のサーバを見なくなる（未知ツールで落ちるか、
見本のツールだけを検査して合格する）。使わない検査の名前は書かない（省略すると SKIP か
既定値になる。使える名前は smoke_check.py の OPTIONAL_SETTINGS）。
"""

# 出力が決定的なツール。毎回新しい子プロセスで同じ順に呼んで同じ値が返ること。
# 戻り値をモデルにしたツールを選ぶ（`-> str` は text が生の文字列になり、写しの検査に落ちる）。
# UUID・現在時刻・乱数を含む結果は落ちる。
HAPPY_TOOL = "measurements_compute"
HAPPY_ARGS: dict = {"samples": 2}
HAPPY_EXPECTED_STRUCTURED: dict = {"value": 3.0, "unit": "ms"}

# 型と範囲は正しいが業務的に失敗し、ツール本体から ToolError が投げられる呼び出し。
# 引数検証で弾かれる値を選ぶと、ToolError の経路を一度も通らずに合格してしまう。
# 業務上の失敗を持たないサーバなら 3 つとも消す（SKIP として記録される）。
INVALID_TOOL = "items_get"
INVALID_ARGS: dict = {"item_id": "bad"}
INVALID_EXPECTED_MESSAGE = "item-"

# 公開するリソース。公開しないなら RESOURCE_URI = ""（SKIP として記録される）。
RESOURCE_URI = "config://runtime"
RESOURCE_EXPECTED_SUBSTRING = "example-server"

# 公開するプロンプト。公開しないなら PROMPT_NAME = ""。引数はワイヤ上つねに文字列。
PROMPT_NAME = "review_request"
PROMPT_ARGS: dict = {"target": "server.py"}
PROMPT_EXPECTED_SUBSTRING = "server.py"

# 設計規則の上限。既定値であり、要件に合わせて変えてよい。
READ_ONLY_SERVER = True  # 書き込みツールを持つなら False
MAX_TOOLS = 10  # 超えるならツールセットに分ける
MAX_TOOL_NAME_LENGTH = 40  # ホストが付ける接頭辞の分を残す
MAX_RESULT_CHARS = 20_000  # 1 回の結果の text の上限
