# bluecore 0.9.27 検証報告（Grok 実機・トークン最小）

| 項目 | 値 |
|---|---|
| 日 | 2026-08-14 |
| 対象 | `~/.grok/plugins/bluecore` → `installed-plugins/bluecore-ef6b8ffa`（0.9.27） |
| ホスト | Grok Build TUI |
| コード変更 | なし |

## 結論

リンク作成後、フックランチャーは動く。本丸は次のとおり **再現確認済み**。

| ID | 判定 | 実測 |
|---|---|---|
| A-01 | **再現** | `spawn_subagent(subagent_type=bluecore:reviewer)` → `Unknown subagent type: bluecore:reviewer. Available types: explore, general-purpose, plan`。nudge matcher は `Task\|Agent` のみ（`spawn_subagent` 非一致） |
| H-03 | **コード再現**（このセッションの既定 env では未発火） | このシェル: `CLAUDECODE` 無し、`GROK_AGENT` 有、`detect_harness()=grok`。`CLAUDECODE=1` を足すと `GROK_*` があっても **`claude`** |
| H-00 | **再現→手動解消** | CLI だけではリンク無し。手動 `ln -sfn .../bluecore-ef6b8ffa ~/.grok/plugins/bluecore` 後に launcher 到達。リンク前は Python ENOENT exit 2 で全 deny |
| D-01 | **再現** | Claude marketplace の README L67 `Commands (10)`、L454 幽霊 `gitflow`、L536「スキルは全てユーザー直接起動不可」。実装は commands 9、gitflow 無し、invocable true が 4 本 |
| C-01 | **再現** | `test-gen.md` が `get_test_command()`。helpers にその関数は無い |
| R-01 | **再現** | reviewer.md が `BLOCKER (CRITICAL\|HIGH)` と `Warning = HIGHのみ` を併記 |
| P-01 | **再現** | planner 例が `mem/search.py`。ファイルは **存在しない** |
| H-01 | **コード確認** | `observe.py` が `sys.stdin.buffer.read()`。stdin を閉じたスモークは exit 0 |
| H-04 | **コード確認** | config matcher に `search_replace` 無し。マップに Grok 名無し |
| L-01 | **コード確認** | observer が `claude --model haiku`。このマシンには `claude` あり（未起動検証） |
| P-04 | **再現** | `plugin.json` に `hooks` キー無し |

## 実機

| 確認 | 結果 |
|---|---|
| `~/.grok/plugins/bluecore/src/bluecore/launcher.py` | 存在。引数なし → usage / exit 1 |
| 本セッション Bash / Write | 通る（リンク前は Python ENOENT → exit 2 → 全 deny） |
| `detect_harness()` このシェル | `grok`。`CLAUDECODE=1` だと `claude`。`GROK_SESSION` を足しても **claude のまま** |
| `block_no_verify` `ls` | exit 0 |
| `block_no_verify` `git commit --no-verify` | exit 2 + BLOCKED |
| `config_protection` `docs/x.md` | exit 0 |
| `config_protection` `ruff.toml` | exit 2 |
| `observe --bg` + 閉じた JSON | exit 0 |
| `pre_agent_nudge` `general-purpose` | additionalContext に `bluecore:executor` 等 |
| 構成 | commands 9 / skills 13 / agents 16。`plugin.json` に **`hooks` キー無し**（`hooks/hooks.json` は存在する） |

リンク前の全滅は observe ハングではなく、**欠落した `~/.grok/plugins/bluecore/launcher.py` を Python が exit 2 で落とし、Grok が deny した**もの。

## 不具合と修正案

### H-00 [P0] Grok は Claude プラグインのフックを読むが `CLAUDE_PLUGIN_ROOT` は `~/.grok/plugins/<name>`

**現象:** Claude にだけ入れるとフックは発火し、実体パスが無く全ツール deny。CLI インストールだけでは symlink が自動では付かない。

**修正:** SessionStart の `ensure_grok_plugin_root_symlink` が「インストール直後・フック実行前」にも走るよう、launcher 先頭で同様のリンクを張る。または hooks.json を `GROK_PLUGIN_ROOT` / installed-plugins 実体に向ける。README に「Grok は `ln -sfn ~/.grok/installed-plugins/bluecore-* ~/.grok/plugins/bluecore` が必要」と書く。

### H-03 [P1] `detect_harness()` が `CLAUDECODE` を GROK より先に見る

**現象:** `CLAUDECODE=1` があると Grok でも `"claude"`。`--bg` が detach せず observe が同期になる。

**修正:** `/.grok/` パスまたは `GROK_*` を `CLAUDECODE` より先に判定。テストで `cache_clear()`。

### H-01 [P1] `observe.py` が `sys.stdin.buffer.read()` で EOF 待ち

**現象:** 共通の 2 秒ガードを使わない。ホストが stdin を閉じないとブロック。stdin を閉じた今回のスモークは 0。

**修正:** `_read_raw_stdin` を削除し `hook_common.read_raw_stdin()` のみ。失敗しても matcher `*` は exit 0（ブロック系は fail-closed 維持）。

### A-01 [P0] Grok に `bluecore:*` が無い

**現象:** `spawn_subagent` は `general-purpose` / `explore` / `plan` のみ。nudge は `bluecore:reviewer` 等への差し替えを勧めるがホストが拒否する。feat-dev / bugfix / review / refactor / loop-dev の委譲が死ぬ。nudge matcher は `Task|Agent` で `spawn_subagent` に非一致。

