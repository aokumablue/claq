# 過剰な防御・フォールバックを抑える定義修正（測定ゲート付き）

## Context

claq プラグインの定義（skills / agents / commands）を使うと、要件に無いセキュリティ対策・安全措置・フォールバックが自発的に実装される（例: `33515cb` mcp-gen +1278 行のクォータ・合流・同時実行上限、`debcfbd`/`816cba4` html-gen のフォント取得の SHA-1 照合と 60 秒上限）。原因は 4 つ。

1. 簡潔さ基準が自己申告で通る許可条件になっている（「1 行で言えれば置ける」「エージェント自身の誤操作も失敗経路」「自分で書いた ADR・参照文書が出所になる」）
2. 逆向きの具体的指示が同居している（quick-review の「null 経路の抜け」自動適用、reviewer の「エラーハンドリング不足」例、security-auditor の機構追加型の修正方針と CSP の例、secure の実装時自動発火、learn の global「セキュリティ実践」）
3. 「穴は最小修正」が判定側にしかなく、loop-dev 反復2 と /refactor の final gate が blocker を生成側へ素通しする
4. テンプレート（mcp-gen / html-gen）に【判断】由来の防御が既定で入っている

利用者の要件:
- 実装前に改善率を全定義について示し、測定で確認する
- 指示は否定文の羅列ではなく肯定文の手順で書き、簡潔にする
- 「最新のベストプラクティスを踏まえる」は実装に必要なので残し、ハードニングとの折衷を取る

## 書き方の方針

- **肯定文の手順で書く。** 「〜しない」「〜は根拠にならない」の列挙と免責文は、望む手順と出力欄に置き換える。否定文は、名前を出すことで逆にその行動を思い出させる。リポジトリ自身の `skills/skill-make/SKILL.md:57`「禁止を並べるより望む形を書く」とも一致する。直近の修正はこれに反して否定文を積み増していた
- **衝動に出口を与える。** 防御を足したくなる判断を抑え込むのではなく、報告の「提案」欄へ流す。判断の価値は利用者に届き、コードには入らない
- **例は正しい分類の例にする。** 本文と矛盾する例（CSP・エラーハンドリング不足）は、正しく分類した例に差し替える
- **否定文を残すのは、過剰防御と無関係な硬い境界だけ。** テスト改ざんのガード、`--no-verify`、元に戻せない操作が該当する
- **純減を目標にする。** 変更した定義の合計文字数を減らす。簡潔さ基準のブロックは現在約 1900 字（9 ファイルに逐語コピー）で、1000 字以下を目標にする

## ベストプラクティスとの折衷: 「どう書くか」に使い、「何を足すか」は提案にする

| | 扱い | 例 |
|---|---|---|
| **書き方（HOW）**: 要件の振る舞いを正しく実現する手段の選択 | 最新の推奨で選んで実装する | パラメータ化クエリ、`subprocess.run` のリスト形式、現行 SDK の API（`MCPServer`）、`datetime.now(UTC)`、ネットワーク呼び出しのタイムアウト（無いと要件の「コマンドが終わる」が崩れることをテストで示せる） |
| **追加（WHAT）**: 要件に無い振る舞い（拒否・上限・再試行・代替経路・新しい仕組み）を足すもの | 報告の「提案」欄に理由と出典を付けて書く。採否は利用者が決める | レート制限、CSP、遮断器、再試行、ハッシュ照合、監査ログ |

判定の問いは 1 つにする: **それが無いと、要件に書かれた振る舞いが崩れることをテストか再現手順で示せるか。** 示せれば HOW として実装し、示せなければ WHAT として提案する。

利用者設定の文言案（任意。設定の変更は利用者が行う）:

> 既存実装に追従するだけでなく、要件を満たす書き方は最新のベストプラクティスで選び、根拠を示すこと。要件に無い追加が望ましいと判断したら、理由と出典を付けて提案すること。間違っていることは一切同調せずに、遠慮なく否定すること。

