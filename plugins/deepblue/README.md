# devgear

> AI-first development acceleration plugin for Claude Code.

---

## 🚀 Commands (10)

| コマンド | 用途 | 一言説明 |
|---|---|---|
| `/c-plan` | 実装前計画 | 要件言い換え→リスク評価→段階的計画。コード前にユーザー確認 |
| `/c-featdev` | 新機能開発 | 発見→探索→質問→設計→実装→レビュー の7段階一気通貫 |
| `/c-bugfix` | バグ修正 | 再現→原因分析→最小修正→回帰防止→レビュー の一気通貫 |
| `/c-refactor` | リファクタリング | プロンプトから自動推論（単純化/デッドコード掃除）。`--mode=simplify` / `--mode=clean` で明示指定も可 |
| `/c-review` | コードレビュー | セキュリティ・品質・保守性を差分またはパス指定でレビュー |
| `/c-harness` | 品質管理 | プロンプトから自動推論（棚卸し/遵守率）。`--scope=stocktake` / `--scope=comply` で明示指定も可 |
| `/c-skillgen` | スキル作成 | リポジトリ固有入力収集→SKILL.md 生成→チューニング委譲 |
| `/c-instinct` | インスティンクト管理 | プロンプトから自動推論（export/import/promote/prune/evolve）。明示サブコマンド指定も可 |
| `/c-dashboard` | 利用率可視化 | 個人(SQLite)とチーム(PostgreSQL)の使用率比較 HTML ダッシュボード |
| `/c-testmod` | テストコード自動生成 | デシジョンテーブル設計→承認→実装。言語非依存。差分 or 指定パス対応 |

---

## 🧭 Workflows

各ワークフローで使われる **コマンド(🔵)・エージェント(🟢)・スキル/フック(🟣)** をカラー別に表示。

---

### WF-1: 新機能開発

```mermaid
flowchart LR
  classDef cmd    fill:#2563eb,stroke:#1e40af,color:#fff,rx:6
  classDef agent  fill:#059669,stroke:#047857,color:#fff,rx:6
  classDef skill  fill:#7c3aed,stroke:#6d28d9,color:#fff,rx:4
  classDef auto   fill:#ea580c,stroke:#c2410c,color:#fff,rx:4

  U(["👤 新機能依頼"]) --> CP["/c-plan"]:::cmd
  CP --> CF["/c-featdev"]:::cmd

  subgraph featdev["⚙️ c-featdev 内部"]
    direction TB
    AE["🔍 a-explore"]:::agent --> AA["🏗️ a-arch"]:::agent
    AA --> AT["🧪 a-tdd"]:::agent
    AT --> AR["✅ a-review"]:::agent
    SS["s-search"]:::skill -.-> AE
    SG["s-grillme"]:::skill -.-> AA
    SA["s-adr"]:::skill -.-> AA
  end

  CF --> featdev
  featdev --> CR["/c-review"]:::cmd
  CR --> AS["a-secure"]:::agent
  AS --> SC["s-secure"]:::skill
  CR --> CH["/c-harness\n--scope=comply"]:::cmd
```

**トリガー**: 新機能実装・機能拡張
**期待効果**: 探索→設計→実装→セキュリティ検証が自動連鎖

---

### WF-2: バグ修正

```mermaid
flowchart LR
  classDef cmd    fill:#2563eb,stroke:#1e40af,color:#fff,rx:6
  classDef agent  fill:#059669,stroke:#047857,color:#fff,rx:6
  classDef skill  fill:#7c3aed,stroke:#6d28d9,color:#fff,rx:4

  U(["🐛 バグ報告"]) --> CB["/c-bugfix"]:::cmd

  subgraph bugfix["⚙️ c-bugfix 内部"]
    direction TB
    ST["s-tdd"]:::skill --> AT["🧪 a-tdd"]:::agent
  end

  CB --> bugfix
  bugfix --> CR["/c-review"]:::cmd
  CR --> AR["✅ a-review"]:::agent
```

**トリガー**: バグ修正・不具合対応
**期待効果**: 最小修正・回帰防止テスト自動生成

---

### WF-3: フルリファクタリング

```mermaid
flowchart LR
  classDef cmd    fill:#2563eb,stroke:#1e40af,color:#fff,rx:6
  classDef agent  fill:#059669,stroke:#047857,color:#fff,rx:6
  classDef skill  fill:#7c3aed,stroke:#6d28d9,color:#fff,rx:4

  U(["🔧 リファクタ依頼"]) --> CP["/c-plan"]:::cmd
  CP --> CREF["/c-refactor"]:::cmd

  subgraph refactor["⚙️ c-refactor 内部（全ステップ）"]
    direction LR
    SP["s-refprep"]:::skill --> RB["s-refrb"]:::skill
    RB --> RO["🎯 a-reforch"]:::agent
    RO --> AC["🧹 a-clean"]:::agent
    AC --> ASI["✨ a-simplify"]:::agent
    ASI --> AP["⚡ a-perf"]:::agent
    AP --> AR["✅ a-review"]:::agent
    AP --> AS["🛡️ a-secure"]:::agent
  end

  CREF --> refactor
```