**修正:** Grok では type を差し替えず、`agents/*.md` をプロンプトに貼る。nudge matcher に実ツール名を足し、文言を分岐。中期はホストが plugin agents を type 登録。

### H-04 [P1] ツール名マップに Grok 名が無い

マップ: agent/apply_patch/bash/edit/glob/grep/multiedit/notebookedit/read/shell/task/view/write。

`search_replace` が Edit 扱いでなく、`config_protection` matcher 外 → `ruff.toml` を StrReplace できる。

**修正:** `search_replace`→Edit、`read_file`→Read、`run_terminal_command`→Bash、`spawn_subagent`→Agent。matcher に `search_replace` を追加。

### L-01 [P1] observer が `claude --model haiku` 固定

Grok 専用環境では pending 知識が生まれない。無いときは明示スキップ（フォールバック実装は置かない）。

### D-01 / C-01 [P1] 文書ドリフト

- 公開 README 見出し Commands (10)、実体 9。WF-1 は旧 7 ステップ、実装は loop-dev。WF-10 `gitflow` は存在しない。
- `test-gen.md` が `get_test_command()` を呼ぶ。helpers に無い（`bluecore_run` / `bluecore_mem_learn` / `bluecore_loop_telemetry*` / `collect_skill_create_inputs` のみ）。
- Architecture「スキルはすべて非 invocable」は誤り。true: grillme / maintain / skill-tune / loop-audit。

### R-01 [P1] reviewer HIGH 矛盾

`BLOCKER (CRITICAL|HIGH)` と `Warning = HIGHのみ` が同居。loop-dev は CRITICAL/HIGH を blocker 扱い。HIGH を BLOCKER に一本化し「Warning = HIGH のみ」を削除。

### P-01 [P2] planner 例が `mem/search.py`

実体は `mem/cli.py`。例を差し替え。`search.py` は作らない。

### P-04 [P2] `plugin.json` に hooks キー無し

`"hooks": "./hooks/hooks.json"` を追加。

## 点検表（短い）

**Commands 9** — 本文は読める。実行は grillme + `bluecore:*` + Bash helpers 前提 → Grok では A-01 で委譲不能。instinct の `list` まで grillme 必須は過剰。

**Skills 13** — 定義は揃う。loop-dev は Grok で generate/evaluate 委譲不能。

**Agents 16** — plugin.json とファイルは一致。Grok から type 起動不可。

**Hooks** — ランチャー到達後、ブロック系は正しい。リンク欠落時は全イベントが偽 deny。

## 修正順

1. H-00（Grok ルート自動リンク / 文書）  
2. A-01 + H-03 + H-01  
3. H-04 / L-01 / 文書（D-01 C-01 R-01 P-01 P-04）

## 未実施（トークン抑制）

`/feat-dev` 等のフル実行、16 エージェント実起動、pytest（公開ツリーに tests 無し）。

---

## 追記: Grok で動かすための修正案（おまけ）

前提: **bluecore は Claude Code 用プラグイン**。Claude 経路の契約・出力・エージェント type（`bluecore:*`）は変えない。Grok は互換で載る副ホストであり、ここで書く案は「Grok でも壊れない / 黙って嘘をつかない」ための最小追加である。実装判断は別途。この追記ではコードを変えていない。

### 方針

| 原則 | 意味 |
|---|---|
| Claude を壊さない | `detect_harness()=="claude"` の分岐・hooks の exit 2 ブロック・`bluecore:*` 指名は現状維持 |
| フォールバック禁止 | 古い経路を残さない。Grok 用は新しい分岐か、Grok では「非対応を 1 行で明示してスキップ」 |
| おまけの到達目標 | フックが偽 deny しない。コマンド本文が存在しない type を勧めない。学習 observer は claude CLI が無いなら解析しない |

Claude 本体の文書バグ（D-01 / C-01 / R-01 / P-01 / P-04）と observe の無限 read（H-01）は **Claude でも直すべき本流**であり、下の Grok 案とは別枠。

### なぜ Grok にインストールしてもシンボリックリンクが必要か

Claude Code では不要で、Grok の `grok plugin install` 後だけ必要になる。ホストがファイルを置く場所と、hooks.json がファイルを探す場所が違うため。

#### 2 つのパス

| 役割 | パス | 誰が決めるか |
|---|---|---|
| **実体**（CLI が置く） | `~/.grok/installed-plugins/bluecore-<hash>/` | Grok の marketplace インストール |
| **フックが見る口** | `~/.grok/plugins/bluecore/` | hooks.json の `${CLAUDE_PLUGIN_ROOT}` を Grok が展開した結果 |

hooks.json は Claude Code 向けに書かれている。どのフックも次の形だけを呼ぶ。

```text
python3 "${CLAUDE_PLUGIN_ROOT}/src/bluecore/launcher.py" …
```

Claude Code では `CLAUDE_PLUGIN_ROOT` が **インストール実体そのもの** なので、この 1 本で足りる。追加のリンクは無い。

Grok は Claude 互換のため同じ変数名を使うが、値は常に `~/.grok/plugins/<プラグイン名>` である。一方 `grok plugin install` は実体を `installed-plugins/bluecore-<hash>` に置く。**`~/.grok/plugins/bluecore` は作らない。** 公式インストール完了 ≠ フックが launcher を見つけられる。

本検証での実測:

