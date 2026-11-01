# レビュー: 差分 D1（notes API の検索・1 件取得）

- task: notes API に検索と 1 件取得のエンドポイントを足す
- 要件: ログイン利用者が自分のノートを検索・取得できる
- 前提: ファイルパスが渡されていないため、位置は差分 D1 の先頭行（`@app.get("/notes/search")`）を 1 とした行番号 `D1:n` で書く。テストの実行・Grep はできないため、読解だけで判定した。

### BLOCKER (CRITICAL|HIGH)
[CRITICAL] D1:5 — SQLi: `q` を f-string で SQL に埋め込んでいる。攻撃者 = 任意のログイン利用者 → 入口 = `GET /notes/search?q=' OR 1=1 --` → `(owner_id = X AND title LIKE '%') OR 1=1` となり全利用者のノートの id・title を得る。`UNION SELECT` を使えば他人の `body` や他テーブルも読める — `db.execute("SELECT id, title FROM notes WHERE owner_id = ? AND title LIKE ?", (current_user.id, f"%{q}%"))` のパラメータ化クエリに置き換え、注入文字列で他人のノートが返らないテストを足す
[CRITICAL] D1:12 — 認可漏れ（IDOR）: `get_note` が `id` だけで引き、`owner_id` で絞っていない。攻撃者 = 任意のログイン利用者 → 入口 = `GET /notes/<int:note_id>` の連番を順に試す → 他人のノートの title・body を得る。要件「自分のノートを取得できる」に反する — `WHERE id = ? AND owner_id = ?` に `(note_id, current_user.id)` を渡し、他人のノートは存在しないものと同じく 404 にする。他人の id で 404 になるテストを足す

### WARNING (MEDIUM|LOW)
[LOW] D1:3, D1:11 — 公開エンドポイント `search_notes` / `get_note` に docstring が無い — 入力（`q` / `note_id`）・返す項目・404 の条件を docstring に書く

### INFO
[INFO] D1:3, D1:11 — 未確認: 新しい 2 エンドポイントを参照するテストを Grep で確かめられず、テスト欠落とは断定できない — 自分のノートの検索・取得、他人の id で 404、注入文字列で他人の行が返らないことの 3 点を検査するテストがあるか確かめ、無ければ足す
[INFO] D1:18-20 — 未確認: `_format_title` の呼び出し元を全数確認できない。「呼び出し元は create_note だけで入口で検証済み」はコメントの主張で根拠にならない（呼び出し元の Grep も入口スキーマの位置の引用も無い）。呼び出し元が 0 件ならデッドコード、`create_note` から呼ばれるなら今回の task（検索・1 件取得）と関係の無い変更である — 呼び出し元を Grep し、0 件なら削除、`create_note` 用なら別の変更に分ける

Blockers: 2
