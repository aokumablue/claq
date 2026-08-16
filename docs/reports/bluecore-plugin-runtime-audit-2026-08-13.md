# bluecore プラグイン実行監査報告

## 監査概要

- 実施日: 2026-08-13
- 対象: `bluecore` plugin 0.9.24
- 実行環境: GitHub Copilot CLI 1.0.79、Linux、Python 3.12.3
- 対象リソース: commands 9、skills 13、agents 16、hooks 14 matcher
- 方針: 実際のエージェント呼び出しを主とし、必要な補助確認のみ launcher 経由で実施
- ソースコード: 変更なし（本報告書の新規作成を除く）

## 検証結果

### 静的・構成検証

以下はすべて成功した。

- `validate_commands.py`
- `validate_skills.py`
- `validate_agents.py`
- `validate_hooks.py`
- Python 構文検証
- ruff 検証

インストール済み plugin の一覧、skills の一覧、manifest の agents 16 件を確認し、登録欠落はなかった。

### 実際のエージェント呼び出し

16 個の bluecore 専門エージェントを実際に起動した。入力不足のエージェントには架空の入力を与えず、規定どおり FAIL または入力不足として応答することを確認した。

`bench-analyzer`、`dead-code-cleaner`、`harness-tuner` も、実際の Copilot CLI から再実行した結果、エージェント／スキル解決エラーは発生しなかった。`Skill not found` は再現しなかった。

### commands の実機スモーク

実際の Copilot CLI の `-p` から以下を起動した。状態変更を避けるため、一時ワークスペースまたは監査専用入力を使用した。

| command | 結果 |
|---|---|
| `/bugfix` | 入力不足を正常報告 |
| `/feat-dev` | 入力不足を正常報告 |
| `/harness --audit-only` | 監査スコアを取得、変更なし |
| `/instinct list` | 空の知識カード一覧を正常報告 |
| `/plan` | 読み取り専用計画を正常終了 |
| `/refactor` | 対象なしを正常報告 |
| `/review` | 差分なしを正常報告 |
| `/skill-gen` | 生成対象なしを正常報告 |
| `/test-gen` | 対象ファイルなしを正常報告 |

### hooks の実機検証

実際のエージェント操作で以下を確認した。

- `ruff.toml` 作成: config protection が拒否
- `git commit --no-verify`: pre-tool hook が拒否
- `bad.py` 作成: quality gate が Ruff の未使用 import を通知
- `Agent` サブエージェント起動: child agent 自体は正常起動
- SessionStart、PostToolUse、Stop、SessionEnd の副作用と出力形式
- 空入力、不正 JSON、巨大入力、正常入力、複数のブロック条件、`--bg` detach

## 確認された不具合

### F-01: Copilot の Agent 入力キーと pre-agent-nudge の参照キーが不一致

- 重要度: 中
- 影響: Copilot CLI で専門エージェント案内が注入されず、静かに機能しない
- 対象: `src/bluecore/hooks/pre_agent_nudge.py`

Copilot CLI の実際の `Agent` ツール入力は次の形式だった。

```json
{
  "tool_name": "Agent",
  "tool_input": {
    "agent_type": "general-purpose"
  }
}
```

実際の観測ログ:

`~/.bluecore/repos/bluecore-hook-nudge-real-veqvtx/observations.jsonl`

一方、`pre_agent_nudge.py` は次のキーだけを参照している。

```python
subagent_type = str(tool_input.get("subagent_type") or "")
```

そのため、Copilot の実 payload では `general-purpose` / `Explore` を検出できず、終了コード 0・無出力になる。実際に child agent へ「追加コンテキスト内の専門エージェント名を返す」と依頼したところ、結果は `NONE` だった。

#### 再現方法

