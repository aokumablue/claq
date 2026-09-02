# ADR-0003: detached background（`--bg`）の親 exit は「起動受付」であり「処理成功」ではない

**日付**: 2026-08-18  **ステータス**: accepted

## コンテキスト

`launcher.py --bg` は `hook_common.detach_process` で子プロセスを
`start_new_session=True`（新しいセッション/プロセスグループ）で起動し、
親プロセスはすぐに終了する。主な用途は SessionEnd の `mem.cli handoff`
呼び出しで、ハーネスの hook timeout に阻まれず non-blocking に終了する
ことが目的。

監査は複数ラウンドにわたって同じ観測を報告している:

- v0.9.32 検証 §6.2: detached child の失敗はログに残るが、`--bg` 親は
  exit 0・無出力のまま。呼び出し側は処理成功と区別できない。
- v0.9.33 検証 A-05: 同じ観測が再提起された。`python3 launcher.py --bg
  nonexistent.module` を実行すると `parent exit=0 / stdout= / stderr=`。

これは実装の欠陥ではなく、`detach_process` の関数 docstring
（`hook_common.py`）が明記する意図的な非同期契約である。しかし監査
ツールは「エラーがあったのに exit 0」という表面だけを見ると欠陥に見える
ため、ラウンドを跨いで同じ指摘が繰り返される。

## 決定

**`--bg` 起動の親プロセスの exit code は「子プロセスの起動を受け付けた
か」だけを表し、「子プロセスの処理が成功したか」は表さない。** この契約は
変更しない。

その上で、処理結果を完全に見えなくするのではなく、次回セッションで
観測可能にする仕組みを既に実装済みとして維持する:

- `detach_process` は子の stdout/stderr を `~/.ple4/logs/
  bg-YYYY-MM-DD.log` へ追記する（`_detach_log_path`）。
- 子は `_WATCHDOG_SCRIPT` でラップされ、`DETACH_TIMEOUT_SECONDS`（600 秒）
  でハングした場合も確実に回収される。
- 次回 SessionStart の `mem context` で、`hook_common
  .recent_bg_failure_notice()` が前回のログファイル内容（正常系では空の
  はず）を検査し、内容があれば「起動受付後に何かが起きた」痕跡として
  1 行だけ通知する（`mem/cli.py:963 _bg_failure_section()`。失敗時のみ
  出力するため出力トークン最小化と両立する）。

## 検討した代替案

### 代替案 1: 親を子の完了まで待たせ、子の exit code をそのまま返す

- 長所: 呼び出し側が同期的に成功/失敗を知れる。
- 短所: `--bg` を使う理由そのもの（ハーネスの hook timeout に阻まれず
  non-blocking に終了する）が失われる。SessionEnd は同期処理に戻り、
  ハーネス側のタイムアウトで handoff 自体が失敗するリスクが再燃する。
- 却下理由: 目的と手段が矛盾する。

### 代替案 2: 起動受付 ID を発行し、ポーリング API で状態を問い合わせられるようにする

- 長所: 呼び出し側が任意のタイミングで成功/失敗を正確に把握できる。
- 短所: ID の永続化・状態遷移管理・ポーリング API という新しい可変状態と
  インターフェースが増える。ple4 のデータモデルは
  `repos`/`knowledge`/`sessions` の 3 テーブルのみという単純さを設計原則
  としており、非同期ジョブの状態管理を持ち込むと原則に反する。
- 却下理由: 「次回 SessionStart で failure ログの痕跡を通知する」という
  既存の軽量な仕組み（§6.2 で実装済み）で十分に「見落とさない」目的を
  達成できており、追加の複雑さに見合わない。

## 結果

### 肯定的

- SessionEnd の handoff は引き続き non-blocking で、ハーネスの hook
  timeout に阻まれない。
- 失敗の痕跡は次回セッション開始時に人間可読の 1 行として提示され、
  「起動受付成功」と「処理成功」の混同による見落としリスクを緩和する。

### 否定的

- 親プロセスを直接呼び出す経路（テスト・手動実行）では、その場で
  成功/失敗を知ることはできない。ログファイルを別途参照する必要がある。

### リスク

- `recent_bg_failure_notice` は当日＋前日の 2 ファイルまでしか読まない
  （SessionStart の hook timeout との兼ね合い）。2 日以上前の失敗ログや、
  次回 SessionStart 自体が実行されないセッションでの失敗は通知されずに
  埋もれる。