**トリガー**: 5件以上のリファクタ・大規模コード整理
**期待効果**: clean→simplify→perf→review の安全な自動連鎖

---

### WF-4: コード単純化のみ

```mermaid
flowchart LR
  classDef cmd    fill:#2563eb,stroke:#1e40af,color:#fff,rx:6
  classDef agent  fill:#059669,stroke:#047857,color:#fff,rx:6

  U(["✨ 単純化依頼"]) --> CREF["/c-refactor\n--mode=simplify"]:::cmd
  CREF --> AS1["✨ a-simplify"]:::agent
  CREF --> AS2["✨ a-simplify"]:::agent
  CREF --> AS3["✨ a-simplify"]:::agent
  AS1 & AS2 & AS3 --> CR["/c-review"]:::cmd
```

**トリガー**: 可読性・保守性向上のみ、機能変更なし
**期待効果**: 最大4並列でファイルグループを同時単純化

---

### WF-5: デッドコード掃除

```mermaid
flowchart LR
  classDef cmd    fill:#2563eb,stroke:#1e40af,color:#fff,rx:6
  classDef agent  fill:#059669,stroke:#047857,color:#fff,rx:6

  U(["🗑️ クリーンアップ"]) --> CREF["/c-refactor\n--mode=clean"]:::cmd
  CREF --> AC["🧹 a-clean"]:::agent
  AC --> TEST(["✅ テスト検証\nファイル単位"])
```

**トリガー**: 未使用コード・依存関係の削除
**期待効果**: 削除ごとにテスト実行、失敗時は即リバート

---

### WF-6: スキル新規作成・改善

```mermaid
flowchart LR
  classDef cmd    fill:#2563eb,stroke:#1e40af,color:#fff,rx:6
  classDef agent  fill:#059669,stroke:#047857,color:#fff,rx:6
  classDef skill  fill:#7c3aed,stroke:#6d28d9,color:#fff,rx:4

  U(["🛠️ スキル作成依頼"]) --> CSG["/c-skillgen"]:::cmd

  subgraph skillgen["⚙️ c-skillgen 内部"]
    direction TB
    SM["s-skillmake"]:::skill --> ST["s-skilltune"]:::skill
    ST --> AG["📊 a-grader"]:::agent
    AG --> AC["⚖️ a-comparator"]:::agent
    AC --> AA["📈 a-analyzer"]:::agent
  end

  CSG --> skillgen
  skillgen --> CH["/c-harness\n--scope=stocktake"]:::cmd
```

**トリガー**: 新スキル作成・既存スキル改善
**期待効果**: 生成→eval→ベンチマーク分析→品質監査の自動連鎖

---

### WF-7: 品質管理サイクル（定期メンテ）

```mermaid
flowchart LR
  classDef cmd    fill:#2563eb,stroke:#1e40af,color:#fff,rx:6
  classDef agent  fill:#059669,stroke:#047857,color:#fff,rx:6

  U(["📊 定期メンテ"]) --> CD["/c-dashboard"]:::cmd
  CD --> CH1["/c-harness\n--scope=harness"]:::cmd
  CH1 --> AH["⚙️ a-harness"]:::agent
  AH --> CH2["/c-harness\n--scope=stocktake"]:::cmd
  CH2 --> CH3["/c-harness\n--scope=comply"]:::cmd
```

**トリガー**: 週次・月次の品質チェック
**期待効果**: ダッシュボード確認→ハーネス最適化→スキル棚卸し→遵守率の一連確認

---

### WF-8: 学習サイクル（自動バックグラウンド）

```mermaid
flowchart TD
  classDef cmd    fill:#2563eb,stroke:#1e40af,color:#fff,rx:6
  classDef agent  fill:#059669,stroke:#047857,color:#fff,rx:6
  classDef auto   fill:#ea580c,stroke:#c2410c,color:#fff,rx:4
  classDef skill  fill:#7c3aed,stroke:#6d28d9,color:#fff,rx:4

  SS(["🌅 SessionStart"]) --> SL["s-slim\n文脈圧縮注入"]:::skill
  SS --> MC(["📥 mem-context\n自動注入"]):::auto

  subgraph session["💻 セッション中"]
    direction LR
    SAD["s-adr\nアーキ決定記録"]:::skill
    SGU["s-guard\n破壊的操作防止"]:::skill
    AO["👁️ a-observer\n5分毎観測"]:::agent
  end

  SE(["🌙 SessionEnd"]) --> SLE["s-learn\n観測→インスティンクト"]:::skill
  SLE --> CI["/c-instinct\n昇格・管理"]:::cmd
```

