# skills / agents / commands 定義の改善修正案

対象は `plugins/claq/` の `agents/` 9 本・`commands/` 10 本・`skills/` 21 本の定義文書（`SKILL.md` 本文まで。`references/` 配下は対象外）。
本書は提案であり、定義ファイルは変更していない。採否は項目ごとに利用者が決める。

## 評価の基準

指摘は次の 2 つに照らして出した。基準に反する案は載せていない。

- `docs/adr/definition-and-subagent-design.md`
  - サブエージェントに届くのはシステムプロンプト・依頼文・CLAUDE.md・git status・preload skills・sibling roster だけ
  - 定義間の逐語重複は消さない。description は「いつ呼ぶか（呼ばない条件を含む）」を書く
  - 論拠・経緯・ツール権限の再説明を定義に書かない。定義に書いた検証規則は CI で実測する
  - 止まってよい条件を列挙し、避ける止まり方 4 種は全定義で同じ文言にする
- `docs/adr/plugin-root-resolution.md`
  - Bash tool の環境に `CLAUDE_PLUGIN_ROOT` は乗らない。plugin root は `claq_plugin_root` で得る

載せなかった種類の案: 重複の参照化、強調語の一括整理、報告件数の絞り込み、モデル・effort の引き下げ、難読化入力への防御追加。

## 優先度の定義と一覧

- **P1**: 定義どおりに動かない、または定義間で食い違っている（実ファイルで確認済み）
- **P2**: 契約の欠落。呼び出し側・受け取り側が推測で埋めている
- **P3**: 体裁・表記の不揃い。動作は変わらない

| ID | 優先度 | 対象 | 要点 |
|---|---|---|---|
| X1 | P1 | planner / loop-dev / plan | モード切替の合図が呼び出し側と受け側で違う |
| X2 | P1 | release-verify | 委譲元一覧に `package-upgrade` が無い |
| A5 | P1 | bench-analyzer | 相対パス参照を Bash 無しの agent が解決できない |
| A9 | P1 | security-auditor | 「安全なコード例を添える」と 1 行出力が両立しない |
| S1 | P1 | release-verify | モデルから起動できない skill が常に `Not run` になる |
| C2 | P1 | package-upgrade | 永続メモリ・停止文言・Assumptions が他の command と揃っていない |
| X3 | P2 | commands 全 10 本 | learn の基準を相対パスで参照（主会話が読める保証が無い） |
| X4 | P2 | quick-debug / quick-refactor / quick-review | 手順 6 が指す「追加した防御」欄が返却形式に無い |
| A3 | P2 | comparator | 出力例が「本文からの引用」規則に合っていない |
| A4 | P2 | grader | 入力欠落時の扱いが無い。既定文言が英語 |
| A7 | P2 | harness-tuner / harness | FAIL 時の出力形と、呼び出し側の扱いが無い |
| A8 | P2 | planner | description・計画フォーマットの欠落 |
| A10 | P2 | tdd-writer | 「統合（常に）」と数値基準の適用範囲 |
| C3 | P2 | harness | 停止点節が無い。再監査の format が未確定 |
| C5 | P2 | plan | `refactor-fix` を選ぶ条件が無い |
| C6 | P2 | refactor | 節の順序、prep → rollback の順序性 |
| C7 | P2 | review | レポートの形が散文だけ |
| S2 | P2 | checkpoint / adr / secure | 起動設定と description の食い違い |
| S3 | P2 | loop-audit | `Outcome Health` の採点式が無い |
| V1 | P2 | CI | 上記のうち機械検査できるものが未検査 |
| P3 群 | P3 | 複数 | 表記・体裁（末尾にまとめて記載） |

---

## 判定結果（P1・P2）

公式ドキュメント（下の「出典」）と実機で裏づけを取り、項目ごとに実施かクローズかを決めた。

