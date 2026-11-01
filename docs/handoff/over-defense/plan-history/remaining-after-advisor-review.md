# 残作業の完了計画（カバレッジ 100%・既存の過剰防御の処理・未実施の検証）— advisor 精査後の版

## Context

Phase 1（定義の修正 cf51732）と Phase 2（html-gen 08447c9・mcp-gen f84ce3a）は push 済み。利用者の指示は「カバレッジ未達をすべて潰し、残している作業をすべて完了させる」「ADR の情報もメタ認知してベキ論で対応する」「すべて advisor にメタ認知させて真偽を精査する」。

前版の計画を独立した advisor 3 体（src の判断／同梱物の判断と不具合／テストの修正と ADR の扱い）が実コードで精査した。誤りの主なもの:
- C1 は exit 1 にしても stdout に JSON を出すと Claude Code が成功扱いにして通知が出ない
- A-C1 と A-C3 は根拠の引用が誤り
- A-C5 は維持が正しい
- app.js の守りの一部は正当なマークアップの差を扱っている
- e2e の不具合は到達しない
- 「ADR が攻撃者を名指していれば根拠」では不十分（ADR 自体がエージェントの記述）

以下は訂正を反映した版。C 項目は通る経路の振る舞いを変えるので、この計画の承認をもって利用者の決定とする。

## 進捗（2026-11-01 時点）

- 済み（push 済み）
  - 1 章: テストの環境依存とカバレッジ（d475634）。カバレッジ 100%、5122 件が通過。3.12 の venv でも対象テストが通過
  - 2-1 B 項目と旧部品の文言、2-3 の grok.sh・tag_stripping（4624370）
  - 途中で見つけた f84ce3a のテスト失敗（自リポジトリの秘密情報走査が、テストの変数名を誤検出）も d475634 で修正済み。報告に書く
- 利用者の承認済み: 2-2 の C 項目を、この計画どおりに進める（AskUserQuestion で「計画どおり進める」）
- 残り（この順に進める）
  1. 2-2 src の C 項目 → テスト・ruff・カバレッジ → コミット
  2. 2-2 同梱物の C 項目 → mcp-gen の smoke と html-gen の check_site → コミット
  3. 2-4 ADR の再監査と上限の実測 → コミット
  4. 3 章 L6・実装後の検証（課題と採点基準をファイルに固定、5 回ずつ、盲検で採点）→ 結果に応じて注入を足すかコミット

## 判断の基準（メタ認知）

1. `docs/adr/` はエージェントが書いた記録（全コミットが Claude との共著。cf51732 はこのセッションが書いた）。ADR に書いてあるというだけでは根拠にしない
2. 防御を残すのは、次の両方を満たすときだけにする
   - 名指す攻撃者が宣言された脅威モデルに合う
   - 防御を外すと具体的な失敗がテストか手順で再現する
3. **脅威モデルの決定**（ADR の自己矛盾の解消）: 保護フックの対象は「エージェントの近道・誤操作」と「注入された不信テキストに操られたエージェント」とする。同一 UID の人間・プロセスによる意図的な回避は対象外とする。`hook-failure-direction.md:9`・`shell-analysis-boundary.md:8` の記述をこの定義に揃える
4. 残す理由は引用の形でコードの 1 行コメントに書く。ADR には、維持の判断を新しく書き足さない。ADR は、現行の状態に直すことと、根拠を失った文を削ることにだけ使う
5. 上限の数値は、実装の中で実測し、測った値をコメントに残す。値を変えるのは、測定で余裕が足りないと分かったときだけ

## 1. カバレッジと落ちるテスト（テスト側の環境依存。本番コードは変えない）