```bash
PLUGIN=/home/tasaki-mamoru/.copilot/installed-plugins/bluecore/bluecore
LAUNCHER="$PLUGIN/src/bluecore/launcher.py"

printf '%s' \
  '{"tool_name":"Agent","tool_input":{"agent_type":"general-purpose"}}' |
  env COPILOT_TEST=1 CLAUDE_PLUGIN_ROOT="$PLUGIN" \
  python3 "$LAUNCHER" bluecore.hooks.pre_agent_nudge
```

実測結果:

```text
終了コード: 0
標準出力: 空
```

比較として `agent_type` を `subagent_type` に置き換えると、`hookSpecificOutput` と専門エージェント対応表が出力される。つまりフック本体の案内文生成は動作するが、Copilot の実入力では到達しない。

### F-02: camelCase payload 互換性の取りこぼし

- 重要度: 条件付き・中
- 影響: camelCase の hook 設定または payload を使用した場合、一部 hook が対象を認識しない
- 対象:
  - `src/bluecore/hooks/config_protection.py`
  - `src/bluecore/hooks/quality_gate.py`
  - `src/bluecore/hooks/pre_agent_nudge.py`

共通の `extract_tool_input()` は `toolArgs` を吸収するが、上記 3 hook は `data["tool_input"]` を直接参照する。`toolArgs` 形式では次の差異を再現した。

| hook | `tool_input` | `toolArgs` |
|---|---|---|
| config protection | `ruff.toml` を deny | 許可される |
| quality gate | lint 結果を additional context に出力 | 無出力 |
| pre-agent nudge | 対応表を出力 | 無出力 |

#### 再現方法

```bash
PLUGIN=/home/tasaki-mamoru/.copilot/installed-plugins/bluecore/bluecore
LAUNCHER="$PLUGIN/src/bluecore/launcher.py"

printf '%s' \
  '{"tool_name":"Edit","toolArgs":{"file_path":"ruff.toml"}}' |
  env COPILOT_TEST=1 CLAUDE_PLUGIN_ROOT="$PLUGIN" \
  python3 "$LAUNCHER" bluecore.hooks.config_protection
```

実測結果は終了コード 0・deny 出力なしだった。`tool_input` に同じ内容を入れた場合は Copilot 用 deny JSON が出力される。

なお、現在の `hooks.json` は PascalCase の `PreToolUse` / `PostToolUse` を使用し、公式契約上の標準入力は snake_case の `tool_input` である。そのため通常の登録経路では F-02 は発生しない。camelCase 形式も吸収すると説明している共通ハーネスとの互換性問題として記録する。

native camelCase の完全な payload は `toolName` と `toolArgs` の組である。上の再現は `toolArgs` の取りこぼしだけを分離するために `tool_name` を残した最小入力だった。後述の TDD では、実際の互換性を検証するため完全な native camelCase payload を使用する。

## 不具合として確定しなかった観測

### `.bluecore` の初期ディレクトリ権限

`launcher.py --bg` を SessionStart 前に単独実行し、umask 022 の環境で新規作成すると `~/.bluecore` が 0755 になるケースを確認した。通常の hook 順序では SessionStart の `session_install` が 0700 に補正し、その後の DB・ログは 0600、ディレクトリは 0700 になるため、通常経路の確定不具合とは分類しなかった。直接 launcher を利用する場合の hardening 課題として残る。

### `Skill not found`

初回の複合ワークフローでは手動フォールバックに見える応答があったが、同じ 3 エージェントを実際の Copilot CLI から再起動した検証では再現しなかった。現在のインストール状態で確定不具合とは扱わない。

### 入力不足時の長時間実行

入力なしの `dead-code-cleaner` / `harness-tuner` は 120 秒以内に応答しない試行があったが、対象または baseline JSON を明示した再試行は正常終了した。入力不足時の探索量による実行時間差と判断し、今回の確定不具合には含めない。

## 結論

主要な commands、skills、agents、hooks は実機で起動でき、設定保護・no-verify 防止・品質ゲートなどの主要ガードレールは正常に動作した。一方、Copilot CLI の実 payload に対する `pre_agent_nudge` の `agent_type` 不一致は再現可能な機能欠落であり、F-01 として記録する。コード修正は実施していない。

