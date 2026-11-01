監査対象: 差分 D1（Flask の notes API に足された `search_notes` / `get_note` / `_format_title`。ファイルパスが渡されていないため、行番号は差分 D1 内の行で示す）

入口:
- `GET /notes/search?q=`（`@login_required`）: クエリ文字列 `q`
- `GET /notes/<int:note_id>`（`@login_required`）: パス引数 `note_id`
- `_format_title(title)`: 外部由来の `title` を受けるが、HTTP の入口はスコープ外の `create_note`

攻撃者: どちらのルートもログイン済みであれば届くため、自分でアカウントを作れる、または持っているネットワーク上の client（＝他ユーザー）が届く。

### BLOCKER (CRITICAL|HIGH)
[CRITICAL] D1:10 — ログイン済みの他ユーザー → `GET /notes/search?q=` → f-string で `q` を SQL に連結しているため、`q=' OR 1=1 --` で `owner_id` の条件を外し全ユーザーのノート title を読み、`' UNION SELECT ...` で `body` や他テーブル（例: users の認証情報）まで読み出す — パラメータ化クエリに変え、`owner_id` も含めて値はプレースホルダで渡す。未計測: `db.execute` が sqlite3 等の素の DB-API である前提（ラッパーが自前でエスケープしていないかはスコープ外）。確かめ方は 2 アカウントを作り、片方で `q=%27%20OR%201%3D1%20--%20` を投げて他方のノートが返るかを見る
[CRITICAL] D1:17 — ログイン済みの他ユーザー → `GET /notes/<note_id>` → `WHERE id = ?` だけで持ち主を確かめていないため、連番の `note_id` を列挙して他人のノートの `title` / `body` / `owner_id` をすべて読む（IDOR） — 同じアプリの既存の認可条件（`search_notes` と同じ `owner_id = current_user.id`）をクエリに足し、他人のノートは存在しないものと同じ 404 を返す。未計測: スコープ内に所有者検査を行う別の層（デコレータ・行レベルセキュリティ）は見当たらない。確かめ方はユーザー A のノート ID をユーザー B のセッションで取得し 200 が返るかを見る

安全なコード例（D1:10）:

```python
@app.get("/notes/search")
@login_required
def search_notes():
    q = request.args.get("q", "")
    pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = db.execute(
        "SELECT id, title FROM notes WHERE owner_id = ? AND title LIKE ? ESCAPE '\\'",
        (current_user.id, pattern),
    ).fetchall()
    return jsonify([dict(row) for row in rows])
```

安全なコード例（D1:17）:

```python
@app.get("/notes/<int:note_id>")
@login_required
def get_note(note_id: int):
    row = db.execute(
        "SELECT id, title, body, owner_id FROM notes WHERE id = ? AND owner_id = ?",
        (note_id, current_user.id),
    ).fetchone()
    if row is None:
        abort(404)
    return jsonify(dict(row))
```

実装は /bugfix に委ねる。

### WARNING (MEDIUM|LOW)
[LOW] D1:9 — ハードニング: `q` に長さの上限が無く、`%` `_` を含む値でワイルドカード検索になる（パラメータ化後は自分のノートの範囲に閉じるため得るものは書けない） — 提案: `q` の長さ上限と LIKE 用のワイルドカードのエスケープ（上の例に含めた `ESCAPE` 処理）。採否は利用者

### INFO
[INFO] D1:23-25 — `_format_title` は `title.strip()` を返すだけで SQL・シェル・出力などの危険な注入先に渡していないため、この関数単体で攻撃者が得るものは無い。コメントの「入口のスキーマで 1〜120 文字に検証済み」は対象コード内の記述であり根拠に使っていない。検証の有無と `title` の書込み・出力経路は `create_note` 側でスコープ外・既存 — 対応不要（`create_note` を監査する場合はスコープに含めて渡す）
[INFO] D1:11, D1:20 — `jsonify` は `application/json` で返すため、title / body をそのまま返しても このレスポンス単体では XSS にならない。title / body を HTML に描画するフロントエンドはスコープ外 — 対応不要

CRITICAL: 2 / HIGH: 0
Blockers: 2
