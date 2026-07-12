---
name: skill-gen
description: リポジトリ固有入力収集→skill-make スキルに SKILL.md 生成委譲→skill-tune スキルに改善委譲→grader/comparator/bench-analyzer による評価。
command: /skill-gen
---

<!-- DRY: grillme 前段（発火〜他処理に進まない）は全コマンド共通。終了条件・永続メモリ・引数は固有 -->

# スキル生成入力収集

リポジトリ固有入力を集めて整理し、SKILL.md 生成は `skill-make` skill に、生成後の empirical 改善は `skill-tune` skill に委譲。改善の各反復で `grader` / `comparator` / `bench-analyzer` の3エージェントが評価を担う。

## grillme 強制起動（必須）

開始直後に grillme スキルで共通理解を固め、完了まで他処理に進まない。完了時は合意方針を1行サマリで確認する。

## 永続メモリ

- context: SessionStart で `<mem-context>` 自動注入
- search: `skill-gen pattern repository workflow`
- record: `{"event_type": "skill-gen", "content": "Skill: {name}. Iterations: {n}. Grader: {pass}/{total}. Comparator winner: {winner}. Bench: {summary}"}`

## skill 起動メカニズム

`skill-make` は `user-invocable: false` の skill で、description マッチにより Claude Code が Skill ツール経由で fork 実行する。`skill-tune` は `user-invocable: true` のため同様の自動発火に加えユーザーが直接呼び出すことも可能。本コマンドのステップ3 / ステップ4で「skill-make skill を起動」「skill-tune skill を起動」と明示することで発火する。

## ステップ1: 入力候補収集

```bash
source "${CLAUDE_PLUGIN_ROOT}/runtime/bluecore-helpers.sh"
collect_skill_create_inputs "${COMMITS:-200}"
bluecore_mem_search "<search query>" 3
```

## ステップ2: パターン検出

コミット規約（feat:/fix:/chore:）・ファイル同時変更パターン・繰り返しワークフロー・フォルダ構造/命名規則・テストパターンを抽出。

## ステップ3: skill-make 起動 → SKILL.md 生成

`skill-make` skill を起動。ステップ1〜2の入力を渡し、SKILL.md 下書きを作成。

## ステップ4: skill-tune 起動 + 評価エージェント連鎖

`skill-tune` skill を起動。empirical 評価と反復改善ループに入る。各反復内で以下の3エージェントを連鎖呼び出し:

1. **`bluecore:grader`**: 実行トランスクリプトと出力を期待値と照合し合否と根拠を整理
2. **`bluecore:comparator`**: 改善前後（または2候補）の出力をブラインド比較しどちらが課題達成度が高いか判定
3. **`bluecore:bench-analyzer`**: ベンチマーク結果と比較結果を要約し勝因・性能傾向を抽出

収束条件: 連続2回の反復で grader の新規不明瞭点ゼロ、または comparator の判定が連続2回同一勝者。

## ステップ5: インスティンクト生成（`--instincts` 時のみ）

learn 連携用インスティンクトもステップ1〜4と同じ流れで生成。

## 役割分担

| ステップ | 担当 | 種別 | 役割 |
|---|---|---|---|
| 入力収集 | skill-gen | command | リポジトリ分析・パターン検出 |
| SKILL.md 生成 | skill-make | skill | 下書き作成・構造化 |
| 品質改善 | skill-tune | skill | empirical 評価・反復改善 |
| 合否判定 | grader | agent | 期待値照合 |
| 盲検比較 | comparator | agent | 改善前後の優劣判定 |
| ベンチ要約 | bench-analyzer | agent | 勝因・性能傾向抽出 |

## 関連

- `/instinct import` — 生成インスティンクトをインポート
- `/dashboard` — 成長候補の可視化
- `/instinct evolve` — インスティンクトを skills/agents にクラスタリング

## 引数

- `--commits=<n>` — 直近コミット件数（既定: 200）
- `--output=<path>` — 生成先（既定: `skills/`）
- `--instincts` — インスティンクト生成も依頼
