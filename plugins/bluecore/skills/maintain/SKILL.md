---
name: maintain
description: ハーネス（commands/skills/agents/hooks）の定期メンテを一気通貫で実施する。レビュー→実害修正→強化→再レビュー→指摘修正→記録まで1回の実行で完了。「ハーネスをメンテ」「commands/skills/agents/hooks を定期メンテ／見直し／強化」「定期メンテとしてプラグイン全体をレビューして直す」「エージェント定義を最新トレンドでブラッシュアップ」等で発火。単発の1ファイル修正は /review /bugfix /refactor、audit スコア改善のみは /harness を使う（定義文書のレビュー・修正まで踏み込むなら本スキル）。
context: fork
user-invocable: true
---

# ハーネス定期メンテ

commands/skills/agents/hooks を周期的にレビューし、実害を修正し、下位モデルでも Fable5 品質で動くよう強化し、再レビューで裏を取るまでを1回で完遂する。

## 焼き込み原則（この5つを全工程で守る）

1. **注入後レビュー**: 指示文書へ条項を追加する強化は、条項自体が新たな穴を生む（パスガード欠落・確信度ゲート過剰適用・経路非対称）。強化後は必ず再レビューを通す。
2. **工程分離**: レビューは READ-ONLY、編集は承認ゲート後。混ぜない。
3. **現物実証**: CRITICAL 指摘は鵜呑みにせず、サンドボックスや実行で失敗を再現してから直す。
4. **canonical 再利用**: 新表現を発明せず executor / tdd-writer / reviewer / feat-dev / loop-dev の既存文言を再利用（トークン増と表現ゆれを回避）。過去知見「列挙型禁止は逆効果、肯定形・原則化が正」に従う。
5. **両ハーネス互換**: プラグインは Claude Code（主）と GitHub Copilot CLI（副）の両方で動く。ハーネス依存の入出力は `hook_common` / `output_adapter` の既存チョークポイント（`emit_block_output` / `adapt_context_output` 等）経由に一本化し、Copilot で実現不可能な機能には**フォールバック**（同等動作、不可能なら安全側の明示スキップ）を実装する。Claude Code 側の処理経路は変更しない。ハーネス判定やプロトコル分岐をフック内へ直書きした実装はレビューで指摘・是正する（前例: insights_security_monitor の Copilot fail-open を emit_block_output 一本化で解消）。

## モデル規律

サブエージェント委譲は常に `model:"fable"` オーバーライドで起動する（`architect`/`planner`/`simplifier` は frontmatter で Opus 固定のため、指定しないと Opus を消費する）。専門プロンプトは保ちつつ Fable で走らせる。

## スコープ

- 主対象: `plugins/bluecore/{commands,skills,agents,hooks}`（引数 `--scope` で上書き）
- 指摘が指す実装ファイル（`src/bluecore/hooks/` 等）への修正も許可
- 非目標: `rules/` のメンテ／スケジューラ内蔵／auto-push／RLS 級の新機能実装（検出時は `/plan` 提示に留める）

## ステップ1: 準備・入力収集（READ-ONLY）

```bash
# 開発リポジトリでは repo 版を source する（プラグインキャッシュ版は stale の可能性）
source plugins/bluecore/runtime/bluecore-helpers.sh
collect_skill_create_inputs "${COMMITS:-200}"        # コミット規約・同時変更パターン
```

集める入力（各ソースは失敗しても本体を止めない＝ベストエフォート）:

- **蓄積メモリ**: `echo '{"query": "maintain harness 勘所 違反"}' | PYTHONPATH=plugins/bluecore/src python3 -m bluecore.mem search` で過去メンテの勘所・繰り返し違反を引く（mem CLI は stdin JSON プロトコル。裸起動は空結果を返すので必ず pipe する）
- **過去セッション**: `~/.bluecore/session-data/checkpoint-*.md` と git log
- **チーム共有 PostgreSQL**（`sync.enabled=True` かつ `postgres_url` 設定時のみ）: mem CLI が判定を内包。無効時は SQLite（`~/.bluecore/mem.db`）に自然フォールバック
- **最新 ClaudeCode トレンド**（既定ON・`--no-web` で無効）: WebSearch/WebFetch でハーネス設計のベストプラクティスを調べる。**ハード上限（検索5件・フェッチ3件）・タイムアウト付き・非ブロッキング**。失敗/オフライン時は「トレンド入力なし」と明記して続行

**baseline 取得**（すべて `.venv` 有効化 + `PYTHONPATH=plugins/bluecore/src` で実行）:

- `python3 -m pytest -q --cov`（カバレッジは pyproject の `fail_under=100` で判定。`--cov` なしでは測定されない）
- `ruff check plugins/bluecore/src`
- `python3 -m bluecore.ci.validate_skills`（`validate_commands` / `validate_agents` / `validate_hooks` も同形式で4つ全て実行）
- `python3 -m bluecore.ci.harness_audit repo --format json`（audit の scope は `repo|hooks|skills|commands|agents` のキーワード。本スキルの `--scope` 引数＝パスとは別物でパス指定不可）

既存失敗を記録し新規失敗判定の基準にする。

## ステップ2: レビュー（READ-ONLY・並列・`model:"fable"`）

対象群に `bluecore:reviewer`（品質・設計・保守性）と `bluecore:security-auditor`（脆弱性）を**同時起動**。両結果を深刻度（CRITICAL/HIGH/MEDIUM/LOW）・ファイル位置・行番号・推奨修正で統合。ステップ1の web トレンドを踏まえ「最新プラクティスとの乖離」も観点に含める。

hooks / `src/bluecore/hooks/` を対象に含む回は**両ハーネス互換（原則5）を必須観点**にする: ブロック系出力は `emit_block_output` 経由か、コンテキスト注入は `adapt_context_output` 経由か、Copilot CLI 非対応のイベント・機能にフォールバック（または安全側スキップ）があるか、Claude Code 経路への影響ゼロか。ハーネスごとの利用可能イベント・環境変数の実態は `src/bluecore/hooks/harness_probe.py` の記録を根拠にする。

## ステップ3: 承認ゲート（AskUserQuestion 1回）

指摘を種別分類して修正実行可否を確認する。ゲートを通すまで編集しない。baseline の既存失敗も同じ表で分類してゲートに載せる（黙って修正もスルーもしない）。AskUserQuestion が使えない実行文脈では、指摘レポートを提示して**停止**する（自己承認で編集に進まない＝原則2）。

| 種別 | 次アクション |
|---|---|
| バグ・実害脆弱性 | ステップ4で tdd-writer 修正 |
| 強化（条項注入・出力形式・トークン整理） | ステップ4で直接編集 |
| 仕様変更・新機能 | `/plan` 提示に留め自動実装しない |

## ステップ4: 修正（承認後・`model:"fable"` 委譲）

- **バグ**: `bluecore:tdd-writer` で RED→GREEN（現物実証＝原則3）。実害脆弱性も同様。
- **フック修正**: ハーネス依存の入出力は `hook_common` / `output_adapter` のチョークポイントへ寄せる（原則5）。新規フック・外部呼び出しは非ブロッキング + ハードタイムアウト必須（CLAUDE.md 準拠）。
- **強化**: 指示文書への条項追加は WHAT を変えるため `/refactor` 機構（WHAT 不変前提）でなく**直接編集**。canonical 文言を再利用（原則4）。
- **仕様変更**: 実装せず `/plan` 用の要件だけ整理。
- 各修正の単位ごとに検証（baseline と同じ4コマンド）→ 新規失敗はその単位をリバートして継続し、リバート分はステップ7の残タスクに記載。作業内容に応じ細分化し、現在のブランチへ `type(scope): 要約` 規約でコミット（main へ直コミットしない）。

## ステップ5: 再レビュー（並列・`model:"fable"`）＝原則1

修正差分に `bluecore:reviewer` + `bluecore:security-auditor` を再起動。強化で新たな穴が入っていないかを必ず確認する。CRITICAL 指摘はステップ4同様に現物実証してから対応。

## ステップ6: final gate（非退行ゲート）

以下を全て満たすまでステップ4-5をループ（最大3周。満たせない場合は残指摘を明記して停止・報告）:

1. `validate_{skills,commands,agents,hooks}` / `pytest --cov` / `ruff` が **baseline 非退行**（新規失敗ゼロ。baseline 既存失敗はステップ3の承認結果に従う）
2. 今回修正したファイルに起因する失敗ゼロ
3. 再レビューで CRITICAL / HIGH ゼロ
4. `harness_audit` の `overall_score` が baseline 非退行

スコア改善・指摘件数減は**副次レポート指標**（改善必須にすると飽和時に不要変更を誘発するため必須にしない）。

## ステップ7: 記録・要約

`{"event_type": "maintain", "content": "Scope. Reviewed. Fixed(bug/enhance). Blockers→0. Audit: {before}→{after}. Trend入力: {あり/なし}"}` を mem record。要約は結論先行で、修正コミット・final gate 結果・残タスク（`/plan` 提示分）を提示。

## 引数

- `--scope=<path>`: 対象上書き（既定 `plugins/bluecore/{commands,skills,agents,hooks}`）
- `--commits=<n>`: 入力収集のコミット数（既定 200）
- `--no-web`: web トレンド調査を無効化
- `--dry-run`: ステップ1-2のみ（audit + レビュー報告）で編集しない（ステップ7の mem record も行わない）
