# 引き継ぎ: 過剰な防御・フォールバックを足させない定義修正と、既存の過剰防御の是正

クラウドのセッション（2026-10-31〜11-01、ブランチ `claude/eager-curie-9dcte5`）から、ローカルのセッションへの引き継ぎ文書。このセッションで得た知見と、ローカルで続ける作業をここにまとめる。

同じフォルダの中身:

| パス | 内容 |
|---|---|
| `plan.md` | 承認済みの最終計画（判断の基準・C 項目の表・検証手順を含む） |
| `plan-history/defs-fix-with-measurement-gate.md` | 最初の計画（根本原因・L1〜L8・パイロットの結果・過去セッションの経路調査・改善率の見積り・Phase 0〜3）。**根本原因とパイロットの記録はここにしか無い** |
| `plan-history/phase2-and-remaining-first.md` | 残作業の初版（棚卸しの B/C 分類の元） |
| `plan-history/remaining-after-advisor-review.md` | advisor 3 体の精査を反映した版 |
| `measurements/` | L6 の A/B と実装後の検証の課題文・採点基準・全回答・採点結果・集計スクリプト・ベンチマーク（下の「測定」） |

## 1. 依頼と利用者の指示

最初の依頼（原文）:

> 最近のコミットで過剰なセキュリティ対策や安全措置ロジック、過剰なフォールバックなどを勝手に実装してしまうのを防止するためにスキル内容を修正したのだが、まだその傾向が強い。明示的にプロンプトを書いて指示すれば多少はマシなのだが、せっかくスキル化しているのでわざわざ言わなくても毎回遵守してほしい。他の厳し目な記載と競合してそっちによっている？当リポジトリのプラグイン内に作っているスキルを一通り調査して、修正計画を立案せよ。

途中の指示（時系列。判断の拠り所なので原文に近い形で残す）:

- 「決めてほしいこと」はエージェントが方針を決める。**べき論で考える**
- 実装前に、全スキル漏れなく改善率の見込みを示す。考慮漏れはこの時点で洗い出す。ある程度の改善率を確認してから実装に入る
- **プロンプトで否定を書くのは限界がある。できるだけ肯定文で処理手順に寄せ、無駄な記載をなくして簡潔な文章でエージェントの処理を誘導する**
- 「最新のベストプラクティスを踏まえた技術判断」は実装には必要だが、ハードニングへ引っ張る一因でもある。**折衷として、ベストプラクティスは「どう書くか（HOW）」に使い、「何を足すか（WHAT）」は報告の「提案」に回す**
- html-gen はフォントを同梱に切り替える。mcp-gen の分割は進める。設計書の N4（上流の保護）とカーソル署名は利用者の要件ではない
- **カバレッジ未達箇所はすべて潰す。残している作業もすべて完了させる**
- C1（SessionStart の失敗を exit 1 にする件）について「1 で終わると何が不都合か、エラーが正しく伝わるメリットもあるのでは」と問われた（→ 下の C1 の判断）
- **ADR の情報もメタ認知してべき論で対応する**（ADR はエージェントが書いた記録で、それ自体は根拠にならない）
- **すべて advisor にメタ認知させて真偽を精査させる**
- C 項目は計画どおり進める（AskUserQuestion で承認）
- **サブエージェントを大量に起動するとクレジットが枯渇するので、時間がかかってもよいので同時起動は 2 体まで**
- 作業ブランチはそのまま push してよい。ローカルへ切り替えるので、引き継ぎ文書を docs の適切なサブフォルダに作って push する（この文書）

利用者設定（ユーザープリファレンス）: 「既存実装に追従するだけでなく、最新のベストプラクティスを踏まえた技術判断を行い、根拠を示すこと。間違っていることは一切同調せずに、遠慮なく否定すること。」— これがハードニング側へ引く要因の 1 つ（L8 → 上の HOW/WHAT の折衷で相殺）。設定文言の見直し案は `plan-history/defs-fix-with-measurement-gate.md` の「ベストプラクティスとの折衷」にある（変更は利用者の判断）。

## 2. 原因の分析

定義（skills / agents / commands）が過剰防御を生む原因は 4 つ（詳細は `plan-history/defs-fix-with-measurement-gate.md`）。

