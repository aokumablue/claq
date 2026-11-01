# 反復2 generate（修正専用）

依頼文の基準: 根拠（攻撃者 → 入口 → 得るもの、または再現手順）が書かれた blocker は、その箇所を最小の変更で直す。新しい仕組み（依存・ミドルウェア・設定項目・検証層）が要る blocker と、続行を決めた文の無い続行経路（握り潰し・既定値・再試行）は直さず「提案」へ回す。

## blocker ごとの判定

### 1. `search_notes` の SQL インジェクション → 直す

- 根拠: 攻撃者 ログイン済みの他ユーザー → 入口 `GET /notes/search?q=` → 得るもの 他人のノートの読み取り。再現: `q=' OR '1'='1` を送ると、SQL が `... AND title LIKE '%' OR '1'='1%'` となり、`OR` の優先順位で `owner_id` 条件が外れて全ユーザーのノートが返る
- 根本原因: `q` と `current_user.id` を f-string で SQL 文字列へ埋め込んでおり、入力が SQL 構文として解釈される
- 最小の変更: プレースホルダでバインドする（`%` の連結は値側で行う）

```python
@app.get("/notes/search")
@login_required
def search_notes():
    q = request.args.get("q", "")
    rows = db.execute(
        "SELECT id, title FROM notes WHERE owner_id = ? AND title LIKE ?",
        (current_user.id, f"%{q}%"),
    ).fetchall()
    return jsonify([dict(row) for row in rows])
```

- 補足: `q` に含まれる `%` / `_` は LIKE のワイルドカードとして働くが、`owner_id = ?` で本人のノートに限定されるため他人のノートには届かない。根拠（攻撃者・得るもの）が無いのでエスケープは足さない

### 2. `get_note` の所有者検査漏れ → 直す

- 根拠: 攻撃者 ログイン済みの他ユーザー → 入口 `GET /notes/<id>` → 得るもの 他人のノートの読み取り。再現: ユーザー A が作ったノートの id を、ユーザー B のセッションで `GET /notes/{id}` すると 200 で `body` まで返る
- 根本原因: `id` だけで行を引き、`owner_id` を `current_user.id` と照合していない
- 最小の変更: WHERE 句に `owner_id = ?` を足す。他人のノートは存在しない id と同じ 404 にし、id の存在有無も漏らさない

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

### 3. 検索 API にレート制限が無い → 「提案」へ回す

- 理由: 根拠が「ベストプラクティス」だけで、攻撃者 → 入口 → 得るもの、要件の文、再現できる失敗のいずれも書かれていない。加えて、レート制限は新しい仕組み（Flask-Limiter 等の依存・ミドルウェア・上限値の設定項目）を要するため、反復2 の最小修正の範囲外
- 根本原因: 該当なし（要件に書かれた振る舞いの崩れが示されていない）

### 4. DB 呼び出しに指数バックオフの再試行 → 「提案」へ回す

- 理由: 失敗の後に続行する経路（再試行）であり、続行を決めた文（要件・利用者の指示・外部 API 契約）が引用されていない。再現できる失敗（一時的な DB エラーで要件の振る舞いが崩れる手順）も示されていない。現状は DB 例外がそのまま呼び出し元へ伝わり（Flask が 500 を返す）、これが既定の方針どおり
- 根本原因: 該当なし

### 対象外

- `_format_title`: blocker に含まれていないため触れない（反復2 はスコープを広げない）

## 回帰テスト（tdd-writer として追加するもの）

```python
def test_search_notes_does_not_leak_other_users_notes_via_injection(client, login, make_note):
    """q に SQL を注入しても他ユーザーのノートが返らないこと。"""
    make_note(owner="alice", title="alice secret")
    login("bob")
    res = client.get("/notes/search", query_string={"q": "' OR '1'='1"})
    assert res.status_code == 200
    assert res.get_json() == []


def test_search_notes_returns_own_matching_notes(client, login, make_note):
    """本人のノートは部分一致で返ること。"""
    note = make_note(owner="bob", title="shopping list")
    login("bob")
    res = client.get("/notes/search", query_string={"q": "shop"})
    assert res.get_json() == [{"id": note.id, "title": "shopping list"}]


def test_get_note_of_other_user_returns_404(client, login, make_note):
    """他ユーザーのノートは存在しない id と同じく 404 になること。"""
    note = make_note(owner="alice", title="t", body="secret")
    login("bob")
    assert client.get(f"/notes/{note.id}").status_code == 404


def test_get_note_of_own_returns_body(client, login, make_note):
    """本人のノートは body まで返ること。"""
    note = make_note(owner="bob", title="t", body="mine")
    login("bob")
    res = client.get(f"/notes/{note.id}")
    assert res.status_code == 200
    assert res.get_json()["body"] == "mine"
```

フィクスチャ（`client` / `login` / `make_note`）は対象リポジトリの既存 conftest に合わせる前提で、名前は仮置き。

## 自己検証

- 未検証: 対象の Flask アプリは実在のリポジトリに無く、本セッションではテストと linter を実行していない。PASS/FAIL は報告しない

## 報告

### 根本原因（反復履歴の rootcause 用）

- B1-1: `search_notes` が `q` と `current_user.id` を f-string で SQL に埋め込み、入力が SQL 構文として解釈される → プレースホルダでバインドして解消
- B1-2: `get_note` が `id` だけで行を引き、`owner_id` を照合していない → WHERE に `owner_id = ?` を足して解消
- B1-3 / B1-4: 根本原因なし（根拠の無い指摘のため提案へ）

### 直した blocker

1. `search_notes` の SQL インジェクション（パラメータ化）
2. `get_note` の所有者検査漏れ（`owner_id` 条件の追加、他人のノートは 404）

### 提案（採否は利用者が決める。既定では実施しない。未解消として数える）

1. 検索 API のレート制限: 新しい依存・ミドルウェア・設定項目が要り、攻撃者・得るもの・要件の文が示されていない。濫用の具体的な被害（例: 負荷による他ユーザーへの影響）や上限値の要件が決まれば別タスクで検討する
2. DB 呼び出しの指数バックオフ再試行: 続行を決めた文が無い続行経路。一時障害時に再試行する要件や DB ドライバの契約が示されれば別タスクで検討する。現状は失敗をそのまま呼び出し元へ伝える

未解消 blocker 数: 2（提案へ回した B1-3・B1-4）