| ID | 判定 | 根拠 | 実施内容 |
|---|---|---|---|
| X1 | 実施 | 呼び出し側と受け側で合図が違うと planner が既定モードで動く（定義の不一致） | `mode: evaluate` / `mode: decide` に統一 |
| X2 | 実施 | 委譲元一覧と loop-dev description の食い違い | `package-upgrade` を追加 |
| A5 | 実施 | サブエージェントは cwd で始まり、定義ファイルの場所を基準にした解決は無い（sub-agents） | 相対参照を外し、本文だけで完結させた |
| A9 | 実施 | security-auditor の tools は Read/Grep/Glob で、1 行出力と「コード例を添える」が両立しない | 置き換え先の API を名指す形に |
| S1 | 実施 | `disable-model-invocation: true` は「Claude can't invoke the skill on its own」（skills）。Skill ツールで `claq:quick-review` を呼ぶと拒否されることを実測 | 利用者へ `/<name>` を依頼し、結果が貼られるまで `Not run`。Gate の意味を変える `Manual` 区分は採らない |
| C2 | 実施 | 停止文言・永続メモリ・Assumptions が他の command と不揃い | 学びの記録は既存のため追加なし |
| X3 | 実施 | command の本文にベースディレクトリは付かない（`claq:instinct` を Skill で起動して実測）。`${CLAUDE_PLUGIN_ROOT}` は本文で置換されるが（plugins-reference）、ベンダ固有パスは ADR とテストで禁止 | 各 command の永続メモリ節に、`claq_plugin_root` の `commands/` を基準に読む旨を追加 |
| X4 | 実施 | 手順 6 が指す欄が返却形式に無い（定義内の矛盾） | quick-debug / quick-refactor / quick-review に加え、同じ欠落のあった html-gen / mcp-gen / release-verify にも追加 |
| A3 | 実施 | 信頼境界は本文引用を求め、引用の無い判定は破棄される。出力例が引用なし | 出力例を引用つきの評語にし、引用を入れるフィールドを出力契約に明記 |
| A4 | クローズ | 入力欠落・判断不能は既存の「FAIL」規則（`:47`・`:56`）で扱える。`No suggestions, evals look solid` を読む箇所は grader.md 以外に無く、英語であることによる不具合は再現できない | — |
| A7・C3 | 実施 | harness-tuner が FAIL でも再監査へ進む手順で、「適用して効果なし」と区別できない | FAIL の先頭行を決め、/harness は `BLOCKED` で終える。停止点節を追加、再監査は `--format json` 固定 |
| A8 | 実施 | description は「When Claude should delegate」（sub-agents）。計画フォーマットに、設計原則と loop-dev が前提にする「変更ファイル一覧」が無い | 呼ばない条件の形に直し、「変更ファイル一覧」「テストコマンド」欄を追加 |
| A10 | クローズ | カバレッジ 100% は利用者の規約で、変えるのは仕様変更。「統合（常に）」は API・DB 操作を対象にした文として読める | — |
| C5 | クローズ | `refactor-fix` は loop-dev の列挙内で、review も同じ値を使う。誤選択の再現が無い | — |
| C6 | 実施（一部） | rollback は prep の出力を入力に取る。reviewer / security-auditor は INFO を返すのに要約で落ちていた | 起動順を明記、`Issues` に INFO を追加。節の並べ替えは P3 として見送り |
| C7 | クローズ | 散文のレポートで壊れる振る舞いを示せない（好みの問題） | — |
| S2 | クローズ | `user-invocable: false` は "Only Claude can invoke"、"hides it from the `/` menu"（skills）。adr・checkpoint は自動起動で成立する。secure は description がモデルに見えないだけで実害なし | — |
| S3 | 実施 | 出力は `n.n / 10` を求めるが式が無く、同じ履歴から値が揺れる | 満たした基準の数から決まる式を追加。blocker 0 件の履歴は満点、実行 0 件は `N/A` |
| V1 | 実施（V1-e を除く） | 下の「CI に足す検査」 | `tests/test_definition_contracts.py` |

### CI に足す検査の実施結果

定義 md は互いを import しないため、片方だけ書き換えた食い違いが実行時まで残る。これを pytest の構造テストで検出する。

- 実施: V1-a（停止文言の一部を書く定義は 4 種を同じ文言で書く）、V1-b（loop-dev の description・release-verify の委譲元一覧・loop-dev を起動する command の 3 者一致）、V1-c（agents に `../` の md 参照が無い）、V1-d（報告の「追加した防御」を求める定義はその欄を持つ）、V1-f（planner を起動する定義は `mode:` を書く）、X3 の検査（`../` の md 参照を持つ command は基準の求め方を書く）
- 見送り: V1-e。commands の `command:` は Claude Code が黙って無視するフィールドで（skills の「ignores a field it doesn't recognize」）、許可一覧を決めるのは P3 の C1 を直すときになる

### 新たに見つけた点（提案）

- fork skill（loop-dev / search / skill-make / maintain / skill-tune / learn / refactor-prep / loop-audit）の frontmatter に `background:` が無い。skills の文書には「The forked subagent runs in the background」とあり、`background: false` で起動したターンに結果を待つとする（**実機未確認**。公式ドキュメント由来）。commands は loop-dev の報告を受け取ってから次へ進む前提で書かれている。`validate_skills` は未知キーを拒否しないので 1 行で足せるが、他のハーネスでの扱いを確かめてから決める

### 出典

- https://code.claude.com/docs/en/skills
- https://code.claude.com/docs/en/sub-agents
- https://code.claude.com/docs/en/plugins-reference

