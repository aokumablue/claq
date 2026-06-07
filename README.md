# bluecore

Claude Code 向けの汎用プラグイン集です。エージェント、スキル、コマンド、フック、永続メモリをひとまとめに導入し、計画・実装・検証・レビューの流れを揃えます。

## これは何か

bluecore は、Claude Code の作業を「最初の計画からレビューまで」通して支えるプラグインです。
ユーザープロジェクトの言語ランタイムに依存せず、必要なときだけ個別のツールやコマンドを使います。

---

## 想定読者

本 README は **bluecore プラグインを Claude Code に導入する開発者** 向けです。

- Claude Code 自体の基本操作（プロンプト送信、ファイル編集の許可など）は前提とします。
- Claude Code がまだの場合は、まず公式の Claude Code を導入してから本プラグインを使ってください。
- 初心者は **WF-2（バグ修正）** から試すと最も簡単で、効果を体感しやすいです。

---

## 特長

| 項目 | 内容 |
| --- | --- |
| Agents | 計画、レビュー、TDD、セキュリティ、性能、探索などの専門サブエージェント |
| Skills | ワークフローや運用知識を段階的に案内 |
| Commands | 定番作業をすぐ呼び出し |
| Hooks | ツール実行の前後に自動チェックや記録を実行 |
| Memory | セッション履歴とチーム共有メモリを保持 |

---

## 用語

5つの要素を理解すれば bluecore の全フローが追えます。

| 用語 | 種別 | 起動方法 | 例 |
|---|---|---|---|
| **Command** | ユーザーが明示的に呼ぶ | `/<name> [args]` | `/plan`, `/review`, `/feat-dev` |
| **Agent** | 内部から委譲される専門家 | コマンド / skill から `Task` ツール経由で `subagent_type` 指定起動 | `reviewer`, `architect`, `tdd-writer` |
| **Skill** | 条件発火 or 委譲先の知識モジュール | description マッチで Claude Code が自動起動 / fork コンテキストで委譲 | `grillme`, `tdd`, `skill-make` |
| **Instinct** | 観測から学習したパターン | SessionStart で `<mem-context>` として自動注入 | `/instinct` で管理 |
| **Hook** | ツール実行時に自動発火するスクリプト | `hooks.json` 登録 → Claude Code が呼ぶ | PreToolUse, SessionStart, SessionEnd |

**ざっくりまとめると**: ユーザーは Command だけを覚えれば OK。Command が内部で必要な Agent / Skill を自動で連れてきます。Instinct と Hook はバックグラウンドで動く仕組みです。

---

## クイックスタート

### プラグインマーケットプレイス

```bash
claude plugin marketplace add aokumablue/bluecore
claude plugin install bluecore@bluecore
```

インストール後、まず試すなら:

```bash
/bugfix "ログイン時に 500 エラーが出る" src/auth/
```

---

## 設定

チーム同期を使う場合だけ、`~/.bluecore/settings.json` を以下のように設定します。

```json
{
  "mem": {
    "sync": {
      "enabled": true,
      "postgres_url": "postgresql://bluecore:PASSWORD@localhost:5432/bluecore_mem"
    }
  }
}
```

`mem.sync.enabled` が `false` の場合は、ローカル利用のみで動きます。

---

## 🚀 Commands (10)

各コマンドの詳細はリンク先 `.md` ファイル参照。

