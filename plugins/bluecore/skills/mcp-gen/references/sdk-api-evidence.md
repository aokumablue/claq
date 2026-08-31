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
ctx.elicit(message, schema) -> ElicitationResult      # MRTR
ctx.elicit_url(message, url, elicitation_id)
ctx.input_responses                                    # MRTR 再送時に読む
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