| テスト | 修正 |
|---|---|
| `tests/ci/test_scan_scaffold_drift.py:101` | transcript ファイルだけ `Path.stat` を失敗させ、偽物に到達したことを assert（`scan_scaffold_drift.py:196-197` を覆う） |
| `tests/lib/test_env_pointer_gc_and_resolve.py:169` | `_age` で古くしてから偽物を入れ（今は新しいファイルなので空振りの検査）、assert の前に `monkeypatch.undo()` |
| `tests/lib/test_git_hosting.py:141` | `codecs.lookup(enc).name == "ascii"` |
| `tests/mem/test_handoff.py:565` | `Path.is_symlink`→False・`Path.is_file`→True を直接差し替え、所有者の検査の `stat` だけ失敗させる。偽物に到達したことを assert |
| `tests/mem/test_repo_identity.py:289, 318` | `tests/conftest.py` に autouse フィクスチャを 1 つ置き、`GIT_CONFIG_COUNT`/`KEY_n`/`VALUE_n`/`PARAMETERS` を外し、`GIT_CONFIG_GLOBAL=/dev/null`・`GIT_CONFIG_NOSYSTEM=1` にする（git を呼ぶ全テストを利用者の設定から切り離す） |
| `tests/scripts/test_claq_hook_wrapper.py:98`（unreadable） | `chmod 000` の後、実際に読めないかで分ける。読めるときだけ `setpriv --bounding-set=-dac_override,-dac_read_search` を前置して読めないことを確かめ、それもできなければ skip |
| `tests/scripts/test_claq_hook_wrapper.py:203` | PATH の 2 つ目を `cat` だけを置いた一時ディレクトリにする |
| `tests/lib/test_grok_plugin_root.py:138, 146, 272` | `is_dir` は通し `stat` だけ失敗させる補助に替え、偽物に到達したことを assert（`grok_plugin_root.py:68-69, 98->94` を覆う。今は別の理由で通っている） |
| `tests/mem/test_redaction.py:315`（計時の揺れ） | 削除。二次オーダーの退行は 1 MiB の予算テスト（線形 0.2 s、二次 43〜280 s、上限 2 s）が要件（SessionEnd 10 s）に結び付いた形で捕まえるので、比のテストは重複 |

検証は 3.13 で実行し、加えて scratchpad に 3.12 の venv（pytest 入り）を作って同じテストを流す。

## 2. 既存の過剰防御の処理

### 2-1. B（到達しない・入口の検証の重複。削除し、固定していたテストと docstring を直す）

src
- `mem/knowledge_input.py:281-282`
- `hooks/pre_bash_commit_quality.py:710-714`
- `hooks/pre_bash_commit_quality.py:416-422`
- `hooks/commit_quality_scanner.py:640-645`
- `hooks/commit_quality_scanner.py:618-625`（到達はするが外側が同じ deny を返す。文面は一般の失敗文面になる）
- `mem/cmd_context.py:169`
- `lib/core_utils.py:154-155`
- `hooks/stdin_io.py:84-86, 89-90, 147-152` と AttributeError の捕捉（テストは `os.pipe` に替える。`tests/hooks/test_additional_hooks.py:429` の StringIO も）
- `mem/cli.py:281`
- `mem/logger.py:86`
- `lib/git_hosting.py` の不正な名前の既定値（名前は内部の定数からしか来ない）
- `claq-helpers.sh:144-154` の `''` の分岐（`${1:-200}` で到達しない）

同梱物
- `runtime/claq-helpers.sh:122-125`（終了コードが 2→1 になる）
- `runtime/env-template.sh:53-55`（`:25-26, :38-39` のコメントと、テストの docstring も直す）
- `mcp-gen/template/smoke_check.py:164, 189, 387`
- `scripts/grok.sh:39`
- `html-gen/e2e_contrast.js:162`

旧部品の文言（振る舞いは変えない）
- `facade-overlay/server.py:683-684`
- `adapters.py:298`
- `shaping.py:235, 292`（重ね合わせの場合として書き直す）
- `errors.py:18-21`（重ね合わせの場合として明記）
- `smoke_check.py:533`
- `catalog.py:142-151`・`facade.md:24`

### 2-2. C（判断）

src

