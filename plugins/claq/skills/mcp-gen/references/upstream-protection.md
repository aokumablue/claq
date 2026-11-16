# 上流の保護と署名付きカーソル（upstream-protection-overlay）

## 目次

- 重ねる条件
- 複製
- 上流の呼び出しを方針に通す
- 署名付きカーソル
- 監査ログ
- smoke の設定

## 重ねる条件

次のどちらかに当たるときに重ねる。当たらなければ既定の facade-overlay（再試行と締切だけ）のままにする。

- 上流の契約にレート制限（クォータ）や同時実行の上限が書かれている
- 共有のリモートサーバで、複数の主体が同じ上流を呼ぶ（他の主体のカーソルを開かせない・個人情報を返したことを記録する）

重ねる部品は、上流の契約の条項か共有のリモートという条件を、`server.py` のコメントに 1 行で名指して置く。

## 複製

facade-overlay を重ねた後に、部品を `facade/` へ足す（`__init__.py` は置き換えない）。

```bash
cp /abs/skill/mcp-gen/assets/upstream-protection-overlay/facade/limits.py /abs/path/to/dest/facade/limits.py
cp /abs/skill/mcp-gen/assets/upstream-protection-overlay/facade/breaker.py /abs/path/to/dest/facade/breaker.py
cp /abs/skill/mcp-gen/assets/upstream-protection-overlay/facade/signed_cursor.py /abs/path/to/dest/facade/signed_cursor.py
cp /abs/skill/mcp-gen/assets/upstream-protection-overlay/facade/audit_log.py /abs/path/to/dest/facade/audit_log.py
```

| モジュール | 部品 |
|---|---|
| `limits` | `Throttled`（サーバ自身の絞り。429 として扱い、再試行も遮断器への計上もしない）、`UpstreamQuota`（上流ごとのトークンバケット）、`ConcurrencyLimit`（上流ごと・主体ごとの同時実行の上限）、`SingleFlight`（同じ要求の合流）、`UpstreamPolicy`（上流ごとの方針。遮断器に、渡された部品だけを重ねる） |
| `breaker` | `CircuitBreaker`（続けて失敗する上流をしばらく呼ばない）、`call_upstream`（遮断器の内側で `call_with_deadline` を呼ぶ） |
| `signed_cursor` | `CursorCodec`（HMAC の署名・有効期限・主体への結び付け）、`SignedPageCursor`（位置の検査は既定の `accepts_position`） |
| `audit_log` | `AuditLog`（個人情報の項目を返したこととカーソルの拒否を残す。主体は鍵付きの要約で残す） |

## 上流の呼び出しを方針に通す

`UpstreamPolicy` には、条件に当たる部品だけを渡す（当たらない部品は引数ごと書かない。契約に無い値で絞ると、
正当な呼び出しが `Throttled` で失敗する）。遮断器は常に入る（上流が落ちている間、再試行が呼び出しを 3 倍にして
上流の契約の枠を使い切るのを止めるため）。遮断器が上流を呼ばない期間は次の 2 通りで決まる。

- 上流が `retry_after` を示したら、回数を待たずにその時刻まで呼ばない（Retry-After は上流が次の要求までの待ちを指定する契約。
  adapter が上流の Retry-After を `UpstreamError(..., retry_after=秒)` に写した値を使う）
- 示さない一時的な失敗は、再試行を使い切った呼び出しが 5 回続いたら 30 秒呼ばない。5 回と 30 秒は契約から導けない判断の値なので、
  上流の契約か利用者の指示に回復の確認の間隔が書かれているときだけ、その値に変える

| 渡す部品 | 渡す条件 | 値の決め方 |
|---|---|---|
| `quota=UpstreamQuota(...)` | 上流の契約にレート制限（クォータ）がある | 回復の速さは契約の値、貯められる上限（burst）は契約の 1 秒ぶんの回数にする。契約より低い値にするのは、余裕の幅が契約か利用者の指示に書かれているときだけ（自分で決めた幅で絞ると、契約が許す呼び出しが `Throttled` で失敗する） |
| `concurrency=ConcurrencyLimit(...)` | 上流の契約に同時実行の上限がある | 全主体の合計は契約の値にする。主体ごとの値は、`_principal()` が固定値を返す間（stdio・認証の無いループバック）は合計と同じにする（小さくすると、実効の同時実行が契約を下回る）。主体を検証済みのトークンから導くようにしたら、利用者が決めた値に下げる |
| `merge_same_requests=True` | 共有のリモート | 値は無い |

渡した `UPSTREAM_BURST`・`UPSTREAM_CONCURRENCY`・`PRINCIPAL_CONCURRENCY` が、smoke_check.py が 1 回の起動で送る
ツール呼び出しの数を下回ると、smoke が `Throttled` で落ちる。そのときも定数は契約の値のままにし、smoke の失敗として報告する。

