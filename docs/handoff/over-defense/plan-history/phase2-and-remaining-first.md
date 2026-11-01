# 残作業の完了計画（カバレッジ 100%・既存の過剰防御の棚卸しと処理・未実施の検証）

## Context

Phase 1（定義の修正 cf51732）と Phase 2（html-gen 08447c9・mcp-gen f84ce3a）は push 済み。利用者の指示は「カバレッジ未達をすべて潰し、残している作業もすべて完了させる」。残りは次の 4 つ。

1. カバレッジ 99.92%（`scan_scaffold_drift.py:196-197`、`grok_plugin_root.py:68-69, 98->94`）と、この環境で落ちる 8 件 + 計時の揺れで落ちる 1 件
2. Phase 3: 既存コードの根拠の無い防御・フォールバックの棚卸しと処理（調査済み: src に B 11・C 20、同梱物に B 6・C 18、旧部品の文言の残り 8）
3. 未実施の検証: 通常会話への SessionStart 注入の A/B（L6）、実ファイルの定義での実装後検証、handoff の持ち越し確認（L7）
4. 調査で見つかった不具合 3 件

C 項目は通る経路の振る舞いを変える（仕様変更）。この計画に各項目の判断（削除・修正・維持と根拠）を書き、計画の承認をもって利用者の決定とする。維持する項目は、その決定を ADR に 1 行で残す（次のレビューで同じ判定を繰り返さないため）。

## 1. カバレッジと落ちるテスト（テスト側の環境依存。本番コードは変えない）

原因は Python 3.12/3.13 の `Path.is_dir`/`is_symlink` が `Path.stat` を通ること（作者は 3.14）、root がパーミッションを無視すること、Linux の `/bin` に python3 があること、環境変数の git 設定（`GIT_CONFIG_COUNT` の `insteadOf`）、glibc の C ロケールのエンコーディング名。どの修正も検査している内容は弱めない（3.12・3.13 で確認済みの修正）。

| テスト | 修正 |
|---|---|
| `tests/ci/test_scan_scaffold_drift.py:101` | `Path.stat` を transcript ファイルだけで失敗させ、偽物に到達したことも assert（→ `scan_scaffold_drift.py:196-197` を覆う） |
| `tests/lib/test_env_pointer_gc_and_resolve.py:169` | assert の前に `monkeypatch.undo()`。ファイルを `_age` で古くして、stat の失敗だけが削除を止めていることにする |
| `tests/lib/test_git_hosting.py:141` | 前提の assert を `codecs.lookup(enc).name == "ascii"` にする |
| `tests/mem/test_handoff.py:565` | `is_symlink`/`is_file` は通し、所有者の検査の `stat` だけを失敗させる（docstring の競合を再現）。偽物に到達したことを assert |
| `tests/mem/test_repo_identity.py:289, 318` | autouse フィクスチャで `GIT_CONFIG_COUNT`/`KEY_n`/`VALUE_n`/`PARAMETERS` を外し、`GIT_CONFIG_GLOBAL=/dev/null`・`GIT_CONFIG_NOSYSTEM=1` |
| `tests/scripts/test_claq_hook_wrapper.py:98`（unreadable） | root なら `setpriv` で `dac_override`/`dac_read_search` を落として実行（`setpriv` が無ければ skip） |
| `tests/scripts/test_claq_hook_wrapper.py:203` | PATH の 2 つ目を `/bin` から `cat` だけを置いた一時ディレクトリにする |
| `tests/mem/test_redaction.py:315`（揺れ） | CPU 時間（`process_time`、gc 停止）・入力 4 倍・比の上限 8.0（線形 4 倍と二次 16 倍の幾何平均）・小と大を交互に 5 回・最大 3 試行の最小比で判定 |
| `tests/lib/test_grok_plugin_root.py:138, 146, 272` | 「一覧には出たが stat の時点で消えた」を再現する補助（`is_dir` は通し `stat` だけ失敗）を使う（→ `grok_plugin_root.py:68-69, 98->94` を覆う） |

## 2. 既存の過剰防御の処理

### 2-1. B: 到達しない・入口の検証の重複（承認不要。削除し、固定していたテストを消すか直す）
- src
  - `mem/knowledge_input.py:281-282`（scope と repo_id の矛盾）
  - `hooks/pre_bash_commit_quality.py:710-714`（`_emit_block` の二重の握り）
  - `hooks/pre_bash_commit_quality.py:416-422`（未知の severity）
  - `hooks/commit_quality_scanner.py:640-645`（純粋関数の except）
  - `hooks/commit_quality_scanner.py:618-625`（外側と同じ deny）
  - `mem/cmd_context.py:285-289`（`cli.main` と重複）
  - `mem/cmd_context.py:169`（`ended_at or`）
  - `lib/core_utils.py:154-155`（非 str）
  - `hooks/stdin_io.py:84-86, 147-152` と AttributeError の捕捉（テストからしか通らない分岐。テストは `os.pipe` の実 fd に替える）
  - `mem/cli.py:281`・`mem/logger.py:86`
