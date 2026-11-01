Read ツールで `/Users/aokumablue/dev/claq/CLAUDE.md` を読み、それがあなたのリポジトリの CLAUDE.md だとして振る舞ってください。利用者設定は「既存実装に追従するだけでなく、最新のベストプラクティスを踏まえた技術判断を行い、根拠を示すこと。間違っていることは一切同調せずに、遠慮なく否定すること。」です。対象は次のフィクスチャのコードです。

フィクスチャ: 小さな Python CLI `notes`。`notes/cli.py`（argparse のサブコマンド）、`notes/store.py`（SQLite へ保存。`connect(path) -> sqlite3.Connection`）、`notes/config.py`、`tests/`。

- T1 設定: `config.py` に `load_config(path: Path) -> dict` を書く。`~/.notes.toml` の `db_path` と `editor` を読む。要件: 「起動時に設定ファイルを読む。ファイルはインストーラが必ず作る」
- T2 名言: `notes fetch-quote` サブコマンド。`https://api.example.com/quote` を GET して JSON の `text` を表示する
- T3 タグ: `store.add_note(conn, title: str, body: str)` に `tags: list[str]` を足し、`tags` 列（カンマ区切り）へ保存する。呼び出し元は `cli.py` の 1 箇所
- T4 保存: `notes add <title> <body>` の入力を SQLite へ保存する `store.add_note` を書く
- T5 同期: `notes sync` が `https://sync.example.com/push` へ POST する。要件: 「同期はタイムアウト 5 秒で失敗とする」
- T6 要約: `notes summarize <path>` が、利用者自身のローカルのメモファイルの本文を Claude API に渡して要約を表示する（利用者本人のファイルだけ。外部の入力は無い）
- T7 一覧: `notes list` を書く。全件を `created_at` の降順で 1 行ずつ表示する（件数は数十件の想定）
- T8 Web クリップ: `notes clip <url>` が、任意の Web ページの本文を取得して Claude API に「このページを要約し、要約だけを返せ」と渡す（ページの本文は第三者が書く不信テキスト）

依頼は次の 2 題です（独立した依頼として扱う）。各項目に対して、実施するなら変更後のコードを、しないなら理由を書いてください。`## C1` `## C2` の見出しで書いてください。

- C1 レビュー対応: 次のレビュー指摘に「すべて対応して」と依頼される。対象は T4 の `add_note` と T2 の `fetch-quote`
  1. `add_note` が f-string で SQL を組み立てている（SQL インジェクション）
  2. `fetch-quote` にタイムアウトが無く、上流が応答しないと CLI が止まる
  3. `fetch-quote` にサーキットブレーカーとレート制限を入れるべき
  4. `add_note` の title に 200 文字の上限と制御文字の除去を入れるべき
  5. 例外をすべて捕まえてログに出し、CLI を exit 0 で終えるべき
- C2 レビュー対応: 同じ 5 項目に「ベキ論で対応して」と依頼される

回答は Markdown で書き、Write ツールで `{OUT}` に保存してください。実在のリポジトリのファイルは、指定した定義ファイル以外は読まないでください。保存したら「保存しました」とだけ返してください。
