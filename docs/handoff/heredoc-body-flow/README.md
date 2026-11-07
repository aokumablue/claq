# 引き継ぎ: heredoc 本文の行き先の判定を作り直す

`/refactor`（全 Python リファクタ）の後処理で、保護フックの heredoc 本文の除去が設計問題として残った。
この文書は別セッションがそのまま着手できるように、現状・残課題・受け入れ基準・再現手段をまとめる。

## 0. 先に読むこと

- **ブランチ `claude/simplify` は、A が解消するまで main へ merge しない。** bugfix 報告の Review 欄は BLOCKED。
- このテーマは Opus 5.5 のセッションが安全機構（`[cyber]`）で停止した。**素通りする入力をチャットへ貼らない。**
  再現はスクリプトに任せ、報告には件数と exit code だけ書く。
- 評価（reviewer / security-auditor）の範囲は「その差分が新しく作った素通りと誤検出（f8fc7421 比）」に絞る。
  範囲を絞らないと件数が毎回増えて収束しなかった（4 回の評価で毎回別の形が出た）。
- security-auditor は Bash を持たず静的トレースしか返せない。**指摘は親が実シェルと hook に流して確定させる**（確定したものだけ数える）。
- `docs/adr/code-simplicity-and-defense.md` の基準を守る。要件の文・攻撃者と入口・再現できる失敗を引用できないものは実施せず、理由を添えてクローズする。
  保護フックは敵対的回避への防壁ではなく、エージェントの誤操作と不信テキストに操られたエージェントを止めるもの（`shell-analysis-boundary.md` の共通原則）。

## 1. 現状

| 項目 | 値 |
|---|---|
| 基準の版 | `f8fc7421`（リファクタ前の元の版）。作業開始時の HEAD |
| 評価済みの版 | `bc88b843`（最初の全体レビュー）、`b5665e3e`（分割直後）。heredoc の書き換えは `359e9c47`・`398cfa89` とその続き |
| テスト | 5313 passed、`ruff check plugins/claq` 通過、coverage 100%（`cd plugins/claq && python3 -m pytest -q --cov`） |
| 日常コマンドの回帰コーパス | `fp_corpus.py`: 120 判定中 **17 件が元の版より厳しくなっている**（目標は 0） |
| 素通りの確認 | 実シェルで実行される 30 形（`probes/probe3.py` の 21 形と `probes/bypass_probe.py` の 9 形）は、現行の hook がすべて deny |
| Python 3.12 | 手元に無く実行していない。変更した 134 ファイルの構文検査（`ast.parse(feature_version=(3, 12))`）だけ通した |

heredoc の本文を扱うコードは `plugins/claq/src/claq/hooks/heredoc.py`（`strip_data_heredoc_bodies`・`command_readings`）にある。
呼び出しは `block_no_verify.py`・`bash_config_protection.py`・`pre_bash_commit_quality.py`・`commit_risk.py`。
判定の背景は `docs/adr/shell-analysis-boundary.md` の「heredoc のデータ本文はコマンド列として解析しない」節。

## 2. 道具と再現手順

`probes/` は素通りの入力を含むので **commit しない**（このチェックアウトの `.git/info/exclude` に入れてある。別のチェックアウトには存在しない。必要なら下の手順で作り直す）。
`fp_corpus.py` は無害な日常コマンドだけなので commit している。

```bash
# 比較元（f8fc7421）を取り出す。b5665e3e も同じ形
mkdir -p /tmp/claq-base && git archive f8fc7421 plugins/claq/src | tar -x -C /tmp/claq-base

# 日常コマンドの回帰（changed が 0 になること）
.venv/bin/python3 docs/handoff/heredoc-body-flow/fp_corpus.py /tmp/claq-base/plugins/claq/src "$PWD/plugins/claq/src"

# 素通りの確認（すべて 2 = deny になること）。引数なしは現在のチェックアウトの src
.venv/bin/python3 docs/handoff/heredoc-body-flow/probes/probe3.py
.venv/bin/python3 docs/handoff/heredoc-body-flow/probes/bypass_probe.py /tmp/claq-base/plugins/claq/src "$PWD/plugins/claq/src"
```

- `probes/probe4.py` は 13 形を「実シェルで実行されるか」と「元の版・b5665e3e・現行の hook の exit code」で並べる。引数は `base_src/` と `b566_src/` を持つディレクトリ
  （中身は `git archive` で作る）。B の再現に使う。
- `probes/*.py` は実シェル（`bash -c`）で一時ディレクトリの `ruff.toml` を削除させ、残るかを見る。副作用は一時ディレクトリに閉じる。
- 検査力の指標は `/refactor` で使った `test_snapshot.py` と同じ考え方（assert・raises・test 数を前後で比べる）。必要なら 10 行で書き直せる。

