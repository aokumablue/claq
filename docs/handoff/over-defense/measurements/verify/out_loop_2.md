# 反復2 generate（担当: `claq:tdd-writer`）

依頼文（反復2 手順 2）に従って振り分けた。根拠（攻撃者 → 入口 → 得るもの、または再現手順）が書かれた blocker は、その箇所を最小の変更で直す。新しい仕組み（依存・ミドルウェア・設定項目・検証層）が要る blocker と、続行を決めた文が無い続行経路（握り潰し・既定値・再試行）は直さずに「提案」へ回す。スコープは B1 の 4 件に限り、blocker に挙がっていない `_format_title` には手を付けない。

## 判定一覧

| # | blocker | 判定 | 判定の根拠 |
|---|---|---|---|
| 1 | `search_notes` の SQL インジェクション | **修正** | 根拠がある（ログイン済みの他ユーザー → `GET /notes/search?q=` → 他人のノートを読める）。既存の `db.execute` のプレースホルダで直せるので、新しい仕組みは要らない |
| 2 | `get_note` の所有者検査漏れ | **修正** | 根拠がある（ログイン済みの他ユーザー → `GET /notes/<id>` → 他人のノートを読める）。既存のクエリに条件を 1 つ足せば直り、新しい仕組みは要らない |
| 3 | 検索 API にレート制限が無い | **提案へ回す** | 根拠は「ベストプラクティス」だけで、攻撃者 → 入口 → 得るもの、再現手順、要件の文のどれも無い。直すには新しい仕組み（レート制限の依存かミドルウェアと、閾値の設定項目）が要る |
| 4 | DB 呼び出しに指数バックオフの再試行 | **提案へ回す** | 失敗の後も処理を続ける経路（再試行）を足す変更だが、続行を決めた文（要件・利用者の指示・外部 API 契約）が引用されていない。CLAUDE.md は「失敗はそのまま呼び出し元へ伝える」と定めている。再現できる失敗も示されていない |

## 根本原因（反復履歴の rootcause に記録する）

- B1: `q` を f-string で SQL 文字列へ直接埋め込んでおり、値とクエリ構造が分かれていない（プレースホルダを使っていない）
- B2: `get_note` の取得条件が `id` だけで、`owner_id = current_user.id` で絞っていない（`search_notes` にある所有者の条件が抜けている）
- B3: （提案へ回したので修正の対象外）根拠が一般論で、要件にも具体的な失敗にも結び付いていない
- B4: （提案へ回したので修正の対象外）続行経路を足す変更で、それを決めた文が無い

## 変更後のコード

### B1: `search_notes`

`q` をバインドパラメータで渡す。`owner_id` もプレースホルダで渡す（同じ行の変更で、f-string をやめれば自然にそうなる）。`LIKE` のパターンは値の側で `%...%` を組み立てる。

```python
@app.get("/notes/search")
@login_required
def search_notes():
    """ログイン中ユーザーが持つノートのうち、タイトルに q を含むものの id と title を返す。"""
    q = request.args.get("q", "")
    rows = db.execute(
        "SELECT id, title FROM notes WHERE owner_id = ? AND title LIKE ?",
        (current_user.id, f"%{q}%"),
    ).fetchall()
    return jsonify([dict(row) for row in rows])
```

補足: `q` に入った `%` と `_` は、今も `LIKE` のワイルドカードとして働く。ただし検索範囲は `owner_id = ?` で本人のノートに限られるので、他人のデータは読めない。blocker の対象外なので手を加えていない（変えるなら仕様の変更になる）。

### B2: `get_note`

取得条件に所有者を足す。他人のノートには、存在しないノートと同じ 404 を返す。403 と分けると、他人のノート ID が存在するかどうかが漏れるため。

```python
@app.get("/notes/<int:note_id>")
@login_required
def get_note(note_id: int):
    """ログイン中ユーザーが持つノートを 1 件返す。存在しないか他人のノートなら 404。"""
    row = db.execute(
        "SELECT id, title, body, owner_id FROM notes WHERE id = ? AND owner_id = ?",
        (note_id, current_user.id),
    ).fetchone()
    if row is None:
        abort(404)
    return jsonify(dict(row))
```

