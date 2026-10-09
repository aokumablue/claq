# example-server

プロトコル `2026-07-28` の MCP サーバ。

## ファイル

| ファイル | 役割 |
|---|---|
| `server.py` | サーバ本体（ツール・リソース・プロンプト） |
| `guard.py` | 未知の引数の拒否と公開スキーマの整形。削除しない |
| `transport.py` | 起動処理（stdio と Streamable HTTP の安全設定）。公開するホスト名を足すとき以外は編集しない |
| `smoke_check.py` | 実起動の適合検査。編集しない |
| `smoke_static.py` | `smoke_check.py` が使う、サーバを起動しない検査（旧仕様の名前の走査と、同梱するファイルの確認）。編集しない |
| `smoke_config.py` | 検査の対象（ツール名・引数・期待値・上限）。サーバに合わせて書き換える |
| `adapters.py` / `facade/` | 上流 API ファサードを重ねたときだけ存在する |
| `pyproject.toml` | 依存と、同梱するファイルの一覧（`only-include`）。モジュールを足したら一覧にも足す |

## セットアップ

```bash
uv venv && source .venv/bin/activate && uv pip install -e .
```

1 つのサーバに 1 つの venv を使う。このサーバはトップレベルに `server`・`guard`・`transport`
などの汎用の名前のモジュールを置くため、同じ venv に別のサーバを入れると名前が衝突する。

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
形そのものを見ている。ツール定義の設計規則（名前・説明・スキーマ・注釈・数）、
未知の引数の拒否、結果の大きさも同時に確かめる。`pyproject.toml` があれば、`server.py` から
辿れるローカルのモジュールが全て `only-include` に入っていることも確かめる。

## 起動

```bash
"$PY" server.py
```

```bash
"$PY" server.py --http
```

stdio が既定。`--http` で Streamable HTTP（`http://127.0.0.1:8000/mcp`）。HTTP に認証は
無いので、ループバック（127.0.0.1 / ::1 / localhost）でだけ待ち受け、それ以外の `--host` は
起動時に拒否する。

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

## エージェントでの評価

登録したら、利用者が実際に聞く問い（問いのカタログ）をエージェントに解かせ、
正答率・ツールの呼び出し回数・誤った引数・切り詰めの発生を記録する。誤りの原因を
説明・引数の既定値・ツールの境界のどれかに帰着させて直し、`smoke_check.py` を
通してからもう一度解かせる。