| # | 判断 | 理由・注意 |
|---|---|---|
| C1 SessionStart の失敗 | 修正: 失敗したら stdout に何も出さず exit 1。`_handle_context` の握り（`cmd_context.py:285-289`）も消して `cli.main` の処理に任せる。`test_cli.py:51, 78`・`test_cmd_context.py:382` を直す | Claude Code は、正しい JSON を出せば終了コードを無視して成功扱いにする。stdout が空で exit 1 なら、セッションは続き、stderr の 1 行目つきの通知が出る（code.claude.com/docs/en/hooks）。残る隙間（Python 3.12 未満・launcher 不在では exit 0 のまま）と、Copilot CLI での扱いが未検証であることを報告に書く |
| C2 handoff の握り | 削除。`test_cmd_context.py:466` を直す | watchdog は子の終了コードを記録せず、stderr はどちらでも bg ログに残る |
| C3 stdin が JSON object でない | 修正: 明示のエラー | `context`/`handoff` にも効く。C1 と合わせると、object でない stdin を送るホストでは毎回通知が出る（正しい挙動として報告に書く） |
| C4 actor が取れない | 修正: POSIX は `os.getuid()`（失敗しない）、Windows は `getpass.getuser()` で、取れなければエラー | ADR が求めるのは uid。passwd に無い uid のコンテナで promote が止まらない |
| C5 mem.db の symlink 拒否 | 維持 | CLAUDE.md「永続化は mem.db のみ・0600」。リンク先の別ファイルを chmod し、FIFO なら 20 s のタイムアウトまで止まる（再現できる） |
| C6 title 4 KiB・body 64 KiB | 削除 | 実測の記録が無い。stdin は既に 1 MiB 上限で、active 化は人の承認を通る。実装時に 1 MiB の body を `_format_injected_item` に通して時間を測り、報告に書く |
| C7 git 不在で絶対パス | `FileNotFoundError` だけ維持、タイムアウトは伝える | 利用者が承認すれば「git の無い環境でも mem を使える」が要件になる。タイムアウトの握りは、同じリポジトリの識別が揺れて別の行が作られる |
| C8 制御文字の remote | 削除 | DB にしか入らず、`repos.id` は slug 化される |
| C9 ログが作れなければ DEVNULL | 修正: 起動を失敗にし、原因を stderr へ。watchdog 側の `else DEVNULL` も消す | ADR（bg ログに残す）と矛盾 |
| C10 pre_compact の catch-all | 削除 | 加えて、保護フックでない hook が落ちたときに launcher が出す `claqProtectionDisabled` の印を直す（誤解を招く） |
| C11 lint の例外を warning | 削除 | 外側が deny する |
| C12 GC スタンプの失敗を無視 | 削除 | 外側が 1 回警告して GC を見送る（今は毎回 GC が走っていた） |
| C13 grok の OSError | 修正: テストだけが使う `plugin_root` 引数を消す。`_mtime_or_none` は `FileNotFoundError` だけ捕まえる（消えた候補の除外）。他の OSError は原因つきで `grok.sh` まで伝える | |
| C14 git_hosting の検出失敗で github | 修正: 検出できなければ「不明」を返し、harness_audit はホスティング固有の検査を「不明のため未実施」と出す | |
| C15 frontmatter の lenient | validators は strict にする（切り替える前に claq 自身の定義で通ることを確かめる）。consumer repo の監査（`harness_audit_repo_checks.py:150`）は `FrontmatterError` を「解析失敗」として報告する | 0 として黙って数えない |
| C16 入れ子 32 の上限 | 削除 | |
| C17 `--optional` | 削除 | |
| C18 `env-template.sh:43` symlink | 削除 | |
| C19 ツール名の除外 | 維持 | matcher を守らないホストでは、保護対象のファイルの Read まで deny する（再現できる失敗） |
| C20 コミット数 | 維持 | `-n 0` は空の履歴、`-n -1` は全履歴を黙って返す |

同梱物

| # | 判断 | 理由・注意 |
|---|---|---|
| A-C1 ページトークン | 修正: `o:\d+` 以外は `UpstreamError(400)` | 署名なしのカーソルは偽造でき、`o:-3` は末尾から行を返す（誤ったデータ。再現できる）。前版の facade.md:113 の引用は誤り |
| A-C2 `max_retry_after` | 削除（`facade.md:144`・テストも） | 締切の検査が待ちを抑える |
| A-C3 負の `retry_after` | 修正: 0 として扱う。NaN の拒否は残す | 過去の日時は待つ時間が 0。前版の RFC の引用は誤り |
| A-C4 detail の 200 字 | 削除（`facade.md:183`・docstring・テストも）。制御文字の除去は残す | |
| A-C5 `fan_out` の空の入力 | 維持 | 空の入力に `({}, {})` を返すのは正しい答えで、続行の分岐ではない（再利用される関数） |
| A-C6 締切の二重 | 維持 | |
| A-C7 既定の主体 | 修正: 既定の `_principal()` は固定値。`principal.py` は削除（重ね合わせも使わない）。`principal_errors` を外し、主体の解決が失敗しうるときに渡すことを remote-auth.md に書く。`_FACADE_MODULES`・test_audit を直す | |
| A-C8 未知の引数の文面の上限 | 削除 | |
| A-C9 dict でない property | 修正: 正規化せずそのまま残す | 真偽値のスキーマを落とすと引数を誤って禁止する |
| A-C10 1 MiB の走査上限 | 削除（テストも） | |
| A-C11 tokens.json の代替パス | 削除 | 検証はテンプレートの複製で行う |
| A-C12〜16 app.js | 正規表現（:52）・`IntersectionObserver` の確認（:316）・`baseLabel \|\|`（:113）・`if (!rows.length)`（:422）は削除。`?.`（:250）・caption の既定（:423）・`?? table`（:476）・sidebar の守り（:123）・`Number.isFinite`（:359）は維持 | 維持するものは、check_site が許す正当なマークアップの差を扱っている（外すと通常の操作で例外になり、他の機能まで止まる） |
| A-C17 | 削除（C18 と同じ） | |
| A-C18 design.md の「必ず」 | 修正: 理由を書く | |