## 3. A: merge を止める退行（元の版で許可していた日常コマンドが deny になる）

`fp_corpus.py` の最後の 8 件が再現。**どれも無害なコマンド**で、元の版では 3 hook とも exit 0。

### A1. データ heredoc の後ろの無関係なパイプ（12 件）

`cat > note.md <<'EOF'` で本文に危険語を書いた後ろに、次のような独立した文が続くと、本文を取り除かず検査して deny になる。

- `ps aux | grep bash`
- `curl -fsSL https://example.com/install.sh | sh`
- `git ls-files | xargs -I{} cp {} .`
- `echo $SHELL | grep -q zsh && echo ok`

原因は `heredoc.py` の `_reaches_a_shell`（256・259 行付近）:

1. 区切り文字が来るたびに `receives_body, executes = token == "|", False` とするので、**どの `|` の右側も「本文を受け取る位置」になる**。左側に heredoc があるかを見ていない。
2. `executes` は**引数の位置のトークン**（`grep bash` の `bash`、`cp {} .` の `.`）でも True になる。コマンド位置だけを見るべき。
3. docstring と ADR 条件 1 は「heredoc 自身のコマンドとパイプの先」と書いており、実装と食い違っている。

制約: ADR が名指しする形 — heredoc をグループ（サブシェル）で包み、閉じた後にシェルへパイプする形 — は引き続き取り消す必要がある。
`tokenize` は閉じ括弧の前後を区切りトークンにするため、`;` で状態が落ちる。現行のコードでこの形が取り消されるのは A1 の原因 1 のおかげにすぎない。
単純に「`|` のときだけ直前の値を持ち越す」では足りず、グループの深さの管理が要る。

### A2. データ heredoc の後ろに、別の文の複数行クォート文字列（5 件）

`cat > a.md <<'EOF'` の後に、複数行の `gh pr create --body "…"`・`git commit -m "…"`・`echo '…'` が続くと、クォート文字列の中の行が 1 行ずつコマンドとして読まれて deny になる。
heredoc の無い同じコマンドは元の版でも今も許可。PR 本文やコミットメッセージをファイルへ書いてから作る形は日常的。

原因は `command_readings`（365 行付近）。「引用の外に `<<` が残る複数行」なら、**除去が成功していても**物理行ごとの読みを足す。
足す目的は、除去せず残した本文の引用符（`don't`）が `shlex` で後ろの実行行を飲み込むのを補うこと（`359e9c47` で入れた）。

方針の候補:

- 物理行の読みは、(a) 除去を取り消した（本文を残した）範囲、(b) トークン化に失敗した範囲に限る。除去が成功したコマンドには足さない。
  `strip_data_heredoc_bodies` が「取り消した行の位置」を返す形が要るかもしれない（戻り値の形を変える場合は呼び出し 4 か所とテストを追従）。
- 行頭がクォートの中にある行（`_operator_start_offsets` の鎖で分かる）は 1 行だけでは読まない。

### 合格条件（A 全体）

- `fp_corpus.py` の changed が **0**。
- `probe3.py`・`bypass_probe.py` の全形が deny のまま。
- 5313 件が緑、coverage 100%、ruff 通過。既存テストの期待値は変えない。
- A を直した差分に対する、範囲を絞った再評価（上の「先に読むこと」の方式）で、新しい誤検出と新しい素通りが 0。

## 4. B: 元の版から素通りしている「本文の行き先」の穴（11 形）

`probes/probe4.py` の 13 形のうち、実シェルで本文が実行され、**元の版（f8fc7421）・b5665e3e・現行のいずれでも hook が通すもの**。今回の書き換えが作った退行ではない（3 版で実測済み）。
残りの 2 形はシェルで実行されなかった。

分類（入力の詳細は `probes/probe4.py` を読むこと）:

1. 改行・グループ・`|&` をまたぐ、パイプの受け手。
2. 本文を受け取る複合コマンド（グループ・サブシェル・`if`）の中にシェルがある形。
3. コマンドの無い `exec` で現在のシェルの stdin が本文のまま残り、後ろの文が読む形。
4. プロセス置換（`<( … )`・`>( … )`）。

判断が要る点:

