監査対象: 差分 D1（Flask notes API に追加された `search_notes` / `get_note` / `_format_title`。ファイルパスが渡されていないため、行番号は差分ブロック先頭を 1 とした相対行）

### BLOCKER (CRITICAL|HIGH)
[CRITICAL] 差分D1:10 — ログイン済みの任意ユーザー（ネットワーク上の client）→ `GET /notes/search?q=` → `q` が f-string で SQL に連結されるため、`q=%' UNION SELECT id, body FROM notes -- ` のような入力で `owner_id` 条件を抜け、他ユーザーを含む全ノートの本文（DB 権限次第で他テーブル）を読み出す — パラメータ化クエリに変え、`current_user.id` と `q` の両方をプレースホルダで渡す。未計測: 確かめ方は別ユーザーのノートを作成した状態で上記 `q` を送り、他人の `body` が返ることを確認する
[CRITICAL] 差分D1:17 — ログイン済みの任意ユーザー（他ユーザー）→ `GET /notes/<int:note_id>` → `WHERE id = ?` だけで所有者を照合していないため、連番の `note_id` を列挙して他ユーザーのノートの `title` / `body` / `owner_id` を読み出す（IDOR）— 同じアプリが `search_notes` で使っている所有者条件 `owner_id = current_user.id` を WHERE に加え、他人のノートは存在有無を漏らさないよう 404 を返す。未計測: 確かめ方はユーザー A でノートを作り、ユーザー B でログインしてその id を GET し 200 と本文が返ることを確認する

安全なコード例（D1:10 と D1:17。実装は /bugfix に委ねる）:

```python
@app.get("/notes/search")
@login_required
def search_notes():
    """ログインユーザー自身のノートをタイトルの部分一致で検索する。"""
    q = request.args.get("q", "")
    rows = db.execute(
        "SELECT id, title FROM notes WHERE owner_id = ? AND title LIKE ?",
        (current_user.id, f"%{q}%"),
    ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.get("/notes/<int:note_id>")
@login_required
def get_note(note_id: int):
    """ログインユーザーが所有するノートを 1 件返す。他人のノートは 404。"""
    row = db.execute(
        "SELECT id, title, body, owner_id FROM notes WHERE id = ? AND owner_id = ?",
        (note_id, current_user.id),
    ).fetchone()
    if row is None:
        abort(404)
    return jsonify(dict(row))
```

### INFO
[INFO] 差分D1:9 — パラメータ化後も `q` 内の `%` / `_` は LIKE のワイルドカードとして働くが、検索範囲は自分のノートに限られるため攻撃者が得るものは無い — 対応不要
[INFO] 差分D1:23-25 — `_format_title` は `strip()` のみで、この関数単体に攻撃者が得るものは無い。呼び出し元 `create_note` と入口スキーマの検証はスコープ外・既存（コメントの「検証済み」は根拠に使っておらず、確認もしていない）— 対応不要

CRITICAL: 2 / HIGH: 0
Blockers: 2