## 横断課題

### X1. planner のモード指定が呼び出し側と一致しない（P1）

**現状**

- `agents/planner.md:19-20`: 「評価モード（既定）」「決定モード（「決定して」と指示されたとき）」
- `skills/loop-dev/SKILL.md:36`: 「`claq:planner` を決定モードで起動し」
- `commands/plan.md:24`: 「`claq:planner` を評価モード（既定。…）で起動する」

**問題**
planner が見る合図は「決定して」という語。loop-dev は「決定モードで起動」としか書いておらず、依頼文に何を書けば決定モードになるかが定義から決まらない。合図が届かなければ既定の評価モードになり、loop-dev は単一ブループリントを受け取れない。

**修正案**
`code-refiner` と同じ `mode:` 方式に揃える。

`agents/planner.md` のモード節:

```markdown
## モード

依頼文の `mode:` で切り替える。指定が無ければ `evaluate`。

- `mode: evaluate`（既定）: トレードオフ分析・代替案の列挙・アーキテクチャへの影響調査。判断材料を並べて呼び出し元に選ばせる
- `mode: decide`: 単一の推奨に絞り、そのまま実装に移れる粒度の実装ブループリントと実施計画を出す。…（以下現行どおり）
```

`skills/loop-dev/SKILL.md:36`:

```markdown
1. plan: `claq:planner` を起動し（依頼文に `mode: decide` を書く）、単一のブループリントを確定させる。`approved_plan` があれば省く
```

`commands/plan.md:24`: 「依頼文に `mode: evaluate` を書いて起動する」。

見出し `agents/planner.md:38`「決定モードで…」も `mode: decide` に合わせる。

### X2. release-verify の委譲元一覧に package-upgrade が無い（P1）

**現状**

- `skills/loop-dev/SKILL.md:3`: 「feat-dev/bugfix/refactor/test-gen/package-upgrade/plan/review からの委譲専用」
- `skills/release-verify/SKILL.md:73`: 「`loop-dev` ← `feat-dev` / `bugfix` / `refactor` / `test-gen` / `plan` / `review`」

**問題**
release-verify はこの一覧で委譲経路を実起動して確かめる。`package-upgrade → loop-dev` が検証対象から漏れる。

**修正案**
`release-verify/SKILL.md:73` に `package-upgrade` を足す。再発防止は V1-b。

### X3. commands から learn の基準を相対パスで参照している（P2）

**現状**
commands 全 10 本が `../skills/learn/SKILL.md` を「基準はここ」と参照し、記録自体は `claq_mem_learn` を直接呼ぶ。`learn` は `context: fork` かつ `user-invocable: false` で、主会話は本文を読んでいない。

**問題**
`tests/test_md_references.py` が検査しているのはリンク先の実在で、実行時に主会話がそのパスを開けるかは別の話。command の展開時に基準ディレクトリが提示されなければ、`../skills/...` は cwd 基準で解釈されて Read が失敗する。その場合、learn 本文にしか無い規則（重複確認は pending と active の 2 回引く・active は更新しない・長い hex/Base64 を伏せる・scope に迷ったら repo）が守られない。

**修正案**（どちらか一方）

- 案 A（推奨）: 各 command の「学びの記録」節に、記録前に守る 4 規則を逐語で持たせる。ADR は定義間の重複を許容しており、参照先が届くかに依存しなくなる
- 案 B: 参照の直前に絶対化の手順を置く

  ```bash
  . "$HOME/.claq/env.sh" || exit 127
  echo "$(claq_plugin_root)/skills/learn/SKILL.md"
  ```

  出力されたパスを Read してから記録する、と書く

**要確認**
インストール済みビルドで `/claq:bugfix` などを起動し、相対パスの Read が実際に通るかを 1 回確かめる。通るなら P3 に下げてよい。

### X4. 「実装の手順」6 が指す欄が返却形式に無い（P2）

**現状**
「実装の手順」1〜7 は `tests/test_simplicity_criteria.py` が一字一句の一致を固定している（正本は `agents/tdd-writer.md`）。6 番は「報告の『追加した防御』に残す」。この欄を返却形式に持つのは `quick-code`（`SKILL.md:69`）・`code-refiner`（`:74`）・`tdd-writer`（`:53`）だけで、`quick-debug`・`quick-refactor`・`quick-review` には無い。

**問題**
手順が書けと言う欄の置き場が無く、書かれるかどうかが実行ごとに変わる。

**修正案**
手順の文言は動かさない（テストと ADR が固定している）。3 本の返却テンプレートに `quick-code` と同じ 1 行を足す。