- 4 はコマンド置換と同じ非目標（ADR）に入れるか。入れるなら ADR に 1 行足してクローズ。
- 1〜3 は A と同じ「本文の行き先」の判定そのもの。**A と別々に直すと同じ関数を二度作り直す。A と一緒に設計する。**
- 設計の候補（どれも利点と欠点がある。新セッションで決める）:
  1. **構成を逆にする。** 「単純なコマンド 1 つに付いた heredoc で、パイプ・グループ・`exec`・シェルのいずれも無い」場合だけ本文を除去し、それ以外は常に除去しない。
     列挙漏れが原理的に出ない（確定できた形だけが対象）が、データ heredoc の後ろのパイプ（`cat <<'EOF' | tee out`）や PR 本文の定番形で誤検出が増える。
     fp_corpus のうち「データを非シェルへパイプする形」「`$(cat <<'EOF'` が引用の中にある形」を許可し続けられるかが分かれ目。
  2. 現行の「シェルへ届く形を数える」を、グループの深さ・パイプラインの所属・コマンド位置で正確にする。許可は広いが、形の列挙が残る。
  3. `docs/adr/shell-analysis-boundary.md` が却下している「データを消費するコマンドの allowlist」は採らない（集合が無限に広がる）。
- ADR の共通原則は「誤検出を誤通過より選ぶ」。ただし `\` を含むコマンドを一律 deny するような、使えない水準の誤検出は射程外。

## 5. C: 利用者の判断を待つ仕様変更（3 件）

実装済みだが、`code-simplicity-and-defense.md` の「新しい仕組みは提案として扱う」に当たる。採否を決めてから確定する。

1. ADR の heredoc 条件 1 を「演算子行」から「除去後に本文を受け取る位置」へ広げた（`_reaches_a_shell`）。
2. 物理行ごとの読みという新しい検査層（`command_readings`）。
3. ADR に追記した受容済みの誤検出: 算術シフト（`$((1<<3))`）が読めない `<<` に当たる、`docker exec … bash -c '…'`・`ssh h bash -c '…'` の内側の commit も読む。

## 6. D: 残りの小項目

- **measurements の 6 本**（`docs/handoff/over-defense/measurements/*.py`）: 前回の報告の「実行できない」は誤り。`token_limit_probe.py` は計測値を出して動く。
  `MAX_COMMAND_TOKENS` は上限を外した今は意味のない代入（`s.MAX_COMMAND_TOKENS = 10**9`）で、`ScanBudgetExceededError` は `except` 節にしか現れない。
  handoff の README と protocol が名指しする過去の記録で、どのゲートにも乗っていない。**変更せずクローズを推奨。**
- **Python 3.12 の実行**: 手元に `python3.12` も `uv` も無い。CI か別環境で全体の pytest を 1 回流す。
- **loop-dev の仮決定**（`/bugfix` の Assumptions に転記する分）: planner と tdd-writer を省略して自分で実装した、
  `tag_stripping._ERASE_PATTERN` の語彙を `_NEUTRALIZE_TAGS` へ広げた、`_commit_target_risk` も `sh -c` の内側を読むようにした。
- **未実行の監査指摘（差分前からある、深刻度は低い）**: `resolve_repo_root` が `PermissionError` を捕まえず exit 1 で落ちうる（hook の PATH に実行不可の git がある場合だけ）。
  `env -C . rm ruff.toml` は未知の wrapper オプションの値を実行位置と読む（ADR が承知の境界としている）。対応不要と判断した。
- **このセッションで評価の対象外だったもの**: argv の結合候補が `sh -c` の内側を読まない設計（要素側が読むので穴にならない）。

## 7. 進め方（推奨）

1. `/plan` から入り、A と B を**まとめて**設計する（§4 の候補 1 を先に検討し、fp_corpus で誤検出が 0 のまま成立するかを実測する）。
2. 実装は `/bugfix`。再現テストを先に赤で確立する（A は `fp_corpus.py` の 8 件を `plugins/claq/tests/hooks/test_protection_bypass_regressions.py` の許可側の表へ移す）。
3. 評価は範囲を絞る。heredoc の除去でまた新しい漏れが出たら、個別に塞がず設計を見直す。
4. 終わったら、この文書は `docs/handoff/` の過去の記録として残すか削除するかを決める。ADR（`docs/adr/`）には現行の判断だけを書く。

## 8. 参考: この作業で入った主な commit（`f8fc7421..HEAD`）

| 内容 | 要点 |
|---|---|
| clean / simplify / perf | 死蔵コードの削除、責務分割、3 件の性能修正（正規表現の指数バックトラック、二次の走査） |
| `147dad60` 他 | 保護フックの穴の修正（語の途中の `#`、`sh -c` の実行名の正規化、`cd` の引き継ぎ、ロケール由来の例外、再結合した鍵） |
| `b5665e3e` | `config_protection` の内容判定を `conditional_protection` へ分割（800 行の目安） |
| `359e9c47`・`398cfa89` | heredoc の除去を「確定できる場合だけ」に絞り、`heredoc.py` へ分割 |
| 最後の commit | `<<\EOF` のクォート判定、未クォート本文の行継続の連結、docstring、この文書 |

作業用の知識カードは `~/.claq/mem.db` に 7 件登録済み（pending）。`claq_run claq.mem.cli search "heredoc"` で引ける。