```python
from facade.limits import ConcurrencyLimit, UpstreamPolicy, UpstreamQuota

UPSTREAM_RATE_PER_SECOND = 5.0  # 上流の契約: 毎秒 N 回（再試行も 1 回と数える）
UPSTREAM_BURST = 5  # 契約の 1 秒ぶんの回数
UPSTREAM_CONCURRENCY = 8  # 上流の契約: 同時 N 本（全主体の合計）
PRINCIPAL_CONCURRENCY = 8  # _principal() が固定値の間は合計と同じ

UPSTREAM_POLICIES = {
    "orders": UpstreamPolicy(
        quota=UpstreamQuota(UPSTREAM_RATE_PER_SECOND, UPSTREAM_BURST),
        concurrency=ConcurrencyLimit(UPSTREAM_CONCURRENCY, PRINCIPAL_CONCURRENCY),
        merge_same_requests=True,
    ),
}
```

`_upstream` は上流の名前と主体を受け取り、`UPSTREAM_POLICIES[name].call(method, arguments, principal=..., deadline=...)`
を返す形に替える。呼び出し元（`_search_orders`・`orders_get`・`_shipments`）は `_principal()` を 1 回だけ引いて渡す。

- 同時実行の上限（`concurrency`）は上流ごと（全主体の合計）と主体ごとの 2 系統で数え、空きが無ければ待たずに `Throttled` で失敗する
  （待つと締切を越え、締切超えが遮断器の失敗として数えられるため）
- 合流（`merge_same_requests=True`）は、同じ主体の同じ要求が同時に来たら 1 本にまとめる（`SingleFlight`。キーは主体・メソッド・引数）。adapter が引数の外の
  状態（資格情報の切り替えなど）を読むなら、それを引数か主体に含める。まとめるのは実行中の呼び出しだけで、終われば
  キーを外す。最後の待ち手が取り消されたら上流の呼び出しも取り消す
- 遮断器が数えるのは一時的な失敗だけ（404 は数えない）
- クォータ（`quota`）は再試行も試行ごとに数え、足りなければ待たずに `Throttled` で失敗する。再試行の途中で尽きたときも `Throttled`
  で返し、直前の上流の失敗は遮断器に数えない
- 共有のリモートでは、主体を検証済みのトークンから導く（固定の主体のまま公開すると、全利用者が 1 つの枠を共有し、
  `SingleFlight` が別の利用者の同じ要求をまとめて他人の結果を返す）

## 署名付きカーソル

既定の `PageCursor` を `SignedPageCursor` に替え、発行と開封に主体を渡す。

```python
import secrets

from facade.audit_log import AuditLog
from facade.signed_cursor import CursorCodec, SignedPageCursor

CURSOR_TTL_SECONDS = 900  # 検索ツールの説明と cursor 引数の説明に分で書く


def _cursor_secret() -> bytes:
    """全インスタンスで共有する 32 バイト以上の鍵（FACADE_CURSOR_SECRET）。"""
    return os.environ["FACADE_CURSOR_SECRET"].encode("utf-8")


SECRET = _cursor_secret()
AUDIT = AuditLog(SECRET, logger=logging.getLogger("audit"))
SEARCH_CURSOR = SignedPageCursor(
    CursorCodec(SECRET, ttl_seconds=CURSOR_TTL_SECONDS),
    scope="orders_search",
    audit=AUDIT,
    max_page_token_chars=MAX_PAGE_TOKEN_CHARS,
    max_cursor_chars=MAX_CURSOR_CHARS,
)
```

- 主体・検索条件の指紋・用途はカーソルの中身に入れず、署名の入力でだけ結びつける。改ざん・別の主体・別の条件・
  別のツールのカーソルは同じ文面で拒否する（他の主体のカーソルかどうかを漏らさない）
- 署名はするが暗号化はしない。位置に機微な値を入れない
- 複数インスタンスでは全インスタンスで同じ `FACADE_CURSOR_SECRET` を設定する

## 監査ログ

個人情報（`personal`）の項目を返したら、summary に落とした後の実際に返したパスで記録する。値は残さない。

```python
AUDIT.personal_returned("orders_get", CATALOG.personal_paths(returned), 1, principal=_principal)
```

## smoke の設定

署名付きカーソルにしたら、`smoke_config.py` の `SERVER_ENV` に実行ごとに作る鍵を渡す（固定の値を置くと、その値を
本番に流用したときに誰でもカーソルを作れる）。`SERVER_ENV` は smoke_check.py がサーバを起動するたびに環境変数へ足す
（鍵が無いとサーバは起動時に落ちる）。

```python
import secrets

SERVER_ENV: dict = {"FACADE_CURSOR_SECRET": secrets.token_hex(32)}
```