## 別セッション向け TDD 実装計画

### デシジョンテーブルのレビューで修正した事項

初稿を実装と再照合し、以下を修正した。

1. R-01 の修正前実測を訂正した。`tool_input.agent_type=general-purpose` は現行の `pre_agent_nudge` では互換動作せず、無出力になる。これは F-01 の直接の RED ケースである。
2. native camelCase payload は `toolArgs` だけではなく `toolName` も組になる。`tool_name` と `toolArgs` を混在させた入力だけをテストする設計は実際の互換性を検証できないため、snake_case と native camelCase をそれぞれ完全な payload として扱うようにした。
3. `quality_gate._extract_target_file_paths()` は旧 `file` キーを fallback しない。旧 `file` の fallback は `config_protection` だけに存在するため、品質ゲートの期待値を `[]` に訂正した。
4. `tool_input` と `toolArgs` が同居する混在 payload の優先順位と、設定保護での安全側の扱いを明文化した。通常 hook は既存どおり `tool_input` を優先し、設定保護だけは、いずれかの候補が保護対象なら block する。
5. coverage の全体閾値は現時点で `fail_under = 100` であり、対象テストだけの実行では満たせない。RED/GREEN 判定と情報取得用 coverage を分離するようにした。

### 目的と実施順序

別セッションでは、以下の順序を厳守する。

1. 本節のテストを先に追加する。
2. 追加テストだけを実行し、既知の不具合を RED（期待値不一致）として確認する。
3. F-01 / F-02 の最小修正を実施する。
4. 追加テストを再実行して GREEN にする。
5. 既存挙動の回帰確認として plugin 全体の pytest、ruff、必要に応じて coverage を実行する。

この監査セッションでは、テストコード・プロダクトコードのいずれも変更しない。テスト実装の対象はインストール済みコピーではなく、リポジトリ内の `plugins/bluecore/` とする。

### テスト実装の前提

- 現時点で `plugins/bluecore/tests/` 配下に既存テストはないため、新規テストを作成する。
- `plugins/bluecore/pyproject.toml` の `pytest>=8.0`、`pytest-cov>=4.0`、`ruff>=0.4` を使用する。
- 外部ライブラリは追加しない。入力差し替えは pytest の `monkeypatch`、出力確認は `capsys`、サブプロセスの必要なケースだけ標準ライブラリ `subprocess` を使う。
- hook の `main()` は常に非ブロッキングを意図しているため、通常入力・不正入力ともに終了コードを明示的に検証する。
- テスト fixture は、DT-03 の競合入力テストを除き、実際の payload 形式を混在させない。snake_case / VS Code 互換形式は `{"tool_name": ..., "tool_input": ...}`、native camelCase 形式は `{"toolName": ..., "toolArgs": ...}` とする。`tool_args` は共通 helper の内部互換性テストだけで使う。
- 単体テストは stdin reader と block emitter を monkeypatch して副作用をなくす。出力プロトコルは launcher を別プロセスで起動する DT-06 で確認する。
- `detect_harness()` は `lru_cache` を持つ。環境変数を変更する単体テストでは、必要に応じて `detect_harness.cache_clear()` を fixture の前後で呼び出す。

### 推奨テストファイル

| ファイル | 目的 |
|---|---|
| `plugins/bluecore/tests/test_harness_payload.py` | `tool_input` / `toolArgs` / `tool_args` と `tool_name` / `toolName` の共通正規化 |
| `plugins/bluecore/tests/test_pre_agent_nudge.py` | F-01、F-02 の agent payload 分岐と出力 |
| `plugins/bluecore/tests/test_config_protection.py` | F-02 の設定ファイル保護分岐 |
| `plugins/bluecore/tests/test_quality_gate.py` | F-02 の対象ファイル抽出と lint rule 起動 |
| `plugins/bluecore/tests/test_hook_cli_contract.py` | launcher 経由の最小 end-to-end 回帰 |

