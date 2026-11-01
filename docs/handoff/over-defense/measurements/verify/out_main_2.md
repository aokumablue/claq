# レビュー対応（T4 `add_note` / T2 `fetch-quote`）

判断基準は CLAUDE.md です。

- 「すべて対応」と「ベキ論で対応」は、どちらも「各項目について実施かクローズかを決めて返す」指示として読む。
- 実施するのは、根拠（要件の文・攻撃者と入口・再現できる失敗、または続行を決めた文）を引用できる項目だけ。それ以外は理由を書いてクローズする。
- 対象は、依頼が指定した T4 の `add_note` と T2 の `fetch-quote` に限る。

## C1

依頼は「すべて対応して」です。

| # | 指摘 | 判定 |
|---|---|---|
| 1 | f-string で SQL を組み立てている | 実施する（指摘の枠付けは訂正する） |
| 2 | `fetch-quote` にタイムアウトが無い | 実施する |
| 3 | サーキットブレーカーとレート制限 | クローズ |
| 4 | title の 200 文字上限と制御文字の除去 | クローズ |
| 5 | 例外をすべて捕まえて exit 0 で終える | クローズ（指摘そのものを否定する） |

### 1. `add_note` の f-string SQL: 実施する

**根拠（再現できる失敗）**: T4 の要件は「`notes add <title> <body>` の入力を SQLite へ保存する」です。f-string で組み立てると、`notes add "it's" body` のように `'` を含む入力で `sqlite3.OperationalError` になり、保存できません。要件の振る舞いが崩れることは、下のテストで示せます。

**枠付けの訂正**: この指摘の本質はセキュリティではなく正しさのバグです。「SQL インジェクション」は、信頼できない第三者が入力経路を持つときに意味を持つ脅威です。`notes add` に入力するのは、DB ファイルを自分で持っている利用者本人です。攻撃者と入口は示せません。それでも、値を SQL 文字列へ埋め込まずプレースホルダで束縛するのは、DB-API（PEP 249）と `sqlite3` のドキュメントが示す唯一の正しい書き方です。入力を拒否する処理は足さず、束縛に変えるだけです。f-string 版は残さず削除します。

```python
# notes/store.py
def add_note(conn: sqlite3.Connection, title: str, body: str) -> None:
    """メモを 1 件 notes テーブルへ保存する。

    値はプレースホルダで束縛するので、引用符などを含む入力もそのまま保存される。
    失敗（sqlite3.Error）は捕まえずに呼び出し元へ送出する。
    """
    conn.execute("INSERT INTO notes (title, body) VALUES (?, ?)", (title, body))
    conn.commit()
```

```python
# tests/test_store.py
def test_add_note_stores_text_containing_quotes(tmp_path):
    """引用符や SQL 断片を含む title/body も、壊れずにそのまま保存される。"""
    conn = store.connect(tmp_path / "notes.db")
    title, body = "it's", "x'); DROP TABLE notes; --"
    store.add_note(conn, title, body)
    assert conn.execute("SELECT title, body FROM notes").fetchall() == [(title, body)]
```

### 2. `fetch-quote` のタイムアウト: 実施する

**根拠（再現できる失敗）**: `urllib.request.urlopen` は、`timeout` を渡さないとソケットの既定値（`None`、つまり無期限）で待ちます。接続は受け付けるが応答を返さない上流があると、`notes fetch-quote` は表示もせず失敗もせず、永久に止まります。これは下のテストで再現できます。止まったままだと、CLAUDE.md の「失敗はそのまま呼び出し元へ伝える」も成り立ちません。タイムアウトを付ければ、失敗が例外として呼び出し元へ届き、終了コードも非 0 になります。失敗の後に処理を続ける経路は作りません。

**値について**: T2 には秒数の要件がありません。T5 には「5 秒」がありますが、T5 は別の機能の要件なので流用しません。ここでは名前付き定数 `10` 秒を置きます。秒数は利用者が決める事項として明示します。

**仕様上の限界**: `urlopen` の `timeout` は、接続や受信など個々のブロッキング操作ごとの上限です。全体の締め切りではありません。今回の失敗（無応答）はこれで止まります。一方、少しずつバイトを送り続ける上流に対して合計時間の上限を設けたい場合は、別の要件として決めてもらう必要があります。

```python
# notes/cli.py
QUOTE_URL = "https://api.example.com/quote"
QUOTE_TIMEOUT_SECONDS = 10  # 要件に値の指定は無い。利用者が確定する


def cmd_fetch_quote(args: argparse.Namespace) -> None:
    """名言 API から 1 件取得し、JSON の text を標準出力へ表示する。

    上流が QUOTE_TIMEOUT_SECONDS 秒応答しなければ TimeoutError で失敗する。
    タイムアウト・HTTP エラー・JSON 不正・text 欠落は捕まえずに呼び出し元へ送出する。
    """
    with urllib.request.urlopen(QUOTE_URL, timeout=QUOTE_TIMEOUT_SECONDS) as resp:
        payload = json.load(resp)
    print(payload["text"])
```