**トリガー**: 自動（ユーザー操作不要）
**期待効果**: セッション知識が自動的にインスティンクトとして蓄積

---

### WF-9: セキュリティ重視の実装

```mermaid
flowchart LR
  classDef cmd    fill:#2563eb,stroke:#1e40af,color:#fff,rx:6
  classDef agent  fill:#059669,stroke:#047857,color:#fff,rx:6
  classDef skill  fill:#7c3aed,stroke:#6d28d9,color:#fff,rx:4

  U(["🔐 認証/決済/秘匿情報\n実装依頼"]) --> CP["/c-plan"]:::cmd
  CP --> CF["/c-featdev"]:::cmd
  CF --> CR["/c-review"]:::cmd

  subgraph review["⚙️ c-review 内部"]
    direction TB
    AR["✅ a-review\n品質・設計"]:::agent
    AS["🛡️ a-secure\nOWASP Top10"]:::agent
    AS --> SS["s-secure\nRLS/CSRF/Upload"]:::skill
  end

  CR --> review
```

**トリガー**: 認証・決済・シークレット・APIエンドポイント実装
**期待効果**: OWASP検出(a-secure) + 設計チェックリスト(s-secure)の二段構え

---

### WF-10: Gitワークフロー支援（自動アドバイス）

```mermaid
flowchart LR
  classDef auto   fill:#ea580c,stroke:#c2410c,color:#fff,rx:4
  classDef skill  fill:#7c3aed,stroke:#6d28d9,color:#fff,rx:4

  U(["👤 git commit / merge\n/ rebase / push"]) --> HK(["🔗 Bash PreToolUse\nフック発火"]):::auto
  HK --> SF["s-gitflow\nブランチ戦略/コミット規約\nマージvsリベース判断"]:::skill
  SF --> ADV(["💡 アドバイス/警告\n注入"]):::auto
  ADV --> GO(["✅ git 操作実行"])
```

**トリガー**: git commit / merge / rebase / push 実行時（自動）
**期待効果**: ユーザーが普通に git 操作するだけでベストプラクティスのアドバイスが自動注入

---

### WF-11: c-testmod — テストコード自動生成

```mermaid
flowchart TD
  classDef cmd    fill:#2563eb,stroke:#1e40af,color:#fff,rx:6
  classDef skill  fill:#7c3aed,stroke:#6d28d9,color:#fff,rx:4

  A(["/c-testmod [path]"]) --> B["s-grillme\n（設計方針確定）"]:::skill
  B --> C["スコープ確定\ngit diff HEAD / 引数パス"]
  C --> D["プロジェクト検出\nget_test_command()"]
  D --> E["ベースライン取得\nカバレッジ測定"]
  E --> F["デシジョンテーブル設計\n関数単位・ブランチ網羅"]
  F --> G{"ユーザー承認"}
  G -- 修正依頼 --> F
  G -- 承認 --> H["テスト実装\n言語慣習に従う"]
  H --> I["検証\ntest + coverage + lint"]
  I --> J{"Gate"}
  J -- PASS --> K(["要約レポート"])
  J -- BLOCKED --> L(["失敗内容を報告\n自動修正しない"])
```

**トリガー**: テストコードの新規追加・カバレッジ補完
**期待効果**: デシジョンテーブルによるブランチ網羅 → 承認後に自動実装

---

## 🏗️ Architecture

```mermaid
flowchart TB
  classDef cmd    fill:#2563eb,stroke:#1e40af,color:#fff,rx:6
  classDef agent  fill:#059669,stroke:#047857,color:#fff,rx:6
  classDef skill  fill:#7c3aed,stroke:#6d28d9,color:#fff,rx:4
  classDef store  fill:#374151,stroke:#1f2937,color:#fff,rx:4

  subgraph user["👤 User Layer"]
    CMD["Commands (10)"]:::cmd
  end

  subgraph internal["⚙️ Internal Layer"]
    direction LR
    AGT["Agents (15)"]:::agent
    SKL["Skills (14, all fork)"]:::skill
  end

  subgraph persistence["💾 Persistence"]
    DB[("~/.devgear/mem.db\nSQLite")]:::store
    PG[("PostgreSQL\nチーム共有")]:::store
  end

  CMD --> AGT
  CMD --> SKL
  AGT -.-> SKL
  AGT --> DB
  DB -.-> PG
```

**設計方針**: ユーザーは Commands のみ選択 → 内部で Agents / Skills が自動連鎖。
スキルは全て `context: fork`（内部委譲専用）に統一。
