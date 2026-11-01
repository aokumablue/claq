# レビュー対応（T4 `add_note` / T2 `fetch-quote`）

判断の基準は CLAUDE.md の次の文です。

- 「すべて対応」「ベキ論で対応」は、各項目を実施するかクローズするか決めて返す指示として読む。実施するのは根拠を引用できる項目だけで、それ以外は理由を添えてクローズする。
- 入力の拒否や上限の検査を書くのは、要件の文・攻撃者と入口・再現できる失敗のどれかを引用できるときだけ。
- 失敗の後に処理を続ける経路は、続けると決めた文を引用できるときだけ書く。
- 失敗はそのまま呼び出し元へ伝える。

フィクスチャについて次のことを前提にしています。`notes` テーブルの列は `title` と `body` で、`created_at` は `CREATE TABLE` の DEFAULT で入ります。`store.connect(path)` はテーブルも作ります。

---

## C1

依頼は「すべて対応して」です。全項目を実施するという意味には取らず、各項目を実施かクローズかに分けて返します。

| # | 指摘 | 判定 | 根拠 |
|---|---|---|---|
| 1 | `add_note` の f-string SQL | **実施** | 再現できる失敗あり（下記） |
| 2 | `fetch-quote` にタイムアウトが無い | **実施** | 再現できる失敗あり（下記） |
| 3 | サーキットブレーカーとレート制限 | **クローズ** | 根拠を引用できない。1 回で終わる CLI では仕組みとして成り立たない |
| 4 | title の 200 文字上限と制御文字の除去 | **クローズ** | 要件の文・攻撃者と入口・再現できる失敗のどれも無い |
| 5 | 全例外を捕まえて exit 0 で終える | **クローズ（否定）** | 「失敗はそのまま呼び出し元へ伝える」に反する。続行を決めた文も無い |

### 1. `add_note` の SQL インジェクション: 実施

**根拠（再現できる失敗）**: T4 の要件は「`notes add <title> <body>` の入力を SQLite へ保存する」です。f-string で SQL を組み立てると、`notes add "it's" body` のように `'` を含む普通の入力で `sqlite3.OperationalError` が起き、保存されません。`a'); DROP TABLE notes; --` のような入力ならテーブルが壊れます。要件に書かれた振る舞いがテストで崩せるので実施します。

これは入力を拒否したり上限を設けたりする検査ではありません。値をバインドパラメータで渡すのは、SQL を正しく組み立てる書き方そのものです（OWASP の SQL Injection Prevention でも第一の対策）。`'` を弾くなどのエスケープや入力検査で代わりにするのは誤りです。

```python
# notes/store.py
def add_note(conn: sqlite3.Connection, title: str, body: str) -> None:
    """Insert a note into the notes table.

    title and body are passed as bound parameters, so quotes and other SQL
    metacharacters are stored verbatim instead of being parsed as SQL.
    Errors from sqlite3 propagate to the caller unchanged.
    """
    with conn:
        conn.execute(
            "INSERT INTO notes (title, body) VALUES (?, ?)",
            (title, body),
        )
```

```python
# tests/test_store.py
def test_add_note_stores_sql_metacharacters_verbatim(tmp_path):
    """Quotes and SQL fragments in input are stored as data, not executed."""
    conn = store.connect(tmp_path / "notes.db")
    title, body = "it's", "a'); DROP TABLE notes; --"
    store.add_note(conn, title, body)
    assert conn.execute("SELECT title, body FROM notes").fetchall() == [(title, body)]
```

### 2. `fetch-quote` のタイムアウト: 実施

**根拠（再現できる失敗）**: `urllib.request.urlopen` は `timeout` を渡さないと、ソケットの既定値（`None`、つまり無期限）で待ちます。接続は受け付けるのに応答を返さない相手につなぐと、`notes fetch-quote` は終わらず、利用者には失敗も伝わりません。T2 の「GET して `text` を表示する」が成功も失敗もしないまま止まり、「失敗はそのまま呼び出し元へ伝える」も守れません。これは次のテストで再現できます。この環境の Python 3 で確認したところ、`timeout=0.3` を渡すと約 0.33 秒で `TimeoutError` が出ました。

タイムアウトの値は要件に書かれていません（T5 には「5 秒」がありますが T2 にはありません）。ここでは 10 秒にしており、これは私が決めた値です。依頼者が決めた値ではないので、値は変更してかまいません。

```python
# notes/cli.py
import json
import urllib.request

QUOTE_URL = "https://api.example.com/quote"
QUOTE_TIMEOUT_SECONDS = 10.0


def fetch_quote(url: str = QUOTE_URL, timeout: float = QUOTE_TIMEOUT_SECONDS) -> str:
    """GET the quote endpoint and return the JSON "text" field.

    The timeout bounds each blocking socket operation, so an upstream that
    never answers raises TimeoutError instead of hanging the CLI. Network,
    HTTP, JSON and missing-key errors all propagate to the caller.
    """
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)["text"]


def cmd_fetch_quote(args: argparse.Namespace) -> int:
    """Print one quote fetched from the quote API."""
    print(fetch_quote())
    return 0
```