### RED で確認すべき既知の失敗

修正前に、次のテストが失敗することを確認する。失敗しない場合は、テスト入力が不具合を再現しているか、インストール済みコピーを誤って import していないかを確認する。

| ID | テスト対象 | 修正前の実測 | 修正後の期待 |
|---|---|---|---|
| R-01 | `pre_agent_nudge` + `tool_input.agent_type=general-purpose` | 無出力 | 対応表を出力 |
| R-02 | `pre_agent_nudge` + native `toolName=agent`, `toolArgs.agent_type=general-purpose` | 無出力 | 対応表を出力 |
| R-03 | `config_protection` + native `toolName=edit`, `toolArgs.file_path=ruff.toml` | 許可される | deny/block |
| R-04 | `quality_gate._extract_target_file_paths` + native `toolName=edit`, `toolArgs.file_path=sample.py` | `[]` | `["sample.py"]` |
| R-05 | `quality_gate.run` + native `toolName=edit`, `toolArgs.file_path=sample.py` | 対象 rule が起動しない | `sample.py` を対象に rule が起動 |

## デシジョンテーブル

### DT-01: 共通 payload 正規化

対象:

- `plugins/bluecore/src/bluecore/lib/harness.py:extract_tool_input`
- 修正時に追加する raw tool name 抽出 helper（`tool_name` / `toolName` を吸収するもの）

既存の `tool_input` 挙動を維持しつつ、各 hook が共通 helper を使えることを固定する。入力コンテナはフィールドの存在順で `tool_input`、`toolArgs`、`tool_args` とし、複数ある場合は `tool_input` を優先する。これは通常の hook で二重実行を起こさないための規則であり、保護対象の判定は DT-03 の安全側規則を優先する。

| # | payload のキー | 値の型・内容 | 期待値 | 備考 |
|---|---|---|---|---|
| 1 | `tool_input` | dict `{"file_path":"sample.py"}` | 同じ dict | 既存形式 |
| 2 | `toolArgs` | dict `{"file_path":"sample.py"}` | 同じ dict | Copilot camelCase |
| 3 | `tool_args` | dict `{"file_path":"sample.py"}` | 同じ dict | 互換 alias |
| 4 | `toolArgs` | JSON 文字列 `"{\"file_path\":\"sample.py\"}"` | dict に decode | 文字列 JSON |
| 5 | `toolArgs` | JSON 配列文字列 `"[\"raw\"]"` | list に decode | `[` も JSON として扱う |
| 6 | `toolArgs` | 不正 JSON 文字列 `"{bad"` | 同じ文字列 | 例外を送出しない |
| 7 | `toolArgs` | 通常文字列 `"raw command"` | 同じ文字列 | JSON として decode しない |
| 8 | `tool_input` + `toolArgs` | 両方 dict、内容が異なる | `tool_input` 側 | 通常 hook の既存優先順位 |
| 9 | `tool_input` + `toolArgs` | `tool_input=None`、`toolArgs` は dict | `None` | 現行のフィールド存在優先を明示 |
| 10 | いずれもなし | キーなし | `None` | 呼び出し側は安全側に倒す |
| 11 | `toolArgs` | `None` / list / 数値 | その値 | helper は型変換しない |

raw tool name 抽出 helper は、`redux_filter.evaluate()` が既に採っている `tool_name`、`toolName` の順序を共通化する。返値は `normalize_tool_name()` の前の文字列とし、`apply_patch` の未解析パッチを fail-closed にする既存判定で使えるようにする。

| # | payload | 期待 raw tool name | 備考 |
|---|---|---|---|
| 1 | `{"tool_name":"Edit"}` | `"Edit"` | snake_case |
| 2 | `{"toolName":"edit"}` | `"edit"` | native camelCase |
| 3 | 両方あり、値が異なる | `tool_name` 側 | 既存形式を優先 |
| 4 | `tool_name` が非文字列、`toolName="edit"` | `"edit"` | 有効な文字列へ fallback |
| 5 | `toolName` が非文字列 | `""` | 型不正は対象外 |
| 6 | 両方なし | `""` | 対象外 |

