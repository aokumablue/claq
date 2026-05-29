---
name: c-skillgen
description: リポジトリ固有入力収集→s-skillmakeにSKILL.md生成委譲→s-skilltune改善委譲。
command: /c-skillgen
---

# スキル生成入力収集

リポジトリ固有入力を集めて整理し、SKILL.md生成は s-skillmake に、生成後の改善は s-skilltune に委譲。

## s-grillme 強制起動（必須）

開始直後に s-grillme を必ず起動し、完了まで他の処理に進まない。

## 使い方

```bash
/c-skillgen                    # 現在のリポジトリ分析
/c-skillgen --commits 100      # 直近100件のコミット分析
/c-skillgen --output ./skills  # 生成先指定
/c-skillgen --instincts        # インスティンクト生成も依頼
```

## 手順

### ステップ1: 入力候補収集

```bash
source "${CLAUDE_PLUGIN_ROOT}/runtime/devgear-helpers.sh"
collect_skill_create_inputs "${COMMITS:-200}"
devgear_mem_search "<search query>" 3
```

### ステップ2: パターン検出

コミット規約（feat:/fix:/chore:）・ファイル同時変更パターン・繰り返しワークフロー・フォルダ構造/命名規則・テストパターン

### ステップ3: s-skillmake 呼び出し → SKILL.md 生成

### ステップ4: s-skilltune 呼び出し

empirical 評価と反復改善。収束（連続2回で新規不明瞭点ゼロ）を確認してから次へ進む。

### ステップ5: インスティンクト生成（--instincts 時）

s-learn 連携用インスティンクトも同流れで生成。

## 役割分担

| ステップ | 担当 | 役割 |
|---|---|---|
| 入力収集 | c-skillgen | リポジトリ分析・パターン検出 |
| SKILL.md 生成 | s-skillmake | 下書き作成・構造化 |
| 品質改善 | s-skilltune | empirical 評価・反復改善 |

## 関連

- `/c-instinct import` — 生成インスティンクトをインポート
- `/c-dashboard` — 成長候補の可視化
- `/c-instinct evolve` — インスティンクトを skills/agents にクラスタリング
