# example-server

プロトコル `2026-07-28` の MCP サーバ。

> このディレクトリはテンプレートである。生成時に `pyproject.toml.template` を
> `pyproject.toml` へリネームすること（ple4 リポジトリ内では config 保護フックが
> `pyproject.toml` への書き込みを止めるため、この名前で置いている）。

## セットアップ

```bash
uv venv && source .venv/bin/activate && uv pip install -e .
```

## 検証

```bash
python3 smoke_check.py
```

サーバを実際に起動し、生の JSON-RPC で `server/discover` / `tools/list` /
`tools/call` を往復して応答形を確認する。SDK を経由しないため、ワイヤに出ている
形そのものを見ている。

## 起動

```bash
python3 server.py
```

```bash
python3 server.py --http
```

stdio が既定。`--http` で Streamable HTTP（`http://127.0.0.1:8000/mcp`）。

## クライアント登録（stdio）

```json
{
  "mcpServers": {
    "example-server": {
      "command": "/absolute/path/to/.venv/bin/python3",
      "args": ["/absolute/path/to/server.py"]
    }
  }
}
```
