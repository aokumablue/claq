# Python SDK `mcp` の実 API と実測ワイヤ形式

採取条件: `mcp==2.1.1`（PyPI 最新）/ Python 3.14 / stdio トランスポート。
以下は**ドキュメントの引用ではなく実際に走らせて得た出力**である。
SDK を更新したら再採取して差分を見ること。

```
mcp.types.LATEST_PROTOCOL_VERSION == "2026-07-28"
mcp のランタイム依存: anyio, httpx2, jsonschema, mcp-types, opentelemetry-api,
                      pydantic, pyjwt, python-multipart, sse-starlette,
                      starlette, typing-extensions, typing-inspection, uvicorn
requires-python: >=3.10
```

## import の実体

```python
from mcp.server import MCPServer, CacheHint          # サーバ本体とキャッシュヒント
from mcp.server.mcpserver import Context             # ハンドラに注入される型
from mcp.server.transport_security import TransportSecuritySettings
```

`FastMCP` は 2.x で `MCPServer` に改名された。`mcp/server/fastmcp.py` は
**まだ存在する**ため、記憶で `FastMCP` と書いても import が通り起動もする。
これが最も危険な失敗モード（壊れているのに動く）。

## `MCPServer.__init__` の主な引数（実測シグネチャ）

```
MCPServer(name=None, title=None, description=None, instructions=None,
          website_url=None, icons=None, version="",
          auth_server_provider=None, token_verifier=None, *,
          tools=None, resources=None, extensions=None, debug=False,
          log_level="INFO", warn_on_duplicate_resources=True,
          warn_on_duplicate_tools=True, warn_on_duplicate_prompts=True,
          dependencies=None, lifespan=None, auth=None,
          resource_security=ResourceSecurity(reject_path_traversal=True,
                                             reject_absolute_paths=True,
                                             reject_null_bytes=True,
                                             exempt_params=frozenset()),
          request_state_security=None, cache_hints=None,
          subscriptions=None, middleware=None)
```

- `cache_hints: Mapping[CacheableMethod, CacheHint]`
  `CacheHint(ttl_ms: int = 0, scope: Literal["public","private"] = "private")`
- `resource_security` の既定は**安全側**（パストラバーサル・絶対パス・NUL を拒否）。

## デコレータ

```
@mcp.tool(name=None, title=None, description=None, annotations=None,
          icons=None, meta=None, structured_output=None)
@mcp.resource(uri, *, name=None, title=None, description=None, mime_type=None,
              icons=None, annotations=None, meta=None, security=None)
@mcp.prompt(name=None, title=None, description=None, icons=None)
```

- `inputSchema` は型注釈から、`outputSchema` は**戻り値の型注釈から**自動生成。
- `structured_output=None` は戻り値注釈による自動判定。
- URI に `{param}` を含めるとテンプレートリソース、含めなければ静的リソース。
- 関数に `Context` 型注釈の引数を足すと SDK が注入する（引数名は任意）。

## `Context` の主なメンバ（実測）

```
ctx.debug/info/warning/error/log(...)    ⚠ 非推奨。下記「ログ」を読むこと
ctx.report_progress(progress, total=None, message=None)
ctx.elicit(message, schema)              # ⚠ 旧経路。下記「MRTR」を読むこと
ctx.elicit_url(message, url, elicitation_id)           # 同上
ctx.input_responses                                    # MRTR 再送時の生データ
ctx.request_state                                      # MRTR の持ち回し状態
ctx.read_resource(uri)
ctx.notify_tools_changed() / notify_resources_changed() / notify_prompts_changed()
ctx.notify_resource_updated(uri)
ctx.client_capabilities / ctx.protocol_version / ctx.headers / ctx.request_id
```

## 起動

```
mcp.run(transport="stdio" | "streamable-http" | "sse")
mcp.run_streamable_http_async(host="127.0.0.1", port=8000,
                              streamable_http_path="/mcp", json_response=False,
                              stateless_http=False, event_store=None,
                              retry_interval=None, max_request_body_size=4194304,
                              transport_security=None)
mcp.streamable_http_app(...) -> Starlette      # 既存 ASGI へマウントする場合
```

