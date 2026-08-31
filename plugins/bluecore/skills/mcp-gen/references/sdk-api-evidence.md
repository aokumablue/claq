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
ctx.debug/info/warning/error/log(...)    ログ（通知として流れる）
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

from mcp.server.mcpserver import Elicit, Resolve
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

- `Annotated[T, Resolve(fn)]` は素の `T` を受け取る（decline / cancel は呼び出しを中断）。
- `Annotated[ElicitationResult[T], Resolve(fn)]` にすると accept / decline / cancel を
  自分で分岐できる。
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