```
追加した防御: <書いた検査・上限・続行経路と、引用した根拠。無ければ「なし」>
```

### X5. 停止 4 種文言の適用範囲（P2）

ADR は避ける止まり方 4 種を「全定義で同じ文言に」としている。文言を持たないファイルを確認した結果:

| ファイル | 判定 | 理由 |
|---|---|---|
| `commands/package-upgrade.md` | **要修正** | 独自文言「途中の報告で応答を終えず」（`:11`）。自律で最後まで進む command |
| `commands/harness.md` | **要修正** | `--apply` 時は harness-tuner 起動 → 再監査まで自律で進むが、停止点節が無い |
| `commands/instinct.md` | 対象外 | 1 回の応答で終わる対話コマンド |
| agents（bench-analyzer / grader / comparator / security-auditor / reviewer / planner） | 対象外 | 1 回の判定を返して終わる。途中で止まる余地が無い |
| skills（quick-* 5 本 / grillme / adr） | 対象外 | 利用者との往復・承認待ちが意図した停止点 |
| skills（checkpoint / refactor-rollback / secure） | 対象外 | 単発の出力で終わる |

修正は C2・C3 に記載。fork で子を起動する skill（loop-dev / search / skill-make / maintain / skill-tune）は文言と `run_in_background: false` を持っており、変更不要。

---

## agents

### A1. reviewer: 3 要素の表記ゆれ（P3）

`agents/reviewer.md:41` は「攻撃者 → 入口 → 得るもの」、`:47` は「攻撃者 → 入口 → 起きること」。security-auditor と loop-dev は「得るもの」。`:47` を「得るもの」に揃える。

### A3. comparator: 出力例が引用規則に合っていない（P2）

**現状**
`agents/comparator.md:14` は「判定根拠には、その候補の本文から引用した具体的な文字列を含める。呼び出し元は、引用が候補の本文に実在しない判定を破棄する」。出力例（`:84-95`）の `strengths` / `weaknesses` は `"Complete solution"` のような評語で、引用を含まない。

**問題**
例に倣った出力は、呼び出し元（skill-gen の完了ゲート）の実在検査で破棄される。

**修正案**
例の値を「評語 + 引用」の形にし、日本語にする。

```json
"strengths": ["全フィールドを出力している（「total: 1,240」）"],
"weaknesses": ["見出しの書式が不揃い（「## Result」と「### result」）"]
```

どのフィールドに引用を入れるか（`strengths` / `weaknesses` / `reasoning`）を「スコアの算出規則」節の後に 1 行で明記する。

### A4. grader: 入力欠落時の扱いと既定文言（P2）

**現状**
`:56` は `expectations` が空のとき FAIL を返すと定めるが、`transcript_path`・`outputs_dir` が存在しない・読めない場合の規定が無い（bench-analyzer と code-refiner には失敗条件の節がある）。`:114` の既定文言は `No suggestions, evals look solid`、例（`:104`）も英語。

**修正案**

- 入力節の後に追加: 「`transcript_path` が読めない、または `outputs_dir` が存在しないときは、判定せず **FAIL** を返し、欠けている入力を名指しする」
- `:114` を「指摘が無ければ `改善提案なし` と書く」に変える。この文字列を機械的に読む側があるかを `skill-make/references/` で確かめてから変える（**要確認**）

### A5. bench-analyzer: 解決できない相対参照（P1）

**現状**
`agents/bench-analyzer.md:138`: 「`../skills/skill-make/references/schemas.md` のスキーマに従って `benchmark.json` を作る等）を示して **FAIL** を返す」。tools は Read / Grep / Glob。

**問題**
agent のシステムプロンプトに定義ファイルの位置は含まれず、Bash が無いので `claq_plugin_root` も呼べない。パスは cwd 基準で解釈され、利用者のリポジトリには存在しない。

**修正案**
参照をやめ、FAIL 時に示す内容を本文に持たせる。この節の直前（`:128-136`）に fixture の必須条件が既に列挙されているので、それを指す。

```markdown
…fixture が無い場合は、必要な採取手順（`with_skill` とベースラインを同一 eval/run 番号で実行し、上の「benchmark fixture の最小検証」を満たす `benchmark.json` を作る）を示して **FAIL** を返す。
```

### A6. code-refiner: description の形（P3）

description が「…専門。」で始まる。ADR の形（いつ呼ぶか）に合わせ、先頭を条件にする。

```yaml
description: コード変更直後の整理・未使用コードの削除・性能懸念の調査で、機能を変えずに既存コードの質を上げるときに使う（simplify / clean / perf の 3 モード）。設計変更・機能追加を伴う作業では呼ばない。
```