### DT-02: `pre_agent_nudge` のエージェント種別判定

対象: `plugins/bluecore/src/bluecore/hooks/pre_agent_nudge.py:main`

実装時は `tool_input` だけを直接参照せず、DT-01 の正規化結果から agent type を取得する。**同じ正規化済み dict 内では** `subagent_type` を優先し、Copilot の `agent_type` はフォールバックとして扱う。複数コンテナが同居する非標準入力では DT-01 の `tool_input` 優先を維持する。

| # | payload 形式 | agent type キー | 値 | 期待終了コード | 期待 stdout |
|---|---|---|---|---:|---|
| 1 | snake_case `tool_input` dict | `subagent_type` | `general-purpose` | 0 | `AGENT_TABLE` |
| 2 | snake_case `tool_input` dict | `subagent_type` | `Explore` | 0 | `EXPLORE_TABLE` |
| 3 | snake_case `tool_name=Agent`, `tool_input` dict | `agent_type` | `general-purpose` | 0 | `AGENT_TABLE`（F-01 RED） |
| 4 | snake_case `tool_name=Agent`, `tool_input` dict | `agent_type` | `Explore` | 0 | `EXPLORE_TABLE` |
| 5 | native `toolName=agent` + `toolArgs` dict | `agent_type` | `general-purpose` | 0 | `AGENT_TABLE`（F-02 RED） |
| 6 | native `toolName=agent` + `toolArgs` dict | `agent_type` | `Explore` | 0 | `EXPLORE_TABLE` |
| 7 | `tool_args` dict | `agent_type` | `general-purpose` | 0 | `AGENT_TABLE` |
| 8 | native `toolName=agent`, `toolArgs` JSON 文字列 | `agent_type` | `general-purpose` | 0 | `AGENT_TABLE` |
| 9 | 両コンテナあり | `tool_input.subagent_type=Explore`、`toolArgs.agent_type=general-purpose` | — | 0 | `EXPLORE_TABLE`（canonical `tool_input` 優先） |
| 10 | native `toolArgs` dict | `agent_type` | 未知の値 | 0 | 空 |
| 11 | native `toolArgs` dict | なし | — | 0 | 空 |
| 12 | native `toolArgs` | 非 dict の値 | — | 0 | 空 |
| 13 | payload | — | 不正 JSON / 空入力 | 0 | 空 |
| 14 | native `toolArgs` dict | `agent_type` | `General-Purpose` / `explore` | 0 | 空（既存の大文字小文字区別を維持） |

追加の出力契約テストでは、対応表が出力される場合に次を検証する。

- JSON として parse できる。
- `hookSpecificOutput.hookEventName == "PreToolUse"` である。
- `additionalContext` が空でない。
- general-purpose の場合は `bluecore:executor` を含む。
- Explore の場合は `bluecore:explorer` を含む。
- ブロック出力や deny decision は返さない。

### DT-03: `config_protection` の入力形式と保護判定

対象: `plugins/bluecore/src/bluecore/hooks/config_protection.py:main`

保護判定の本体は変更せず、入力コンテナの違いだけで判定結果が変わらないことを固定する。`emit_block_output` は単体テストでは spy に差し替え、理由文字列と呼び出し回数を検証する。

設定保護はセキュリティ境界であるため、非標準の混在 payload でも、**いずれかの parse 可能な入力コンテナが保護対象を指すなら deny** とする。通常 hook の canonical input 選択だけに依存すると、`tool_input` に安全なパス、`toolArgs` に保護対象を入れた競合入力で保護が外れるためである。

