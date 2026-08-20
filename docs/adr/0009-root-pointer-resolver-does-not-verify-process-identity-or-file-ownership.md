# ADR-0009: roots pointer resolver は owner/mode 検証を行わない（PID 開始時刻照合は撤回）

**日付**: 2026-08-20（初版）／2026-08-20 改訂 ×2  **ステータス**: accepted

## 改訂履歴（2026-08-20）

**改訂 1**: 初版が「実装しない」とした 2 項目のうち、**PID 開始時刻
（`lstart`）照合は撤回し、実装した**。`docs/reports/
PLUGIN_ROOT_RESOLVER_2026-08-20_V0.9.36_REVERIFICATION.md` の H-02
（祖先不一致時の root 値合意 fallback が別 host の root を採用しうる）に
対し、ユーザーから「リスクがあるとみなせる fallback は望ましくない、
完璧に対応したい」との明示指示があり、推測 fallback を排除した厳格な
祖先 proof 方式へ作り直した（ADR-0008 改訂 2）。この新方式は祖先 PID の
再利用を lstart 照合で検出することが前提になるため、当初「コストが
見合わない」としていた判断そのものを覆した。旧「1. PID 開始時刻照合を
実装しない理由」節は撤回の記録として残し、取り消し線的な注記を付ける。
「2. owner/mode 検証を実装しない理由」節は変更なしで、`docs/reports/
PLUGIN_ROOT_RESOLVER_2026-08-20_V0.9.36_REVERIFICATION.md` の H-01
再検証でも同じ結論を維持した（再検証での追加知見をこの節へ追記した）。

**改訂 2（本改訂）**: 再々検証
`docs/reports/PLUGIN_ROOT_RESOLVER_2026-08-20_V0.9.37_REVERIFICATION.md`
の H-01 が同じ owner/mode 非検証を再指摘した。追加調査で、「2. owner/mode
検証を実装しない理由」節が挙げていた補助論拠の一つ——「POSIX sh に
移植性のある `stat` フォーマットが無い」——は**owner/symlink 判定に
限れば誤りだった**ことが判明した: `test -O`（自分所有）/`test -h`
（symlink 判定）は POSIX sh ビルトインであり、`stat` の機種依存パース
なしで検証できる。macOS `/bin/sh` と `dash` の両方で実機動作を確認した。
一方、mode ビット（group/world-writable）の判定には POSIX test に該当
演算子が無く、`stat` が依然として必要——この部分は訂正されない。

いずれにせよこれは補助論拠の部分訂正であり**主論拠は健在で決定は
変わらない**:
`~/.bluecore/roots/` は 0700・同一 UID 所有であり、書き込めるのは同一 OS
ユーザーだけ（ADR-0002 の脅威モデルの外）。同一 UID の攻撃者は fake root を
「自分が所有する実ディレクトリ」として作れるため、`test -O`/`-h`/`-G` は
攻撃者自身が用意した fake root に対して全て真になる——検証を実装しても、
まさに防ぎたい攻撃者がその検証を満たしてしまう。「2. owner/mode 検証を
実装しない理由」節を、この訂正を反映して更新した。

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
自身の PID より寿命の長いプロセス（host バイナリ等）を含むため、PID
再利用の実害が大きくなる（TTL 7 日の間、誤った祖先に一致してしまう
可能性期間が長い）。厳格化した以上、その正確性の根拠を「次の hook
起動での自己修復」という弱い保証だけに置くのは一貫しない。`lstart` は
1 回の `ps -eo pid=,ppid=,lstart=` で祖先チェーンと同時に取得でき
（実測 10〜16ms）、追加の `ps` 呼び出しは発生しない — 「移植性の複雑さ」
という当初の懸念は、実装時に `LC_ALL=C` を明示することで解消した
（`ps -o lstart=` の出力がロケール依存だったことは実機で発見し、
ADR-0008 に記録済み）。ADR-0008 改訂 3 で祖先チェーンの深さを 2 段
（host インスタンス専有の PID のみ）へ縮小したが、`lstart` 照合の
必要性はこの縮小と無関係——PID 再利用は 2 段のどちらの PID でも
起こりうるため、開始時刻照合を撤回する理由には戻らない。

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

owner/mode 検証を実装しない理由の核心は**移植性ではなく実効性**にある:
resolver が読める入力（pointer file・その root が指すディレクトリ・
そこに置かれた helper）は、すべて同一 UID の攻撃者が自由に用意できる状態
でしかない。攻撃者は fake root を「自分が所有する実ディレクトリ」として
作成できるため、どんな検証を追加しても、まさに防ぎたい攻撃者がその検証を
満たしてしまう。これは `test -O`/`-h`/`-G`（owner/symlink 判定）でも
`stat` ベースの検証でも、`docs/reports/
PLUGIN_ROOT_RESOLVER_2026-08-20_V0.9.37_REVERIFICATION.md` H-01 が提案する
manifest/digest 方式（後述）でも変わらない——検証手段の実装コストの問題
ではなく、**検証すべき対象が「攻撃者と正規 writer が区別できない」という
原理的な限界**である。