`transport="sse"` は非推奨トランスポート（2024-11-05 HTTP+SSE）。新規では選ばない。

`TransportSecuritySettings()` の既定値（実測）:
`{'enable_dns_rebinding_protection': True, 'allowed_hosts': [], 'allowed_origins': []}`
— 保護は既定で有効だが**許可リストは空**。明示的に列挙すること。

## MRTR（追加入力の要求）— `ctx.elicit` は使わない

`Context.elicit` は docstring 上「ツール実行中に対話的に情報を求める」と読めるが、
**サーバ発リクエスト（旧経路）を張る実装**であり、2026-07-28 のステートレスな
トランスポートでは張れずに失敗する。実測:

```
{"jsonrpc":"2.0","id":1,"error":{"code":-32600,
 "message":"Cannot send 'elicitation/create': this transport context has no
            back-channel for server-initiated requests."}}
```

MRTR の経路は**リゾルバによる依存注入**である。ツール引数を
`Annotated[T, Resolve(fn)]` で宣言し、`fn` が `Elicit(...)` / `Sample(...)` /
`ListRoots()` を返す。SDK がそれを `InputRequiredResult` へ束ね、
クライアントの再送で解決して本体を実行する。

```python
from typing import Annotated

from mcp.server.mcpserver import Elicit, ElicitationResult, Resolve
from pydantic import BaseModel


class Confirm(BaseModel):
    approve: bool


def ask_confirm(target: str) -> Elicit[Confirm]:
    """削除の確認をクライアントへ求める。リゾルバはツール引数を名前で受け取れる。"""
    return Elicit(f"{target} を削除してよいですか", Confirm)


@mcp.tool()
async def danger(target: str, confirm: Annotated[Confirm, Resolve(ask_confirm)]) -> str:
    """確認を取ってから破壊的操作を行う。"""
    return f"deleted {target}" if confirm.approve else "cancelled"
```

受け方は 2 通りあり、**decline / cancel 時のワイヤ出力が違う**。実測（同一サーバ・
同一リクエストに `{"action": "decline"}` を返した場合）:

| 受け方 | `isError` | `content` |
|---|---|---|
| `Annotated[T, Resolve(fn)]` | `true` | `Error executing tool bare: Resolver for parameter 'c' could not resolve: elicitation was decline` |
| `Annotated[ElicitationResult[T], Resolve(fn)]` | `false` | ハンドラが返した文字列（例 `aborted (decline)`） |

`ElicitationResult` の import 元は `Elicit` / `Resolve` と同じ
`mcp.server.mcpserver`。確認用途の完全な形:

```python
@mcp.tool()
async def danger(target: str, confirm: Annotated[ElicitationResult[Confirm], Resolve(ask_confirm)]) -> str:
    """確認を取ってから破壊的操作を行う。"""
    if confirm.action != "accept":
        return f"中止しました（{confirm.action}）"
    return "削除しました" if confirm.data.approve else "中止しました"
```

つまり素の `T` は「拒否＝呼び出しの失敗」という意味になる。
**確認・同意の用途では `ElicitationResult[T]` を使う**（拒否は正常な結果であり、
「中止しました」を戻り値で返せる必要があるため）。素の `T` が適切なのは、
入力が取れなければ処理を続けられない場合だけ。
`ElicitationResult` は `action`（`accept` / `decline` / `cancel`）と、
accept のときだけ埋まる `data` を持つ。
- リゾルバを使うツールは `async def` でも同期 `def` でもよい（実測。同期でも
  `input_required` → 再送 → 解決まで通る）。同期関数はスレッドプールで走る。
- `Sample` / `ListRoots` に decline は無い。ただし **Sampling と Roots は非推奨**なので
  新規実装では使わない。実質使うのは `Elicit` だけ。
- クライアントが対応 capability を宣言していない場合、SDK は
  `MissingRequiredClientCapability`（`-32021`）を返す。