| # | payload 形式 | 対象パス・入力 | 期待結果 |
|---|---|---|---|---|
| 1 | snake_case `tool_name=Edit`, `tool_input` dict | `file_path=ruff.toml` | block、理由に `ruff.toml` |
| 2 | native `toolName=edit`, `toolArgs` dict | `file_path=ruff.toml` | block、#1 と同じ理由（F-02 RED） |
| 3 | native `toolName=edit`, `toolArgs` JSON 文字列 | `file_path=ruff.toml` | block |
| 4 | `tool_name=Write`, `tool_args` dict | `file_path=.eslintrc` | block |
| 5 | native `toolName=edit`, `toolArgs` dict | 旧 `file=ruff.toml` | block、既存 legacy fallback を維持 |
| 6 | native `toolName=multiedit`, `toolArgs` dict | `file_path=src/sample.py` | allow、block 未呼び出し |
| 7 | native `toolName=edit`, `toolArgs` dict | `file_path=src/ruff.toml` | block、basename 判定 |
| 8 | native `toolName=bash`, `toolArgs` dict | `file_path=ruff.toml` | allow、書込み系以外は対象外 |
| 9 | native `toolName=edit`, `toolArgs` dict | path key なし | allow（通常の非対象入力） |
| 10 | native `toolName=apply_patch`, `toolArgs` dict | パッチ本文を抽出不能 | fail-closed block |
| 11 | 任意 | stdin が `MAX_STDIN_BYTES` 超過 | fail-closed block、切り捨て理由 |
| 12 | `tool_input` と `toolArgs` の両方 | 一方が `src/sample.py`、他方が `ruff.toml` | block（保護対象がどちらにあっても deny） |
| 13 | `tool_input` と `toolArgs` の両方 | 両方とも非保護パス | allow |
| 14 | `tool_input=None` と `toolArgs` の両方 | `toolArgs.file_path=ruff.toml` | block（canonical input が空でも保護を外さない） |

### DT-04: `quality_gate._extract_target_file_paths()` の対象抽出

対象: `plugins/bluecore/src/bluecore/hooks/quality_gate.py:_extract_target_file_paths`

この関数は lint 実行そのものではなく、対象パスの抽出責務だけをテストする。`toolArgs` 対応後も構造化パッチとトップレベル `file_path` fallback を壊さない。なお、旧 `file` キーの fallback はこの関数には存在しないため、その非対応を `[]` の期待値で固定する。

| # | payload 形式 | 入力 | 期待パス |
|---|---|---|---|---|
| 1 | snake_case `tool_name=Edit`, `tool_input` dict | `{"file_path":"sample.py"}` | `["sample.py"]` |
| 2 | native `toolName=edit`, `toolArgs` dict | `{"file_path":"sample.py"}` | `["sample.py"]` |
| 3 | `tool_name=Edit`, `tool_args` dict | `{"file_path":"sample.py"}` | `["sample.py"]` |
| 4 | native `toolName=edit`, `toolArgs` JSON 文字列 | `{"file_path":"sample.py"}` | `["sample.py"]` |
| 5 | native `toolName=apply_patch`, `toolArgs` dict | `{"input":"*** Update File: a.py\n..."}` | `["a.py"]` |
| 6 | native `toolName=apply_patch`, `toolArgs` dict | 複数の Add/Update/Delete marker | marker 順の全パス |
| 7 | snake_case `tool_name=Edit` | 入力コンテナなし、トップレベル `file_path=sample.py` | `["sample.py"]` |
| 8 | snake_case `tool_name=Edit`, `tool_input` dict | 旧形式 `{"file":"sample.py"}` | `[]`（quality gate に legacy fallback はない） |
| 9 | native `toolName=edit`, `toolArgs` dict | パスなし | `[]` |
| 10 | native `toolName=bash`, `toolArgs` dict | `file_path=sample.py` | `["sample.py"]`（抽出自体は可能。`run()` 側で対象外） |

### DT-05: `quality_gate.run()` の rule 起動

対象: `plugins/bluecore/src/bluecore/hooks/quality_gate.py:run`