スキル側にも同じ区別を入れる（下の正本の 1 と 5）。設定を変えなくても定義側だけで効くようにする。

## 新しい正本（簡潔さ基準）の案

`agents/tdd-writer.md` の見出しを「実装の手順」に変え、写し 8 ファイルと一緒に差し替える（`tests/test_simplicity_criteria.py` は見出し名だけ更新）。

```
## 実装の手順

1. 依頼・計画・仕様から要件の文を拾い、それを満たすコードを書く。書き方は言語・ライブラリ・仕様の現行の推奨で選び、要件の振る舞いを成り立たせる書き方（パラメータ化クエリ、ネットワーク呼び出しのタイムアウト、トランザクションの commit 等）もここに含める
2. 外部入力は入口で 1 回検証し、内側は型と呼び出し元を信頼して書く
3. 失敗は例外・エラー値のまま呼び出し元へ返す
4. 入力の拒否・上限の検査は、根拠（要件の文、攻撃者と入口、テストか手順で再現できる失敗、外部 API 契約の条項）を引用できるときに書く
5. 失敗の後に続行する経路（握り潰し・既定値・再試行・代替経路）は、続行を決めた文（要件・利用者の指示・外部 API 契約）を引用できるときに書く
6. 4・5 で書いたものは、根拠をコメント 1 行と報告の「追加した防御」に残す。要件に無いが入れたほうがよいと判断したもの（ベストプラクティス由来を含む）は、報告の「提案」に理由と出典を付けて書く
7. 原因の箇所を直し、変更で使われなくなったコード・テスト・互換分岐は同じ変更で消す。抽象と設定項目は、今ある呼び出し元が使う分だけ作る
```

パイロットで直した点: 初版は「防御」と「続行」の根拠を 1 つにまとめていたため、「再現できる失敗」を根拠に例外の握り潰しが入った（4 と 5 に分けた）。また、タイムアウトを「提案」に回しすぎた（1 に書き方として明記した）。

「エージェント自身の誤操作」の条項は正本から外す。保護フック固有の例外として、CLAUDE.md と ADR だけに残す。

## 判定側と周辺の書き換え（肯定文の手順で）

- **reviewer**:
  - 余剰の節を手順にする: 差分で足された拒否・上限・タイムアウト・再試行・代替経路ごとに根拠の引用を照合し、引用が無いものは task があれば HIGH、無ければ WARNING
  - 「エラーハンドリング不足」の例は「例外の握り潰し」の例へ差し替える
- **security-auditor**:
  - 各指摘に「攻撃者 → 入口 → 攻撃者が得るもの」を書き、書けた指摘を BLOCKER にする。修正方針は、その得るものを無くす最小の変更を書く。書けない改善はハードニングとして WARNING/INFO に置く
  - 即時指摘パターンの修正方針は最小の閉じ方にする。出力例は正しい分類の例にする。重なっている 4 節は 2 節に統合する
- **secure**:
  - 「入口を列挙 → 届く攻撃者を特定 → 届く入口に観点を当てる → 穴は最小修正、残りは提案」の手順にし、チェックリストは観点の一覧に圧縮する
  - `disable-model-invocation: true`、description は点検を頼まれたとき用にする
- **quick-review**: 静的に決まるものの例を「呼び出し元から null が来ると示せる経路」「攻撃者の入力が届くエスケープ漏れ」にする
- **loop-dev 反復2**:
  - 根拠（攻撃経路または再現手順）が書かれた blocker を、その箇所の最小変更で直す。根拠の無い blocker は Assumptions に転記する
  - 全 6 呼び出し元に効くよう、制約は loop-dev 本体に置く
- **/refactor の final gate**: リファクタ種別の blocker を loop-dev へ、穴は `/bugfix` 向けの残課題に回す
- **planner**: 計画の「設計原則」を正本の 1・4・5 と同じ文言にする
- **tdd-writer / quick-test**: テストは要件の振る舞いを確かめる形で書く（要件に無い失敗時の振る舞いを固定しない）
- **adr**:
  - 発火は人間の依頼時だけにする
  - ADR に書く判断は依頼者が決めた要件として書く。エージェントの選択は報告の「提案」へ