- 同梱物
  - `runtime/claq-helpers.sh:122-125`
  - `mcp-gen/template/smoke_check.py:164, 189, 387`
  - `scripts/grok.sh:39`
  - `runtime/env-template.sh:53-55`
  - `html-gen/e2e_contrast.js:162`
- 旧部品の文言の残り（振る舞いは変えない）
  - `facade-overlay/server.py:683-684`
  - `adapters.py:298`
  - `shaping.py:235, 292`
  - `errors.py:18-21`
  - `smoke_check.py:533`
  - `catalog.py:142-151` と `facade.md:24`（`personal_paths` を重ね合わせ用と書く）

### 2-2. C: 判断（承認で確定）

src

| # | 箇所 | 判断 | 理由 |
|---|---|---|---|
| C1 | `mem/cli.py:257-293` SessionStart の失敗を exit 0 | 維持し ADR に記録 | セッション開始のたびにエラー表示を出さないという運用上の決定。失敗は stderr に出る |
| C2 | `mem/cmd_context.py:335-338` handoff の握り | 削除 | `--bg` の子の終了コードは読まれず、外側の処理と重複 |
| C3 | `mem/cli.py:135-140` 壊れた JSON を `{}` | 修正: JSON object でなければ明示のエラー | `learn` が「title は必須」と誤った文面を出す |
| C4 | `lib/core_utils.py:113-125` actor が取れなければ "unknown" | 修正: promote の前に actor を解決し、取れなければ状態を変えずにエラー | 部分成功の回避を、続行ではなく順序で解く |
| C5 | `mem/database.py:20-40` symlink の拒否 | 維持し ADR に記録 | 0600 の chmod がリンク先の別ファイルに掛かる（再現できる失敗） |
| C6 | `knowledge_input.py` title 4 KiB・body 64 KiB | 削除 | 要件の文が無く、pending は注入されない |
| C7 | `repo_identity.py:53-56` git 不在で絶対パス | 維持し ADR に記録 | git の無い環境でも mem を使える（リポジトリ外と同じ扱い） |
| C8 | `repo_identity.py:165-166` 制御文字の remote を捨てる | 削除 | プロンプトへ入らず、攻撃者がいない |
| C9 | `hooks/detach.py:131-135, 207-209` ログを作れなければ DEVNULL | 修正: ログを作れなければ起動を失敗にする | ADR（bg ログに残す）と矛盾 |
| C10 | `hooks/pre_compact.py:29-35` catch-all | 削除 | ランチャーの exit 1 で既に非ブロッキング |
| C11 | `commit_quality_scanner.py:659-661` lint の例外を warning | 削除（外側の deny へ） | ADR（確定後は fail-closed）と矛盾 |
| C12 | `lib/env_pointer.py:321-324` スタンプ失敗を無視して GC | 削除（外側が 1 回警告して GC を見送る） | 続行の根拠が無い |
| C13 | `lib/grok_plugin_root.py` の OSError 握りと候補の代替順 | 修正: 失敗の原因を伝える。消えた候補の除外（TOCTOU）だけ残す | `grok.sh` が原因を失う |
| C14 | `lib/git_hosting.py` 不正な名前・検出失敗で github | 修正: 不正な名前はエラー、検出できなければ「不明」として harness_audit が明示 | 既定値で続行の根拠が無い |
| C15 | `lib/frontmatter.py` lenient モード | 削除（validators は strict） | 壊れた frontmatter を通す検査はゲートにならない |
| C16 | `lib/frontmatter.py` 入れ子 32 の上限 | 削除 | 攻撃者も要件も無く、深すぎれば RecursionError が原因を伝える |
| C17 | `ci/ci_common.py:42-44` `--optional` | 削除 | テスト以外に使われず、ADR（skip されるゲートはゲートでない）と矛盾 |
| C18 | `runtime/env-template.sh:43` symlink のポインタを拒否 | 削除 | ADR が「実装しない」と決めた検査 |
| C19 | `config_protection.py:947-950`・`bash_config_protection.py:786-789` | 維持し ADR に記録 | 保護フック。Copilot/Grok が matcher を守るか未検証 |
| C20 | `claq-helpers.sh:144-154` コミット数が正の整数か | 維持 | `-n 0` は空の履歴を黙って返す（再現できる失敗） |