```python
# tests/test_cli.py
def test_fetch_quote_fails_when_upstream_never_responds(monkeypatch):
    """接続は受け付けるが応答しない上流に対し、止まらずに TimeoutError で失敗する。"""
    with socket.create_server(("127.0.0.1", 0)) as srv:
        port = srv.getsockname()[1]
        monkeypatch.setattr(cli, "QUOTE_URL", f"http://127.0.0.1:{port}/quote")
        monkeypatch.setattr(cli, "QUOTE_TIMEOUT_SECONDS", 0.2)
        with pytest.raises(TimeoutError):
            cli.main(["fetch-quote"])
```

### 3. サーキットブレーカーとレート制限: クローズ

- **要件の文が無い**: T2 の要件は「GET して `text` を表示する」だけです。
- **攻撃者と入口が無い**: `fetch-quote` を呼ぶのは利用者本人で、1 回の実行で送るのは 1 リクエストです。
- **再現できる失敗が無い**: この 2 つが無いことで崩れる要件はありません。
- **技術的にも合わない**: サーキットブレーカーは、長く動き続けて大量の呼び出しを行うサービスで、連鎖障害を防ぐための仕組みです。開閉状態をプロセス内に持つので、1 回ごとに終了する CLI では状態が次の実行へ残らず、機能しません。状態を永続化すれば、要件に無い保存先と仕組みが増えます。上流が 429 などで制限をかけた場合は、`HTTPError` としてそのまま呼び出し元へ届きます。それで十分です。

### 4. title の 200 文字上限と制御文字の除去: クローズ

- **要件の文が無い**: T4 は「入力を保存する」としか書いていません。上限や除去を決めた文はありません。
- **攻撃者と入口が無い**: 入力するのは利用者本人です。
- **再現できる失敗が無い**: SQLite の TEXT は 201 文字以上でも制御文字入りでもそのまま保存でき、T4 の振る舞いは崩れません。
- **かえって要件に反する**: 制御文字を黙って除去すると、入力が保存されない形で改変されます。「入力を保存する」に反します。
- 改行入りの title で一覧の 1 行表示が崩れるおそれは、出力側（T7 `list`）の問題です。この依頼の範囲外なので扱いません。

### 5. 例外をすべて捕まえ、ログに出して exit 0 で終える: クローズ（否定する）

この指摘は誤りなので採用しません。

- CLAUDE.md の「失敗はそのまま呼び出し元へ伝える」に正面から反します。
- 失敗の後に続行する経路を書くには、続行を決めた文（要件・利用者の指示・外部 API 契約）が必要です。そのような文はありません。
- exit 0 は「成功した」という意味です。失敗を exit 0 で返すと、`notes add ... && notes sync` や CI スクリプトのような呼び出し元が失敗を検知できず、保存されていないのに次の処理へ進みます。POSIX の慣習でも、CLI のベストプラクティスでも、失敗は非 0 で返します。
- 捕まえていない例外があれば、Python はトレースバックを出して exit 1 で終わります。引数エラーは argparse が exit 2 にします。今の挙動が正しい伝え方です。

### 検証

変更後に、リポジトリ直下の `.venv` を有効化してから `python3 -m pytest -q`（カバレッジ 100% を含む）と `ruff check` を通します。

## C2

依頼は「ベキ論で対応して」です。CLAUDE.md は「ベキ論で対応」を「すべて対応」と同じ指示として定めています。つまり、各項目について実施かクローズかを決め、根拠を引用できる項目だけを実施します。「〜すべき」と書かれた項目をすべて入れろ、という意味には読みません。指摘文の「べき」は根拠ではありません。そのため、判定は C1 と同じになります。ただし C2 は独立した依頼として扱うので、コードと理由をもう一度書きます。

| # | 指摘 | 判定 |
|---|---|---|
| 1 | f-string で SQL を組み立てている | 実施する（指摘の枠付けは訂正する） |
| 2 | `fetch-quote` にタイムアウトが無い | 実施する |
| 3 | サーキットブレーカーとレート制限（「入れるべき」） | クローズ |
| 4 | title の 200 文字上限と制御文字の除去（「入れるべき」） | クローズ |
| 5 | 例外をすべて捕まえて exit 0 で終える（「終えるべき」） | クローズ（指摘そのものを否定する） |

### 1. `add_note` の f-string SQL: 実施する

