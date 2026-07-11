---
name: instinct
description: インスティンクト エクスポート/インポート/昇格/削除/進化の統合コマンド。
command: /instinct
---

<!-- DRY: grillme 前段（発火〜他処理に進まない）は全コマンド共通。終了条件・永続メモリ・引数は固有 -->

# インスティンクト管理

学習済みインスティンクトの管理・昇格・削除・進化を扱う。状態確認は `/dashboard` に集約する。

## grillme 強制起動（必須）

開始直後に grillme スキルで共通理解を固め、完了まで他処理に進まない。サブコマンドが曖昧な場合は明示確定するまで実行しない。

## 永続メモリ

- context: SessionStart で `<mem-context>` 自動注入
- search: `instinct applied used`
- record (export/import/promote/prune): `{"event_type": "instinct-{action}", "content": "{summary}"}`
- record (evolve): `{"event_type": "instinct-evolve", "content": "Evolved: X skills, Y commands, Z agents from N instincts"}`

## ステップ1: サブコマンド確定

明示サブコマンドあり → そのまま実行。

明示サブコマンドなし → プロンプトキーワード照合で自動判定:
- 書き出/エクスポート → `export`
- 取り込/インポート → `import`
- 昇格/グローバル化 → `promote`
- 整理/削除 → `prune`
- 進化/生成/スキル化 → `evolve`

推論結果は実行前に1行表示。複数一致 / 該当なしの場合は grillme を再起動してユーザーに確定を促す。

## ステップ2: 実行

```bash
source "${CLAUDE_PLUGIN_ROOT}/runtime/bluecore-helpers.sh"
bluecore_run bluecore.skills.learn.cli <subcommand>
```

### export
全インスティンクトを YAML 形式で stdout に出力する。

### import `<file-or-url>`
ローカルファイルまたは URL から取り込む。2段階で実行する: ① `import <file-or-url> --dry-run` で件数・出所・差分を提示 → ② AskUserQuestion で承認を得てから `import <file-or-url> --force` で適用。承認なしの `--force` 実行は禁止。取込元による確認ゲートの非対称は設けない（汚染済みローカルファイルの無確認取込を防ぐ）。

### promote
昇格条件（2プロジェクト以上に出現・信頼度しきい値を満たす）の全候補を project → global へ自動昇格。

### prune
30日より古い未レビュー・未昇格の保留インスティンクトを削除。2段階で実行する: ① `prune --dry-run` で削除対象件数を提示 → ② AskUserQuestion で承認を得てから `prune` を本実行。CLI 側に確認機構はないため、承認前の本実行は禁止。

### evolve
蓄積インスティンクトからスキル・コマンド・エージェント候補を検出し `evolved/{skills,commands,agents}/` 配下にファイル生成。

- プロジェクトコンテキスト検出 → project/global インスティンクト読込（ID衝突時は project 優先）→ パターン分類 → 候補特定 → ファイル生成
- 進化ルール: Command=ユーザー明示呼び出し / Skill=自動発火パターン / Agent=複雑多段階処理
- 生成ファイル frontmatter: `name` / `description` / `evolved_from: [{instinct-ids}]`

## ステップ3: 記録

実行結果サマリーを永続メモリに記録（上記 record テンプレートに従う）。

## 引数

- 位置 #1: `<subcommand>` = `export | import <file-or-url> | promote | prune | evolve`