- インストール直後: `~/.grok/plugins/bluecore/src/bluecore/launcher.py` が無い
- Stop フック: `can't open file '.../.grok/plugins/bluecore/src/bluecore/launcher.py' (Errno 2)`
- Python の「ファイルが開けない」は **exit 2**
- Grok の PreToolUse は exit 2 を deny とみなす → Bash / Read / Write が全滅
- 手動 `ln -sfn ~/.grok/installed-plugins/bluecore-ef6b8ffa ~/.grok/plugins/bluecore` のあと、同じパスで launcher が動き、ブロック系フックは正常になった

#### プラグイン側にもリンク処理があるが、鶏と卵で届かない

`grok_plugin_root.py` のモジュールコメントどおり、作者はこの差を知っている。`ensure_grok_plugin_root_symlink()` が `~/.grok/plugins/bluecore` → `installed-plugins/bluecore-*` を張る。

呼び出し元は **`session_start` フック**（SessionStart）。その起動経路は:

```text
Grok が SessionStart を発火
  → ${CLAUDE_PLUGIN_ROOT}/src/bluecore/launcher.py を exec
    → そこにファイルが無いと Python が exit 2 で死ぬ
      → ensure_grok_plugin_root_symlink() は一度も走らない
```

リンクを作るコードが、リンクが無いと実行できない場所にある。だから「インストールすれば SessionStart が直してくれる」は成立しない。初回からリンクが無い限り、直す主体に到達できない。

加えて、偽 deny は SessionStart より先の **PreToolUse**（ツールを使うたび）でも起きる。SessionStart が成功しても、それ以前のターンは既に壊れている。

#### まとめ

```text
grok plugin install
        │
        ▼
  実体は installed-plugins/bluecore-<hash>/
        │
        │  Grok は CLAUDE_PLUGIN_ROOT を
        │  ~/.grok/plugins/bluecore に展開
        ▼
  そのパスに launcher.py が無い
        │
        ▼
  python3 …/launcher.py → ENOENT → exit 2
        │
        ▼
  PreToolUse = deny（プラグイン未動作に見える）
```

シンボリックリンクは「お好み」ではなく、**Claude 用パス規約と Grok の配置規約を一致させる橋**である。実ディレクトリを `~/.grok/plugins/bluecore` に置いても同じ。必要なのはリンクという形式ではなく、**フックが見るパスに `src/bluecore/launcher.py` があること**。

#### 解決方法（運用 / プラグイン / ホスト）

**今すぐ（運用・コード不要）**

```bash
ln -sfn ~/.grok/installed-plugins/bluecore-<hash> ~/.grok/plugins/bluecore
# 例: bluecore-ef6b8ffa
```

Grok セッションを開き直す。今開いているセッションは起動時のパス解決のままなので、リンクを後から張っても直らない。

**プラグイン側（おまけ実装するなら）** — G-1 と同じ。要点だけ再掲する。

1. hooks.json の command を、launcher を呼ぶ前に実体を探す薄いシェルにする。見つからなければ **exit 0**（exit 2 にすると今と同じ偽 deny）。見つかったらそのルートの launcher を exec。これならリンク無しでも初回から動く。
2. そのシェルまたは launcher 成功後に `ensure_grok_plugin_root_symlink()` を呼び、以降の `${CLAUDE_PLUGIN_ROOT}` 展開と一致させる。SessionStart だけに置くのは不十分（上記の鶏と卵）。
3. 実装しないなら README に「Grok は公式非対応。使うならリンクを自分で張れ」と書く。

**ホスト側（Grok Build）**

`grok plugin install` が Claude 互換フック用に `~/.grok/plugins/<name>` → `installed-plugins/<name>-<hash>` を張る。または `${CLAUDE_PLUGIN_ROOT}` を installed-plugins の実体に展開する。プラグインを変えずに済むが、xAI 側の話。

**やらないこと:** 開発リポジトリ（bluecore-dev）をリンク先にしない。`grok_plugin_root.py` が既に禁止している。配布実体（installed-plugins）だけを見る。

### G-1. プラグインルート（H-00）— インストール直後にフックが死なない

**なぜ必要か:** Grok は Claude の hooks.json を読み、`${CLAUDE_PLUGIN_ROOT}` を `~/.grok/plugins/bluecore` に展開する。CLI インストール先は `~/.grok/installed-plugins/bluecore-<hash>/`。リンクが無いと `launcher.py` が ENOENT → Python exit 2 → PreToolUse が全 deny。

**案（どれか 1 つ。併用しない）:**

1. **launcher より前にパスを直す（推奨）**  
   hooks.json の command を「存在確認してから launcher を呼ぶ」1 行ラッパにするのではなく、`grok_plugin_root.ensure_grok_plugin_root_symlink()` を **launcher.py の最初**（import 可能な Python に入った直後）で呼ぶ。SessionStart まで待つと、その前の PreToolUse で死ぬ。  
   制約: リンクが無いと launcher 自身が起動できない。したがって **hooks.json の command を installed-plugins 実体に向けられないなら**、ラッパが先に必要。

2. **hooks.json の command を実体解決する薄いシェル 1 本**（新規。旧 command 行は消す）  
   `GROK_PLUGIN_ROOT` または `~/.grok/installed-plugins/bluecore-*` の最新で `launcher.py` があるディレクトリを選び、無ければ exit 0（Grok ではフック未インストール＝何もしない）。Claude では `CLAUDE_PLUGIN_ROOT` が実体なので従来どおり。  
   注意: 解決失敗を exit 2 にすると今と同じ偽 deny になる。**解決失敗は 0**。