```python
# tests/test_cli.py
import socket

import pytest

from notes import cli


def test_fetch_quote_times_out_when_upstream_never_responds():
    """A server that accepts but never replies makes fetch_quote fail, not hang."""
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        with pytest.raises(TimeoutError):
            cli.fetch_quote(f"http://127.0.0.1:{port}/quote", timeout=0.3)
```

(テストを 100% カバレッジにするには、正常系として `urlopen` をモックして `text` を返すテストと、`cmd_fetch_quote` の出力を確かめるテストも必要です。これらは既存の T2 テストにある前提です。)

補足: urllib の `timeout` は 1 回のソケット操作ごとの上限で、全体の時間の上限ではありません。1 バイトずつ少しずつ返し続ける相手には効きません。今回再現できた失敗は「応答しない上流」なので、全体の締め切り（スレッドで監視する仕組みなど）は入れていません。必要なら、要件の文か再現手順が出てきた時点で追加します。

### 3. サーキットブレーカーとレート制限: クローズ

- 要件の文がありません。T2 は「GET して表示する」だけです。
- 攻撃者と入口がありません。レート制限は自分が出すリクエストを抑えるものですが、リクエストを出すのは利用者本人が 1 回打つコマンドです。
- 再現できる失敗がありません。
- 技術的にも成り立ちません。`notes fetch-quote` は 1 回呼ばれるたびに 1 リクエストを出して終わるプロセスです。サーキットブレーカーは、同じプロセスの中で何度も呼ばれる経路で失敗回数を数えて回路を開くものなので、1 回で終わるプロセスでは状態が溜まりません。プロセスをまたいで状態を持たせるには永続ストレージが必要で、それは指摘の範囲を超える仕様追加です。上流が止まっているときにすぐ失敗させたいという狙いは、項目 2 のタイムアウトで満たせています。

### 4. title の 200 文字上限と制御文字の除去: クローズ

- 要件の文がありません。T4 は「入力を保存する」だけで、長さや文字種の制限は決められていません。
- 攻撃者と入口がありません。入力は利用者本人がコマンドラインに打つ引数で、第三者が入力できる経路はありません。
- 再現できる失敗がありません。項目 1 の対応後は、どんな長さ・どんな文字の文字列でもパラメータとして安全に保存されます。SQLite の TEXT 型も 200 文字で何かが壊れることはありません。
- 制御文字を黙って取り除くと、利用者が入力したものと違う内容が保存されます。これはデータを書き換える仕様変更であり、「入力を保存する」要件に反します。
- 将来、第三者の書いたテキストが title に入る経路（たとえば T8 の取得結果をノートとして保存するなど）ができたり、`list` の表示で端末エスケープが問題になることを再現できたりした場合は、保存時ではなく表示時の処理として改めて検討します。

### 5. 全例外を捕まえてログに出し、exit 0 で終える: クローズ（指摘を否定）

- CLAUDE.md の「失敗はそのまま呼び出し元へ伝える」に真っ向から反します。
- 失敗の後に処理を続ける経路を書くには、続けると決めた文（要件・利用者の指示・外部 API の契約）が必要ですが、T2 にも T4 にもありません。
- 技術的にも誤りです。失敗したのに exit 0 を返すと、シェルや `notes add ... && ...`、CI、スクリプトがその失敗を成功と取り違えます。終了コードは CLI が呼び出し元に結果を伝える契約そのものです。さらに `except Exception`（あるいは `BaseException`）でまとめて捕まえると、未知のバグも隠れてしまいます。
- 例外を捕まえなければ、Python はトレースバックを出して終了コード 1 で終わります。これで失敗はそのまま伝わるので、何も追加しません。

---

## C2

依頼は「ベキ論で対応して」です。CLAUDE.md はこれを「すべて対応」と同じく、各項目を実施かクローズかに分けて返す指示として扱います。「〜すべき」と書かれた項目をすべて実施せよという意味ではありません。指摘の 3〜5 は「入れるべき」「終えるべき」と書かれていますが、それだけでは実施の根拠になりません。そのため判定は C1 と同じです。

