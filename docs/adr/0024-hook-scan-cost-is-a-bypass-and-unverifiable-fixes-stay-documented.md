# ADR-0024: 走査コストは保護のバイパスとして扱い、実測できない修正は残存として書く

**日付**: 2026-09-07  **ステータス**: accepted

## コンテキスト

2026-09-07 の hooks 監査（`hooks/hooks.json` / `src/ple4/hooks/*.py` / `runtime/` /
`launcher.py`）で、**実測して初めて確定した保護の完全バイパスが 4 件**見つかった。いずれも
静的レビューでは「未確認」「MEDIUM」に留まっていたもので、実行して数値を取ったことで
severity が上がった。

1. **`\` + 改行（行継続）** — シェルは `\` と改行の両方を消して行を連結するが、
   `_strip_line_comments` / `_replace_unquoted_newlines` はエスケープ済みの 1 文字を
   そのまま出力するため両方が残る。残ったまま `shlex(posix=True)` へ渡すと whitespace
   状態の `\` が escape 状態へ遷移して改行を次トークンの先頭文字として吸収し、継続行が
   0 桁目から始まるときにその先頭語が別トークンへ化ける。実測で 4 経路が exit 0 で素通り
   した（`block_no_verify` の `--no-verify` / `-n`、`pre_bash_commit_quality` の commit
   判定、`bash_config_protection` のリダイレクト先判定）。**「継続行が 0 桁目から始まるか」
   だけで保護の有無が反転していた。**
2. **走査コストによる timeout 誘発** — `block_no_verify` は git トークンごとに残り
   セグメントを再走査するため O(N²)。実測で 24,000 トークン（96KB = `MAX_STDIN_BYTES` の
   **9.2%**）が 15.23 秒かかり、`hooks.json` の timeout（15 秒）を超えた。
3. **`sh -c` 再帰への予算漏れ** — 2 の修正を entry loop だけに置いたところ、
   `sh -c '<巨大>'` は外側 3 トークンなので検査を通過し、再帰先が無予算で走って 16.23 秒。
   **修正がネスト経由でそのまま迂回できた。**
4. **`git` のグローバルオプション** — `_segment_mutates_worktree_or_index` が git トークン
   直後の最初の非 `-` トークンでサブコマンドを決めるため、`git -C . add x && git commit`
   の `.` がサブコマンドと読まれ、mutation-before-commit ガードが不発（実測 exit 0）。

2・3 が示すのは、ADR-0001 が受容した fail-open の**外側**にある経路である。ADR-0001 は
「検査を完走できないとき fail-open」を採ったが、それは**環境が壊れている**ケース
（Python が無い、launcher が無い）を想定している。ここで起きているのは**入力が検査時間を
支配し、攻撃者が timeout を任意に誘発できる**ケースで、host が kill した hook は exit code
を返さないため保護そのものが消える。知識カード `sync-hook-regex-backtracking-is-fail-open`
が「host timeout は fail-open と等価」として既に記録していた形である。

一方、Windows 側には**実測できない**非対称が残っている。`ple4-hook.cmd` は
`if defined PLE4_PYTHON goto :ple4_override` を PATH 検査より前に置くため、絶対パスの
`PLE4_PYTHON` を設定すると PATH 検査が丸ごと飛ぶ。POSIX 側は逆順で、絶対パスの
`PLE4_PYTHON` でも `path_not_absolute` から回復しない。POSIX 側が正しい —
launcher とその子プロセス（`git` を含む）は汚染された PATH で解決を続けるのであって、
`PLE4_PYTHON` はそれを直さないからである。しかし cmd.exe は開発ホスト（darwin）で
実行できない。

## 決定

1. **入力が走査時間を支配する経路は、fail-open ではなく fail-closed にする。**
   `MAX_COMMAND_TOKENS`（5,000）を超えるコマンドは走査せず BLOCKED を返す。
   ADR-0001 の fail-open は「環境が壊れて検査できない」ケースに限り、「攻撃者が検査を
   完走させない」ケースには適用しない。両者は「検査が完了しない」点で同じに見えるが、
   後者は入力側が任意に誘発できるため、fail-open にすると保護がオプトアウト可能になる
2. **予算・サニタイズ・正規化のガードは、再帰境界にも同じものを置く。**
   entry point だけに置いたガードは `sh -c` 1 段で迂回できる（実測 16.23 秒）。
   1 段の `sh -c` は ADR-0002 が対応範囲と明記した経路である
3. **deny の終了コードは出力層の失敗から独立させる。** `emit_block_output` の内側で
   出力例外を捕まえ、exit code は必ず返す。PreToolUse では exit 1 が non-blocking error
   ＝ ツール実行なので、`write_stdout` の BrokenPipeError が deny を allow へ反転させる。
   誘因（host がパイプを閉じる）は host の timeout と同時に起きるため、最も守りたい局面で
   ちょうど外れていた
4. **実測できない修正は、適用せず残存として書く。** `ple4-hook.cmd` の検査順は POSIX 側へ
   揃えるのが正しいが、cmd.exe を開発ホストから実行できず、壊れた並べ替えは Windows で
   保護を黙って無効化する。`KNOWN RESIDUAL` として `.cmd` 本文とテストのコメント両方へ
   書き、CLAUDE.md にも非対称を明記する（`feedback-cross-platform-common-logic-first` の
   「無理なら制限を明記」の適用）

## 検討した代替案

### 代替案 1: トークン上限を設けず、走査を線形へ書き換える

- 長所: 正当な入力を 1 つも拒まない。上限値の調整が要らない。
- 短所: `parse_git_segment` は git トークンごとに新しい git 起動としての解釈を要求する
  ため、線形化は解析の意味論そのものを変える。回帰が静的に読み切れない。
- 却下理由: 5,000 トークンのシェルコマンドは実務上存在せず（実測 8,000 トークンで
  1.66 秒＝ timeout の 1/9）、上限で得る確実性のほうが大きい。ADR-0002 の
  「誤検出を誤通過より選ぶ」に沿う。

### 代替案 2: 上限超過を fail-open（allow）にする

- 長所: ADR-0001 の文面（検査を完走できないとき fail-open）に素直。
- 短所: 攻撃者が上限を超える入力を作るだけで保護を切れる。ADR-0001 が想定した
  「環境が壊れている」とは、誘発可能性がまったく違う。
- 却下理由: 同ファイルが stdin 上限超過に対して既に fail-closed
  （`_TRUNCATED_INPUT_MESSAGE`）を採っており、そちらと整合しない。

### 代替案 3: `bash_config_protection` にもトークン予算を入れて 3 フックで揃える

- 長所: 非対称が消える。ADR-0021 は「揃っていない実装が後続の読み手に取りこぼしと
  誤読される」危険を記録している。
- 短所: 実測では線形（50,000 トークンで 0.07〜0.67 秒）で、予算が発火する条件が無い。
  発火しないガードは、いずれ「効いている」と誤認される。
- 却下理由: 揃えるのではなく、**線形なので不要**である理由をコードのコメントとして残した。

### 代替案 4: `ple4-hook.cmd` の検査順を POSIX 側へ揃える

- 長所: 非対称が消え、汚染された PATH で子プロセスが動く経路が塞がる。
- 短所: cmd.exe を darwin から実行できない。静的なテキスト検査しか通せず、壊れた
  並べ替えは「Windows で保護が黙って無効」という最悪の失敗へ倒れる。
- 却下理由: 未検証の編集より、文書化された残存のほうが安全。`.cmd` の KNOWN RESIDUAL・
  テストのコメント・CLAUDE.md の 3 箇所へ書いたので、Windows ホストを持つ者が
  実測したうえで直せる。**この却下は「直さない」ではなく「実測できる者が直す」である。**

### 代替案 5: `config_protection` の保護対象へ `hooks.json` とホストの `settings.json` を加える

**当初は「`/harness --apply` と両立しない」として却下したが、精査の結果その判断が誤りで
あることが分かったため採択へ改めた（2026-09-07、利用者の承認済み）。**

保護フック自身の設定は保護対象外で、1 回の Edit で PreToolUse ガード 4 種すべてを
無効化できる。`config_protection` の脅威モデルは「エージェントが、自分の作業を通すために
ガードレール側を緩める」であり（ブロックメッセージが明言している）、同じ動機で最も効く
緩め方が対象外だった。`.git/hooks` を保護対象へ加えた理由（「片側だけ塞ぐと、塞いだ
経路の存在が誤った安心になる」）がそのまま当てはまる。

却下時の理由「`/harness --apply` の編集先そのものなので保護すると成立しない」は、
実装を読み直すと**粗すぎた**:

- `config_protection` の判定は **basename 一致**であり、`"hooks.json"` を素で足すと
  プラグイン自身の `plugins/ple4/hooks/hooks.json` と consumer の `.claude/hooks.json`
  を区別できない。「両立しない」ように見えたのはこの basename 衝突が原因で、
  `.git/hooks` 用に既にある**パス連続一致**（`_PROTECTED_PATH_SEGMENTS`）を使えば分離できる
- `harness_audit` が provider モードで `hooks/hooks.json` を指す check は 5 件すべてが
  存在・整合チェックで、fix は**追加**であって弱化ではない
- consumer モードで `.claude/settings.json` を指すのは 2 件だけで、fix は初回セットアップ
  1 回きりの行動

さらに、実装直前の再検討で**当初案のもう 1 つの欠陥**が見つかった。`settings.json` の
条件を「`env` キーを触る場合」にすると、`update-config` skill の中心的な仕事
（`set DEBUG=true`）と `.vscode/settings.json` の `terminal.integrated.env.*` を巻き込む。
守れるものが増えないのに頻繁な正当作業を壊すため、条件を **`PLE4_PYTHON` の出現そのものと
`hooks` / `enabledPlugins` / `disabledPlugins` の 3 キーだけ**に絞った。

採択した実装:

1. `plugins/ple4/hooks/hooks.json` を**パス連続一致**（`("hooks", "hooks.json")`）で
   無条件保護する。consumer の `.claude/hooks.json`（親が `.claude`）には当たらない
2. `settings.json` / `settings.local.json` を**条件付き保護**へ加え、`PLE4_PYTHON` の出現・
   `hooks` / `enabledPlugins` / `disabledPlugins` のときだけ deny する。とくに
   `PLE4_PYTHON` は `runtime/ple4-hook` が exec するため、保護の無効化ではなく
   **全ツール呼び出しでの任意コード実行**の入口である
3. ブロックメッセージの「正当な変更ならフックを一時的に無効化せよ」を
   「必要ならユーザーに依頼せよ。フックを自分で無効化するな」へ改める。
   **止めた直後に「代わりに検査ごと止めていい」と言えば、止めた意味が消える。**
   加えて 1 を入れるとこの指示は循環する（無効化先が保護対象になる）

残るコスト: `/harness --apply` の `consumer-hook-guardrails` fix（初回セットアップ 1 回）と、
ple4 自身の開発で `hooks.json` を触る作業が deny される。どちらも deny メッセージが
「ユーザーに依頼せよ」と案内する。

**この保護は決定的ではない。** `python3 -c "open('.claude/settings.json','w').write(...)"` は
書込み先がリダイレクトでも既知エディタでもないため、おそらく素通りする。守っているのは
`config_protection` が元から守っている層 —— 素直なエージェントが近道を取る経路と、
低コストな注入 —— であって、決意した攻撃者ではない。

### 代替案 6: `_ScanBudget` を「走査単位数」でも積算する

`check(scanned_bytes)` は空行（`scanned_bytes == 0`）で残量を減らさないため、初回以降は
deadline を読まない。

- 長所: docstring が「行数基準では走査開始前の 1 回しか確認されない」として退けた欠陥の
  鏡像が消える。
- 短所: 残量が減らないのは空行のときだけで、空行の走査自体が定数時間。実質的な
  取りこぼし区間が存在しない。
- 却下理由: 実害の無い対称性のための変更で、`_ScanBudget` の docstring が説明する設計
  （上界は共有 `deadline` が与え、積算間隔は 1 ファイル内の取りこぼしだけを受け持つ）を
  読みにくくする。

### 代替案 7: `git` の起動を `shutil.which` で絶対パス解決する

Windows の `CreateProcess`（`lpApplicationName=NULL`）は PATH より先にカレント
ディレクトリを探すため、リポジトリ同梱の `git.exe` が hook 権限で起動しうる。

- 長所: POSIX 側が PATH 洗浄まで責務を広げているのに Windows 側に同じ担保が無い、
  という非対称が消える。
- 短所: 実装では argv[0] が絶対パスに変わり、既存のテストスタブ 6 箇所が argv[0] を
  `"git"` として照合しているため一斉に壊れる（実際に試して 7 件が赤になった）。
  argv を変えずに `executable=` を渡す形なら壊れないが、`subprocess.run` を直接呼ぶ
  箇所と `run_text` 経由の箇所が混在しており、片方だけ直すと新しい非対称になる。
- 却下理由: darwin から Windows の挙動を実測できず、代替案 4 と同じ理由で未検証の
  編集を避けた。残存として本 ADR に記録する。優先度は代替案 5 より低い —
  攻撃者は既に同一 UID でコマンドを実行できる立場にあり、得るのは hook の git 問い合わせ
  を偽装する能力に限られる。

## 結果

### 肯定的

- 実測で 4 件の完全バイパスが塞がった。いずれも静的レビューでは「未確認」または
  MEDIUM に留まっていたもので、**実行して数値を取ることが severity の確定に必要だった**。
- 「host timeout は fail-open と等価」が規約として明文化され、ADR-0001 の fail-open が
  適用される範囲（環境の故障）とされない範囲（入力が誘発する検査不能）が分かれた。
- `launcher.py` の「全フックは SystemExit 経由で終了する」不変条件に検知器が付いた
  （ADR-0011 決定6 / ADR-0023 の適用）。
- CLAUDE.md に `ple4_python_not_absolute` / `path_has_empty_entry` / `path_not_absolute`
  の 3 理由コードと、POSIX / Windows の検査順の非対称が明記された。

### 否定的

- `MAX_COMMAND_TOKENS = 5000` は実測に基づく閾値だが、遅いホストでの余裕は測っていない。
  darwin で 8,000 トークン 1.66 秒という 1 点からの外挿である。
- Windows 側に 2 つの未検証残存（検査順、`git` の CWD 解決）が残った。どちらも
  Windows ホストでの実測を待つ。

### リスク

- 決定 1 の fail-closed は、5,000 トークンを超える正当なコマンドを拒む。実務上そのような
  コマンドは無いと判断したが、生成されたスクリプトを 1 行で流す使い方があれば衝突する。
  その場合は上限値を上げるのではなく、コマンドを分割する側を正とする。
- 代替案 5 の採択により、`/harness --apply` の consumer 向け fix 1 件と、ple4 自身の
  開発での `hooks.json` 編集が deny される。どちらも「ユーザーに依頼する」へ倒れるので
  復旧不能ではないが、摩擦は増える。
- 代替案 5 の保護は素直なエージェントの近道と低コストな注入にしか効かない。Python の
  `open()` 経由の書き込みなど、書込み先がトークンとして現れない経路は素通りする。
  **「hooks.json は守られている」と読むのは誤りで、正しくは「Edit/Write と Bash の
  リダイレクト経路では守られている」である。**