1. 簡潔さ基準が自己申告で通る許可条件になっていた（「1 行で言えれば置ける」「エージェント自身の誤操作も失敗経路」「自分で書いた ADR・参照文書が出所になる」）
2. 逆向きの具体的指示が同居していた（quick-review の「null 経路の抜け」自動適用、reviewer の「エラーハンドリング不足」例、security-auditor の機構追加型の修正方針と CSP の例、secure の実装時自動発火、learn の global「セキュリティ実践」）
3. 「穴は最小修正」が判定側にしかなく、loop-dev 反復2 と /refactor の final gate が blocker を生成側へ素通しした
4. テンプレート（mcp-gen / html-gen）に判断由来の防御が既定で入っていた

**最大の経路**（過去セッション 10-21〜10-28 の調査）: 防御 8 件のうち 6 件は、BLOCKER 未満の指摘（MEDIUM・LOW）が報告に「残り」「未確認」として並び、人間の包括指示（「未着手・未確認・未実測をすべて対応せよ」「残したものもすべてベキ論で対応せよ」）を受けてメインセッションが全部実装したもの。残りは参照文書の「義務」表現 1 件、生成時の自発 1 件。

考慮漏れとして洗い出した L1〜L8 と処置:

| # | 内容 | 処置 |
|---|---|---|
| L1 | /refactor の final gate がセキュリティ blocker を loop-dev へ渡す | 振り分けを変えた（穴は /bugfix 向けの残課題へ） |
| L2 | カバレッジ 100% で防御分岐にテストが付き、削除が「仕様変更」になる一方向のラチェット | テストは要件の振る舞いを確かめる形で書く。既存分は棚卸し（B/C）で処理済み |
| L3 | adr の自動発火で、エージェントの選択が後の「出所」になる | 発火は人間の依頼時だけ |
| L4 | 参照文書への判断の混入（mcp-gen の監査ログ） | 仕様の MUST と設計上の推奨に分けて直した |
| L5 | maintain の「強化」使命と「最新プラクティス」観点 | 使命を「実害を直す」、観点を廃止 API・仕様の版ずれに絞った |
| L6 | スキルを経由しない通常会話は CLAUDE.md しか効かない | SessionStart 注入を A/B で測った → **不採用**（下の測定） |
| L7 | handoff 経由で「残セキュリティ課題」が次セッションへ持ち越される | コード読解でクローズ。引き継ぐのはユーザーの発話・変更したパス・ツール名だけ（`plugins/claq/src/claq/mem/handoff.py`）。残る経路は payload の明示の `handoff` キー（どの定義も設定していない）と `mem learn`（承認まで pending） |
| L8 | `~/.claq/mem.db` の active カードに防御の一般則がある可能性 | ローカルで確認してクローズ。active は 0 件（pending 91・archived 1）で、注入されるカードが無い（下の 10 章） |

## 3. 定義側の設計（cf51732）

- **実装の手順（7 ステップ、肯定文）**を `plugins/claq/agents/tdd-writer.md` に正本として置き、本番コードを書き換える 8 つの定義へ逐語で写す。`plugins/claq/tests/test_simplicity_criteria.py` が見出し「## 実装の手順」と全写しの一致を固定する。手順の要点:
  1. 要件の文を満たすコードを書く。書き方（パラメータ化・ネットワーク呼び出しのタイムアウト・commit 等）は現行の推奨で選ぶ
  2. 外部入力は入口で 1 回検証し、内側は型と呼び出し元を信頼する
  3. 失敗は例外・エラー値のまま返す
  4. 拒否・上限の検査は根拠（要件の文・攻撃者と入口・再現できる失敗・外部 API 契約）を引用できるときに書く
  5. 続行経路（握り潰し・既定値・再試行・代替経路）は続行を決めた文を引用できるときに書く
  6. 4・5 は根拠をコメント 1 行と報告の「追加した防御」に残す。要件に無いが入れたほうがよいもの（ベストプラクティス由来を含む）は報告の「提案」へ
  7. 原因の箇所を直し、使われなくなったコード・テスト・互換分岐は同じ変更で消す