実測ワイヤ（1 往復目 → 再送 → 完了）:

```json
{"jsonrpc":"2.0","id":1,"result":{
  "resultType":"input_required",
  "inputRequests":{"__main__:ask_confirm":{"method":"elicitation/create",
    "params":{"message":"db を削除してよいですか","mode":"form",
              "requestedSchema":{"type":"object",
                "properties":{"approve":{"type":"boolean","title":"Approve"}},
                "required":["approve"]}}}},
  "requestState":"v1.bJldIQpdiNduk..."}}
```

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{
  "name":"danger","arguments":{"target":"db"},
  "inputResponses":{"__main__:ask_confirm":{"action":"accept",
                    "content":{"approve":true}}},
  "requestState":"v1.bJldIQpdiNduk..."}}
```

```json
{"jsonrpc":"2.0","id":2,"result":{
  "resultType":"complete","isError":false,
  "content":[{"type":"text","text":"deleted db"}],
  "structuredContent":{"result":"deleted db"}}}
```

`requestState` の `v1.` 接頭辞は SDK の AEAD 保護済みブロブ
（`AESGCMRequestStateCodec`）。仕様は `requestState` を**攻撃者制御入力**として
扱うことを要求しており、SDK 既定の codec がその完全性保護を担う。
自前で `requestState` を組み立てるなら、認証主体・短い TTL・元リクエストの識別子を
保護対象ペイロードへ入れて毎回検証すること。

再送は**別の JSON-RPC id** で来る。同じ id で送るのは仕様違反。

## エラー設計 — 素の例外はメッセージが伏せられる

実測（`mcp` 2.1.1）。**ここを間違えると `isError` は立つのにモデルが何も学べない。**

| ツール本体で投げるもの | ワイヤに出る `content` | モデルは直せるか |
|---|---|---|
| `ToolError("count は 1..100")` | `Error executing tool t: count は 1..100` | **直せる** |
| `ValueError("count は 1..100")` | `Error executing tool t` | 直せない（本文が消える） |
| `RuntimeError(...)` | `Error executing tool t` | 直せない |
| `MCPError(ErrorData(...))` | `Error executing tool t` | 直せない（`UnexpectedToolError` に包まれる） |

```python
from mcp.server.mcpserver.exceptions import ToolError
```

`mcp.server.mcpserver.exceptions` が公開する例外:
`MCPServerError` / `ToolError` / `UnexpectedToolError` /
`ResourceError` / `ResourceNotFoundError` / `UnexpectedResourceError`。

上流ドキュメントの判断基準は「賢いモデルなら避けられた失敗か？」
— Yes なら `ToolError`、No なら `MCPError`。ただし**ツール本体から投げた
`MCPError` は実測では JSON-RPC error にならず本文も伏せられる**ため、
ツール内での使い分けとしては機能していない。ツール本体では `ToolError` を使い、
それ以外の例外は「クラッシュ（本文はサーバログのみ）」として扱うのが実態に合う。

### 引数の制約は Field で宣言する（自前の if より強い）

```python
samples: Annotated[int, Field(ge=1, le=100, description="サンプル数。1〜100")]
```

実測: `inputSchema` に `"minimum": 1, "maximum": 100` が載り、違反時は
pydantic の検証メッセージがそのままモデルへ届く（`ToolError` 相当の扱い）。

```
Error executing tool fetch: 1 validation error for fetchArguments
count
  Input should be greater than or equal to 1 [type=greater_than_equal, input_value=0, ...]
```

自前の `if` + 素の `ValueError` にすると、スキーマに制約が載らず（モデルが
呼ぶ前に範囲を知れない）、メッセージも伏せられる。二重に損。

## ログ — プロトコル機能は非推奨

`Context` の `log` / `info` / `debug` / `warning` / `error` はすべて
`__deprecated__` を持つ（実測）:

```
The logging capability is deprecated as of 2026-07-28 (SEP-2577).
```

置き換えは標準ライブラリ。`MCPServer(log_level="INFO")` を渡すと SDK が
`basicConfig()` を面倒見る（既に設定済みなら触らない）。出力先は stderr。

```python
import logging