同じ形の `reviewer`「コードレビュー専門。」、`security-auditor`「…修正提案専門。」、`tdd-writer`「…TDD 専門。」も同様に直す。

### A7. harness-tuner: FAIL 時の出力と呼び出し側の扱い（P2）

**現状**
入力契約違反で FAIL を返すことは定義済み（`tests/test_md_references.py` が検査）。出力節（`:60-64`）は成功時の 3 項目だけ。`commands/harness.md:35-48` は FAIL を受けたときの進み方を書いていない。

**問題**
FAIL のまま `/harness` がステップ4（再監査）・ステップ5（差分要約）へ進むと、変更ゼロの差分を「改善結果」として報告する。

**修正案**

- `agents/harness-tuner.md` 出力節に追加: 「入力契約を満たさないときは 1 行目に `FAIL: <欠けている項目>` と書き、3 項目は書かない」
- `commands/harness.md` ステップ3 に追加: 「harness-tuner が `FAIL` を返したら、ステップ4・5 へ進まず、その理由を添えて `BLOCKED` で終える」

### A8. planner: description と計画フォーマット（P2）

**現状と修正案**

1. description（`:3`）の「コード変更は行わない。」はツール権限（Read / Grep / Glob）が保証する内容で、ADR の「ツール権限の保証を散文で再説明しない」に当たる。呼ばない条件に置き換える。

   ```yaml
   description: 機能実装・アーキテクチャ変更・複雑なリファクタ・テスト設計の着手前に、実装計画を立てるときに使う。計画が既に承認済みの段階や、1 ファイルで閉じる修正では呼ばない。
   ```

2. `:35` は「削除を、ステップと変更ファイル一覧に含める」と書くが、計画フォーマット（`:56-71`）に変更ファイル一覧の欄が無い。`commands/plan.md:37-39` が後から足しているため `/plan` 経由では成立するが、loop-dev から直接起動した場合（`mode: decide`）は欄が無い。フォーマットに 2 欄を足す。

   ```markdown
   ## 変更ファイル一覧（正確なパス。削除するファイルも含める）
   ## テストコマンド
   ```

   これで loop-dev のスコープガード（`loop-dev/SKILL.md:50`「一覧があるときだけ照合」）が常に働く。

### A9. security-auditor: コード例と 1 行出力の矛盾（P1）

**現状**
`agents/security-auditor.md:49`: 「各指摘を『[深刻度] ファイルパス:行 — 内容 — 修正方針』の 1 行で書く。…CRITICAL には安全なコード例を添え、実装は /bugfix に委ねる。」

**問題**

- 1 行規定とコード例が両立しない。直後の出力例（`:51-58`）の CRITICAL 行にコード例は無い
- 「実装は /bugfix に委ねる」は agent が実行できない指示（agent は command を起動できない）。実際の後処理は `commands/review.md:56` が loop-dev を直接起動している

**修正案**
`:49` の末尾を置き換える。

```markdown
…BLOCKER の行は内容の欄を「攻撃者 → 入口 → 得るもの」で書く。修正方針は置き換え先の API・構文を名指す（例: パラメータ化クエリ）。実装はしない。
```

### A10. tdd-writer: テストタイプと数値基準（P2）

**現状**
`:71-72`: 「ユニット: …（常に）」「統合: API エンドポイント・DB 操作（常に）」。`:58`: 「カバレッジ 100%」。

**問題**
API も DB も無い変更で「統合（常に）」は満たせない。カバレッジ 100% が変更行・変更ファイル・リポジトリ全体のどれを指すかが決まらず、リポジトリ既定の閾値と食い違ったときの優先も無い。

**修正案**

```markdown
- ユニット: 独立した個別の関数（常に）
- 統合: API エンドポイント・DB 操作に触れる変更のとき
```

```markdown
- カバレッジ: 変更したファイルで 100%。リポジトリに閾値の設定（`fail_under` 等）があればそれに従う
```

---

## commands

### C1. frontmatter の `command:` キー（P3）

全 10 本が `name` / `description` / `command: /xxx` を持つ。`command:` を読む実装は `src/claq/ci/` に無い。引数は本文末尾の「## 引数」節にあるが、`argument-hint` が無いので補完時に表示されない。

**修正案**
`command:` を削り、`argument-hint` を足す。例（`bugfix`）:

```yaml
argument-hint: "<症状・再現手順> [scope]"
```

`command:` を参照する外部ツールが無いことを確かめてから削る（**要確認**）。

### C2. package-upgrade: 他の command との不揃い（P1）

**現状**
節構成は「停止点 / ステップ1〜4 / 引数」（`commands/package-upgrade.md`）。loop-dev へ委譲する他の command（feat-dev / bugfix / test-gen）が持つ次の要素が無い。