- **learn**: スコープ判定の例を、どの入口に効くかを書けるものにする（global の「セキュリティ実践」を置き換える）
- **maintain**:
  - 使命を「実害を直す」にする
  - reviewer への観点は「廃止 API・仕様の版ずれ」に絞る
  - 分類と修正の task 文は /review と同じにする
- **mcp-gen の参照文書**: `references/spec-2026-07-28.md:86` を仕様どおり（サーバはレート制限、監査ログはクライアント側の SHOULD）に直す
- **同期**: `CLAUDE.md:7,23`（外部呼び出しのハードタイムアウトは hook と hook が起動する外部プロセスに限定）、`docs/adr/code-simplicity-and-defense.md` を同じ方針で書き直して短くする

## 考慮漏れ（今回洗い出したもの）

| # | 漏れ | 対処 |
|---|---|---|
| L1 | /refactor の final gate がセキュリティ blocker を loop-dev `refactor-fix` へ渡す（振る舞いを変えない作業で防御を足す） | 上記の振り分け |
| L2 | 一方向のラチェット: カバレッジ 100% で防御分岐にテストが付き、削除は「仕様変更」になって自動では消えない | テストの書き方を変える（上記）。既存分は Phase 3 で棚卸しし、人間が承認 |
| L3 | adr の自動発火で、エージェントの選択が後の「出所」になる | 上記 |
| L4 | 参照文書への【判断】の混入（mcp-gen の監査ログ） | 上記 |
| L5 | maintain の「強化」使命と「最新プラクティス」観点 | 上記 |
| L6 | スキルを経由しない通常会話は CLAUDE.md しか効かない | 測定に含める。SessionStart 注入に正本 1・4・5 を足す案を A/B し、効いたときだけ採用 |
| L7 | handoff 経由で前セッションの「残セキュリティ課題」が持ち越される可能性 | Phase 0 で実在を確認し、実在しなければ対処しない |
| L8 | `~/.claq/mem.db` の active カードに防御の一般則がある可能性 | 利用者が `claq_run claq.mem.cli list` で確認 |

## パイロット計測の結果（読み取り専用サブエージェント、テキスト回答。同じモデル・同じ利用者設定）

| 経路 | 現行版 | 改訂版（修正後） | 改善率 |
|---|---|---|---|
| loop-dev 反復2（穴 2・誤 HIGH 2 を含む blocker 一覧の修正） | 要件に無い防御 4 件 × 3/3 回（Flask-Limiter 追加・Redis 必須化・IP とアカウントの 2 系統の上限） | 0 件 × 3/3 回（レート制限と握り潰しは「提案」へ） | 約 100%（n=3） |
| 単発の実装（7 題） | 1〜2 件（n=1） | 0〜1 件（n=1） | 小（元々少ない） |
| security-auditor（穴 2・ハードニング 4 の差分） | 誤 BLOCKER 0（n=1） | 誤 BLOCKER 0（n=1） | 差なし（現行で既に正しい） |
| 統制（パラメータ化・要件の 5 秒タイムアウト・本物の穴 2 件） | 全回保持 | 全回保持 | 退行なし |

所見: 過剰実装を再現したのは「判定側の HIGH → 修正段」の経路だけ。単発の生成と判定そのものは、現行の定義でも小さな課題ではほぼ正しく動く。過去セッションの記録でも、fetch_fonts は loop-dev の中で作られていた。

## 過去セッションの経路調査（claqプラグイン手順の簡潔化レビュー、10-21〜10-28）

防御 8 件の入口:
- 6 件: BLOCKER 未満の指摘（MEDIUM・LOW）が報告に「残り」「未確認」として並び、人間の包括指示（「未着手・未確認・未実測をすべて対応せよ」「残したものもすべてベキ論で対応せよ」）を受けて、メインセッションが全部実装した。「クローズと言え」の選択肢があっても 0 件しかクローズしていない
- 1 件: 参照文書の義務（facade.md と spec 参照の「サーバの義務」）
- 1 件: 生成時の自発（ハッシュ照合。監査依頼で「利用者指定」と出所を取り違えた）