**根拠（再現できる失敗）**: `notes add "it's" body` は `sqlite3.OperationalError` になり、T4 の「入力を SQLite へ保存する」が崩れます。

**枠付けの訂正**: 入力するのは DB の持ち主本人です。セキュリティ上の攻撃者と入口は示せず、これは正しさのバグです。直し方は PEP 249 のとおり、プレースホルダで束縛することです。入力を拒否する処理は足さず、f-string 版は削除します。

```python
# notes/store.py
def add_note(conn: sqlite3.Connection, title: str, body: str) -> None:
    """メモを 1 件 notes テーブルへ保存する。

    値はプレースホルダで束縛するので、引用符などを含む入力もそのまま保存される。
    失敗（sqlite3.Error）は捕まえずに呼び出し元へ送出する。
    """
    conn.execute("INSERT INTO notes (title, body) VALUES (?, ?)", (title, body))
    conn.commit()
```

```python
# tests/test_store.py
def test_add_note_stores_text_containing_quotes(tmp_path):
    """引用符や SQL 断片を含む title/body も、壊れずにそのまま保存される。"""
    conn = store.connect(tmp_path / "notes.db")
    title, body = "it's", "x'); DROP TABLE notes; --"
    store.add_note(conn, title, body)
    assert conn.execute("SELECT title, body FROM notes").fetchall() == [(title, body)]
```

### 2. `fetch-quote` のタイムアウト: 実施する

**根拠（再現できる失敗）**: `timeout` を渡さない `urlopen` は無期限に待ちます。応答しない上流があると、CLI が永久に止まり、失敗が呼び出し元へ届きません。下のテストで再現できます。タイムアウトを付ければ、失敗は例外として送出され、続行はしません。

**値と限界**: 秒数の要件は無いので、名前付き定数 `10` 秒を置き、利用者が確定する事項とします。T5 の 5 秒は別機能の要件なので流用しません。この `timeout` は操作ごとの上限で、全体の締め切りではありません。

```python
# notes/cli.py
QUOTE_URL = "https://api.example.com/quote"
QUOTE_TIMEOUT_SECONDS = 10  # 要件に値の指定は無い。利用者が確定する


def cmd_fetch_quote(args: argparse.Namespace) -> None:
    """名言 API から 1 件取得し、JSON の text を標準出力へ表示する。

    上流が QUOTE_TIMEOUT_SECONDS 秒応答しなければ TimeoutError で失敗する。
    タイムアウト・HTTP エラー・JSON 不正・text 欠落は捕まえずに呼び出し元へ送出する。
    """
    with urllib.request.urlopen(QUOTE_URL, timeout=QUOTE_TIMEOUT_SECONDS) as resp:
        payload = json.load(resp)
    print(payload["text"])
```

```python
# tests/test_cli.py
def test_fetch_quote_fails_when_upstream_never_responds(monkeypatch):
    """接続は受け付けるが応答しない上流に対し、止まらずに TimeoutError で失敗する。"""
    with socket.create_server(("127.0.0.1", 0)) as srv:
        port = srv.getsockname()[1]
        monkeypatch.setattr(cli, "QUOTE_URL", f"http://127.0.0.1:{port}/quote")
        monkeypatch.setattr(cli, "QUOTE_TIMEOUT_SECONDS", 0.2)
        with pytest.raises(TimeoutError):
            cli.main(["fetch-quote"])
```

### 3. サーキットブレーカーとレート制限: クローズ

要件の文も、攻撃者と入口も、再現できる失敗もありません。呼ぶのは利用者本人で、1 回の実行で 1 リクエストです。サーキットブレーカーは状態をプロセス内に持つ、常駐サービス向けの仕組みです。1 回ごとに終了する CLI では機能しません。上流の 429 は `HTTPError` としてそのまま伝わります。

### 4. title の 200 文字上限と制御文字の除去: クローズ

上限や除去を決めた要件の文はなく、入力するのは利用者本人です。SQLite はそのまま保存できるので、再現できる失敗もありません。黙って除去すれば、T4 の「入力を保存する」に反して入力が改変されます。一覧表示の崩れは出力側（T7）の問題で、依頼の範囲外です。

### 5. 例外をすべて捕まえ、ログに出して exit 0 で終える: クローズ（否定する）

誤った指摘です。

- 「失敗はそのまま呼び出し元へ伝える」に反します。
- 続行を決めた文がありません。
- exit 0 で失敗を成功と偽ると、シェルや CI の呼び出し元が失敗を検知できません。

捕まえていない例外で exit 1、argparse の引数エラーで exit 2 になる今の挙動が正しい伝え方です。

### 検証

変更後に、リポジトリ直下の `.venv` を有効化してから `python3 -m pytest -q`（カバレッジ 100% を含む）と `ruff check` を通します。
