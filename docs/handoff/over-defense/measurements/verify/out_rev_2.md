# レビュー: notes API に検索と 1 件取得のエンドポイントを足す（差分 D1）

- task: notes API に検索と 1 件取得のエンドポイントを足す
- 要件: 「ログイン利用者が自分のノートを検索・取得できる」
- 前提: ファイルパスが示されていないため、位置は `D1:行`（差分ブロック内の行番号、`@app.get("/notes/search")` を 1 行目とする）で示す。テストの実行と Grep はできないため、読解で判定した。

### BLOCKER (CRITICAL|HIGH)
[CRITICAL] D1:5 — SQLi: 検索語 `q` を f-string で SQL に埋め込んでいる。攻撃者 = ログイン済みの任意の利用者 → 入口 = `GET /notes/search?q=` → 得るもの = `q=' OR 1=1 --` で `owner_id` 条件を外して他人のノートの id・title を取得でき、`UNION SELECT` で他人の `body` や他テーブル（例: 利用者テーブル）も読める — プレースホルダに置き換える: `db.execute("SELECT id, title FROM notes WHERE owner_id = ? AND title LIKE ? ESCAPE '\\'", (current_user.id, f"%{escaped_q}%"))`（`escaped_q` は `\` `%` `_` をエスケープしたもの）
[CRITICAL] D1:12 — 認可漏れ（IDOR）: `get_note` が `id` だけで取得し `owner_id` を照合しないため、要件「自分のノートを取得できる」が崩れる。攻撃者 = ログイン済みの任意の利用者 → 入口 = `GET /notes/<note_id>`（整数の連番を総当たり）→ 得るもの = 他人のノートの title・body — `WHERE id = ? AND owner_id = ?` に `current_user.id` を渡し、他人のノートは存在しない場合と同じ 404 を返す

### WARNING (MEDIUM|LOW)
[LOW] D1:3 — 公開 API のドキュメント欠落: 追加した `search_notes`・`get_note`（と `_format_title`）に docstring が無い — 各関数に、エンドポイントの入力・返り値・所有者で絞ることを書いた docstring を付ける

### INFO
[INFO] D1:18 — 未確認: `_format_title` は呼び出し元が 1 つしか無い抽象の候補だが、「呼び出し元は create_note だけ」「入口で 1〜120 文字に検証済み」はコメントの主張で、呼び出し元の全数確認（Grep）も入口スキーマの検証位置の引用もできていないため余剰とは示せない。また差分内に呼び出しが無く、今回足されたのに未使用の可能性もある — Grep で `_format_title` の呼び出し元を全数確認し、1 箇所ならインライン化、0 箇所なら削除する
[INFO] D1:1 — 未確認: 追加した 2 エンドポイントのテストが差分に含まれていない。テスト欠落の根拠となる Grep（`/notes/search`・`get_note` を参照するテストが 0 件）を取れていない — テストの有無を確かめ、無ければ「他人のノートは検索結果に出ない」「他人のノート id は 404」「`q` に `'` や `%` を含めても注入・ワイルドカード化しない」を足す

Blockers: 2