3. **コードを触らず文書だけ**  
   README に Grok おまけ節を足す。  
   `ln -sfn ~/.grok/installed-plugins/bluecore-<hash> ~/.grok/plugins/bluecore`  
   セッションを開き直すこと。これが今の運用回避策。

**受け入れ:** リンク無しの Grok で `ls` が deny されない。Claude のフック拒否（`--no-verify` / `ruff.toml`）は変わらない。

### G-2. ハーネス判定（H-03）— Grok を Claude に誤認しない

**なぜ必要か:** `detect_harness()` が `CLAUDECODE` を先に見る。Grok が互換のため同変数を立てると `--bg` が detach せず、observe が同期になる。

**案:** `/.grok/` が `CLAUDE_PLUGIN_ROOT` に含まれる、または `GROK_*` があるなら **先に `"grok"`**。`CLAUDECODE` 単独のときだけ `"claude"`。旧「CLAUDECODE なら即 claude」は削除（フォールバックで残さない）。

`lru_cache` があるのでテストは前後で `cache_clear()`。

**受け入れ:** `CLAUDECODE=1` + `GROK_AGENT=1` → `"grok"`。`CLAUDECODE=1` のみ → `"claude"`。

### G-3. observe の stdin（H-01）— Claude 本流だが Grok でも効く

**案:** `observe.py` の `_read_raw_stdin`（`stdin.buffer.read()`）を削除し、`hook_common.read_raw_stdin()` に一本化。matcher `*` の observe は例外・空入力でも **exit 0**。ブロック系フックは exit 2 のまま。

これは Claude で stdin リダイレクト漏れを防ぐ本流修正。Grok おまけというより共通耐久性。

### G-4. 専門エージェント（A-01）— Grok では type を差し替えない

**なぜ必要か:** Grok の `spawn_subagent` は `general-purpose` / `explore` / `plan` のみ。`bluecore:reviewer` は `Unknown subagent type`。commands / loop-dev / nudge は `bluecore:*` 前提。

**案（Claude の本文は残す）:**

1. **nudge（必須・小さい）**  
   `detect_harness()=="grok"` のとき、対応表を出さないか、次の 1 段落だけ出す。  
   「このホストに `bluecore:*` は無い。`explore` / `plan` / `general-purpose` を使い、必要なら `agents/<name>.md` を Read してプロンプト先頭に貼れ」  
   Claude では現行の差し替え表のまま。  
   matcher にホストが渡すツール名を足す（実測で確定。候補 `spawn_subagent`）。足しても Claude の `Task|Agent` は残す。

2. **コマンド / loop-dev 本文（Grok 節を末尾に足す）**  
   冒頭の `bluecore:explorer` 必須は Claude 向けとして残す。Grok 節: type は上表にマップし、ペルソナは `agents/*.md` の Read 埋め込み。ホスト type 登録を待たない。

   | Claude type | Grok type |
   |---|---|
   | explorer / session-observer | explore |
   | planner / architect | plan |
   | その他（reviewer, tdd-writer, …） | general-purpose + 当該 md |

3. **やらないこと**  
   Claude の `subagent_type: bluecore:reviewer` を消さない。Grok 用に type 名をエイリアスするホスト改修はプラグインの外（おまけの範囲外）。

**受け入れ:** Grok で nudge が `bluecore:reviewer` への差し替えを勧めない。Claude の nudge 出力は今と同じ。

### G-5. ツール名（H-04）— 保護が Grok の Write 相当で抜けない

**案:** `_TOOL_NAME_MAP` に Grok 名を足す。hooks.json の Write 系 matcher に `search_replace` を足す。Claude に無いツール名をマップしても Claude 側は無影響。

| Grok 名 | Claude 相当 |
|---|---|
| search_replace | Edit |
| read_file | Read |
| list_dir | Glob |
| run_terminal_command | Bash |
| spawn_subagent | Agent |

**受け入れ:** Grok の `search_replace` で `ruff.toml` が deny。Claude の Write `ruff.toml` は従来どおり deny。

### G-6. observer（L-01）— claude CLI が無い Grok では解析しない

**案:** `which("claude")` が無いときの既存スキップを維持し、ログを「Grok おまけ: observer 解析は claude CLI 依存。未導入なら pending は増えない」と 1 行にする。Grok 用に別 LLM 呼び出しは足さない（フォールバック禁止・おまけの範囲を超える）。

SessionStart で grok かつ claude 無しなら additionalContext に同じ 1 行。

### G-7. 文書

README に短い「Grok（非対応公式・自己責任）」節。

- 公式ホストは Claude Code。
- Grok はフックとスキルが載ることがある。`~/.grok/plugins/bluecore` → installed-plugins のリンクが要る。
- 専門エージェント type は使えない。
- 上以外の Commands (10) / gitflow / `get_test_command` / HIGH 矛盾は **Claude 本流の文書修正**であり Grok おまけではない。

### おすすめ実装順（Grok おまけだけ）

| 順 | 案 | 理由 |
|---|---|---|
| 1 | G-1 案 3（文書）または案 2（解決失敗は exit 0） | リンク無しの偽 deny が最悪。文書だけでも運用可能 |
| 2 | G-2 | `--bg` 誤同期を止める |
| 3 | G-4 の nudge だけ | コマンド全文を書き換える前に、誤った type 推奨を止める |
| 4 | G-5 | 保護の抜け |
| 5 | G-6 / G-4 のコマンド節 | あれば親切。無くても Claude 本流は困らない |

