# ADR-0009: roots pointer resolver は owner/mode 検証を行わない（PID 開始時刻照合は撤回）

**日付**: 2026-08-20（初版）／2026-08-20 改訂  **ステータス**: accepted

## 改訂履歴（2026-08-20）

初版が「実装しない」とした 2 項目のうち、**PID 開始時刻（`lstart`）照合は
撤回し、実装した**。`docs/reports/PLUGIN_ROOT_RESOLVER_2026-08-20_V0.9.36_REVERIFICATION.md`
の H-02（祖先不一致時の root 値合意 fallback が別 host の root を採用しうる）
に対し、ユーザーから「リスクがあるとみなせる fallback は望ましくない、
完璧に対応したい」との明示指示があり、推測 fallback を排除した厳格な
祖先 proof 方式へ作り直した（ADR-0008 改訂 2）。この新方式は祖先 PID の
再利用を lstart 照合で検出することが前提になるため、当初「コストが
見合わない」としていた判断そのものを覆した。旧「1. PID 開始時刻照合を
実装しない理由」節は撤回の記録として残し、取り消し線的な注記を付ける。
「2. owner/mode 検証を実装しない理由」節は変更なしで、`docs/reports/
PLUGIN_ROOT_RESOLVER_2026-08-20_V0.9.36_REVERIFICATION.md` の H-01
再検証でも同じ結論を維持した（再検証での追加知見をこの節へ追記した）。

## コンテキスト（初版時点のもの。上記改訂履歴を参照）

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

**pointer file の owner/mode 検証は実装しない。PID の「開始時刻」照合は
（下記のとおり撤回し）実装済み。**

### 1. 〔撤回済み〕PID 開始時刻照合を実装しない理由（初版の記録）

初版はこの節で「実装しない」と判断していたが、ADR-0008 改訂 2 で撤回し
実装した。当時の判断とその後の展開を、訂正の透明性のためそのまま残す。

> `launcher.py:main()` は **全 hook 起動**（`PreToolUse`/`SessionStart`/
> `SessionEnd`/`PreCompact`）のたびに `write_env_pointer()` を呼び、対応する
> `roots/<pid>` を無条件で上書きする。Bash tool の呼び出しは必ず
> `PreToolUse` を経由するため、**resolver がポインタを読む直前に、必ず
> 同じ PID への書き込みが起きている。** したがって「別プロセスに再利用された
> 古い PID のポインタを誤って正規のものとして読む」という事象は、
> GC（liveness+TTL 判定）を待つまでもなく、次の hook 起動で自己修復する。
>
> 開始時刻の記録・照合を実装するには、writer 側で `ps -o lstart=`（または
> `/proc/<pid>/stat` 相当。macOS には `/proc` が無いため移植性のある手段が
> `ps` 呼び出しに限られる）を追加で呼び、resolver 側（POSIX sh）でも
> 同等の照合ロジックを持つ必要がある。得られる効果（前段落の自己修復機構が
> 既にカバーしている PID 再利用対策の補強）に対して、bootstrap 1 回ごとの
> `ps` 呼び出し増加とプラットフォーム間パース差異という複雑さが不釣り合いに
> 大きい。

**撤回の経緯**: 上記の分析自体は誤っていなかった — 「直接の PPID だけを
書く」設計であれば、開始時刻照合の限界効用は小さかった。しかし
ADR-0008 改訂 2 で writer が「祖先チェーン全体」に書く設計へ変わり、
かつ「祖先で解決できなければ推測せず 127」という厳格化（H-02 対応、
ユーザー指示）を採用したことで前提が変わった: 祖先チェーンは launcher
自身の PID より寿命の長いプロセス（host バイナリ、ログインシェル等）を
含むため、PID 再利用の実害が大きくなる（TTL 7 日の間、誤った祖先に
一致してしまう可能性期間が長い）。厳格化した以上、その正確性の根拠を
「次の hook 起動での自己修復」という弱い保証だけに置くのは一貫しない。
`lstart` は 1 回の `ps -eo pid=,ppid=,lstart=` で祖先チェーンと同時に
取得でき（実測 10〜16ms）、追加の `ps` 呼び出しは発生しない — 「移植性の
複雑さ」という当初の懸念は、実装時に `LC_ALL=C` を明示することで
解消した（`ps -o lstart=` の出力がロケール依存だったことは実機で発見し、
ADR-0008 に記録済み）。

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

