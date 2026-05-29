---
name: skill-gen
description: リポジトリ固有入力収集→skill-makeにSKILL.md生成委譲→skill-tune改善委譲。
command: /skill-gen
---

# スキル生成入力収集

リポジトリ固有入力を集めて整理し、SKILL.md生成は skill-make に、生成後の改善は skill-tune に委譲。

## grillme 強制起動（必須）

開始直後に grillme を必ず起動し、完了まで他の処理に進まない。

## 使い方

```bash
/skill-gen                    # 現在のリポジトリ分析
/skill-gen --commits 100      # 直近100件のコミット分析
/skill-gen --output ./skills  # 生成先指定
/skill-gen --instincts        # インスティンクト生成も依頼
```

## 手順

### ステップ1: 入力候補収集

```bash
source "${CLAUDE_PLUGIN_ROOT}/runtime/deepblue-helpers.sh"
collect_skill_create_inputs "${COMMITS:-200}"
deepblue_mem_search "<search query>" 3
```

### ステップ2: パターン検出

コミット規約（feat:/fix:/chore:）・ファイル同時変更パターン・繰り返しワークフロー・フォルダ構造/命名規則・テストパターン

### ステップ3: skill-make 呼び出し → SKILL.md 生成

### ステップ4: skill-tune 呼び出し

empirical 評価と反復改善。収束（連続2回で新規不明瞭点ゼロ）を確認してから次へ進む。

### ステップ5: インスティンクト生成（--instincts 時）

learn 連携用インスティンクトも同流れで生成。

## 役割分担

| ステップ | 担当 | 役割 |
|---|---|---|
| 入力収集 | skill-gen | リポジトリ分析・パターン検出 |
| SKILL.md 生成 | skill-make | 下書き作成・構造化 |
| 品質改善 | skill-tune | empirical 評価・反復改善 |

## 関連

- `/instinct import` — 生成インスティンクトをインポート
- `/dashboard` — 成長候補の可視化
- `/instinct evolve` — インスティンクトを skills/agents にクラスタリング