G-3（observe stdin）は Claude 本流として先にやってよい。Grok 専用ではない。

---

## 追記: 実装時の補足（このセッションで得た事実）

コードは変えていない。後で直すときの落とし穴と、触らなくてよい範囲。

### ホスト契約（Grok 公式ドキュメント 2026-07）

- PreToolUse だけがブロックできる。stdout の `{"decision":"deny"}` または **exit 2** が deny。
- timeout / crash / 不正出力は **fail-open**（ツールは通る）。偽 deny の主因は「ファイル無しの python が exit 2」であり、タイムアウトではない。
- hooks.json の `timeout` 未指定時の既定は **5 秒**。`block_no_verify` / `pre_bash_commit_quality` は timeout キー無し。
- matcher はツール名の正規表現。Claude 名は Grok 側でマップされる、とドキュメントはある。実測の Grok 名（`search_replace` / `spawn_subagent` / `run_terminal_command`）は bluecore の matcher・`_TOOL_NAME_MAP` に無い。
- プラグインフックには `GROK_PLUGIN_ROOT` / `GROK_PLUGIN_DATA` が付く、とドキュメントはある。本セッションの Bash 子プロセスでは `CLAUDE_PLUGIN_ROOT` も `GROK_PLUGIN_ROOT` も **未設定**。フック command の展開と、エージェントが叩くシェルの env は別。

### このセッションの env（Grok エージェントの Bash）

- `CLAUDECODE` 無し
- `GROK_AGENT` 有
- `detect_harness()` → `grok`
- `CLAUDECODE=1` だけ足すと `claude`。`GROK_SESSION` を足しても **claude のまま**（H-03）

### 触ってよいファイル / 触らなくてよいもの

| 直すとき | ファイル |
|---|---|
| G-1 リンク鶏卵 | `hooks/hooks.json` の command 行、必要なら新規薄いシェル。`src/bluecore/lib/grok_plugin_root.py` は流用（リンク先は `installed-plugins/bluecore-*` のみ。**bluecore-dev を指させない**） |
| G-2 | `src/bluecore/lib/harness.py` の `detect_harness` のみ。`lru_cache` → テストは `cache_clear()` |
| G-3 | `src/bluecore/skills/learn/observe.py` の `_read_raw_stdin` を削除し `hook_common.read_raw_stdin` |
| G-4 | `pre_agent_nudge.py` と `hooks.json` matcher。コマンド md は後回し可 |
| G-5 | `harness.py` の `_TOOL_NAME_MAP` + hooks.json の Edit/Write matcher |
| G-6 | `observer.py` のログ文言。別 LLM は足さない |
| Claude 本流の文書 | 公開 README、`commands/test-gen.md`、`agents/reviewer.md`、`agents/planner.md`、`.claude-plugin/plugin.json` の `"hooks"` |

`session_start` にリンク処理を足すだけでは足りない（launcher に届かない）。既にある `ensure_grok_plugin_root_symlink()` を SessionStart 専用のまま増やすのは解にならない。

### 受け入れテストの最小セット（実装後）

リンク無し Grok: `ls` が deny されない（exit 2 にならない）。  
リンク有: `git commit --no-verify` は deny、`ruff.toml` の Write は deny、`.md` の Write は許可。  
`CLAUDECODE=1` + `GROK_AGENT=1` → `detect_harness()=="grok"`。  
Grok で `spawn_subagent(bluecore:reviewer)` は今どおり type 不正。nudge がそれを勧めないこと。  
Claude の nudge / `bluecore:*` / exit 2 ブロックは回帰しないこと。

公開 `aokumablue/bluecore` に `tests/` は無い。テストは bluecore-dev 側。カバレッジ 100%・`python3 -m pytest -q`・`ruff check plugins/bluecore/src`。後方互換フォールバックは置かない。

### 既にコード上クローズ（再実装しない）

- 前日監査 F-01: `pre_agent_nudge` は `subagent_type or agent_type` を見る
- 前日監査 F-02: `config_protection` / `quality_gate` は `extract_tool_input` 使用
- テスト未投入だけが残っている

### 実測コマンド（再確認用）

```bash
# 実体と口
ls -l ~/.grok/plugins/bluecore
test -f ~/.grok/plugins/bluecore/src/bluecore/launcher.py

# フック（CLAUDE_PLUGIN_ROOT を実体に向けて直叩き）
export CLAUDE_PLUGIN_ROOT="$HOME/.grok/plugins/bluecore"
L="$CLAUDE_PLUGIN_ROOT/src/bluecore/launcher.py"
printf '%s' '{"tool_name":"Bash","tool_input":{"command":"ls"}}' | python3 "$L" bluecore.hooks.block_no_verify
printf '%s' '{"tool_name":"Write","tool_input":{"file_path":"ruff.toml"}}' | python3 "$L" bluecore.hooks.config_protection
```

### 構成の正（0.9.27）

commands 9 / skills 13 / agents 16。invocable true は grillme / maintain / skill-tune / loop-audit のみ。helpers の関数は `bluecore_plugin_root` / `bluecore_run` / `bluecore_run_bg` / `bluecore_mem_learn` / `bluecore_loop_telemetry` / `bluecore_loop_telemetry_list` / `collect_skill_create_inputs`。`get_test_command` も `mem/search.py` も `gitflow` スキルも無い。