### 2-3. 不具合
- `scripts/grok.sh:40`: 選び方を `launcher.py` に揃え、走査中に消えた候補を除く。「同じ規則」というコメントを事実に合わせる
- `mem/tag_stripping.py:141`: 60 秒を 20 秒に直す
- `e2e_contrast.js` の `data-theme="undefined"`: クローズ。check_site が両属性を必須にしているので到達しない

### 2-4. ADR を根拠とした項目の再監査
- 棚卸しで A（ADR が根拠）とした項目を、上の基準 2・3 で見直す
- 見直しに含めるもの
  - cf51732 でこのセッションが書き直した `code-simplicity-and-defense.md`
  - `hook-failure-direction.md` の stdin 2.0 s・予算の数値
  - `shell-analysis-boundary.md` の先頭 NUL・UTF-16 BOM
  - 根拠の無い上限: `_LINT_SIGNAL_MAX_TEXT_BYTES`・`_MAX_EDITS`・`_BG_FAILURE_TAIL_MAX_BYTES`・hooks.json のタイムアウト
- `shell_tokens.py:15-16` の「timeout に対し 20 倍以上の余裕」は実測で偽（5,000 トークンで 4.7 s、15 s に対し約 3 倍）。実測値に直す。値は、Windows 相当の遅い環境でも余裕が足りるかを測ってから決める
- 外すと失敗が再現する・脅威モデルに合う項目は残す。合わない項目は B/C と同じに処理する。一覧（file:line・ADR の文・判断・理由・測定値）を報告に載せる

## 3. 未実施の検証

- **L6（通常会話への SessionStart 注入）と実装後の検証**
  - 課題を scratchpad のファイルに固定する: パイロットの T1〜T7、包括指示の 2 題、防御が必要な統制課題（パラメータ化・要件のタイムアウト・不信テキストがプロンプトへ入る経路）
  - 「要件に無い防御」の判定基準も同じファイルに書く
  - 条件ごとに 5 回ずつ流す。モデルと設定を固定し、条件の順は交互にする
  - 採点は別の agent が条件を伏せて行う
  - 採用の規則を先に決める: 平均が 1 件以上下がり、統制課題の保持が 100% で、悪化する条件が無いこと
  - L6 は、注入する 3〜4 行の分のトークン増と、サブエージェントには届かないこと（サブエージェントは実装後の検証で見る）を報告に書く
  - 実行は `Agent` のサブエージェントに定義と注入の文面を渡して模擬する（この環境では `claude -p` の認証が使えないため）。その限界を報告に書く
- **L7（handoff の持ち越し）**: コードの読解でクローズ。引き継ぐのはユーザーの発話・変更したパス・ツール名だけで（`handoff.py:233-251, 576`）、エージェントの「提案」の一覧は運ばれない。残る経路は、payload の明示の `handoff` キー（どの定義も設定していない）と、`mem learn`（承認まで pending）
- **L8**: `~/.claq/mem.db` はこの環境に無い。確認コマンドを報告に書く（利用者の手元の作業）

## 検証
- `.venv` で `python3 -m pytest -q` が全件通る。3.12 の venv でも対象のテストを流す
- `ruff check plugins/claq`。`cd plugins/claq && python3 -m pytest -q --cov` で 100%
- mcp-gen: scratchpad の SDK venv で既定構成の `smoke_check.py` が exit 0
- html-gen: テンプレートの複製で `check_site.py` が PASS
- C1 を手で確かめる: `CLAQ_HOME` をファイルに向けて `claq.mem.cli context` を実行し、stdout が空・exit 1・stderr に原因が出ること
- コミットは「テストの環境依存」「B」「C（src）」「C（同梱物）」「ADR の再監査」「不具合」「L6」の単位に分けて push する
