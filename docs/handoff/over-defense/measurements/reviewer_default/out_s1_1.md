# レビュー結果: 差分 D1（notes API に検索と 1 件取得を追加）

- task: notes API に検索と 1 件取得のエンドポイントを足す
- 要件: ログイン利用者が自分のノートを検索・取得できる
- 行番号は提示された差分 D1 の先頭行（`@app.get("/notes/search")`）を 1 とした相対行。ファイルパスは提示されていないため `D1` と表記する
- テストは実行していない（読解のみ）。リポジトリへの Grep もしていないため、呼び出し元の全数確認やテストの参照確認を根拠にする判定は INFO に置いた

### BLOCKER (CRITICAL|HIGH)
[CRITICAL] D1:5 — SQLi: 検索語 `q` を f-string で SQL に直接埋め込んでいる。攻撃者 → 入口 → 得るもの: 任意のログイン利用者 → `GET /notes/search?q=` → `q=%' OR '1'='1' OR title LIKE '` で `owner_id` 条件を外して他人の全ノートの id/title を得る。`UNION SELECT id, body FROM notes --` などで他人の本文や他テーブルの読み出しもできる（`current_user.id` の埋め込みも同じ書き方） — `db.execute("SELECT id, title FROM notes WHERE owner_id = ? AND title LIKE ?", (current_user.id, f"%{q}%"))` のようにパラメータ化クエリにする
[CRITICAL] D1:12 — 認可漏れ（IDOR）: `WHERE id = ?` だけで所有者を絞っておらず、要件の「自分のノート」に反して他人のノートを返す。攻撃者 → 入口 → 得るもの: 任意のログイン利用者 → `GET /notes/<note_id>` の連番 ID を総当たり → 他人のノートの title・body・owner_id — `WHERE id = ? AND owner_id = ?` に `current_user.id` を渡し、一致しなければ既存の `abort(404)` に落とす（他人のノートの存在も漏らさない）

### INFO
[INFO] D1:18-20 — 未確認: `_format_title` は task（検索・1 件取得）の範囲外の追加で、差分内に呼び出しが無い。「呼び出し元は create_note だけ」「title は入口で検証済み」はコメントの主張であり根拠にしない（呼び出し元の全数確認もスキーマの検証位置の引用もできていない） — 呼び出し元を Grep で全数確認し、0 件なら関数ごと削除、create_note から使うなら task 外の変更として別の差分に分ける
[INFO] D1:1-15 — 未確認: 差分にテストが含まれていない（Grep でリポジトリ全体の参照 0 件は確認していない） — 他人のノート ID への `GET /notes/<id>` が 404 になること、`'` や `%` を含む `q` で他人のノートが返らないこと、自分のノートだけが検索・取得できることのテストを足す

Blockers: 2

承認判定: Block（CRITICAL 2 件）