- パイロットで直した点: 初版は防御と続行の根拠を 1 つにまとめていたため「再現できる失敗」を根拠に握り潰しが入った（4 と 5 に分けた）。タイムアウトを提案に回しすぎた（1 に書き方として明記）
- **用語の固定**: 要件の文 = 依頼・仕様・ADR に依頼者の決定として書かれた文（エージェントが書いた参照文書・コメントの「義務」は含めない）。再現できる失敗 = 要件の振る舞いが崩れることをテストか手順で示せるもの（検査が何かを見逃すことだけでは数えない）
- **報告の区分**: BLOCKER 未満の指摘と生成側の改善案は「提案」（採否は利用者、既定では実施しない）。「すべて対応」「ベキ論で対応」は各項目に実施かクローズかを決めて返す指示として読む。範囲は依頼が決め、範囲の説明を「未確認」として並べない
- 判定側: reviewer は根拠を書けた指摘だけ CRITICAL/HIGH、余剰（到達しない防御・重複検証・呼び出し元 1 つの抽象）と根拠の無い足された検査・続行経路を HIGH。security-auditor は「攻撃者 → 入口 → 得るもの」を書けたものを BLOCKER、修正方針は最小の変更。secure は `disable-model-invocation: true`
- loop-dev 反復2 の依頼文: 根拠の書かれた blocker を最小の変更で直し、新しい仕組みが要る blocker と続行を決めた文の無い続行経路は提案へ
- CLAUDE.md と `docs/adr/code-simplicity-and-defense.md` を同じ方針で書き直した

## 4. コミット一覧（main..claude/eager-curie-9dcte5）

| コミット | 内容 |
|---|---|
| cf51732 | 定義の修正（上の 3 章） |
| 08447c9 | html-gen: Geist / Geist Mono と OFL.txt をテンプレートに同梱し、`fetch_fonts.py`（SHA-1 照合と 60 秒上限）を削除 |
| f84ce3a | mcp-gen: 遮断器・クォータ・同時実行上限・合流・署名付きカーソル・監査ログを `assets/upstream-protection-overlay/` へ移し、既定は再試行と締切だけにする |
| d475634 | Python 3.12/3.13・root・Linux・環境変数の git 設定で落ちる/空振りするテストを直し、カバレッジ 100% に戻す。f84ce3a が入れたテスト失敗（自リポジトリの秘密情報走査が `lambda token: token[:-2]` を誤検出）もここで修正 |
| 4624370 | B 項目（到達しない分岐・入口の検証と重複する検査）の削除と旧部品の文言 |
| 9cccf1c | C 項目（src） |
| 3215aec | C 項目（mcp-gen・html-gen の同梱物） |
| e911735 | ADR の再監査と上限の実測 |

全コミットの時点で `python3 -m pytest -q` 全件通過（最終 5093 passed / 7 skipped）、`ruff check plugins/claq` 通過、`cd plugins/claq && python3 -m pytest -q --cov` 100%。

## 5. 既存の過剰防御の処理（判断の基準と結果）

判断の基準（メタ認知。`plan.md` の「判断の基準」）:

1. `docs/adr/` はエージェントが書いた記録。ADR に書いてあるだけでは根拠にしない
2. 防御を残すのは、名指す攻撃者が脅威モデルに合い、かつ外すと具体的な失敗がテストか手順で再現するときだけ
3. 脅威モデル: 保護フックの対象は「エージェントの近道・誤操作」と「プロンプトへ入った不信テキストに操られたエージェント」。同一 UID の人間・プロセスによる意図的な回避は対象外
4. 残す理由はコードの 1 行コメントに書く。ADR は現行の状態に直すことと、根拠を失った文を削ることにだけ使う
5. 上限の数値は実測し、測った値をコメントに残す。値を変えるのは余裕が足りないと分かったときだけ

C 項目（src、9cccf1c）の主なもの。全項目の表は `plan.md`:

- **C1 SessionStart の失敗**: 失敗したら stdout を空にして exit 1。Claude Code は正しい JSON を出した hook を終了コードによらず成功扱いにするので、失敗時に JSON を出すと通知が出ない。stdout が空で exit 1 ならセッションは続き、stderr の 1 行目つきの通知が出る（code.claude.com/docs/en/hooks）。Claude が使えなくなることはない。手で確認済み（`CLAQ_HOME` をファイルに向けて `claq.mem.cli context` → stdout 空・exit 1・stderr に原因）。**残る隙間**: Python 3.12 未満と launcher 不在では exit 0 のまま。Copilot CLI での扱いは未検証
- C2 handoff の握りを削除。C3 stdin が JSON object でなければ明示のエラー（object でない stdin を送るホストでは毎回通知が出る。正しい挙動）
- C4 actor は POSIX で `uid=<getuid()>`、Windows で `getpass.getuser()`。取れなければエラー
- C5 mem.db の symlink 拒否は**維持**（リンク先の別ファイルを chmod し、FIFO なら 20 秒止まる）
- C6 title/body のバイト上限を削除（実測: 1 MiB の body でも `_format_injected_item` は最悪 0.48 秒）
- C7 git 不在（`FileNotFoundError`）だけ維持、git のタイムアウトは伝える。C8 remote URL の制御文字検査を削除
- C9 bg ログを作れなければ起動を失敗にして原因を stderr へ（DEVNULL へ流さない）
- C10 pre_compact の catch-all を削除し、`claqProtectionDisabled` の印は保護フック 4 つ（block_no_verify・pre_bash_commit_quality・bash_config_protection・config_protection）が落ちたときだけ出す
- C11〜C13 lint の例外・GC スタンプ・grok の OSError を握らずに伝える。C14 git hosting を判定できないときは github と見なさず「不明」として両方の検査を当てる
- C15〜C18 frontmatter の lenient と入れ子 32 の上限、validators の `--optional`、env-template の symlink 検査を削除
- C19 ツール名の除外・C20 コミット数の検査は**維持**（再現できる失敗がある）

同梱物（3215aec）:

- mcp-gen の見本の検索は、自分が返した形（`o:件数`）以外のページトークンを 400 にする（既定のカーソルは署名しないので書き換えられ、`o:-3` で末尾から行が返っていた）
- `max_retry_after` を削除（待ちの上限は締切だけ）。負の `retry_after` は待たない、NaN は諦める
- `UpstreamError` の detail の 200 字切りを削除（制御文字の除去は残す）
- 既定の主体を固定値にし、`principal.py` と guard の `principal_errors` を削除
- 未知の引数の文面の件数・長さ上限を削除。inputSchema の真偽値のサブスキーマを落とさず残す（落とすと引数を禁止していた）
- 旧仕様の走査の 1 MiB 上限を削除（FIFO・シンボリックリンクを開かない検査は残す）
- html-gen の check_site は tokens.json をサイトの中からだけ読む。app.js の保存済み配色の正規表現・`IntersectionObserver` の有無・空の一覧の早期 return を削除。design.md の「必ず」に理由を書いた
- **計画から変えた点**: app.js の `baseLabel ||` は維持（check_site が `data-base-label` を必須にしていないので、外すと「配色を undefined に変更しました」になる）

ADR の再監査（e911735）:

- 脅威モデルを `docs/adr/hook-failure-direction.md`・`docs/adr/shell-analysis-boundary.md` で上の定義に揃えた
- **stdin を読めないときの deny は維持**。一度は「エージェントには作れない失敗なので許可へ倒すべき」と考えたが、許可へ倒すこと自体が続行を決めた文の無い続行経路になる（CLAUDE.md）。読み取りの不具合（Windows の通常パイプに `select` を使うと全呼び出しで起きる）が保護の全面的な喪失として黙って進むのを防ぐ。ADR の理由（「攻撃者が stdin を壊す」）だけを直した
- 保護フック以外の hook の失敗（exit 1、SessionStart は成功時だけ JSON）と bg ログの作成失敗を ADR に書いた
- 実測と判断:

