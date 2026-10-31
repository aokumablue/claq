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
| `limits` | `Throttled`（サーバ自身の絞り。429 として扱い、再試行も遮断器への計上もしない）、`UpstreamQuota`（上流ごとのトークンバケット）、`ConcurrencyLimit`（上流ごと・主体ごとの同時実行の上限）、`SingleFlight`（同じ要求の合流）、`UpstreamPolicy`（上流ごとの方針） |
| `breaker` | `CircuitBreaker`（続けて失敗する上流をしばらく呼ばない）、`call_upstream`（遮断器の内側で `call_with_deadline` を呼ぶ） |
| `signed_cursor` | `CursorCodec`（HMAC の署名・有効期限・主体への結び付け）、`SignedPageCursor`（位置の検査は既定の `accepts_position`） |
| `audit_log` | `AuditLog`（個人情報の項目を返したこととカーソルの拒否を残す。主体は鍵付きの要約で残す） |

## 上流の呼び出しを方針に通す

上限は上流の契約より低くする（上流の 429 より先に絞る）。`UPSTREAM_BURST`・`UPSTREAM_CONCURRENCY`・
`PRINCIPAL_CONCURRENCY` は、smoke_check.py が 1 回の起動で送るツール呼び出しの数以上にする。

```python
from facade.limits import UpstreamPolicy

UPSTREAM_RATE_PER_SECOND = 5.0  # 上流の契約: 毎秒 N 回（再試行も 1 回と数える）
UPSTREAM_BURST = 10
UPSTREAM_CONCURRENCY = 8  # 全主体の合計
PRINCIPAL_CONCURRENCY = 4  # 1 つの主体が上流の枠を使い切らないように

UPSTREAM_POLICIES = {
    "orders": UpstreamPolicy(
        rate_per_second=UPSTREAM_RATE_PER_SECOND,
        burst=UPSTREAM_BURST,
        max_concurrent=UPSTREAM_CONCURRENCY,
        max_concurrent_per_principal=PRINCIPAL_CONCURRENCY,
    ),
}
```

`_upstream` は上流の名前と主体を受け取り、`UPSTREAM_POLICIES[name].call(method, arguments, principal=..., deadline=...)`
を返す形に替える。呼び出し元（`_search_orders`・`orders_get`・`_shipments`）は `_principal()` を 1 回だけ引いて渡す。

- 同時実行の上限は上流ごと（全主体の合計）と主体ごとの 2 系統で数え、空きが無ければ待たずに `Throttled` で失敗する
  （待つと締切を越え、締切超えが遮断器の失敗として数えられるため）
- 同じ主体の同じ要求が同時に来たら 1 本にまとめる（`SingleFlight`。キーは主体・メソッド・引数）。adapter が引数の外の
  状態（資格情報の切り替えなど）を読むなら、それを引数か主体に含める。まとめるのは実行中の呼び出しだけで、終われば
  キーを外す。最後の待ち手が取り消されたら上流の呼び出しも取り消す
- 遮断器が数えるのは一時的な失敗だけ（404 は数えない）
- 再試行もクォータを試行ごとに数え、足りなければ待たずに `Throttled` で失敗する。再試行の途中で尽きたときも `Throttled`
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

署名付きカーソルにしたら、`smoke_config.py` の `PAGED_ENV` に実行ごとに作る鍵を渡す（固定の値を置くと、その値を
本番に流用したときに誰でもカーソルを作れる）。

```python
import secrets

PAGED_ENV: dict = {"FACADE_CURSOR_SECRET": secrets.token_hex(32)}
```