| コマンド | 用途 | 引数 | 一言説明 |
|---|---|---|---|
| [`/plan`](plugins/bluecore/commands/plan.md) | 実装前計画 | `[要件説明]` | 要件言い換え→リスク評価→段階的計画。コード前にユーザー確認 |
| [`/feat-dev`](plugins/bluecore/commands/feat-dev.md) | 新機能開発 | `[機能説明]` | 発見→探索→質問→設計→実装→レビュー の7段階一気通貫 |
| [`/bugfix`](plugins/bluecore/commands/bugfix.md) | バグ修正 | `[症状] [パス]` | 再現→原因分析→最小修正→回帰防止→レビュー の一気通貫 |
| [`/refactor`](plugins/bluecore/commands/refactor.md) | リファクタリング | `[パス] [--mode=simplify\|clean]` | clean→simplify→perf→review の安全な自動連鎖。`--mode` で部分実行 |
| [`/review`](plugins/bluecore/commands/review.md) | コードレビュー | `[パス]`（省略=差分） | reviewer + security-auditor 並列。**READ-ONLY 完全保証** |
| [`/harness`](plugins/bluecore/commands/harness.md) | 品質管理 | `[scope] [--audit-only] [--format=text\|json]` | スコア取得→harness-tuner で改善→再採点 |
| [`/skill-gen`](plugins/bluecore/commands/skill-gen.md) | スキル作成 | `[--commits=N] [--output=path] [--instincts]` | 入力収集→skill-make→skill-tune→grader/comparator/bench-analyzer 評価 |
| [`/instinct`](plugins/bluecore/commands/instinct.md) | インスティンクト管理 | `<export\|import\|promote\|prune\|evolve>` | 学習成果の昇格・削除・スキル化 |
| [`/dashboard`](plugins/bluecore/commands/dashboard.md) | 利用率可視化 | `[--days=N] [--output=path] [--format=html\|json]` | 個人(SQLite) と チーム(PostgreSQL) の使用率比較 HTML |
| [`/test-gen`](plugins/bluecore/commands/test-gen.md) | テストコード自動生成 | `[パス]`（省略=差分） | デシジョンテーブル設計→承認→実装。言語非依存 |

---

## 🧭 Workflows

bluecore が「どの場面でどう動くか」を 11 のワークフロー図で示します。各図には **コマンド・エージェント・スキル・自動処理** が登場します。

### 凡例

WF 図で繰り返し使う色・線・記号の意味は以下で統一しています。