外部 lint コマンドを実行しないよう、`load_config` と `_run_configured_rules` を monkeypatch して、抽出されたパスが rule 実行に渡ることだけを検証する。

| # | write tool 判定 | payload 形式 | 対象パス | 期待値 |
|---|---|---|---|---|
| 1 | true | snake_case `tool_name=Edit`, `tool_input` | `sample.py` | `load_config("sample.py")` と rule 起動 |
| 2 | true | native `toolName=edit`, `toolArgs` | `sample.py` | #1 と同じ（F-02 RED） |
| 3 | true | native `toolName=edit`, `toolArgs` JSON 文字列 | `sample.py` | #1 と同じ |
| 4 | false | native `toolName=bash`, `toolArgs` | `sample.py` | 結果 `[]`、rule 未起動 |
| 5 | true | native `toolName=apply_patch`, `toolArgs` structured patch | `a.py`, `b.py` | 各ファイルを対象に rule 起動し、`run_pathless` は順に `True`, `False` |
| 6 | true | native `toolName=edit`, `toolArgs`、対象パスなし | — | `load_config(None)` を1回呼び、pathless rule だけを1回実行 |
| 7 | false | `toolName` が非文字列 / 欠落 | `sample.py` が `toolArgs` に存在 | 結果 `[]`、rule 未起動 |
| 8 | true | native `toolName=edit`, `toolArgs` | rule の `tool_names=["edit"]` | `_rule_matches()` が true |
| 9 | false | native `toolName=bash`, `toolArgs` | rule の `tool_names=["edit"]` | `_rule_matches()` が false |

### DT-06: 実行入口の最小 end-to-end 契約

対象: `plugins/bluecore/src/bluecore/launcher.py` 経由の hook 実行。単体テストで十分に分離できない入力変換だけを確認し、lint や実エージェント API には依存しない。

| # | hook | payload | 期待終了コード | 期待出力 |
|---|---|---|---:|---|
| 1 | `pre_agent_nudge` | `tool_input.subagent_type=general-purpose` | 0 | AGENT_TABLE |
| 2 | `pre_agent_nudge` | snake_case `tool_input.agent_type=general-purpose` | 0 | AGENT_TABLE |
| 3 | `pre_agent_nudge` | native `toolName=agent`, `toolArgs.agent_type=general-purpose` | 0 | AGENT_TABLE |
| 4 | `config_protection` | snake_case `tool_name=Edit`, `tool_input.file_path=ruff.toml` | Copilot は 0 | `permissionDecision=deny` |
| 5 | `config_protection` | native `toolName=edit`, `toolArgs.file_path=ruff.toml` | Copilot は 0 | #4 と同じ deny |
| 6 | `config_protection` | native `toolName=edit`, `toolArgs.file_path=sample.py` | 0 | deny なし |

この契約テストでは、リポジトリ内の `plugins/bluecore/src/bluecore/launcher.py` を `subprocess.run()` で起動する。`CLAUDE_PLUGIN_ROOT`、`COPILOT_TEST=1`、一時 `HOME` を明示し、インストール済み plugin や既存 user home に副作用を出さない。`COPILOT_TEST=1` は `detect_harness()` が Copilot 形式の deny JSON を選ぶことを確認するために必要である。

## テストケース名と検証コマンド

別セッションでの推奨テスト名は次のとおり。

