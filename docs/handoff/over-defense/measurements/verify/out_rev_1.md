# レビュー: notes API 検索・1 件取得エンドポイント（差分 D1）

task: notes API に検索と 1 件取得のエンドポイントを足す
要件: ログイン利用者が自分のノートを検索・取得できる

差分にファイルパスが無いため、位置は `D1:行`（差分 D1 のコードブロック先頭を 1 行目とする）で示す。テストの実行と Grep はできないので、判定は読解による。

### BLOCKER (CRITICAL|HIGH)
[CRITICAL] D1:10 — SQLi: ログイン済みの任意の利用者 → `GET /notes/search?q=` の `q` が f-string で SQL に直接埋め込まれる → `q=%' UNION SELECT id, body FROM notes --` などで他人のノート本文を含む DB 全体を読める（`owner_id` 条件も外せる） — `db.execute("SELECT id, title FROM notes WHERE owner_id = ? AND title LIKE ? ESCAPE '\\'", (current_user.id, f"%{escaped_q}%"))` のようにパラメータ化し、`q` 内の `%` `_` `\` はエスケープして渡す
[CRITICAL] D1:17 — 認可漏れ（IDOR）: ログイン済みの任意の利用者 → `GET /notes/<note_id>` で連番の id を順に指定 → `WHERE id = ?` だけで所有者を見ないため、他人のノートの `title` / `body` / `owner_id` を取得できる。要件の「自分のノートを取得できる」に反する — `WHERE id = ? AND owner_id = ?` に `current_user.id` を渡し、他人のノートも 404 で返す（存在の有無を漏らさない）
[HIGH] D1:9 — 余剰: `request.args.get("q", "")` は `q` が無いときに既定値 `""` で続行し、`LIKE '%%'` で自分の全ノートを返す。この続行を決めた文は要件（「自分のノートを検索・取得できる」）に無い — `request.args["q"]` にして欠落時は 400 で呼び出し元へ伝える。全件返却を意図するなら、それを決めた要件の文を引用する

### WARNING (MEDIUM|LOW)
[LOW] D1:8, D1:16 — 公開 API（`search_notes` / `get_note`）に docstring が無く、パラメータ `q` の意味と 404 の条件が読めない — 各エンドポイントに docstring を付ける

### INFO
[INFO] D1:6-20 — 未確認: 差分に 2 エンドポイントのテストが含まれていないが、対象を参照するテストの Grep を実施できず「0 件」を示せない — 他人のノートの検索・取得が 0 件 / 404 になること、`q` に `'` `%` を含む入力、`q` 欠落時の挙動を検証するテストの有無を確かめ、無ければ追加する
[INFO] D1:23-25 — 未確認: `_format_title` の「呼び出し元は create_note だけ」「title は入口で 1〜120 文字に検証済み」はコメントの主張で、根拠にしない。呼び出し元の全数と入口スキーマを確認できていない。また差分内に呼び出し箇所が無い — 呼び出し元を Grep し、0 件なら今回足した未使用定義として削除、1 件なら呼び出し元 1 つの抽象としてインライン化を検討する（なお `strip()` は検証ではなく、入口検証後に空白のみの title を `""` に変えうる点も合わせて確認する）

Blockers: 3

判定: Block（CRITICAL 2 件・HIGH 1 件）