### 変更しない箇所

- `_format_title`: blocker に挙がっていない。反復2 の修正ではスコープを広げない

## TDD（先に書く red テスト）

修正の前に追加し、red になることを確かめるテスト。フィクスチャ名（`client`・`login_as`・`make_note`）は対象リポジトリの既存フィクスチャに合わせて読み替える。

```python
def test_search_notes_injection_does_not_leak_other_users_notes(client, login_as, make_note):
    """q に SQL を注入しても、他人のノートが検索結果に入らないこと。"""
    alice, bob = login_as.create("alice"), login_as.create("bob")
    make_note(owner=bob, title="bob-secret")
    login_as(alice)
    res = client.get("/notes/search", query_string={"q": "' OR '1'='1"})
    assert res.status_code == 200
    assert res.get_json() == []


def test_search_notes_returns_own_matching_notes(client, login_as, make_note):
    """本人のノートのうちタイトルが一致するものだけを返すこと。"""
    alice = login_as.create("alice")
    note = make_note(owner=alice, title="groceries")
    make_note(owner=alice, title="work")
    login_as(alice)
    res = client.get("/notes/search", query_string={"q": "groc"})
    assert res.get_json() == [{"id": note.id, "title": "groceries"}]


def test_get_note_of_other_user_returns_404(client, login_as, make_note):
    """他人のノート ID を指定すると 404 が返ること。"""
    alice, bob = login_as.create("alice"), login_as.create("bob")
    note = make_note(owner=bob, title="bob-secret")
    login_as(alice)
    assert client.get(f"/notes/{note.id}").status_code == 404


def test_get_note_of_owner_returns_note(client, login_as, make_note):
    """本人のノートは本文つきで返ること。"""
    alice = login_as.create("alice")
    note = make_note(owner=alice, title="t", body="b")
    login_as(alice)
    res = client.get(f"/notes/{note.id}")
    assert res.status_code == 200
    assert res.get_json()["body"] == "b"
```

## 自己検証

- テスト（`python3 -m pytest -q`）: **未検証**。依頼で対象リポジトリのファイルを読むことが禁じられており、テストを実行する環境も無いので、実行していない
- linter（`ruff check`）: **未検証**。理由は同じ
- 上の red テストは、修正前のコードで red、修正後で green になる想定で書いた。ただし実際に実行して確かめてはいない
- circuit breaker の照合に使える反復2 の自己検証シグネチャは無い（未実行）。反復2 の evaluate の前に、ループ側がテストを実行して確かめる必要がある

---

## 報告

### 根本原因

- B1: `search_notes` が `q` を f-string で SQL に直接埋め込んでいる（プレースホルダを使っていない）
- B2: `get_note` の取得条件に `owner_id = current_user.id` が無い

### 直した blocker

1. [BLOCKER] `search_notes` の SQL インジェクション: `q` と `owner_id` をバインドパラメータで渡すように変えた
2. [BLOCKER] `get_note` の所有者検査漏れ: `WHERE id = ? AND owner_id = ?` に変え、他人のノートには 404 を返す

### 提案（採否は利用者が決める。既定では実施しない。未解消の blocker として数え、Proposals に転記する）

3. 検索 API のレート制限: 根拠が「ベストプラクティス」だけで、攻撃者 → 入口 → 得るもの、再現手順、要件の文のどれも無い。導入するには依存かミドルウェアと閾値の設定項目という新しい仕組みが要る。濫用のシナリオ（例: 検索を大量に叩いて DB 負荷で可用性を落とす）と閾値を利用者が決めれば、導入を検討できる
4. DB 呼び出しへの指数バックオフ再試行: 失敗の後も続ける経路を足す変更だが、続行を決めた文（要件・利用者の指示・外部 API 契約）が無い。プロジェクトの方針も「失敗はそのまま呼び出し元へ伝える」である。一時的な DB 障害を再試行で吸収するという要件を利用者が決めたときに検討する

未解消の blocker: 2 件（#3・#4。どちらも提案へ回した）