logger = logging.getLogger(__name__)
logger.info("searching for %r", query)
```

ログはモデルには届かない（届くのは戻り値だけ）。運用者のためのもの。
`ctx.report_progress()` は非推奨では**ない**ので、進捗はこちらを使う。

## デプロイ時に効く設定

- **`transport_security` を設定せずに実ホスト名で公開すると全リクエストが
  `421 Misdirected Request` になる。** 既定は localhost のみ。
  裸のホスト名とポート付きの両方を `allowed_hosts` に挙げる
  （`["mcp.example.com", "mcp.example.com:*"]`）。`allowed_origins` は
  ブラウザ由来のリクエストにのみ効く。リバースプロキシが Host/Origin を
  制御しているなら `enable_dns_rebinding_protection=False`。
- **複数インスタンスで MRTR を使うなら `RequestStateSecurity(keys=[32バイトの共有鍵])`。**
  既定はプロセスごとに鍵を生成するため、再送が別ワーカーへ行くと復号に失敗する。
  併せて**全インスタンスで同じサーバ名**にする（名前が封印トークンの audience になる）。
- `subscriptions/listen` の長命ストリームは 1 レプリカに貼り付く。
  SDK 同梱の `InMemorySubscriptionBus` は単一プロセス専用。プロセス跨ぎは
  `SubscriptionBus` プロトコル（2 メソッド）を Redis / NATS 等で自前実装する。
- `stateless_http=True` は**レガシー版（2025-11-25 以前）向けの逃げ道**で、
  サーバ→クライアント機能を無効化する。2026-07-28 は元からステートレスなので
  この版だけを相手にするなら触る必要がない。
- SDK は `workers=` もヘルスチェック経路も TLS 設定も持たない。ASGI サーバ側で行う。

## 実測ワイヤ出力

`server/discover`（クライアントは何も設定していない。SDK が自動応答する）:

```json
{"jsonrpc":"2.0","id":1,"result":{
  "cacheScope":"private",
  "capabilities":{"prompts":{"listChanged":true},
                  "resources":{"listChanged":true,"subscribe":true},
                  "tools":{"listChanged":true}},
  "resultType":"complete",
  "supportedVersions":["2026-07-28"],
  "ttlMs":0,
  "_meta":{"io.modelcontextprotocol/serverInfo":{"name":"probe","version":"0.1.0"}}}}
```

`tools/call`（`def echo(text: str) -> str`）:

```json
{"jsonrpc":"2.0","id":1,"result":{
  "content":[{"text":"A","type":"text"}],
  "structuredContent":{"result":"A"},
  "resultType":"complete",
  "_meta":{"io.modelcontextprotocol/serverInfo":{"name":"probe2","version":"0.1.0"}}}}
```

未知ツール（`isError` によるツール実行エラー。JSON-RPC error ではない）:

```json
{"jsonrpc":"2.0","id":2,"result":{
  "content":[{"text":"Unknown tool: nope","type":"text"}],
  "isError":true,"resultType":"complete"}}