| 対象 | 実測（Linux・Python 3.13） | 判断 |
|---|---|---|
| `MAX_COMMAND_TOKENS = 5000`（`plugins/claq/src/claq/hooks/shell_tokens.py`） | 最悪形 `git ` × 4,999 で block_no_verify の起動から終了まで 4.9 秒（`has_bypass_flag` がトークン数の二次）。timeout 15 秒の約 3 倍 | コメントの「20 倍以上」は偽なので実測値に直した。値は据え置き（Windows は未測定） |
| `MAX_COMMAND_BYTES = 64 KiB` | 1 トークン 64 KiB で 0.2 秒 | 据え置き、実測をコメントに |
| lint 兆候の走査上限 256 KiB | テキストは payload 由来で stdin の上限 1 MiB（超過は deny）が抑える。走査は線形で 1 MiB 25 ms・16 MiB 0.4 秒 | 重複なので削除 |
| 全文の上限（`_FULL_TEXT_MAX_BYTES`、旧 `_LINT_SIGNAL_MAX_TEXT_BYTES`） | 置換前はディスクから読み stdin の上限が効かない。置換後は `replace_all` を重ねると倍々に伸びる | 維持し、名前と理由を直した |
| `_MAX_EDITS = 64`（`git_config_text.py`） | 上限なしだと 1 MiB の payload に 13,981 件入り、256 KiB の全文で 5.2 秒 | 維持し、実測を残した |
| 先頭 NUL・UTF-16 BOM の秘密走査 | — | 維持（エージェントが Windows の `Out-File` で UTF-16 の秘密ファイルを作る・バイナリに秘密が入る誤操作で再現する） |
| stdin 最初のバイト 2.0 秒・読み取り 3.0 秒・`_BG_FAILURE_TAIL_MAX_BYTES` 4096・hooks.json の timeout | ホストの書き込みはパイプに溜まるので、インタプリタの起動の遅さは 2.0 秒に数えられない。末尾 4 KiB は 1 行通知の読み方 | 変更なし |

## 6. 測定

すべて `Agent` のサブエージェントに定義と課題の文面を渡す模擬（この環境では `claude -p` の認証が使えなかった）。プロンプト内のパスはクラウドのスクラッチパッド（`/tmp/claude-0/...`）を指しているので、ローカルで流し直すときは置き換える。

### パイロット（実装前、`plan-history/defs-fix-with-measurement-gate.md`）

- loop-dev 反復2: 現行は要件に無い防御 4 件 × 3/3 回（Flask-Limiter・Redis 必須化・2 系統の上限）→ 改訂版 0 件 × 3/3 回
- 包括指示（過去の実際の依頼文 2 種）: 現行 4〜5 件 → v3 で 0〜1 件
- 単発の生成と判定そのものは、現行でも小さな課題ではほぼ正しかった。過剰実装を再現したのは「判定側の HIGH → 修正段」と「包括指示 → メインセッション」の経路

### L6: SessionStart への基準注入の A/B（`measurements/protocol.md`）

- 条件 A: 利用者設定 + 規則を持たない CLAUDE.md。条件 B: A + 注入 4 行（`measurements/protocol.md` の「注入する 4 行」）
- 課題 10 題（T1〜T8・C1・C2）、条件ごとに 5 回、交互に起動。回答者のプロンプトから採点側の語（「統制」等）を外した（`measurements/prompt_A.md`・`prompt_B.md`）
- 採点は別のエージェント 2 体が、ファイル名を伏せて順番を混ぜて行った（`measurements/grader_prompt.md`・`blind/rubric.md`、対応は `mapping.json`）。採用の規則は実行前に固定
- 結果（`python3 measurements/tally.py measurements grade_1.json grade_2.json`）:

| 採点者 | A の UD/回 | B の UD/回 | 統制 |
|---|---|---|---|
| 1 | 5.4 ± 0.5 | 5.4 ± 1.5 | 両条件 25/25 |
| 2 | 4.4 ± 0.5 | 4.0 ± 1.2 | 両条件 25/25 |

- **判定: 不採用**（規則 1「1 件以上減る」を満たさない）。コード変更なし
- A は T3・T8 以外ほぼ 0。UD の大半は T8（第三者の Web ページを取得して LLM に渡す）の取得タイムアウト・本文サイズ上限・`errors="replace"`・出力の制御文字除去で、両条件同数。T8 には実在する攻撃者がいるので、これらの一部は根拠があり、**採点基準が厳しすぎた可能性がある**

### 実装後の検証（実際の定義ファイル、各 2 回、`measurements/verify_protocol.md`）