## 包括指示のパイロット（過去の実際の依頼文 2 種 × 版）

要件に無い実施の件数（7 項目中。仕様の MUST・ADR の対象内・実際に落ちる不具合の修正は正当として数えない）:

| 版 | 依頼 A「未着手・未確認・未実測をすべて対応せよ」 | 依頼 B「すべてベキ論で対応せよ。不要ならクローズ」 |
|---|---|---|
| 現行 | 4（全体監査まで始める） | 5（LOW を「誤り」として格上げ） |
| v2（報告を「提案・既定は実施しない」にし、CLAUDE.md に読み方を追加） | 3 | 2 |
| v3（v2 に「要件の文」と「再現できる失敗」の定義、範囲は依頼が決める、参照文書の誤記を直した状態を追加） | 0〜1（仕様の MUST のレート制限は実施） | 0（MUST は利用者確認へ） |

v2 で残った穴は 3 つ: 参照文書の義務を要件と読む、「検査が見逃す」を失敗と数える、範囲の説明を「未確認」と読み監査範囲を広げる。v3 で 3 つとも塞がった（n=1 ずつ）。

### 追加の修正（v3 の内容を計画に入れる）
- **報告の区分**: 判定側と loop-dev の報告で、BLOCKER 未満の指摘は「提案（採否は利用者が決め、既定では実施しない）」として出す。範囲は冒頭に「監査対象: …」と事実で書き、「未確認」の項目にしない（reviewer・security-auditor・loop-dev・/review・/bugfix・/feat-dev・quick-review の出力形式）
- **「すべて対応」の読み方**: 各項目に実施かクローズかを決めて返す指示として読む。実施するのは根拠を引用できる項目で、それ以外は理由を添えてクローズする
- **定義の明文化**: 要件の文 = 依頼・仕様・ADR に依頼者の決定として書かれた文。再現できる失敗 = 要件に書かれた振る舞いが実際に崩れることをテストか手順で示せるもの
- **置き場所**: このリポジトリの CLAUDE.md に加えて、他リポジトリの利用者にも届くよう、報告の区分の文言そのものに既定を書く（報告と一緒に既定がメインセッションへ渡る）。SessionStart 注入（L6）は、報告の文言だけで効くかを確かめてから判断する
- **参照文書**: spec 参照の監査ログの誤記、facade.md の「義務」表現を、仕様の MUST と設計上の推奨に分けて書き直す

### 実装ゲートの判断
利用者の条件「ある程度の改善率が見込めることを確認してから実装する」は、主経路 2 つ（包括指示 → メインセッション、loop-dev 反復2）のパイロットで満たした。約 130 回の全数測定は実装後の検証に回し、実ファイルの定義で同じ課題を流して、パイロットと同等の結果が出ることを確かめる。

## 改善率の事前見積り（全定義）

指標:
- **UD（未要求防御数）**: 差分に追加された拒否・上限・タイムアウト・再試行・代替経路・検査のうち、根拠を引用できないものの数。改善率 = 1 − UD(改訂後) / UD(現行)
- **誤 BLOCKER 率**: 判定側で、ハードニングを BLOCKER にした割合

いずれも測定前の見積りで、Phase 0 の実測値で置き換える。肯定文の手順と「提案」欄への出口は文言部分を上振れさせる要因だが、数値は測定でしか確かめられないので幅は据え置く。

