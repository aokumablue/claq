---
name: feat-dev
description: 新機能開発統括。発見→探索→loop-dev 反復実装（plan/generate/evaluate 最大2反復）→サマリー。専門エージェント連携で一気通貫。新機能実装・機能拡張・中規模リファクタリング時に使用。
command: /feat-dev
---

<!-- DRY: grillme 前段（発火条件〜他処理に進まない）は全コマンド共通。終了条件・永続メモリ・引数は固有 -->

# 機能開発フロー

新機能を発見から納品サマリーまで直線遂行。実装は loop-dev skill に委譲する。

## grillme 起動（条件付き）

要件が曖昧で複数の読み方が成立するときのみ、開始直後に grillme スキルで共通理解を固める。依頼が明確なら省略して着手する。起動した場合は完了まで他処理に進まず、完了時は合意方針を1行サマリで確認する。

## 永続メモリ

- 注入: SessionStart の `mem context` が `<bluecore-memory>` を自動投入（`status='active'` のみ）
- 参照: `bluecore_run bluecore.mem.cli search "..."`（`source .../runtime/bluecore-helpers.sh` 前提。クエリ例 `feat-dev workflow {feature}` / `phase blocker feature`）→ 本文が要る key だけ `bluecore_run bluecore.mem.cli show <key>`
- 記録: 再利用可能な学びだけ `bluecore_mem_learn` で登録する。基準は `../skills/learn/SKILL.md` の「記録する / しない」

## skill 起動メカニズム

`loop-dev` は `user-invocable: false` の skill。本文で「loop-dev skill を起動」と明示することで Skill ツール経由の fork 実行で発火する。

## ステップ1: 発見 + 探索

要求抽出・成功条件明確化（曖昧 → 利用側確認）後、Explore agent を起動し、既存構造・命名規約・類似実装・
影響範囲・依存・現行テスト実態を1回の調査で洗い出す（breadth は調査範囲に応じて `medium`〜`very thorough`
を選択）。対象領域が明確に独立し並列化する価値がある大規模調査に限り複数並列起動する。

探索結果は再取得しない（既取得情報の再探索・重複調査を避ける）。

## ステップ2: 残分岐確認

grillme を起動した場合はその結果、省略した場合は依頼時点の要件が前提。探索結果により**新たに発生した**分岐のみ確認する（推奨回答付き）。新規分岐がゼロなら本ステップは省略可。

## ステップ3: loop-dev 反復実装

`loop-dev` skill を起動（必須）。plan→generate→evaluate を最大 2 反復で収束させる。

入力:

- `task` = 合意済み要件
- `task_type` = `feature`
- `converge_extra` = ステップ1 の成功条件から導出

loop-dev から収束 or 停止報告を受領して次段階へ。

## ステップ4: 学びの記録（必須実行・記録は該当時のみ）

ステップ1の探索で「調べないと分からなかった」ことと、実装中に選んだ設計は、
次に同じ領域を触るときの探索コストをそのまま削る。サマリーの前にここで残す。

記録する:

| 見つけたもの | kind | title に書くこと |
|---|---|---|
| 探索に 2 手以上かかったエントリポイント・呼び出し関係 | `fact` | 事実そのもの（「hook は launcher.py 経由で起動する」） |
| 採用した設計と却下した案 | `decision` | 選択と理由（「FTS5 を使わず Python 側で採点する」） |
| 新機能を足すときの定型手順（登録先・テスト配置・生成物） | `howto` | 手順の目的 |
| 実装中に踏んだ環境・ビルド・テストの罠 | `pitfall` | 回避条件 |
| 既存コードから読み取った、明文化されていない規約 | `convention` | 守るべきルール |

記録しない:

- **機能仕様そのもの** — コードと README が正。二重管理すると必ず食い違う
- **リポジトリを読めば分かること** — ファイル一覧、公開 API、型定義
- **進捗・作業ログ** — 「ステップ3 まで完了」。SessionEnd の `handoff` が自動で残す
- **探索の過程** — 何を読んだかではなく、分かった結論だけを書く
- **そのセッション限りの事情** — 「今回はテストデータを手で用意した」

```bash
source "${CLAUDE_PLUGIN_ROOT}/runtime/bluecore-helpers.sh"
bluecore_mem_learn --kind fact --scope repo --domain <domain> \
  --title "<分かった結論を 1 行で>" \
  --body "<根拠と、次に同じ領域を触るときの入口>"
```

該当ゼロなら 1 件も記録しない（0 件は正しい結果）。
確信が持てないものは `--status pending` を付け、採否は `/instinct` のレビューに委ねる。
詳細基準は `../skills/learn/SKILL.md` の「記録する / しない」。

## ステップ5: サマリー

変更ファイル/追加テスト/残課題を一覧化し、loop-dev の箱形出力（Loop-Dev Result）を転記する。報告には loop-dev の箱形出力等の一次証跡の転記のみを用い、未確認事項を完了として報告しない:

```
### 変更ファイル
- path — 変更内容

### 追加テスト
- path:fn — カバー範囲

### 残課題
- ...

### 記録した知識
- key — title（ステップ4。記録が無ければ「なし」）
```

**段階飛ばし禁止**: 探索スキップ → 既存パターン無視 → 重複実装発生。

## 制約

- 既存拡張 > 新規作成
- テスト実行・緑必須（pytest / jest / go test 等）
- 後方互換フォールバック禁止 → 古コード削除

## 引数

- 位置 #1: `[機能説明]`（省略時: 直前の会話文脈から要件抽出）