| 要素 | 他の command | package-upgrade |
|---|---|---|
| 「## 永続メモリ」節 | あり | なし |
| 停止 4 種文言 | あり | 独自文言（`:11`） |
| 「学びの記録」ステップ | あり | あり（ステップ4 の `claq_mem_learn --kind pitfall`。当初「なし」としたのは誤り） |
| loop-dev が未判定で返したときの扱い | あり | なし |

また `:20` は「リリースノート・CHANGELOG・移行ガイドを読み」と書くが、取得手段（インストール済みパッケージ内の CHANGELOG・レジストリのメタデータ・WebFetch）と、取得できなかった場合の扱いが無い。

**修正案**

1. `:11` を他の command と同じ停止文言に置き換える（feat-dev の「## 停止点」節をそのまま写す）
2. 「## 永続メモリ」節を feat-dev から写す。過去の更新で踏んだ pitfall が最も効く command
3. ステップ2 に追加:

   ```markdown
   取得元は、インストール済みパッケージ同梱の CHANGELOG → リポジトリのリリースノート（WebFetch）の順に当たる。どちらも得られなければ、破壊的変更を「未確認」として Assumptions に書き、ステップ3 の `converge_extra` に「全テスト green」を必ず含める。
   ```

4. ステップ4 の前に「学びの記録」ステップを足す（破壊的変更への追従パターンを `pitfall` として記録）
5. 報告テンプレートに `Assumptions:` 行を足す

**補足（事実）**
本書作成時のセッションでは、利用可能な skill 一覧に `claq:package-upgrade` が出ていなかった。インストール済みビルドがリポジトリより古い可能性がある（release-verify の `same=no` に当たる状態）。

### C3. harness: 停止点と再監査の format（P2）

**現状**
`commands/harness.md` に「## 停止点」節が無い。`:25` は「`--apply` 指定時は `--format json` で実行」と定めるが、ステップ4 のコマンド（`:43`）は `--format <text|json>` のまま。

**修正案**

- 「## 停止点」節を足す: 「`--apply` が無ければステップ2 の提示で完了。`--apply` があればステップ5 まで応答を待たずに進む」＋停止 4 種文言
- ステップ4 のコマンドを `--format json` に固定し、「ステップ1 と同じ `<scope>`・`--root`・`--target-kind` で実行する」と書く（差分はカテゴリ別スコアの比較で、text 出力からは取れない）
- FAIL 時の扱いは A7 の修正案

### C5. plan: `task_type` の選び方（P2）

**現状**
`commands/plan.md:58`: 「`task_type` = 計画の性質に応じて `feature` | `bugfix` | `test` | `refactor-fix`」。

**問題**
`refactor-fix` は `/refactor` の final gate が blocker 解消のためだけに使う種別（`commands/refactor.md:72-76`）。計画から始める作業で選ぶ基準が無い。

**修正案**

```markdown
- `task_type` = 振る舞いを足すなら `feature`、誤りを直すなら `bugfix`、テストだけなら `test`。振る舞いを変えない整理は本コマンドで実装せず `/refactor` を案内する
```

見出し「## 実装」（`:53`）を「## ステップ4: 実装（承認後）」にし、他のステップと番号を揃える（P3）。

### C6. refactor: 節の順序と preflight の順序性（P2）

**現状**

- 節の並びが「ステップ6 → 部分モード → ステップ7」（`:52-60`）
- `:22-25` は refactor-prep と refactor-rollback を「次の 2 つを起動する」と並べる。rollback は prep の `scope_files` を必要とする
- 要約の `Issues:` 行（`:114`）に INFO が無い。security-auditor と reviewer は INFO を出す

**修正案**

- 「## 部分モード」を「## ステップ1」の前（停止点・永続メモリの後）へ移す。実行するステップの組は着手前に決まる
- `:22` を「着手前に Skill ツールで次の 2 つを**この順に**起動する。`refactor-rollback` には `refactor-prep` が返した `scope_files` を渡す」に変える
- `Issues:` 行を `CRITICAL {c} / HIGH {h} / MEDIUM {m} / LOW {l} / INFO {i}` にする

### C7. review: レポートの形（P2）

**現状**
`commands/review.md:37` はレポートの内容を散文で列挙する。loop-dev へ委譲する他の command は箱形のテンプレートを持つ。

**問題**
`Block` 判定の有無・件数を機械的に拾えず、ステップ4 の後処理（指摘ごとに loop-dev を起動）が指摘の取りこぼしを確認できない。

**修正案**
テンプレートを足す。