### 優先度の切り分け（本流 vs おまけ）

冒頭の「修正順」は発見順で、Claude 本流と Grok おまけが混ざっている。実装するときは次で切る。

| 枠 | ID | 備考 |
|---|---|---|
| Claude 本流（Grok と無関係でも直す） | H-01, D-01, C-01, R-01, P-01, P-04 | observe stdin、README、test-gen、reviewer HIGH、planner 例、plugin.json hooks |
| Grok おまけ（Claude 契約は変えない） | H-00/G-1, H-03/G-2, A-01/G-4, H-04/G-5, L-01/G-6 | リンク鶏卵、harness 判定、nudge 文言、ツール名、observer 明示スキップ |
| 文書のみ・実装しないでも可 | C-02（instinct の list でも grillme） | 結論表に無いが点検表にある。Claude でも過剰 |

先頭の H-00「launcher 先頭で symlink」は、後述の鶏と卵で **不十分** と訂正済み。実装は G-1 案 2（launcher より前の実体解決）か案 3（文書+手動リンク）。案 1 だけ採用しない。

### Claude 未インストールでも Grok でフックが走る

Grok は Claude の marketplace / プラグインを自動で読む（公式: zero-config 互換）。**Grok に入れなくても** Claude 側の bluecore だけで `global/bluecore-from-plugin` が発火する。そのときも `CLAUDE_PLUGIN_ROOT` は `~/.grok/plugins/bluecore` なので、Grok 未インストール＋リンク無しでも同じ ENOENT deny になる。

したがって「Grok 用に install したのに動かない」だけでなく、「Claude に入れただけで Grok セッションが死ぬ」が先に起きる。H-00 の被害範囲は Grok ユーザ全体ではなく、**Claude に bluecore があるマシンで Grok を開いた人**。

### hooks.json の command は全部同じ口

`${CLAUDE_PLUGIN_ROOT}/src/bluecore/launcher.py` を使う行（G-1 のラッパを入れるなら **全行**）:

| イベント | 引数 | timeout | `--bg` |
|---|---|---|---|
| PreToolUse Bash | `bluecore.hooks.block_no_verify` | 無し（Grok 既定 5s） | いいえ |
| PreToolUse Bash | `bluecore.hooks.pre_bash_commit_quality` | 無し | いいえ |
| PreToolUse Edit/Write/… | `bluecore.hooks.config_protection` | 5 | いいえ |
| PreToolUse `*` | `bluecore.skills.learn.observe pre` | 10 | はい + async |
| PreToolUse Task/Agent | `bluecore.hooks.pre_agent_nudge` | 5 | いいえ |
| PreCompact `*` | `bluecore.hooks.pre_compact` | 無し | いいえ |
| SessionStart | `bluecore.mem.cli context` | 60 | いいえ |
| SessionStart | `bluecore.hooks.session_start` | 無し | いいえ |
| PostToolUse Bash | `bluecore.hooks.redux_filter` | 10 | いいえ |
| PostToolUse Edit/Write | `bluecore.hooks.quality_gate post-edit` | 15 | いいえ |
| Stop | `bluecore.hooks.session_end` | 10 | はい + async |
| Stop | `bluecore.hooks.desktop_notify` | 5 | はい + async |
| SessionEnd | `bluecore.mem.cli handoff` | 10 | はい + async |

`--bg` は `launcher.py` が `detect_harness() != "claude"` のときだけ detach。H-03 で grok を claude と誤認すると、observe / session_end / desktop_notify / handoff が同期になる。

`emit_block` は grok でも Claude と同じ **exit 2 + stderr**。Copilot 用の `permissionDecision` JSON に寄せない。Grok 公式は stdout の `{"decision":"deny"}` も認めるが、実測では exit 2 で deny できている。

### リンク先ハッシュは更新のたびに変わる

実体ディレクトリは `bluecore-ef6b8ffa` のようにハッシュ付き。`grok plugin update` 後は別ディレクトリになり、古い symlink は切れる。`grok_plugin_root.find_latest_installed_bluecore()` は mtime 最大を選ぶ。手動運用するなら update のたびに `ln -sfn` し直す。自動化するなら「最新 bluecore-*」を毎回解決する（固定ハッシュを hooks.json に書かない）。

### launcher の Python 契約

- PATH の `python3` を使う。venv は作らない・見ない。
- 3.12 未満はフックを走らせず stderr に理由を書いて **exit 0**（fail-open）。
- 本機の Stop フックは Homebrew の Python 3.14 で launcher を開こうとしていた。

### Claude 本流で報告書が薄いが直すときあるもの

- **C-02** `/instinct list` でも grillme 必須。読み取りサブコマンドは即 `mem.cli` でよい。
- **WF-1 / WF-2 / WF-11** README が feat-dev 7 ステップ・`tdd` スキル・`get_test_command` 直実装のまま。正は loop-dev。
- **maintain 原則 5** が Copilot 用「フォールバック実装」を要求。プロジェクト規則はフォールバック禁止。Grok おまけを足すとき、この原則を「不可能なら明示スキップ（旧経路は残さない）」に直さないと、メンテ実行がフォールバックを足し始める。
- **learn / loop-audit** の例が `PYTHONPATH=plugins/bluecore/src`。開発は editable install、配布は `bluecore_run`。
- **quality_gate / redux_filter** はリンク後にスモークしていない。PostToolUse。G-5 の matcher 漏れと同じ系統で、Grok の `search_replace` 後に quality_gate が走らない可能性。

