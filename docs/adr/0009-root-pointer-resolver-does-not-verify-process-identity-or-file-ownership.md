# ADR-0009: roots pointer resolver は PID 開始時刻照合と owner/mode 検証を行わない

**日付**: 2026-08-20  **ステータス**: accepted

## コンテキスト

`docs/reports/PLUGIN_ROOT_RESOLVER_2026-08-20_VERIFICATION.md`（R-01〜R-06）の
Phase 2/3 は、次の 2 点を release gate の受入条件として求めていた。

1. pointer record に writer が観測した host PID と「process start identity」
   （プロセス開始時刻等）を保存し、resolver が実際の ancestor process と
   照合する（PID 再利用対策）。
2. `~/.bluecore`・`roots/`・pointer file を `lstat` で regular file /
   expected owner / non-group-writable / non-world-writable と検証し、
   symlink・ownership 不正・unexpected type は fail-closed とする。

ADR-0008（改訂）で roots pointer は shell script からデータファイル
（絶対パス 1 行のみ、`read` でのみ取り出す）へ変更済みであり、R-01/R-03
（任意 shell 実行）は既に解消している。本 ADR は、上記 2 点を**今回は
実装しない**という判断とその理由を記録する。

## 決定

**PID の「開始時刻」照合と、pointer file の owner/mode 検証は実装しない。**

### 1. PID 開始時刻照合を実装しない理由

`launcher.py:main()` は **全 hook 起動**（`PreToolUse`/`SessionStart`/
`SessionEnd`/`PreCompact`）のたびに `write_env_pointer()` を呼び、対応する
`roots/<pid>` を無条件で上書きする。Bash tool の呼び出しは必ず
`PreToolUse` を経由するため、**resolver がポインタを読む直前に、必ず
同じ PID への書き込みが起きている。** したがって「別プロセスに再利用された
古い PID のポインタを誤って正規のものとして読む」という事象は、
GC（ADR-0008 記載の liveness+TTL 判定）を待つまでもなく、次の hook 起動で
自己修復する。

開始時刻の記録・照合を実装するには、writer 側で `ps -o lstart=`（または
`/proc/<pid>/stat` 相当。macOS には `/proc` が無いため移植性のある手段が
`ps` 呼び出しに限られる）を追加で呼び、resolver 側（POSIX sh）でも
同等の照合ロジックを持つ必要がある。得られる効果（前段落の自己修復機構が
既にカバーしている PID 再利用対策の補強）に対して、bootstrap 1 回ごとの
`ps` 呼び出し増加とプラットフォーム間パース差異という複雑さが不釣り合いに
大きい。

### 2. owner/mode 検証を実装しない理由

roots pointer が「実行されるコード」から「読まれるデータ」に変わったことで
（ADR-0008 改訂）、pointer file への書き込み権限を持つ攻撃者が得られるのは
**任意コード実行ではなく、誤った root path を resolver に読ませること**
まで縮小した。誤った root path の結果は「存在しないパスなら 127」
「存在するが `runtime/bluecore-helpers.sh` を欠くパスなら 127」
「別の（同一ユーザーが書き込み可能な）bluecore install を source する」
のいずれかであり、シェルコード注入は既に構造的に不可能になっている。

`~/.bluecore` はディレクトリとして 0700（`_ensure_private_dir`）に締めて
おり、同一 OS ユーザー内の他プロセスからの書き込みは元々 ADR-0002 が
定義する脅威モデルの範囲外（同一 OS ユーザーの敵対的回避は非対象）である。
POSIX sh には移植性のある `stat` フォーマット文字列が無く（macOS の `stat -f`
と GNU coreutils の `stat -c` は非互換）、owner/mode 検証を resolver 側で
行うには外部コマンドの出力パースをさらに増やす必要があり、縮小済みの
残余リスクに対してコストが見合わない。

## 検討した代替案

### 代替案 1: レポート Phase 2/3 の受入条件をそのまま全面採用する

- 長所: レポートが要求する検証をすべて満たす。
- 短所: 前述のとおり、両者とも「シェルコード注入」という質的に重大な
  リスクではなく、「別ユーザーは関与しない前提の下での誤 path 選択」
  という限定的なリスクへの対処になる。実装コスト（`ps` 呼び出しの追加、
  `stat` フォーマットの機種依存パース）に対して得られる安全性の増分が
  小さい。
- 却下理由: ADR-0002 の脅威モデル（同一 OS ユーザーの敵対的回避は非対象）
  の外側にある残余リスクへ、bootstrap のたびに外部コマンドを追加で
  呼ぶコストを払う判断は割に合わない。

### 代替案 2: 何もしない（現状維持のまま R-01/R-03 の修正だけで終える）

- 長所: 実装コストがゼロ。
- 短所: レポートの Phase 2/3 が指摘した項目を検討すらしなかった記録に
  なり、次回監査で同じ指摘が繰り返される。
- 却下理由: 検討はしたが採用しない、という判断とその根拠を明示的に
  記録するほうが、単に沈黙するより将来の監査・引き継ぎに資する。

## 結果

### 肯定的

- 実装・保守コストを増やさずに済んだ。

### 否定的

- なし（意図的な非対応）。

### リスク

- PID 再利用対策は「次の hook 起動での自己修復」と GC の liveness+TTL 判定
  に依存する。hook が長時間（TTL である 7 日を超えて）まったく起動されない
  期間があり、かつその間に該当 PID が別プロセスに再利用された場合、次の
  hook 起動まではその誤ったポインタが `roots/` に残る可能性がある。
  ただし resolver 自身は毎回「自分の $PPID または祖先」のポインタしか
  直接には使わないため、この残留が実害になるのは「候補ちょうど 1 本」
  規則を経由する場合に限られる。
- owner/mode 検証を行わないため、`~/.bluecore` の権限が何らかの理由で
  0700 から緩んだ場合（手動変更、バックアップ復元時の権限崩れ等）、
  同一 OS ユーザー内の他プロセスが pointer を書き換えられる。これは
  ADR-0002 の脅威モデル外だが、`~/.bluecore` の権限監視自体は
  `_ensure_private_dir` が毎回 `chmod(0o700)` で締め直す形で緩和して
  いる（能動的な検証ではなく、書き込みのたびの再強制）。