```
Review Result
Scope:      {scope}
Verdict:    {Block | Pass}
Issues:     CRITICAL {c} / HIGH {h} / MEDIUM {m} / LOW {l} / INFO {i}

[深刻度] path:line — 内容 — 推奨修正（過去の類似: {key | なし}）
```

### C9. test-gen: テンプレートの空白（P3）

`commands/test-gen.md:84` の `Tests added:{tests_added}` だけコロンの後の空白が無い。他の行に揃える。

### C10. feat-dev / bugfix: 報告形式（P3）

feat-dev は Markdown 見出し（`### 変更ファイル` …）、bugfix は箱形。どちらも loop-dev の結果を転記する。箱形に揃えると `/review`（C7）・`/test-gen`・`/refactor` と同じ形になる。

---

## skills

### S1. release-verify: モデルから起動できない skill の扱い（P1）

**現状**
`skills/release-verify/SKILL.md:122`: 「skills（`user-invocable: true`）は同じく `/<name>` で直接起動する」。`quick-code`・`quick-debug`・`quick-review`・`quick-refactor`・`quick-test`・`secure` は `disable-model-invocation: true`。

**問題**
`disable-model-invocation: true` の skill はモデルが Skill ツールで起動できない。release-verify は全コンポーネントの起動を終了条件にし、`Not run` が 1 以上なら `Gate: BLOCKED`（`:250`）。この 6 本を起動する手段が定義に無ければ、全件実行は常に BLOCKED になる。

**修正案**
起動表に行を足し、扱いを決める。

```markdown
| skills（`disable-model-invocation: true`） | モデルからは起動できない。利用者に `/<name>` の実行を依頼し、結果が貼られるまでは `Not run` に数える。依頼は 1 回にまとめる |
```

利用者の手を借りない運用にしたい場合は、この 6 本を `Not run` と別の値（`Manual`）に数え、Gate の判定から外す。どちらにするかは利用者の決定事項。

**要確認**
実機で `disable-model-invocation: true` の skill に対する Skill ツール呼び出しが拒否されることを確かめる。

### S2. 起動設定と description の食い違い（P2）

| skill | 現状 | 問題 | 修正案 |
|---|---|---|---|
| `checkpoint` | `user-invocable: false`。description は「レート制限中断が多いセッションや10ステップ超の反復ループで使用」 | 実際の利用者は loop-dev（skill を呼ばず形式に従って直接読み書き）と loop-audit。「前のチェックポイントから再開して」という発話での起動は自動発火頼み | description を「loop-dev が進捗の保存と再開に使うチェックポイントの形式。利用者が『チェックポイントから再開して』と頼んだときも使う。」に変える |
| `adr` | `user-invocable: false`。description は「『ADRにして』と頼まれたときに使う」 | 利用者が明示起動できない | `user-invocable: true` にする |
| `secure` | `disable-model-invocation: true`。description は「利用者が…頼んだときに使う」 | モデルは起動できないので、「頼んだときに使う」は成立しない。`search`・`release-verify` は `../secure/SKILL.md` を Read で参照しており、こちらは問題ない | description を「`/secure` で起動するセキュリティ点検のチェックリスト（…が対象）。」に変える。または `disable-model-invocation` を外す |

### S3. loop-audit: Outcome Health の採点式（P2）

**現状**
出力は `Outcome Health: {n.n} / 10`。採点の規定は「最低基準を下回ったら満点にしない」だけ。Process Readiness は「実測できた領域数」が分母。

**問題**
同じログから実行ごとに違う点数が出る。2 つの指標で尺度が違う。

**修正案**
Process Readiness と同じ「満たした基準数 / 実測できた基準数」にする。

```
Outcome Health: {満たした基準数} / {実測できた基準数}
```

10 点満点を残す場合は、基準ごとの配点表を定義に置く。

### S4. description の形（P3）

| skill | 現状 | 修正案 |
|---|---|---|
| `refactor-prep` | 「…事前準備スキル。」委譲専用の記載なし | 「`/refactor` のステップ1 からの委譲専用。リファクタ着手前に対象分割・依存可視化・実行前テストセットを確定する。」 |
| `skill-make` | 「新スキル生成/eval実行/ベンチマーク分析/説明文最適化。/skill-genからの委譲先。」 | 「`/skill-gen` からの委譲専用。スキルの生成・eval 実行・ベンチマーク分析・説明文の最適化を行う。」自身の「書き方のポイント」（何をするか・いつ呼ぶか・いつ呼ばないか）に合わせる |
| `release-verify` | `/maintain`・`/secure`・`/loop-audit` | 3 つとも skill。`maintain`・`loop-audit` が `/claq:<name>` で呼べるならその形に、呼べないなら「maintain skill」と書く |
| `mcp-gen` | `/secure` | 同上 |