| 経路 | 結果 | 合格基準 |
|---|---|---|
| tdd-writer・quick-code（T1〜T8） | UD 課題平均 0.12〜0.50、統制 100%（`grade_gen.json`・`mapping_gen.json`） | 合格 |
| reviewer・security-auditor（差分 D1: 穴 2・ハードニング 4） | 穴 2 つを毎回 CRITICAL、ハードニングの HIGH 以上 0 | 合格 |
| loop-dev 反復2（blocker 一覧 B1） | 1・2 を修正、根拠の無い 3・4 を提案へ（2/2） | 合格 |
| メインセッション + 実 CLAUDE.md（C1「すべて対応」・C2「ベキ論で対応」） | 1・2 を実施、3〜5 を理由付きでクローズ（2/2） | 合格 |

観察: reviewer の 1 回が `request.args.get("q", "")` の既定値を「続行を決めた文の無い続行経路」として HIGH にした（仕込み外）。正当な既定値まで指摘する逆方向の傾向の可能性（ローカル作業 5）。

測定の限界: サブエージェントでの模擬（実際の Claude Code のメインセッションや SessionStart ではない）、1 回で全課題に答えさせたので課題間の持ち越しがある（両条件で同じ）、採点者もモデル、回数が少ない。

## 7. 誤りと訂正の記録

- f84ce3a で入れたテスト失敗を、当初「環境の失敗」と誤って報告した。実際は自リポジトリの秘密情報走査がテストの変数名を誤検出したもので、d475634 で変数名を変えて直した
- advisor 3 体が前版の計画で見つけた誤り: C1 は exit 1 にしても JSON を出すと成功扱い、A-C1 と A-C3 の根拠の引用（facade.md・RFC）が誤り、A-C5（`fan_out` の空入力）は維持が正しい、app.js の守りの一部は正当なマークアップの差を扱う、e2e の `data-theme="undefined"` は到達しない、「ADR が攻撃者を名指せば根拠」は不十分
- stdin を読めないときの方向を一度「許可へ倒す」と判断しかけ、上の理由で撤回した

## 8. ローカルで続ける作業

1. **L8（必須）**: active カードに防御の一般則（入口に結び付かない「常に検証する」等）が無いかを見る。あれば利用者が判断して `claq_run claq.mem.cli forget <key>` で外す（status が archived になり注入されなくなる）
   ```bash
   . "$HOME/.claq/env.sh" && claq_run claq.mem.cli list
   ```
2. **実 `claude -p` での測り直し**: `measurements/` の課題で L6 と実装後の検証を流し直し、模擬の限界を外す。先に T8 の採点基準を見直す（取得タイムアウト・サイズ上限・出力の制御文字除去は、第三者のページという攻撃者がいるので「根拠あり」に移すのが妥当か）。採用の規則は `measurements/protocol.md` のものを使う。L6 が採用になったら、注入は `plugins/claq/src/claq/mem/cmd_context.py` の `_build_context` で `<claq-memory>` の**外**に置く（メモリ節はデータとして扱われるので指示を混ぜない）
3. **Copilot CLI での C1 の確認**: SessionStart の hook が stdout 空・exit 1 を返したときに、セッションが続き利用者に通知が出るかを確かめる。出ないなら報告する
4. **Windows でのトークン上限の測定**: `measurements/bench_hooks.py` を参考に、`git ` × 4,999 の Bash payload を `runtime/claq-hook.cmd` 経由で block_no_verify に渡して時間を測る。timeout 15 秒に余裕が無ければ、`MAX_COMMAND_TOKENS` を下げるか次の 6 を行う
5. **reviewer の逆方向の指摘の観察**: 正当な既定値（省略可能なクエリ引数の `""` 等）を「続行経路」として HIGH にするかを、数回流して確かめる。再現するなら reviewer の余剰の節の文言を見直す（「失敗の後に続行する経路」と「省略時の既定値」を区別する）
6. **提案（採否は利用者）**: `block_no_verify` の `has_bypass_flag` をトークン数に対して線形にする。上限の余裕が約 3 倍しか無い原因

## 9. 作業環境の注意

