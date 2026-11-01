Read ツールで `/home/user/claq/plugins/claq/agents/reviewer.md` を読み、その reviewer として振る舞ってください。task は「notes API に検索と 1 件取得のエンドポイントを足す」、要件は「ログイン利用者が自分のノートを検索・取得できる」です。次の差分をレビューし、定義が求める形式で指摘を出してください（テストの実行はできないので、読解で判定する）。

## 差分 D1（Flask の notes API に足された変更）

```python
@app.get("/notes/search")
@login_required
def search_notes():
    q = request.args.get("q", "")
    rows = db.execute(f"SELECT id, title FROM notes WHERE owner_id = {current_user.id} AND title LIKE '%{q}%'").fetchall()
    return jsonify([dict(row) for row in rows])


@app.get("/notes/<int:note_id>")
@login_required
def get_note(note_id: int):
    row = db.execute("SELECT id, title, body, owner_id FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        abort(404)
    return jsonify(dict(row))


def _format_title(title: str) -> str:
    # 呼び出し元は create_note だけで、create_note は入口のスキーマで title を 1〜120 文字に検証済み
    return title.strip()
```

回答は Markdown で書き、Write ツールで `{OUT}` に保存してください。実在のリポジトリのファイルは、指定した定義ファイル以外は読まないでください。保存したら「保存しました」とだけ返してください。