### コマンド / エージェントの正（ファイル名）

commands: `plan` `feat-dev` `bugfix` `refactor` `review` `harness` `instinct` `skill-gen` `test-gen`  
agents（plugin.json 16）: architect, bench-analyzer, comparator, dead-code-cleaner, executor, explorer, grader, harness-tuner, session-observer, perf-optimizer, planner, refactor-orchestrator, reviewer, security-auditor, simplifier, tdd-writer  
nudge 表に載るのは executor / explorer / planner / architect / tdd-writer / reviewer / security-auditor / simplifier / dead-code-cleaner / perf-optimizer / refactor-orchestrator。grader / comparator / bench-analyzer / harness-tuner / session-observer は skill-gen / harness / learn から直接指名。

---

## 追記: bluecore-dev 上での実コード照合と対応（2026-08-15）

Claude Code（このリポジトリ自体、bluecore-dev）上で本報告書の各指摘が実コードに対して成立するかを照合し、修正した記録。実機（Grok）での再検証ではなく、コードリーディングと一部の実測（stdinブロック実験・detect_harness単体テスト）による検証。

### 1→2 切り分け（確定）

**枠1: 共通問題（Claude/CopilotCLIでも成立しうる、ホスト判定と無関係にコードとして誤り）**

| ID | 内容 | 対応 |
|---|---|---|
| H-01 | `observe.py` が独自の無防備な stdin 読み込みを持っていた | 修正済み（コミット `ac6fc4c`）。**当初の見立てを反転**: `launcher.py:163` の `--bg` 分岐は非Claudeハーネスでのみ detach し、その際は launcher 自身がガード付き `read_raw_stdin()` で読み切ってから渡す。Claude実行時は逆にインプロセス実行のため `observe.py` の無防備な読み込みがそのまま使われる。実測（パイプ書き込み側を5秒開いたまま）で実際に5秒ブロックすることを確認。Claude Codeは通常hookのstdinを閉じるため実害は未確認だが、無制限・無ガードという設計自体がリスクであり、他フックと同じ `hook_common.read_raw_stdin()`（2秒タイムアウト・1MiB上限）に統一した |
| D-01 | README「Commands (10)」と実9個の自己矛盾、存在しない `gitflow` スキルのmermaid記載 | 修正済み（コミット `0ca8fa1`） |
| R-01 | `reviewer.md` の `BLOCKER (CRITICAL\|HIGH)` と `Warning = HIGHのみ` の矛盾 | 修正済み（同上）。HIGHをBlock扱いに統一、Warning区分を削除 |
| P-01 | `planner.md` の計画出力例が実在しない `mem/search.py` / `tests/mem/test_search.py` を参照 | 修正済み（同上）。実在する `mem/cli.py` / `tests/mem/test_cli.py` に修正 |
| P-04 | `plugin.json` にトップレベル `hooks` キーが無い | **対応しない（確定）**。Claude Code公式ドキュメントで `hooks` フィールドが無い場合デフォルトパス `hooks/hooks.json` が Auto-Discovery で自動スキャンされると明記されている。既に自動検出で動いている（Grok実機でもキー無しでStopフックがENOENTを出して発火した実績＝発火自体はしている証拠）。明示キーを追加しても機能はゼロで、デフォルト検出と明示指定が同一ファイルを指した場合の「マージ」挙動が未定義（二重登録のリスクがゼロではない）。実験してまで確かめる価値がない（ゼロ機能追加のためにClaudeの全フックが二重発火するリスクを取る理由がない）ため、キーを追加しない判断とした |
| C-01 | `test-gen.md` が呼ぶ `get_test_command()` がhelpersに無い | **前提誤り。対応不要**。関数は `plugins/bluecore/src/bluecore/lib/project_detect/commands.py:83` に実在し、シグネチャも `test-gen.md:33` の呼び出し記法と一致する。報告書は公開版0.9.27ツリーに対する検証で、本リポジトリの現状とは既に乖離していた |

**枠2: Grok固有問題（Grokの互換動作・ホスト実装に起因、Claude/CopilotCLIの契約は変えない）**