- 開発用 venv はリポジトリ直下 `.venv`（`scripts/install-dev.sh`）。変更後は `python3 -m pytest -q` と `ruff check plugins/claq`、カバレッジは `cd plugins/claq && python3 -m pytest -q --cov`（`fail_under=100` はこのディレクトリでのみ効く）。パイプするときは `set -o pipefail`
- 3.12 での確認は別の venv に `pip install -e plugins/claq` と pytest を入れて流す（入れないと `No module named claq`）
- mcp-gen の smoke は公式 SDK（`mcp` 2.x）を入れた venv が要る。テンプレート一式（必要なら facade-overlay も）を一時ディレクトリへ複製し、`python smoke_check.py` が exit 0 になることを確かめる
- html-gen はテンプレート一式と `references/tokens.json`・`fonts/` を一時ディレクトリへ複製して `python3 check_site.py <dir>` が PASS。app.js の挙動は Playwright で開いてコンソールエラーが無いことを見る（`measurements/pw_check.js`）
- コミットメッセージに Co-Authored-By / Claude-Session の行を付ける運用。PR は依頼されたときだけ作る
- ADR（`docs/adr/`）には経緯・測定の記録を置かない方針なので、測定の記録はこのフォルダに置いている

## 10. ローカルでの結果（2026-11-02）

8 章の作業をローカル（macOS・Python 3.14）で進めた結果。

| 作業 | 判断 | 結果と理由 |
|---|---|---|
| L8 active カード | クローズ | active 0 件で外す対象なし。pending に防御寄りのカード `pitfall-a4485436`（二乗の入力から長さの上限を逆算する）があるが、注入されない。`promote` されれば注入経路になる |
| T8 の採点基準 | 実施 | ページの作者を攻撃者として「攻撃者 → 入口 → 得るもの」を書けるので、取得のタイムアウト（全体の期限を含む）・本文サイズの上限・モデル出力の制御文字の除去を「数えない」へ移した。`errors="replace"`・URL スキームの限定・`stop_reason` による拒否は数えると明記した（`measurements/protocol.md`・`blind/rubric.md`）。プロンプトのパスは `/home/user/claq/` だった（`/tmp/claude-0/` は無かった）のでローカルへ置き換えた |
| 採点し直し | 実施（作り直しなし） | 新しい乱数で伏せ直し、採点者 2 体（互いのファイルを読んでいないことをトランスクリプトで確認）。L6: A 2.4 → B 2.0 / 1.8 件/回で規則 1 を満たさず**不採用のまま**、注入は足さない。生成側: UD 課題平均 0.38〜0.50・統制 100% で**合格のまま**（`grade_*_v2.json`・`mapping*_v2.json`、`python3 tally.py l6|gen ...`）。結論が変わらないので回答は作り直していない |
| Copilot CLI の C1 | 確認して報告 | Copilot CLI 1.0.79 で `--plugin-dir` に claq を読ませ、`CLAQ_HOME` をファイルに向けた。`-p`・対話とも、セッションは続き回答も出るが、画面への通知は無い。`~/.copilot/logs` に ERROR と stderr の 1 行目が残る。ADR（`hook-failure-direction.md`）に現行の状態として書いた。通知を出す手段を足すかは利用者の判断 |
| Windows の計測 | 実行できず | このマシンに Windows・`pwsh`・VM が無い。代わりに macOS で測った（下の作業 7） |
| reviewer の既定値 | クローズ | `reviewer_default_protocol.md` を固定して 3 回流し、別のエージェントが判定。3 回とも `request.args.get("q", "")` を HIGH 以上にせず、穴 2 つは毎回 CRITICAL、ハードニングの HIGH 以上は 0（`measurements/reviewer_default/`）。再現しないので reviewer.md は変えない |
| `has_bypass_flag` の計算量 | 実施 | `sh -c` で最悪形を束ね、`\` で 2 方言にすると、上限内の 60KB の入力で block_no_verify が 15.84 秒（timeout 15 秒）。kill された hook は許可として扱われるので再現できる失敗。位置ごとの集計を 1 回の走査で作る形にし、判定は旧実装と 102 万件のランダム入力・既存テストの全呼び出しで不一致 0。同じ入力で 0.19 秒。最悪形の時間を縛るテストを置いた |

残課題・提案:

- **提案（採否は利用者）**: 線形になったので `MAX_COMMAND_TOKENS = 5000` の根拠（二次の走査）は弱まった。外すと 5,000 トークン超のコマンドが通るようになる判定の変更なので、今回は据え置いた
- Windows での実測は未実施
- Copilot CLI で SessionStart の失敗を利用者へ見せる手段は無い（ログだけ）