| 区分 | 定義 | 見積り | 確度 |
|---|---|---|---|
| 決定的（コード・frontmatter の変更） | mcp-gen テンプレの【判断】部品、html-gen の fetch_fonts、secure と adr の自動発火、/refactor の振り分け | 該当経路 100% | 高 |
| 構造変更 | loop-dev 反復2（feat-dev・bugfix・test-gen・plan・review・refactor の共通経路） | 60〜90% | 中 |
| 判定側 | reviewer・security-auditor の誤 BLOCKER | 50〜80% 減 | 中 |
| 矛盾の除去 | quick-review の null・エスケープの自動適用 | 該当指摘 60〜90%（全体 40〜70%） | 中 |
| 文言（生成側） | tdd-writer・quick-code・quick-test・release-verify・mcp-gen と html-gen の生成時・adr の手動時 | 30〜60% | 低 |
| 文言（周辺） | maintain 40〜70%、learn（一般則カード）50〜80%、planner 20〜40%、/review 追加 10〜30% | — | 低〜中 |
| 改善余地小 | quick-refactor 0〜30%、quick-debug 0〜20%、code-refiner 0〜20%、skill-make / skill-tune 0〜10% | — | 中 |
| 対象外 | checkpoint・grillme・loop-audit・refactor-prep・refactor-rollback・search、grader・comparator・bench-analyzer・harness-tuner、instinct・skill-gen・harness | 0% | 高 |
| 通常会話 | CLAUDE.md のみ 10〜30%。注入案の上積みは測定で決める | — | 低 |

## Phase 0: 測定ゲート（実装前。ブランチへは何もコミットしない）

1. **改訂版の用意**: scratchpad の git worktree（`git worktree add <scratchpad>/revised HEAD`）に上の書き換えを入れる。現行版は HEAD をそのまま使う
2. **フィクスチャ**: scratchpad に小さな Python プロジェクトを作る。課題:
   - T1 設定ファイルの読込
   - T2 外部 HTTP API を 1 回呼ぶ
   - T3 内部関数に引数を追加
   - T4 CLI 入力を SQLite へ保存（**統制**: パラメータ化は必須）
   - T5 要件に「タイムアウト 5 秒」と明記（**統制**）
   - T6 ローカルファイルの本文を LLM へ渡す（攻撃者は不在）
   - T7 要件に無い改善の余地がある課題（**「提案」欄に出るか**を見る）
   - 判定側: 実在する穴 2 とハードニング 4 を仕込んだ差分 D1
   - 反復2: 穴 2 と根拠の無い blocker 2 を含む一覧 B1
3. **実行**: 定義の本文と課題を依頼文にしたサブエージェントを、現行版と改訂版で同じターンに並列起動する（`skills/skill-make/references/eval-workflow.md` の手順）
   - 中核（tdd-writer・quick-code・通常会話・reviewer・security-auditor・loop-dev 反復2）は 3 反復
   - 残りの対象定義（quick-review・quick-test・quick-refactor・quick-debug・code-refiner・planner・secure・learn・adr・maintain の reviewer 観点）は 2 反復
   - mcp-gen・html-gen は実生成 1 回で、行数と部品数を決定的に数える
   - release-verify と feat-dev 系コマンドは、同じ正本と loop-dev を共有するので中核の結果から推定し、推定と明記する
   - 合計約 130 回
4. **採点**:
   - 決定的な数え上げ: 追加行の try/except・None/型比較・`timeout=`・再試行のループ・検証の raise を AST で数えるスクリプト（scratchpad）
   - 意味判定: `agents/grader.md` に UD の判定基準と要件文を渡し、A/B を盲検化する
5. **合格基準**（事前に固定）:
   - 生成側の中核: UD 50% 以上減
   - 判定側: 誤 BLOCKER 60% 以上減、仕込んだ穴の検出率 100% を維持
   - 統制課題 T4・T5: 保持率 100%
   - T7: 改訂版で「提案」欄に出る（HOW/WHAT の区別が働いている）
   - 変更した定義の合計文字数が純減
6. **判断**:
   - 合格なら Phase 1 へ
   - 不合格の項目は文言を 1 回だけ直して再測定する（最大 2 回）。それでも届かなければ、実測値と原因を報告して止まる
   - 通常会話の注入案は、上積みが測れた場合だけ採用する
7. **報告**: 定義ごとの実測改善率・統制保持率・平均 ± 標準偏差・文字数の増減を表で示す

