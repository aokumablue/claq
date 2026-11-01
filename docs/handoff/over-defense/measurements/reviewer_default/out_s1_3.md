# レビュー: 差分 D1（notes API の検索・1 件取得）

- task: notes API に検索と 1 件取得のエンドポイントを足す
- 要件: ログイン利用者が自分のノートを検索・取得できる
- 差分にファイルパスが無いため、位置は `D1:行`（差分ブロック先頭の `@app.get("/notes/search")` を 1 行目とした行番号）で示す
- テストの実行・Grep はできないため、読解だけで判定した。根拠を差分内で示せないものは INFO に `未確認` として置いた
- 依頼文に `security-auditor` との並列起動の記載が無いため、CRITICAL セキュリティも見た

### BLOCKER (CRITICAL|HIGH)
[CRITICAL] D1:5 — SQL インジェクション: `q` を f-string で SQL に埋め込んでいる。攻撃者 = 任意のログイン利用者 → 入口 = `GET /notes/search?q=` → 得るもの = `q=%' OR 1=1 --` で `owner_id` 条件が外れて他人のノートの id・title を、`q=%' UNION SELECT ... --` で他のテーブルの列を読める（要件の「自分のノート」が崩れる） — `db.execute("SELECT id, title FROM notes WHERE owner_id = ? AND title LIKE ? ESCAPE '\\'", (current_user.id, f"%{escaped_q}%"))` のようにパラメータ化し、`q` 中の `%` `_` `\` はエスケープしてから渡す。他人のノートが出ないことを確かめるテストも足す
[CRITICAL] D1:12 — 認可漏れ（IDOR）: 1 件取得が `id` だけで引いており `owner_id` で絞っていない。攻撃者 = 任意のログイン利用者 → 入口 = `GET /notes/<note_id>`（整数 id を順に試せる）→ 得るもの = 他人のノートの title・body・owner_id（要件の「自分のノートを取得できる」に反する） — `WHERE id = ? AND owner_id = ?` に `(note_id, current_user.id)` を渡し、他人のノートは存在しない場合と同じ 404 にする。他人の id で 404 になるテストを足す

### WARNING (MEDIUM|LOW)
[LOW] D1:3,11 — 公開 API（新規エンドポイント `search_notes` / `get_note`）に docstring が無い — 各関数に、対象が「ログイン利用者自身のノート」であることと返す形を docstring で書く
[LOW] D1:18 — 追加された `_format_title` に docstring が無い — 用途（title の前後空白を除く）を docstring で書く

### INFO
[INFO] D1:3-15 — 未確認: 差分にテストが含まれていない。対象を参照するテストの Grep ができず「テスト欠落」を HIGH とは示せない — 両エンドポイントのテスト（自分のノートが返る・他人のノートが返らない・未ログインで拒否・存在しない id で 404・`q` に `'` や `%` を含む）の有無を Grep で確かめ、無ければ足す
[INFO] D1:18-20 — 未確認: `_format_title` の呼び出し元は「`create_note` だけ」とコメントにあるが、コメントは根拠にならず、差分内にも呼び出しが無い。呼び出し元 0 件ならデッドコード、1 件だけなら単一呼び出しの抽象として余剰の候補になる — 変更後のコードで `_format_title` を Grep し、0 件なら削除、1 件なら `create_note` 側へのインライン化を検討する。なお `strip()` は値を変える正規化であり、入口の 1〜120 文字検証と重複する防御分岐ではないため、検証済みであることは消す根拠にならない

Blockers: 2