```

## SDK が自動でやること / やらないこと

| 項目 | 自動か | 備考 |
|---|---|---|
| `server/discover` への応答 | 自動 | 自前実装は不要 |
| 全 result への `resultType` 付与 | 自動 | |
| `_meta` の `serverInfo` | 自動 | `version=` を渡していれば載る |
| `inputSchema` / `outputSchema` 生成 | 自動 | 型注釈から |
| `structuredContent` + テキスト併記 | 自動 | 後方互換のミラーも SDK が出す |
| 例外 → `isError: true` 変換 | 自動 | `ValueError` 等がツール実行エラーになる |
| `ttlMs` / `cacheScope` の**有用な値** | **手動** | 既定は `0` / `private` ＝ 即時陳腐化 |
| `allowed_hosts` / `allowed_origins` | **手動** | 既定は空リスト |

## 検証時の落とし穴（実測で踏んだもの）

stdio で生 JSON-RPC を流すとき、**リクエストを書いた直後に stdin を閉じると
成功応答が返らない**。サーバが処理を終える前にシャットダウンするため。
失敗が速いケース（未知ツール）だけ応答が返るので、
「特定のツールだけ壊れている」ように誤読しやすい。
全応答を受け取るまで stdin を開いたままにすること
（`assets/template/smoke_check.py` はそう実装してある）。


## 上流の一次資料

- Python SDK ドキュメント: <https://py.sdk.modelcontextprotocol.io/>
  （`migration/` `whats-new/` `deprecated/` `handlers/multi-round-trip/`
  `handlers/elicitation/` `handlers/logging/` `servers/handling-errors/`
  `run/deploy/` が特に効く）
- SDK リポジトリ（24k★）: <https://github.com/modelcontextprotocol/python-sdk>
  2.x で書かれた実例は `examples/mcpserver/` と `examples/servers/`。

### 高スターの MCP サーバを写経してはいけない

実測（2026-08-31 時点）:

| リポジトリ | ★ | 実装状況 |
|---|---|---|
| `modelcontextprotocol/servers`（公式リファレンス） | 約 90,000 | Python 実装（`src/git` `src/fetch` `src/time`）は **`mcp>=1.29.0,<2` 固定**。2026-07-28 ではない |
| `github/github-mcp-server` | 約 32,600 | Go |
| `modelcontextprotocol/python-sdk` | 約 24,200 | **2.x の実例はここの `examples/` だけ** |

星の多さは「最新仕様である」ことを意味しない。むしろ実績のあるサーバほど
1.x に固定されており、写経すると旧仕様が入る。2.x の実例として信頼できるのは
SDK リポジトリの `examples/` のみ。

そのうえで **`examples/` も上流のドキュメントより遅れている**:
`examples/mcpserver/logging_and_progress.py` は非推奨の `await ctx.info(...)` を、
`examples/servers/simple-streamablehttp/` は非推奨の `send_log_message` を
（pyright の `reportDeprecated` 抑止コメント付きで）今も使っている。
例を読むときは `deprecated/` のページと突き合わせること。


## Streamable HTTP のヘッダ強制（実測）

テンプレートを `--http` で起動し `curl` で確認（`mcp` 2.1.1）:

| リクエスト | 結果 |
|---|---|
| `MCP-Protocol-Version` + `Mcp-Method` あり、`Mcp-Name` なしで `tools/list` | `200` |
| `MCP-Protocol-Version` なしの POST | `400` `-32600` **`Bad Request: Missing session ID`** |
| `MCP-Protocol-Version` あり・`Mcp-Method` なし | `400` `-32020` `mcp-method header does not match the request body's method` |
| MCP エンドポイントへの `GET` | `400` |

### `Missing session ID` に釣られないこと

`MCP-Protocol-Version` ヘッダを付け忘れると、SDK は**旧版（セッションがあった頃）の
経路へフォールバック**し、`Bad Request: Missing session ID` を返す。

このメッセージは**この仕様に存在しない機構を名指ししている**。真に受けて
セッション ID を付けようとすると、まさに廃止された `Mcp-Session-Id` を
実装しにいくことになる。正しい対処は**プロトコル版ヘッダを付けること**。
`-32600` かつ本文にセッションの語が出たら、まずヘッダの付け忘れを疑う。

1 行目は仕様どおり（`Mcp-Name` が必須なのは `tools/call` /
`resources/read` / `prompts/get` だけで、`tools/list` には要らない）。
2 行目も仕様どおり（`MCP-Protocol-Version` 欠落は `400`）。

3 行目は**仕様との差異**。仕様はこの版のみを実装するサーバが GET に
`405 Method Not Allowed` を返すことを SHOULD としているが、SDK 2.1.1 は `400` を返す。
旧クライアントの後方互換探索は `400` / `404` / `405` のいずれでも
フォールバックへ進むため実害は無いが、`405` を期待した検査を書くと落ちる。