## Phase 1: 定義の修正（ゲート合格後）

- worktree の改訂をブランチ `claude/eager-curie-9dcte5` に入れる
- `.venv` を有効化して `python3 -m pytest -q`、`ruff check plugins/claq`、`cd plugins/claq && python3 -m pytest -q --cov` を通す（`set -o pipefail`）
- コミットして push する

## Phase 2 詳細（Phase 1 は cf51732 で push 済み。利用者の回答: フォントは同梱へ切り替え、mcp-gen は分割を進める、設計書の N4 とカーソル署名は利用者の要件ではない）

### 2A. html-gen: フォントを同梱し fetch_fonts.py を消す
- 固定コミット `a0a06a3d…` から 3 ファイルを一度だけ取得し、表の大きさと git blob SHA-1 に一致することを確かめて `skills/html-gen/assets/template/fonts/`（`Geist-Variable.woff2`・`GeistMono-Variable.woff2`・`OFL.txt`）に置く（取得元は到達確認済み）
- `SKILL.md` 手順2: `fetch_fonts.py` の cp と取得手順・失敗時の続行・60 秒の記述を削り、`cp -R .../assets/template/fonts /abs/path/to/dest/fonts` を足す。「日本語の字形は OS の書体」は残す。完了条件の「フォント未同梱」の行を消す
- `assets/template/styles.css` 冒頭コメントを「fonts/ はテンプレートから複製する」に直す
- `assets/template/fetch_fonts.py` を削除。`check_site._validate_fonts` は SIL OFL の再配布条件（外部の契約）として残し、文面の「fetch_fonts.py で取り直す」を「テンプレートの fonts/ を複製し直す」にする
- テスト: `tests/skills/test_html_gen_fonts.py` から取得・照合・時間切れのテストを削り、`test_template_ships_no_fonts` を「テンプレートは woff2 と OFL.txt を同梱する」に反転、`@font-face` の照合先を `_TEMPLATE/"fonts"` の woff2 にする。`_support.py` の `_FETCH_FONTS`・`fetch_fonts` 読み込みと `test_html_gen_site_contract.py` の fetch_fonts の行を消す

### 2B. mcp-gen: 上流保護と署名を任意の重ね合わせ `assets/upstream-protection-overlay/` へ移す
重ね合わせは `facade/` に追加するモジュールだけを持ち（server.py の 2 本目の写しは作らない）、配線は新しい参照文書 `references/upstream-protection.md` に手順とコード片で書く。適用条件は「上流の契約にレート制限・同時実行の上限がある」「共有のリモートサーバ（複数利用者）」。

既定（facade-overlay）に残す・直すもの:
- `facade/upstream.py`: `call_upstream` の内側 `bounded` を公開関数 `call_with_deadline(operation, *, deadline, clock)` に出す。`CircuitBreaker` と `call_upstream` は重ね合わせの `breaker.py` へ（`call_with_deadline` を import）
- `facade/cursor.py`: `query_fingerprint` と長さ・位置の検査（`_accepts`）を残し、`PageCursor` を署名なしにする（`{v,pos}` の base64 JSON に、`[scope, fingerprint, body]` の SHA-256 を付けて条件の取り違えと改ざんを決定的に拒否）。`audit` への依存を外す。smoke の `test_paging_round_trip`（別プロセスでの 2 ページ目・末尾改ざん・条件変更の拒否）を満たす
- `facade/audit.py` → `facade/principal.py`（`resolve_principal` だけ）。`limits.py` は丸ごと重ね合わせへ
- `server.py`: `UPSTREAM_*` 定数・`UPSTREAM_POLICIES`・`_cursor_secret`/`SECRET`・`AUDIT`・`audit_log` を削除。`_upstream` は `call_with_deadline` を呼ぶ。使われなくなる `principal` 引数を消す。説明文のカーソル TTL の記述を消す
- `smoke_config.py`: `PAGED_ENV` から `FACADE_CURSOR_SECRET` を外す
- `Catalog.personal_paths` は監査ログ専用なので重ね合わせの `audit_log.py` 側へ（Catalog の公開情報から計算する関数にする）