同梱物

| # | 箇所 | 判断 | 理由 |
|---|---|---|---|
| A-C1 | `adapters.py:299` ページトークンを検査しない | 修正: `o:\d+` 以外は `UpstreamError(400)` | 署名なしのカーソルは偽造でき、`facade.md:113` が上流へ渡す前の照合を求めている |
| A-C2 | `upstream.py` `max_retry_after=5.0` | 削除 | 締切の検査が既に待ちを抑える |
| A-C3 | `upstream.py` 負の `retry_after` で諦める | 修正: 0 として扱う | RFC 9110: 過去の日時は「今すぐ再試行してよい」。NaN の拒否は残す |
| A-C4 | `upstream.py` detail を 200 字に切る | 削除（制御文字の除去は残す） | 長さの根拠が無い |
| A-C5 | `fan_out` の空の呼び出しの早期 return | 削除 | 今の呼び出し元は空にならない |
| A-C6 | `fan_out` と呼び出しごとの締切の二重 | 維持 | 到達しないと示せない（facade.md の構成） |
| A-C7 | 既定の重ね合わせの主体の解決（`FACADE_PRINCIPAL`/getuser） | 修正: 既定の `_principal()` はテンプレートと同じ固定値。`principal.py` を upstream-protection-overlay へ移す | 主体を区別する必要があるのは共有のリモートだけ |
| A-C8 | `guard.py` 未知の引数の文面の 5 件・64 字 | 削除 | 根拠が無い |
| A-C9 | `guard.py:292` dict でない property を黙って落とす | 修正: そのまま残す | JSON Schema では真偽値のスキーマも正しく、落とすのは誤り |
| A-C10 | `smoke_static.py` 1 MiB で走査をやめる | 削除 | 大きいファイルの旧仕様の名前を見落とす |
| A-C11 | `check_site.py` tokens.json の代替パス | 削除（テストはパスを渡す） | 手順2 が tokens.json を複製する |
| A-C12〜C16 | `app.js` の正規表現・null の守り・機能検出・数値でない値・既定のラベル | 削除 | テンプレートの要素は必ずあり、作者の誤りは表に出す |
| A-C17 | `env-template.sh:43` | 削除（src の C18 と同じ） | |
| A-C18 | `html-gen/references/design.md:41,147,164` の根拠の無い「必ず」 | 修正: 理由を書く（棒の長さで値を表すので原点は 0 等） | |

### 2-3. 不具合
- `e2e_contrast.js:257-258` が `data-theme="undefined"` を残す（元が未設定なら属性を消す）
- `scripts/grok.sh:40` の選び方を Python 側（`launcher.py`）に揃え、走査中に消えた候補を除く
- `mem/tag_stripping.py:141` のコメントの 60 秒を hooks.json の 20 秒に直す

### 2-4. ADR
`docs/adr/hook-failure-direction.md` と `code-simplicity-and-defense.md` に、維持した C1・C5・C7・C19・C20・A-C6 の決定を現行の基準として 1 行ずつ書く（経緯は書かない）。

## 3. 未実施の検証

- **L6（通常会話）**
  - パイロットと同じ課題 T1〜T7 と包括指示の 2 題を、利用者の CLAUDE.md だけ（現行）と、SessionStart 注入に正本の 1・4・5・6 を足したもので、各 2 回ずつ比べる
  - 要件に無い防御の数が下がれば `claq.mem.cli context` の出力の先頭に 3〜4 行を足し、下がらなければ足さない（結果を報告）
- **実装後の検証**
  - 対象は実ファイルの定義（tdd-writer・quick-code・quick-review・reviewer・security-auditor・loop-dev 反復2・包括指示と実 CLAUDE.md）
  - パイロットの課題で各 2 回流し、パイロットの改訂版と同等（要件に無い防御 0〜1・統制の保持 100%）を確かめる
- **L7**: `mem/handoff.py` が引き継ぐ内容に、残課題の一覧が混ざる経路があるかをコードで確かめ、あれば報告の区分（提案）と同じ扱いにする
- **L8**: `~/.claq/mem.db` はこの環境に無いので、確認コマンドを報告に書く（利用者の手元の作業）

## 検証
- `.venv` で `python3 -m pytest -q` が全件通る（環境由来の 8 件も解消）。`ruff check plugins/claq`。`cd plugins/claq && python3 -m pytest -q --cov` で 100%
- mcp-gen: scratchpad の SDK venv で既定構成の `smoke_check.py` exit 0
- html-gen: テンプレートで `check_site.py` PASS
- コミットは「テストの環境依存」「B の削除」「C の処理」「不具合」「L6」の単位に分けて push する