| 色 | 種別 | 役割 |
|---|---|---|
| 🔵 **青** (#2563eb) | Command | ユーザーが明示的に呼ぶ |
| 🟢 **緑** (#059669) | Agent | 内部から委譲される専門家 |
| 🟣 **紫** (#7c3aed) | Skill / Hook | 条件発火する知識モジュール |
| 🟠 **橙** (#ea580c) | 自動処理 | システム側で自動発火（SessionStart 等） |
| ⬛ **灰** (#374151) | 永続化ストア | SQLite / PostgreSQL |

| 線種 | 意味 |
|---|---|
| 実線 `-->` | 同期的呼び出し・必須遷移（処理が完了するまで次に進まない） |
| 点線 `-.->`| 任意・条件付き・並行参照（補助的に動く） |
| `&` 連結 | 並列実行（複数を同時に走らせて結果をマージ） |
| subgraph 枠 | 1つのコマンド内部の処理単位（外から見ると1コマンド） |
| 菱形 `{...}` | 条件分岐（YES/NO 判定） |

**初心者向けの読み方**: まず 🔵 のコマンドだけ目で追えば「ユーザー操作の流れ」がわかります。🟢🟣 は気にしなくて OK。慣れてきたら subgraph の中を覗いてください。

---

### WF-1: 新機能開発

```mermaid
flowchart LR
  classDef cmd    fill:#2563eb,stroke:#1e40af,color:#fff,rx:6
  classDef agent  fill:#059669,stroke:#047857,color:#fff,rx:6
  classDef skill  fill:#7c3aed,stroke:#6d28d9,color:#fff,rx:4
  classDef auto   fill:#ea580c,stroke:#c2410c,color:#fff,rx:4

  U(["👤 新機能依頼"]) --> CP["/plan"]:::cmd
  CP --> CF["/feat-dev"]:::cmd

  subgraph featdev["⚙️ feat-dev 内部（7ステップ）"]
    direction TB
    SG1["grillme"]:::skill -.-> AE
    AE["🔍 explorer × 3<br/>並列探索"]:::agent --> SG2["grillme<br/>質問確定"]:::skill
    SG2 --> AA["🏗️ architect<br/>決定モード"]:::agent
    AA & APO["⚡ perf-optimizer"]:::agent --> AT["🧪 tdd-writer<br/>RED→GREEN"]:::agent
    AT --> AR["✅ reviewer"]:::agent
    AT --> ASEC["🛡️ security-auditor"]:::agent
    SS["search"]:::skill -.-> AE
    SA["adr"]:::skill -.-> AA
  end

  CF --> featdev
  featdev --> CR["/review"]:::cmd
  CR --> CH["/harness<br/>--scope=hooks"]:::cmd
```

**トリガー**: 新機能実装・機能拡張・中規模リファクタリング
**期待効果**: 探索→設計→実装→レビュー→セキュリティ検証が自動連鎖。段階飛ばし禁止のため既存パターン無視・重複実装が起きない

**実行例**:
```bash
/plan "ユーザー認証に 2FA を追加"
/feat-dev
```

**難度**: ★★★☆☆ (中)
**想定所要時間**: 探索 10分 / 設計 15分 / 実装 30分〜

---

### WF-2: バグ修正

```mermaid
flowchart LR
  classDef cmd    fill:#2563eb,stroke:#1e40af,color:#fff,rx:6
  classDef agent  fill:#059669,stroke:#047857,color:#fff,rx:6
  classDef skill  fill:#7c3aed,stroke:#6d28d9,color:#fff,rx:4

  U(["🐛 バグ報告"]) --> CB["/bugfix"]:::cmd

  subgraph bugfix["⚙️ bugfix 内部（5ステップ）"]
    direction TB
    SG["grillme"]:::skill --> REPRO["再現テスト確立"]
    REPRO --> ROOT["原因分析"]
    ROOT --> FIX["最小修正"]
    FIX --> ST["tdd"]:::skill
    ST --> AT["🧪 tdd-writer"]:::agent
    AT --> AR["✅ reviewer"]:::agent
    AT --> ASEC["🛡️ security-auditor"]:::agent
  end

  CB --> bugfix
```

**トリガー**: バグ修正・不具合対応
**期待効果**: 再現テストを先に作るので「再現できないバグ」を直したつもりが残らない。回帰防止テストも自動追加

**実行例**:
```bash
/bugfix "ログイン直後にダッシュボードが空になる" src/dashboard/
```

**難度**: ★★☆☆☆ (初心者向け)
**想定所要時間**: 再現 10分 / 修正 10分 / 検証 5分

---

### WF-3: フルリファクタリング

```mermaid
flowchart LR
  classDef cmd    fill:#2563eb,stroke:#1e40af,color:#fff,rx:6
  classDef agent  fill:#059669,stroke:#047857,color:#fff,rx:6
  classDef skill  fill:#7c3aed,stroke:#6d28d9,color:#fff,rx:4

  U(["🔧 リファクタ依頼"]) --> CP["/plan"]:::cmd
  CP --> CREF["/refactor"]:::cmd

  subgraph refactor["⚙️ refactor 内部（8ステップ）"]
    direction TB
    SG["grillme"]:::skill --> SP["refactor-prep"]:::skill
    SP --> RB["refactor-rollback"]:::skill
    RB --> RO["🎯 refactor-orchestrator"]:::agent
    RO --> AC["🧹 dead-code-cleaner"]:::agent
    AC --> ASI1["✨ simplifier #1"]:::agent
    AC --> ASI2["✨ simplifier #2"]:::agent
    AC --> ASI3["✨ simplifier #3"]:::agent
    ASI1 & ASI2 & ASI3 --> AP["⚡ perf-optimizer"]:::agent
    AP --> AR["✅ reviewer"]:::agent
    AP --> AS["🛡️ security-auditor"]:::agent
    AR & AS --> GATE{{"final gate<br/>CRITICAL/HIGH残→BLOCK"}}
  end

  CREF --> refactor
```

**トリガー**: 5件以上のリファクタ・大規模コード整理
**期待効果**: clean → simplify（並列）→ perf → review を安全に自動連鎖。各段階の失敗は即ファイル単位でリバート、CRITICAL/HIGH 残存時は最終 gate でブロック

**実行例**:
```bash
/plan "認証層のユーティリティ重複を整理"
/refactor src/auth/
```

**難度**: ★★★★☆ (上級)
**想定所要時間**: prep 5分 / clean 10分 / simplify 20分 / perf 15分 / review 10分

---

### WF-4: コード単純化のみ

```mermaid
flowchart LR
  classDef cmd    fill:#2563eb,stroke:#1e40af,color:#fff,rx:6
  classDef agent  fill:#059669,stroke:#047857,color:#fff,rx:6

  U(["✨ 単純化依頼"]) --> CREF["/refactor<br/>--mode=simplify"]:::cmd
  CREF --> AS1["✨ simplifier #1"]:::agent
  CREF --> AS2["✨ simplifier #2"]:::agent
  CREF --> AS3["✨ simplifier #3"]:::agent
  AS1 & AS2 & AS3 --> CR["/review"]:::cmd
```

**トリガー**: 可読性・保守性向上のみ、機能変更なし
**期待効果**: 最大並列でファイルグループを同時単純化。clean / perf はスキップ

**実行例**:
```bash
/refactor --mode=simplify src/api/
```

**難度**: ★★☆☆☆ (初心者向け)
**想定所要時間**: 10〜20分

---

### WF-5: デッドコード掃除

```mermaid
flowchart LR
  classDef cmd    fill:#2563eb,stroke:#1e40af,color:#fff,rx:6
  classDef agent  fill:#059669,stroke:#047857,color:#fff,rx:6

  U(["🗑️ クリーンアップ"]) --> CREF["/refactor<br/>--mode=clean"]:::cmd
  CREF --> AC["🧹 dead-code-cleaner"]:::agent
  AC --> TEST(["✅ テスト検証<br/>ファイル単位"])
  TEST -->|失敗| REVERT(["git checkout -- file"])
  TEST -->|成功| NEXT(["次ファイルへ"])
```

**トリガー**: 未使用コード・依存関係の削除
**期待効果**: 削除ごとにテスト実行、失敗時は即リバート。安全に dead code を排除

**実行例**:
```bash
/refactor --mode=clean
```

**難度**: ★☆☆☆☆ (最易)
**想定所要時間**: 5〜10分

---

### WF-6: スキル新規作成・改善

```mermaid
flowchart LR
  classDef cmd    fill:#2563eb,stroke:#1e40af,color:#fff,rx:6
  classDef agent  fill:#059669,stroke:#047857,color:#fff,rx:6
  classDef skill  fill:#7c3aed,stroke:#6d28d9,color:#fff,rx:4

  U(["🛠️ スキル作成依頼"]) --> CSG["/skill-gen"]:::cmd

  subgraph skillgen["⚙️ skill-gen 内部（5ステップ）"]
    direction TB
    COL["入力収集<br/>collect_skill_create_inputs"] --> PAT["パターン検出"]
    PAT --> SM["skill-make<br/>SKILL.md生成"]:::skill
    SM --> ST["skill-tune<br/>反復改善ループ"]:::skill

    subgraph loop["🔁 各反復"]
      direction LR
      AG["📊 grader<br/>期待値照合"]:::agent --> AC["⚖️ comparator<br/>盲検比較"]:::agent
      AC --> AA["📈 bench-analyzer<br/>勝因・性能傾向"]:::agent
    end

    ST --> loop
    loop -->|"未収束"| ST
  end

  CSG --> skillgen
```

**トリガー**: 新スキル作成・既存スキル改善
**期待効果**: 生成→eval→ベンチマーク分析が自動連鎖。連続2回で新規不明瞭点ゼロ、または同一勝者で収束判定

**実行例**:
```bash
/skill-gen --commits=200 --instincts
```

**難度**: ★★★★☆ (上級)
**想定所要時間**: 20〜40分（反復回数次第）

---

### WF-7: 品質管理サイクル（定期メンテ）

```mermaid
flowchart LR
  classDef cmd    fill:#2563eb,stroke:#1e40af,color:#fff,rx:6
  classDef agent  fill:#059669,stroke:#047857,color:#fff,rx:6

  U(["📊 定期メンテ"]) --> CD["/dashboard"]:::cmd
  CD --> CH["/harness"]:::cmd

  subgraph harness["⚙️ harness 内部（5ステップ）"]
    direction TB
    BASE["ベースライン取得<br/>harness_audit"] --> TOP["トップ3アクション特定"]
    TOP --> AHT["⚙️ harness-tuner<br/>設定最適化適用"]:::agent
    AHT --> RESC["改善後再採点"]
    RESC --> DIFF["差分要約"]
  end
```

**トリガー**: 週次・月次の品質チェック
**期待効果**: ダッシュボードで利用率を確認 → ハーネスで自動採点 → harness-tuner で改善案を適用 → 再採点で効果検証

**実行例**:
```bash
/dashboard --days=7
/harness repo
```

**難度**: ★★☆☆☆ (初心者向け)
**想定所要時間**: dashboard 1分 / harness 10分

---

### WF-8: 学習サイクル（自動バックグラウンド）

```mermaid
flowchart TD
  classDef cmd    fill:#2563eb,stroke:#1e40af,color:#fff,rx:6
  classDef agent  fill:#059669,stroke:#047857,color:#fff,rx:6
  classDef auto   fill:#ea580c,stroke:#c2410c,color:#fff,rx:4
  classDef skill  fill:#7c3aed,stroke:#6d28d9,color:#fff,rx:4

  SS(["🌅 SessionStart"]) --> SL["slim<br/>文脈圧縮注入"]:::skill
  SS --> MC(["📥 mem-context<br/>自動注入"]):::auto

  subgraph session["💻 セッション中"]
    direction LR
    SAD["adr<br/>アーキ決定記録"]:::skill
    SGU["guard<br/>破壊的操作防止"]:::skill
    AO["👁️ session-observer<br/>5分毎観測"]:::agent
  end

  SE(["🌙 SessionEnd"]) --> SLE["learn<br/>観測→インスティンクト"]:::skill
  SLE --> CI["/instinct<br/>昇格・管理"]:::cmd
```

**トリガー**: 自動（ユーザー操作不要）
**期待効果**: セッション中の知識が自動的にインスティンクトとして蓄積。ユーザーは `/instinct` でメンテだけ実行

**実行例**: ユーザー操作不要。週次で `/instinct promote && /instinct prune` を実行する程度

**難度**: ★☆☆☆☆ (自動)
**想定所要時間**: バックグラウンド常時

---

### WF-9: セキュリティ重視の実装

```mermaid
flowchart LR
  classDef cmd    fill:#2563eb,stroke:#1e40af,color:#fff,rx:6
  classDef agent  fill:#059669,stroke:#047857,color:#fff,rx:6
  classDef skill  fill:#7c3aed,stroke:#6d28d9,color:#fff,rx:4

  U(["🔐 認証/決済/秘匿情報<br/>実装依頼"]) --> CP["/plan"]:::cmd
  CP --> CF["/feat-dev"]:::cmd
  CF --> CR["/review"]:::cmd

  subgraph review["⚙️ review 内部（並列）"]
    direction LR
    AR["✅ reviewer<br/>品質・設計"]:::agent
    AS["🛡️ security-auditor<br/>OWASP Top10"]:::agent
    SS["secure<br/>RLS/CSRF/Upload"]:::skill -.-> AS
  end

  CR --> review
  review --> NEXT(["⚠️ CRITICAL/HIGH → commit BLOCK"])
```

**トリガー**: 認証・決済・シークレット・API エンドポイント実装
**期待効果**: OWASP 検出 (security-auditor) + 設計チェックリスト (secure) の二段構え。CRITICAL/HIGH があれば commit をブロック

**実行例**:
```bash
/plan "決済モジュールに 3D セキュア対応を追加"
/feat-dev
/review
```

**難度**: ★★★★☆ (上級)
**想定所要時間**: 計画 15分 / 実装 60分 / レビュー 20分

---

### WF-10: Gitワークフロー支援（自動アドバイス）

```mermaid
flowchart LR
  classDef auto   fill:#ea580c,stroke:#c2410c,color:#fff,rx:4
  classDef skill  fill:#7c3aed,stroke:#6d28d9,color:#fff,rx:4

  U(["👤 git commit / merge<br/>/ rebase / push"]) --> HK(["🔗 Bash PreToolUse<br/>フック発火"]):::auto
  HK --> SF["gitflow<br/>ブランチ戦略/コミット規約<br/>マージvsリベース判断"]:::skill
  SF --> ADV(["💡 アドバイス/警告<br/>注入"]):::auto
  ADV --> GO(["✅ git 操作実行"])
```

**トリガー**: `git commit` / `merge` / `rebase` / `push` 実行時（自動）
**期待効果**: 普通に git 操作するだけでベストプラクティスのアドバイスが自動注入

**実行例**: 通常の git 操作のみ。意識的に呼ぶ必要なし

**難度**: ★☆☆☆☆ (自動)
**想定所要時間**: 即時（< 1秒）

---

### WF-11: test-gen — テストコード自動生成

```mermaid
flowchart TD
  classDef cmd    fill:#2563eb,stroke:#1e40af,color:#fff,rx:6
  classDef skill  fill:#7c3aed,stroke:#6d28d9,color:#fff,rx:4

  A(["/test-gen [path]"]) --> B["grillme<br/>設計方針確定"]:::skill
  B --> C["スコープ確定<br/>git diff HEAD / 引数パス"]
  C --> D["プロジェクト検出<br/>get_test_command()"]
  D --> E["ベースライン取得<br/>カバレッジ測定"]
  E --> F["🎯 デシジョンテーブル設計<br/>関数単位・ブランチ網羅"]
  F --> G{"ユーザー承認"}
  G -- 修正依頼 --> F
  G -- 承認 --> H["テスト実装<br/>言語慣習に従う"]
  H --> I["検証<br/>test + coverage + lint"]
  I --> J{"Gate"}
  J -- PASS --> K(["✅ 要約レポート"])
  J -- BLOCKED --> L(["⚠️ 失敗内容を報告<br/>自動修正しない"])
```

**トリガー**: テストコードの新規追加・カバレッジ補完
**期待効果**: デシジョンテーブルによるブランチ網羅 → 承認後に自動実装。失敗テストは自動修正しないので仕様の不明点が露呈する

**実行例**:
```bash
/test-gen src/auth/login.py
```

**難度**: ★★★☆☆ (中)
**想定所要時間**: テーブル設計 10分 / 実装 15分 / 検証 5分

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
    AGT["Agents (15)<br/>reviewer / architect / tdd-writer ..."]:::agent
    SKL["Skills (14, all fork)<br/>grillme / tdd / secure ..."]:::skill
  end

  subgraph persistence["💾 Persistence"]
    DB[("~/.bluecore/mem.db<br/>SQLite")]:::store
    PG[("PostgreSQL<br/>チーム共有")]:::store
  end

  CMD --> AGT
  CMD --> SKL
  AGT -.-> SKL
  AGT --> DB
  DB -.-> PG
```

**設計方針**:
- ユーザーは **Commands のみ選択** すれば内部で Agents / Skills が自動連鎖
- スキルは全て `context: fork`（内部委譲専用、ユーザー直接起動不可）に統一
- 永続化は **SQLite（個人）→ PostgreSQL（チーム共有）** の 2 層

各コマンドの詳細仕様は [`plugins/bluecore/commands/`](plugins/bluecore/commands/) 配下を参照。
