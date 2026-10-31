"""上流 API を包む取得ファサードの部品（編集せずに使う）。

- `cursor`: 自己完結カーソル（ページング。用途と検索条件の取り違え・書き換えを拒否する）
- `shaping`: 入れ子を保った射影・配列の上限・文字数の予算・自由記述の無害化
- `catalog`: 項目カタログ（fields_describe と項目パスの検証）
- `upstream`: 上流の失敗の分類・モデル向けの文面・再試行・締切付きの呼び出し・ファンアウト
- `principal`: 呼び出しの主体の決定
- `errors`: モデルに読ませる失敗 `FacadeError`

上流の契約に上限がある・共有のリモート（複数の主体）のときは、upstream-protection-overlay の
`limits`・`breaker`・`signed_cursor`・`audit_log` をこのディレクトリへ足す。

標準ライブラリだけで書く（`mcp` を import しない）。SDK の無い環境でも検査できるように
するため。失敗は `FacadeError` で投げ、server.py が `ToolError` に変換して返す。
"""