**再検証での追加知見（`docs/reports/PLUGIN_ROOT_RESOLVER_2026-08-20_V0.9.36_REVERIFICATION.md`
H-01）**: `roots/` を汚染できたとしても、影響範囲は enforcement hook には
及ばない。`hooks.json` の全 7 エントリ（`block_no_verify` /
`pre_bash_commit_quality` / `bash_config_protection` / `config_protection` /
`pre_compact` / `mem.cli context` / `mem.cli handoff`）を実際に確認したところ、
いずれも `python3 launcher.py <module>` を直接呼ぶだけで `env.sh` を
source しない。つまり resolver（`env.sh`/`roots/`）は保護 hook の実行経路に
一切登場せず、`roots/` の汚染で保護 hook を無効化することはできない。
影響は md/agents/skills から呼ばれる `bluecore_run`/`bluecore_mem_learn`
呼び出しの汚染（H-01 のシナリオそのもの）に限定される。同レポートは
「manifest/hash による root の真正性検証」も提案していたが、この検証も
同一 OS ユーザーの書き込み権限があれば偽装できるため（manifest ファイルも
helpers.sh も同じ権限で自由に作成できる）、真正性検証としては機能しない
——検討したが採用しなかった（本 ADR 冒頭の「決定」で述べたとおり、
owner/mode/manifest/hash はいずれも同一ユーザー内では防御にならない）。

## 検討した代替案

### 代替案 1: レポート Phase 2/3 の受入条件をそのまま全面採用する

初版時点での評価（PID 開始時刻照合を含む）。**PID 開始時刻照合は
その後 ADR-0008 改訂 2 で採用したため、以下は owner/mode 検証にのみ
現在も当てはまる。**

- 長所: レポートが要求する検証をすべて満たす。
- 短所: 前述のとおり、両者とも「シェルコード注入」という質的に重大な
  リスクではなく、「別ユーザーは関与しない前提の下での誤 path 選択」
  という限定的なリスクへの対処になる。実装コスト（`ps` 呼び出しの追加、
  `stat` フォーマットの機種依存パース）に対して得られる安全性の増分が
  小さい。
- 却下理由（owner/mode のみ）: ADR-0002 の脅威モデル（同一 OS ユーザーの
  敵対的回避は非対象）の外側にある残余リスクへ、`stat` フォーマットの
  機種依存パースという複雑さを追加で払う判断は割に合わない
  （PID 開始時刻照合は、既に祖先チェーン計算で 1 回呼んでいる `ps` に
  相乗りできたため、この却下理由が当てはまらなくなった）。

### 代替案 2: 何もしない（現状維持のまま R-01/R-03 の修正だけで終える）

- 長所: 実装コストがゼロ。
- 短所: レポートの Phase 2/3 が指摘した項目を検討すらしなかった記録に
  なり、次回監査で同じ指摘が繰り返される。
- 却下理由: 検討はしたが採用しない、という判断とその根拠を明示的に
  記録するほうが、単に沈黙するより将来の監査・引き継ぎに資する。

## 結果

### 肯定的

- owner/mode 検証を実装しないことで、resolver に `stat` の機種依存パースを
  持ち込まずに済んだ。
- PID 開始時刻照合は撤回して実装したが、既存の祖先チェーン計算
  （`ps -eo pid=,ppid=,lstart=` 1 回）に相乗りできたため、追加の `ps`
  呼び出しコストはゼロだった（ADR-0008 参照）。

### 否定的

- なし（owner/mode は意図的な非対応。PID 開始時刻照合は撤回済み）。

### リスク

- owner/mode 検証を行わないため、`~/.bluecore` の権限が何らかの理由で
  0700 から緩んだ場合（手動変更、バックアップ復元時の権限崩れ等）、
  同一 OS ユーザー内の他プロセスが pointer を書き換えられる。これは
  ADR-0002 の脅威モデル外だが、`~/.bluecore` の権限監視自体は
  `_ensure_private_dir` が毎回 `chmod(0o700)` で締め直す形で緩和して
  いる（能動的な検証ではなく、書き込みのたびの再強制）。PID 再利用への
  対処（撤回して実装した `lstart` 照合の残余リスク）は ADR-0008 側に
  記録した。