〔訂正、範囲限定〕初版・改訂 1 時点では、この核心論拠に加えて「POSIX sh
には移植性のある `stat` フォーマット文字列が無く（macOS の `stat -f` と
GNU coreutils の `stat -c` は非互換）」という実装コスト面の補助論拠を
owner/mode 検証全般に対して併記していたが、これは**半分だけ誤りだった**。

- **owner・symlink 判定**（コンテキスト節が要求する「expected owner」
  「non-regular-file（symlink 等）」の部分）は、`test -O`（自分所有）・
  `test -h`（symlink 判定）という POSIX sh ビルトイン test 演算子で
  `stat` なしに実装できる。macOS の `/bin/sh` と `dash`（Linux でよく
  使われる `/bin/sh` の実体）の両方で実機動作を確認した——この部分の
  「移植性が無い」は誤りだった。
- **mode ビット判定**（コンテキスト節が要求する「non-group-writable」
  「non-world-writable」の部分）は、POSIX test に group/world-writable
  を判定する演算子が無い（`test -w` は「呼び出しユーザーが書けるか」
  であり、group/other の書き込みビットとは無関係——実機で `chmod 777`
  したディレクトリに対して `test -w` が true を返すことを確認した）。
  この部分を検証するには依然として `stat -f`/`stat -c` の機種依存
  パースが必要であり、「移植性が無い」という当初の論拠はここでは
  訂正されない。

**いずれにせよ決定（owner/mode 検証を実装しない）そのものは変わらない。**
上記の核心論拠（同一 UID 攻撃者は自分が所有し、自分のグループに属し、
どんな mode でも設定できる fake root を用意できるため、owner 検証も
symlink 検証も mode ビット検証も、実装できるかどうかに関わらず
無力）がそのまま適用されるため。

**H-01 は、ユーザーが表明した「推測 fallback を許容しない」の対象では
ない。** H-02（ADR-0008 改訂 2 で対応）は「複数の候補から、祖先である
証明のないまま推測で 1 つを選ぶ」構造であり、そこには「証明できる
候補だけを使う」というより厳格な代替が存在した。H-01 にはこの構造が無い
——resolver が読める入力そのものが同一 UID の攻撃者と正規 writer を
原理的に区別できないため、「より厳格な検証」という選択肢が存在しない。
したがって H-01 の非対応は「リスクの許容」ではなく「防御不能な対象への
対処の見送り」である。

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
`docs/reports/PLUGIN_ROOT_RESOLVER_2026-08-20_V0.9.37_REVERIFICATION.md`
の H-01 再指摘（同一の manifest/digest 方式を改めて提案）でも、この結論
（攻撃者は fake manifest/digest も同じ権限で書けるため機能しない）は
変わらない。

## 検討した代替案

### 代替案 1: レポート Phase 2/3 の受入条件をそのまま全面採用する

初版時点での評価（PID 開始時刻照合を含む）。**PID 開始時刻照合は
その後 ADR-0008 改訂 2 で採用したため、以下は owner/mode 検証にのみ
現在も当てはまる。**

- 長所: レポートが要求する検証をすべて満たす。
- 短所: 前述のとおり、両者とも「シェルコード注入」という質的に重大な
  リスクではなく、「別ユーザーは関与しない前提の下での誤 path 選択」
  という限定的なリスクへの対処になる。owner/mode 検証は
  `test -O`/`-h`/`-G` により実装コスト自体は低い（改訂 2 で訂正）が、
  同一 UID 攻撃者は検証対象そのものを自由に用意できるため、実装しても
  効果が無い。
- 却下理由（owner/mode のみ）: ADR-0002 の脅威モデル（同一 OS ユーザーの
  敵対的回避は非対象）の外側にある残余リスクに対し、実装コストの多寡に
  関わらず、検証手段そのものが同一 UID 攻撃者を区別できないため意味を
  なさない（PID 開始時刻照合は、既に祖先チェーン計算で 1 回呼んでいる
  `ps` に相乗りできたため、この却下理由が当てはまらなくなった）。

### 代替案 2: 何もしない（現状維持のまま R-01/R-03 の修正だけで終える）

- 長所: 実装コストがゼロ。
- 短所: レポートの Phase 2/3 が指摘した項目を検討すらしなかった記録に
  なり、次回監査で同じ指摘が繰り返される。
- 却下理由: 検討はしたが採用しない、という判断とその根拠を明示的に
  記録するほうが、単に沈黙するより将来の監査・引き継ぎに資する。

## 結果

### 肯定的

- owner/mode 検証を実装しないことで、resolver の分岐を増やさずに済んだ。
  `test -O`/`-h`/`-G` により実装コスト自体は低いことが改訂 2 で判明した
  が、同一 UID 攻撃者を区別できない以上、実装しても resolver の複雑さが
  増すだけで実効的な防御にはならない。
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
