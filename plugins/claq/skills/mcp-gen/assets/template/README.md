# example-server

プロトコル `2026-07-28` の MCP サーバ。

> このディレクトリはテンプレートである。生成時に `pyproject.toml.template` を
> `pyproject.toml` へリネームすること（claq リポジトリ内では config 保護フックが
> `pyproject.toml` への書き込みを止めるため、この名前で置いている）。

## セットアップ

```bash
uv venv && source .venv/bin/activate && uv pip install -e .
```

以降のコマンドは**すべて `mcp` が入った interpreter の絶対パス**（以下 `$PY`）で実行する。
`smoke_check.py` はサーバを `sys.executable` で起動するため、裸の `python3` で呼ぶと
サーバ側だけが `ModuleNotFoundError` で死ぬ。venv の有効化はシェル呼び出しをまたいで
持続しないので、有効化に頼らず絶対パスで指す。

## 検証

```bash
"$PY" smoke_check.py
```

サーバを実際に起動し、生の JSON-RPC で `server/discover` / `tools/list` /
`tools/call` を往復して応答形を確認する。SDK を経由しないため、ワイヤに出ている
形そのものを見ている。

## 起動

```bash
"$PY" server.py
```

```bash
"$PY" server.py --http
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
