---
name: harness
description: ハーネス監査→改善を一気通貫で実行。スコアカード取得→トップ3改善適用→改善後スコア報告。
command: /harness
---

<!-- DRY: grillme 前段（発火条件〜他処理に進まない）は全コマンド共通。終了条件・永続メモリ・引数は固有 -->

# ハーネス管理

## grillme 起動（条件付き）

要件が曖昧で複数の読み方が成立するときのみ、開始直後に grillme スキルで共通理解を固める。依頼が明確なら省略して着手する。起動した場合は完了まで他処理に進まず、完了時は合意方針を1行サマリで確認する。

## 永続メモリ

- 注入: SessionStart の `mem context` が `<bluecore-memory>` を自動投入（`status='active'` のみ）
- 参照: `bluecore_run bluecore.mem.cli search "..."`（`. "$HOME/.bluecore/env.sh"` 前提。クエリ例 `harness audit score` / `harness config optimization audit` (days: 90)）→ 本文が要る key だけ `bluecore_run bluecore.mem.cli show <key>`
- 記録: 再利用可能な学びだけ `bluecore_mem_learn` で登録する。基準は `../skills/learn/SKILL.md` の「記録する / しない」

## 使い方

```bash
/harness                    # ベースライン取得→トップ3提示までで停止（apply しない）
/harness --audit-only       # スコアカード出力のみ
/harness --apply            # トップ3を確認なしで harness-tuner に適用させる
/harness skills --format json
/harness --scope hooks --root /path/to/repo --target-kind repo
```

`scope` は位置引数でも `--scope` でも指定可。既定値は `repo`。

`--root` は監査対象ルートを明示するために必須（省略時は cwd を自動判定し、marketplace レイアウトの provider リポジトリ直下では consumer と誤判定されるため）。`--target-kind repo|consumer` も必須で、自動判定と食い違えば FAIL する（誤った root を監査したまま気付かず進むことを防ぐ）。

## ステップ1: ベースライン取得

```bash
. "$HOME/.bluecore/env.sh" || exit 127
bluecore_run bluecore.ci.harness_audit <scope> --format <text|json> --root <path> --target-kind <repo|consumer>
```

スコアカードを出力。`--audit-only` は /harness レベルの制御フラグであり `bluecore_run` へ渡さない。指定時はここで終了。`--root` / `--target-kind` はステップ4のベースライン比較まで同一値を保持する。

スコアリングはこのスクリプトのみを根拠とし、手動採点は行わない。

ルーブリック版: `2026-08-27` — 固定カテゴリ7個（各0〜10に正規化）:
ツール網羅性 / 文脈効率 / 品質ゲート / メモリ永続性 / 評価網羅性 / セキュリティガードレール / コスト効率

## ステップ2: トップ3アクション特定

`top_actions[]` から最も効果が高い3件を抽出。各アクションは `checks[]` の失敗チェックに紐付く正確なファイルパス付き。

**apply 承認境界**: `--audit-only` は監査のみで完全に分離済みだが、`--audit-only` を指定しない既定の呼び出しでも、ここから先（ステップ3の変更適用）は無条件委譲しない。`--apply` が明示されていなければ、トップ3アクション（提案内容・対象ファイル・想定効果）をここで提示してユーザーに提示するだけで停止する（ステップ3〜5は実行しない）。`--apply` が明示されている場合のみステップ3へ進む。人間の承認を経ずに harness-tuner へ変更適用を委譲しない。

## ステップ3: harness-tuner による改善適用（`--apply` 指定時のみ）

`bluecore:harness-tuner` を起動。ベースラインJSONとトップ3アクションを渡し、信頼性・コスト・スループット最適化を委譲。

harness-tuner は:
- ハーネス設定（hooks.json / settings.json 等）の最小限の変更を提案
- 元に戻せる設定変更のみを適用
- 適用前後の影響範囲を要約

## ステップ4: 改善後スコア（`--apply` 指定時のみ）

```bash
. "$HOME/.bluecore/env.sh" || exit 127
bluecore_run bluecore.ci.harness_audit <scope> --format <text|json> --root <path> --target-kind <repo|consumer>
```

ステップ1と同一の `--root` / `--target-kind` で再採点（異なる root・スケールのスコアを比較しない）。

## ステップ5: 差分要約（`--apply` 指定時のみ）

変更前後の差分・カテゴリ別スコア変化・harness-tuner が適用した変更内容を出力。

## 制約

- 測定可能効果を持つ小変更優先
- クロスプラットフォーム動作保持・脆弱シェルクォーティング導入禁止
- `checks[]` と `top_actions[]` に含まれる正確なファイルパスを残す
- スクリプト出力をそのまま使い、手動で再採点しない

## 出力仕様

1. ベースライン `overall_score` と `max_score`（`repo` では58）
2. カテゴリ別スコアと指摘
3. 失敗チェックと正確なファイルパス
4. 上位3件のアクション（`top_actions`）。`--apply` 未指定ならここで停止し、harness-tuner 適用内容は出さない
5. （`--apply` 指定時）harness-tuner 適用内容
6. （`--apply` 指定時）改善後スコアカード
7. （`--apply` 指定時）変更前後の差分サマリー

## 引数

- 位置 #1: `[scope]` = `repo|hooks|skills|commands|agents`（既定: `repo`）
- `--scope=<scope>`: 位置引数の別名（互換維持）
- `--format=text|json`（既定: `text`）
- `--root=<path>`: 監査対象ルート（必須。省略時の自動 cwd 判定は誤判定しうる）
- `--target-kind=repo|consumer`: 期待する判定モードの明示（必須。自動判定と食い違えば FAIL）
- `--audit-only`: ステップ1のみで終了（/harness レベルの制御フラグ。`bluecore_run` へ渡さない）
- `--apply`: トップ3アクションの harness-tuner への適用（ステップ3〜5）を実行する。未指定時はステップ2で停止する（`--audit-only` とは独立。`--audit-only` はステップ1のみで停止しトップ3提示すら行わない、より早い停止点）。これは**引数によるスコープ指定**であって承認待ちではない — `--apply` を付ければ止まらない