```text
test_extract_tool_input_accepts_tool_args_dict
test_extract_tool_input_decodes_tool_args_json_object
test_extract_tool_input_preserves_malformed_json_string
test_extract_tool_input_prefers_tool_input_over_tool_args
test_extract_raw_tool_name_accepts_snake_and_camel_case_fields
test_pre_agent_nudge_emits_agent_table_for_tool_input_agent_type
test_pre_agent_nudge_emits_agent_table_for_native_camel_case_payload
test_pre_agent_nudge_emits_explore_table_for_agent_type
test_pre_agent_nudge_accepts_native_tool_args_json_string
test_pre_agent_nudge_prefers_subagent_type_when_both_keys_exist
test_pre_agent_nudge_ignores_unknown_agent_type
test_config_protection_blocks_protected_file_from_native_camel_case_payload
test_config_protection_blocks_legacy_file_from_native_tool_args
test_config_protection_denies_conflicting_payload_when_any_target_is_protected
test_config_protection_allows_unprotected_native_camel_case_payload
test_quality_gate_extracts_file_path_from_native_camel_case_payload
test_quality_gate_does_not_treat_legacy_file_as_target_path
test_quality_gate_extracts_all_patch_paths_from_native_tool_args
test_quality_gate_run_applies_rules_to_native_camel_case_file
test_quality_gate_runs_pathless_rules_once_for_multi_file_patch
test_quality_gate_rule_matches_native_tool_name_filter
test_launcher_pre_agent_nudge_accepts_actual_copilot_agent_payload
test_launcher_config_protection_denies_native_camel_case_payload
```

最初の RED 確認:

```bash
cd plugins/bluecore
PYTHONPATH=src python3 -m pytest -q --no-cov \
  tests/test_pre_agent_nudge.py \
  tests/test_config_protection.py \
  tests/test_quality_gate.py
```

修正後の対象確認:

```bash
cd plugins/bluecore
PYTHONPATH=src python3 -m pytest -q --no-cov tests
ruff check src tests
```

情報取得として対象モジュールの branch coverage を確認する場合:

```bash
cd plugins/bluecore
PYTHONPATH=src python3 -m pytest -q \
  --cov=bluecore.lib.harness \
  --cov=bluecore.hooks.pre_agent_nudge \
  --cov=bluecore.hooks.config_protection \
  --cov=bluecore.hooks.quality_gate \
  --cov-branch \
  --cov-fail-under=0 \
  tests/test_harness_payload.py \
  tests/test_pre_agent_nudge.py \
  tests/test_config_protection.py \
  tests/test_quality_gate.py
```

`pyproject.toml` の coverage `fail_under = 100` は既存テストのない現状では全体ゲートとして扱えない。上記の `--cov-fail-under=0` は数値を確認するためだけに使い、RED/GREEN 判定は pytest の成否で行う。テスト失敗を本体コード側で無条件に隠すこと、外部依存を追加して解決すること、テストを通すために期待値を下げることは禁止する。

## 修正セッションへの引き継ぎ事項

1. F-01 は `pre_agent_nudge` が `extract_tool_input()` を使い、同一 dict 内で `subagent_type` を優先しつつ `agent_type` も受け付ける最小修正を第一候補とする。
2. F-02 は `config_protection`、`quality_gate`、`pre_agent_nudge` の3箇所で共通入力 helper を使い、native camelCase の完全な組 `toolName` / `toolArgs` をサポートする。raw tool name の抽出は、同じ対応をしている `redux_filter.evaluate()` を既存実装の手本にする。
3. 設定保護では、混在 payload のどちらかが保護対象なら deny する。品質 gate と agent nudge は二重処理を避けるため `tool_input` 優先を維持する。
4. 既存の未知 agent type の無出力、hook の常時 exit 0、protected file の fail-closed、apply_patch の既存抽出仕様を維持する。コード、docstring、型注釈の `subagent_type` 専用表現も実装後の実契約に合わせる。
5. GREEN 後に実際の Copilot CLI から、`Agent` の `agent_type`、設定ファイル編集、品質 gate の3操作を再実行し、単体テストだけでなく監査時の症状が解消したことを確認する。

## 訂正節（2026-08-16 追記）

本監査が検証対象とした `pre_agent_nudge`（DT-02 のエージェント種別判定、F-01/F-02 の
`agent_type` 分岐を含む）は、Claude Opus 5 向けプロンプティングガイド
（https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5）
の観点による過剰ハーネス介入の見直しで廃止した。本監査時点（0.9.24）での動作記述は
当時の事実として有効だが、現在のリポジトリには該当フック・テストは存在しない。