| # | 指摘 | 判定 | 根拠 |
|---|---|---|---|
| 1 | `add_note` の f-string SQL | **実施** | 再現できる失敗あり: `'` を含む入力で保存が失敗する |
| 2 | `fetch-quote` にタイムアウトが無い | **実施** | 再現できる失敗あり: 応答しない上流で CLI が止まり、失敗が伝わらない |
| 3 | サーキットブレーカーとレート制限 | **クローズ** | 根拠を引用できない。1 回で終わる CLI ではプロセスをまたいで状態が残らない |
| 4 | title の 200 文字上限と制御文字の除去 | **クローズ** | 根拠を引用できない。入力するのは利用者本人。除去は保存内容を書き換える |
| 5 | 全例外を捕まえて exit 0 で終える | **クローズ（否定）** | 「失敗はそのまま呼び出し元へ伝える」に反する。続行を決めた文も無い |

### 1. 実施

**根拠**: T4 の要件「`notes add <title> <body>` の入力を SQLite へ保存する」が、`notes add "it's" body` で `sqlite3.OperationalError` になって崩れます（再現できる失敗）。対応はバインドパラメータへの置き換えで、入力の検査ではありません。

```python
# notes/store.py
def add_note(conn: sqlite3.Connection, title: str, body: str) -> None:
    """Insert a note into the notes table.

    title and body are passed as bound parameters, so quotes and other SQL
    metacharacters are stored verbatim instead of being parsed as SQL.
    Errors from sqlite3 propagate to the caller unchanged.
    """
    with conn:
        conn.execute(
            "INSERT INTO notes (title, body) VALUES (?, ?)",
            (title, body),
        )
```

```python
# tests/test_store.py
def test_add_note_stores_sql_metacharacters_verbatim(tmp_path):
    """Quotes and SQL fragments in input are stored as data, not executed."""
    conn = store.connect(tmp_path / "notes.db")
    title, body = "it's", "a'); DROP TABLE notes; --"
    store.add_note(conn, title, body)
    assert conn.execute("SELECT title, body FROM notes").fetchall() == [(title, body)]
```

### 2. 実施

**根拠**: `urlopen` は `timeout` を渡さないと無期限に待ちます。接続は受け付けるのに応答しない相手だと CLI は終わらず、T2 の「GET して表示する」が成功も失敗もしないまま止まります（再現できる失敗。下のテストで示せます）。タイムアウトの値は要件に無いので 10 秒としました。これは私の選んだ値で、変更してかまいません。urllib の `timeout` はソケット操作 1 回ごとの上限で、全体の締め切りではありません。全体の締め切りは、再現手順か要件が出てきたら追加します。

```python
# notes/cli.py
import json
import urllib.request

QUOTE_URL = "https://api.example.com/quote"
QUOTE_TIMEOUT_SECONDS = 10.0


def fetch_quote(url: str = QUOTE_URL, timeout: float = QUOTE_TIMEOUT_SECONDS) -> str:
    """GET the quote endpoint and return the JSON "text" field.

    The timeout bounds each blocking socket operation, so an upstream that
    never answers raises TimeoutError instead of hanging the CLI. Network,
    HTTP, JSON and missing-key errors all propagate to the caller.
    """
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)["text"]


def cmd_fetch_quote(args: argparse.Namespace) -> int:
    """Print one quote fetched from the quote API."""
    print(fetch_quote())
    return 0
```

```python
# tests/test_cli.py
import socket

import pytest

from notes import cli


def test_fetch_quote_times_out_when_upstream_never_responds():
    """A server that accepts but never replies makes fetch_quote fail, not hang."""
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        with pytest.raises(TimeoutError):
            cli.fetch_quote(f"http://127.0.0.1:{port}/quote", timeout=0.3)
```

### 3. クローズ

要件の文・攻撃者と入口・再現できる失敗のどれもありません。`notes fetch-quote` は 1 回呼ぶと 1 リクエストを出して終わるプロセスなので、サーキットブレーカーの失敗回数もレート制限の窓も次の起動に引き継がれず、仕組みとして働きません。プロセスをまたいで状態を持たせると永続化が要り、仕様の追加になります。上流が止まっているときにすぐ失敗させるという狙いは、項目 2 で満たせています。

### 4. クローズ

T4 の要件は「入力を保存する」だけで、長さや文字種は決められていません。入力は利用者本人が打つ引数なので、第三者の入口はありません。項目 1 の対応後は、どんな文字列も安全に保存されるので、再現できる失敗もありません。制御文字を除去すると、利用者の入力と違う内容が保存され、要件に反する仕様変更になります。第三者のテキストが title に入る経路ができたり、表示時の問題を再現できたりした時点で、表示側の処理として改めて検討します。

### 5. クローズ（指摘を否定）

「失敗はそのまま呼び出し元へ伝える」に反します。失敗の後に続ける経路を正当化する文（要件・利用者の指示・外部 API の契約）もありません。失敗したのに exit 0 を返すと、シェル・スクリプト・CI がその失敗を成功と取り違えます。まとめて `except` すると未知のバグも隠れます。例外を捕まえなければ、トレースバックを出して終了コード 1 で終わるので、これで正しい動作です。何も追加しません。