| ID | 内容 | 対応 |
|---|---|---|
| H-03 | `detect_harness()` が `CLAUDECODE` を最優先で見るため、ネストされた実行等で `GROK_*` と共存すると `"claude"` に誤判定 | 修正済み（コミット `7ffa184`）。codex→copilot→grokの非Claude系マーカー判定を先に評価し、いずれにも該当しない場合のみ `CLAUDECODE` を見るよう対称化。`CLAUDECODE` 単独時は必ず `"claude"` を返す不変条件をテストで固定（`launcher.py` の detach 分岐が `!= "claude"` をゲートに使うため、ここが崩れるとClaudeの `--bg` フックが誤ってdetachされる） |
| H-04 | `_TOOL_NAME_MAP` と `hooks.json` の matcher に Grok固有ツール名（`search_replace`/`run_terminal_command`/`spawn_subagent`等）が無く、保護フックが発火しない | 修正済み（コミット `7ee29b0`）。5つの別名をマップに追加、matcher 6箇所（Bash系3・Edit/Write系2・Agent系1）に反映。**未検証の既知の不確実性**: matcher一致によりフック自体は発火するようになったが、`config_protection`/`quality_gate` の `extract_file_paths` は `tool_input.file_path` のみを見て、`block_no_verify`/`pre_bash_commit_quality` の `extract_bash_command` は `tool_input.command`/`cmd` のみを見る。Grokの `search_replace`/`run_terminal_command` が実際にこのキー名でペイロードを渡すかは実機未確認（報告書の実測はいずれも `Write`/`file_path` 形式のみで、Grok固有ツール名でのペイロード形式は記録されていない） |
| A-01 | Grokに `bluecore:*` subagent typeが無く、`pre_agent_nudge` の対応表が使えない型を勧める | 修正済み（H-04と同一コミット）。`spawn_subagent`→Agent のマップ追加でこのフックがGrok上でも発火するようになったため、同時に `detect_harness()=="grok"` 分岐を追加し、`explore`(小文字)/`general-purpose`/`plan` の3型に対して `bluecore専門エージェント型は無い、agents/<name>.mdをReadしてプロンプトに貼れ` という文面に差し替えた。Claude/Copilot側の `AGENT_TABLE`/`EXPLORE_TABLE` 出力は無変更（固定回帰テストで保証） |
| H-00 | Grokの `${CLAUDE_PLUGIN_ROOT}` 展開先とCLIインストール実体が異なり、symlink無しでは launcher が ENOENT → 全フック偽deny | 修正済み（コミット `d55b467`）。詳細は次節 |
| L-01 | `observer.py` が `claude --model haiku` 固定でGrok専用環境ではpending知識が生まれない | **対応不要**。`shutil.which("claude") is None` での skip + ログ記録が `observer.py:615-616` に既に実装済みで、報告書のG-6提案（未導入なら解析しない）を既に満たしている。文言をGrok向けに書き換えるのは churn、SessionStartの `additionalContext` へのGrok専用メッセージ追加は新機能でありYAGNI（かつ非Grok環境でclaude CLIが無い全セッションでノイズになる） |

### H-00 シンボリックリンク自動化（実装済み・コミット `d55b467`）

**結論: 実装した。「symlink作成ロジックを新規に書く」のではなく、既存の `ensure_grok_plugin_root_symlink()`（`grok_plugin_root.py`、変更無し）が実行される前提を満たす方向で解決した。**

鶏卵構造の再確認: Grokは hooks.json の command（`python3 "${CLAUDE_PLUGIN_ROOT}/src/bluecore/launcher.py" ...`）をそのまま解釈し、`${CLAUDE_PLUGIN_ROOT}` を `~/.grok/plugins/bluecore` に展開する。CLIインストール実体は `~/.grok/installed-plugins/bluecore-<hash>/`。symlinkが無い初回、このパスに `launcher.py` が存在せず、Python自体が起動できず ENOENT → exit 2 → 全PreToolUseが偽deny。symlinkを作る `ensure_grok_plugin_root_symlink()` はSessionStartフック（`session_start.py`）から呼ばれるが、そのSessionStart自体も同じ壊れたパス経由でしか起動できないため、初回は永久に到達しない。

**実装内容**: hooks.json 全13エントリの command を以下のシェルラッパーに書き換えた。
```sh
L="${CLAUDE_PLUGIN_ROOT}/src/bluecore/launcher.py"
[ -f "$L" ] || L=$(ls -t "$HOME"/.grok/installed-plugins/bluecore-*/src/bluecore/launcher.py 2>/dev/null | head -n1)
[ -n "$L" ] && [ -f "$L" ] || exit 0
exec python3 "$L" <元の引数（--bg含む場合は位置を保持）>
```
- Claude Code環境では `$CLAUDE_PLUGIN_ROOT` が実体そのものなので1行目で確定し、挙動はバイト単位で不変（実測で確認）
- 一度 launcher に到達すれば、既存の `ensure_grok_plugin_root_symlink()` がSessionStartで symlink を張り、以降は正規パスで直接見つかる
- 解決失敗は必ず `exit 0`（手動実測・pytest両方で確認）
- `exec` を使うことで `block_no_verify`/`config_protection` 等の `exit 2` ブロック契約を維持（ダミーlauncherに `sys.exit(2)` させ、ラッパー経由でも `exit=2` が伝播することを独立に2回実測して確認）
- リンク先は `~/.grok/installed-plugins/bluecore-*` のみ。開発リポジトリ（bluecore-dev）は参照しない（`grok_plugin_root.py` の既存方針のまま）

**付随修正**: `harness_audit_repo_checks.py` の `_hook_command_argv`（CI監査の `memory-hooks-lifecycle` チェックが使う）が `command_tokens[0] in {"python","python3"}` という旧形式前提の解析だったため、ラッパー化で静かに全滅することをpytest失敗で検出し、`exec python3 "$L"` マーカー基準の解析に修正した。テストファイル2点（`test_harness_audit_more.py`/`test_validate_hooks_additional.py`）のフィクスチャも同様に更新。

**既知の未検証事項（残存）**:
- テスト・手動検証はいずれも `sh` 経由。zshでは `bluecore-*` にマッチが無い場合グロブ展開失敗の `nomatch` 警告がstderrに出ることを確認したが、`exit 0` という結果自体には影響しない（ノイズのみ、実害なし）。ホストごとに実際に使われるシェルを網羅検証してはいない
- Grok実機での最終確認（実際に `grok plugin install` → symlink無し → 初回セッションでフックが偽denyしないこと）は未実施。ローカルでの模擬実験（偽HOME・偽launcher）による検証に留まる