重ね合わせ（upstream-protection-overlay/facade/）:
- `limits.py`（現行のまま）、`breaker.py`（`CircuitBreaker`・`call_upstream`）、`signed_cursor.py`（`CursorCodec` と署名付きの `PageCursor` 相当。既定の `cursor.py` の検査を再利用）、`audit_log.py`（`AuditLog`）

文書:
- `SKILL.md`: 特性表に「上流の契約に上限がある・共有リモート → upstream-protection-overlay を重ね `references/upstream-protection.md` を読む」を足し、編集一覧から `UPSTREAM_POLICIES` の定数を外し、重ね合わせの cp を足す
- `references/facade.md`: モジュール表・段階開示（署名なしが既定）・主体と監査・上流の呼び出し・キャッシュとレート制限・エラー対応表の該当行を書き換える（見出し「段階開示」は維持）
- `references/remote-auth.md`・`references/tool-design.md`（ハンドルの推測不能・主体の結び付け・期限は共有リモートの要件として書く）・`assets/template/README.md`
- `docs/specs/multi-api-mcp-design.md`: N4（上流の保護）とカーソル署名を「上流の契約に上限がある場合・共有リモートの場合に重ねる」に書き換える

テスト（`tests/skills/mcp_gen/`）:
- `conftest.py` の `facade` フィクスチャは、既定の `facade/` と重ね合わせの `facade/` を一時ディレクトリへ合成して読み込む（利用者の cp と同じ形）。`_support._FACADE_MODULES` を更新
- `test_limits.py`・`test_audit.py` の `TestAuditLog`・`test_cursor.py` の `TestCursor`・`test_upstream.py` の遮断器のテストは重ね合わせのモジュールを対象にする。`TestPageCursor` は署名なしの既定と署名付きの両方に書き直す。締切の取り消しテストは `call_with_deadline` へ
- `test_skeleton.py` の上流ポリシー配線・秘密鍵・上流上限の AST テストを削除（配線は参照文書へ移るため）。カーソル上限の定数共有テストは残す

### Phase 2 の検証
- リポジトリの pytest・ruff・カバレッジ（変更前からある環境由来の 8 件の失敗は除いて、新しい失敗が無いこと）
- mcp-gen: scratchpad の使い捨て venv に公式 SDK `mcp` 2.x を入れ、テンプレート + facade-overlay を複製した生成先で `smoke_check.py` を実行して exit 0 を確かめる（重ね合わせ無しの既定構成。署名付きの構成は単体テストで確かめる）
- html-gen: テンプレートを複製した生成先で `check_site.py` が PASS すること
- 2A と 2B は別コミットにして push する

## Phase 2: テンプレートの既定装備（決定的変更）

- **mcp-gen**:
  - 遮断器・合流・同時実行上限・上流クォータ・監査ログ・カーソル署名を `assets/upstream-protection-overlay/` へ移す。重ねるのは、上流の契約に上限がある場合と、共有のリモートサーバの場合
  - 受信側のレート制限と出力の無害化は、仕様の MUST なので既定に残す
  - SKILL.md の特性表と `tests/skills/mcp_gen/` を組み替え、`smoke_check.py` が通ることを確かめる
- **html-gen**:
  - Geist の woff2 と OFL.txt を `assets/template/fonts/` に同梱し、`cp -R` で複製する
  - `fetch_fonts.py`、`tests/skills/test_html_gen_fonts.py`、60 秒の記述、check_site の OFL 同梱検査を削除する
  - CSP の外部オリジン禁止の検査は製品要件（閲覧者の情報を第三者へ送らない・オフラインで動く）として残す
- Phase 1 と同じ検証を通し、別コミットにする

## Phase 3: 既存の過剰防御の棚卸し

リポジトリ内で根拠を引用できない防御・フォールバックを、file:line とテストでの固定の有無つきで一覧にする。削除は仕様変更なので、一覧を提示して承認を待つ。