### S5. 体裁（P3）

- `skill-tune`: 「停止点」が末尾の「打ち切り基準」内にある。他の fork skill は冒頭。冒頭へ移す。`Step N` 表記を他と同じ「ステップN」にする
- `secure`: 「永続メモリ」節だけ箇条書き記号が無い
- `html-gen` / `mcp-gen` / `release-verify`: 永続メモリ節のラベルが「参照: / 記録:」。他は「search: / record:」。どちらかに揃える
- `mcp-gen`: 「実装の手順」1〜7 の直後に箇条書き（`- cache_hints…`）が続く。間に見出しを 1 つ入れる
- `maintain`: 「フック修正: 原則4 に従う。」など、箇条書き末尾の句点が混在

---

## CI に足す検査（V1・P2）

ADR は「定義に書いた検証規則は CI で実測する」としている。上の指摘のうち機械検査できるもの:

| ID | 検査 | 置き場 | 防ぐもの |
|---|---|---|---|
| V1-a | 停止 4 種文言が、対象ファイル一覧（commands 9 本 + fork skill + release-verify）に同じ行として存在する | `tests/test_md_references.py` か新規テスト。`test_simplicity_criteria.py` と同じ形（正本 1 つ + 写しの一覧） | X5・C2・C3 |
| V1-b | `release-verify` の委譲元一覧と、`loop-dev` description の委譲元が一致する | `tests/test_md_references.py` | X2 |
| V1-c | agents の本文に `../` で始まる `.md` 参照が無い | `tests/test_md_references.py` | A5 |
| V1-d | 「実装の手順」を持つ定義の返却形式に「追加した防御」がある | `tests/test_simplicity_criteria.py` | X4 |
| V1-e | commands の frontmatter キーが許可一覧（`name` / `description` / `argument-hint`）に収まる | `src/claq/ci/validate_commands.py` | C1 |
| V1-f | `planner` を起動する定義の本文に `mode: evaluate` か `mode: decide` がある | `tests/test_agent_regex_contracts.py` | X1 |

各検査は対応する定義修正と同じコミットで足す（先に足すと落ちる）。

---

## 確認して問題なしとした点

再調査を避けるために残す。

- fork skill（loop-dev / search / skill-make / maintain / skill-tune）は停止文言と `run_in_background: false` を持つ
- `learn` / `loop-audit` / `refactor-prep` は子を起動しないので、子の `run_in_background` の規定は不要（fork 自身の `background:` は「新たに見つけた点」で扱う）
- loop-dev は「委譲先には注入が届かない。渡したい知識は依頼文に書く」と明記しており、Bash を持たない agent へのメモリ経路は手当て済み
- `harness-tuner.md` の `rubric_version`・`max_score` の例は `tests/test_md_references.py` が実値との一致を検査している
- agents の `tools` 宣言は同テストが期待値との完全一致を検査している
- skill-gen の git-history 囲い込み規則は `tests/test_agent_regex_contracts.py` が検査している
- `package-upgrade` が `task_type = feature` を使うのは loop-dev の列挙内
- `instinct` の `forget` は本文（`:71`）で「`archived` にする（行は消さない）」と明記されている
- `security-auditor.md:55` の `/search?q=` は URL の例で、command 参照ではない
- 「実装の手順」の逐語コピーは ADR とテストが意図して固定している
- `mcp-gen` 冒頭の仕様版日付は skill の存在理由そのもので、ADR の「版付き外部標準の列挙」には当たらない
- `maintain` のメモリ参照が `python3 -m claq.mem.cli` なのは開発リポジトリ前提の skill だから

## 要確認事項（実機で 1 回確かめれば決まるもの）

1. command 展開時に `../skills/learn/SKILL.md` を主会話が Read できるか → 本文にベースディレクトリは付かない（X3 を実施）
2. `disable-model-invocation: true` の skill を Skill ツールで呼んだときの挙動 → 拒否される（S1 を実施）
3. grader の既定文言 `No suggestions, evals look solid` を文字列一致で読む箇所が `skill-make/references/` にあるか → 無い（A4 をクローズ）
4. `command:` frontmatter を読む外部ツールの有無（C1）
5. `maintain`・`loop-audit` が `/claq:<name>` で起動できるか（S4）

## 推奨する実施順

1. P1 の 6 件（X1・X2・A5・A9・S1・C2）。X1・X2・A5 は置換だけで済み、V1-b・V1-c・V1-f を同時に足せる
2. 要確認 1・2 を実機で確かめ、X3・S1 の扱いを確定する
3. P2 を定義 1 本 = 1 コミットで進める
4. P3 は P2 で同じファイルを触るときに一緒に直す
