---
name: skill-make
description: 新スキル生成/eval実行/ベンチマーク分析/説明文最適化。/skill-genからの委譲先。
context: fork
user-invocable: false
---

# スキル生成・改善

`/skill-gen` は入力収集フロントエンド、ここが生成本体。

## 全体の流れ

1. スキルの目的を決める
2. SKILL.md 下書きを書く
3. テストプロンプト2〜3個作成
4. eval スクリプト実行→結果確認
5. 定性的・定量的に確認
6. フィードバックをもとに書き直す
7. 十分になるまで繰り返す
8. テスト数を増やしてスケール確認

## スキル生成

### ヒアリング内容

1. Claudeに何をさせたいか
2. いつトリガーされるべきか
3. 期待する出力形式
4. テストケースが必要か

### SKILL.md 基本構造

```text
skill-name/
├── SKILL.md (必須) — YAML frontmatter + Markdown 指示文
└── Bundled Resources (任意)
    ├── src/bluecore/skills/ — 決定的・反復処理用 Python モジュール
    ├── references/ — 必要に応じて読む文書
    └── assets/ — テンプレートや画像
```

### 書き方のポイント

- `description`: いつトリガーされるか・何をするか。Claudeはスキルを使い渋るのでやや強めに書く
- SKILL.md は 500 行未満
- 長くなるなら `references/` に分割
- 命令形を基本にし、なぜその指示が大事かを説明する

### テストケース（evals/evals.json）

```json
{"skill_name": "example-skill", "evals": [{"id": 1, "prompt": "...", "expected_output": "...", "files": []}]}
```

完全スキーマ: `references/schemas.md`

## テスト実行と評価

詳細: `references/eval-workflow.md`

1. スキルあり/ベースラインの2サブエージェントを同時起動
2. 定量的アサーションを下書き
3. `timing.json` に即時保存
4. grading → ベンチマーク集約 → 分析 → viewer 表示

## スキル改善

詳細: `references/improvement-guide.md`

- フィードバックを一般化（その例だけに最適化しない）
- プロンプトを軽く保つ（効いていない説明は削る）
- なぜを説明する（ALWAYS/NEVER の連発は黄色信号）
- テストケース間の重複を探す

## 参照ファイル

- `references/eval-workflow.md` — eval 実行・採点・viewer
- `references/improvement-guide.md` — 改善ループ・盲検比較・説明文最適化
- `references/schemas.md` — JSON 構造定義
- `bluecore:grader` / `bluecore:comparator` / `bluecore:bench-analyzer`

## 永続メモリ

search: `skill eval benchmark {skill_category}` / `skill improve iteration {skill_name}`
record: `{"event_type": "skill-eval", "content": "Evaluated {skill_name}: pass_rate {rate}%, iterations {n}"}`
