# bluecore プラグイン未解決事項

## 判定

現行 v0.9.30 で再検証した結果、未解決事項は **HIGH 3件、LOW 1件**。本書は解消済み・対象外化した項目を除き、残存している内容だけを記載する。

- 対象報告: `docs/reports/PLUGIN_RUNTIME_AUDIT_2026-08-15.md`
- 再検証報告: `docs/reports/PLUGIN_RUNTIME_AUDIT_REVALIDATION_2026-08-17.md`
- リポジトリ版とインストール版: tracked 88ファイルに欠落・内容差分なし
- 実行環境: macOS、Python 3.14.6、Copilot CLI 1.0.80
- コード、設定、agent 定義は修正していない
- テストコード・coverage はユーザー指定により対象外

## 優先順位

| ID | 重大度 | 残存リスク |
|---|---|---|
| F-04 | HIGH | SessionStart の memory context が EOF 待ちで無期限停止 |
| F-07 | HIGH | 未承認 knowledge の context 注入と wrapper 境界破壊 |
| F-09 | HIGH | read-only agent が Bash 経由で書込み可能 |
| F-18 | LOW・条件付き | background child の失敗が exit 0 に隠れる |

## F-04: memory context の stdin 無期限待機

- 対象: `plugins/bluecore/src/bluecore/mem/cli.py:175-179`
- 原因: context 入力経路が `sys.stdin.read()` で EOF まで待つ。

### 再現方法

1. `bluecore.mem.cli context` を stdin pipe 付きで起動する。
2. `{}` を write/flush する。
3. pipe を close せず3秒以上待つ。

### 実測結果

```text
約3.2秒後も poll=None
```

プロセスは EOF まで終了しない。入力を閉じると処理は継続する。

### 影響

SessionStart の memory context が、入力元の pipe close 忘れや host 側の部分送信によって host timeout まで占有される。セッション開始の遅延、または hook 全体の失敗につながる。

### 修正案

- `hook_common` の bounded reader と共通の deadline を使う。
- 初回入力待ち、部分 JSON、全体 timeout を別々に扱う。
- timeout 時は構造化された警告を返し、無期限待機と区別できるようにする。

## F-07: knowledge の既定 active と未エスケープ注入

- 対象: `plugins/bluecore/src/bluecore/mem/knowledge_input.py:250-253`
- 原因:
  - `status` 未指定時の既定値が `active`。
  - title/body の `<bluecore-memory>` 終端相当文字列が escape されない。

### 再現方法

1. 隔離 DB に status 未指定の knowledge payload を登録する。
2. title または body に `</bluecore-memory>` を含める。
3. SessionStart の memory context を生成する。

### 実測結果

- payload は `active` として保存される。
- `</bluecore-memory>` が SessionStart 出力に残る。

### 影響

- 未承認の agent/user 由来データが次回セッションの context 注入対象になる。
- XML-like wrapper の境界を壊し、後続モデルに制御文として解釈される余地がある。

明示的な `learn` 操作だけがこの変換関数を使い、入力内容を完全に信頼する運用なら直ちに exploit とは限らない。ただし observer/hook 等から同じ関数を再利用する場合、安全側の既定値ではない。

### 修正案

- 外部入力、observer、hook は `status_override="pending"` を必須にする。
- `active` は承認済み経路だけが選択できるよう分離する。
- context 注入時に wrapper 内の特殊文字を escape する。
- source / scope / status を context 注入前に監査可能な形で記録する。

## F-09: review の READ-ONLY が技術的に強制されない

- 対象:
  - `plugins/bluecore/agents/reviewer.md:4`
  - `plugins/bluecore/agents/security-auditor.md:4`
  - `plugins/bluecore/commands/review.md:27-30`
- 原因: frontmatter が `Read, Grep, Glob, Bash` を許可する。本文の READ-ONLY 指示だけでは Bash の書込み能力を制限できない。

### 再現条件

reviewer または security-auditor に read-only を指定した上で、Bash から次のような操作を誘導する。

```bash
printf x > /tmp/reviewer-probe
git apply change.patch
git update-index --add path
```

今回の再検証では安全のため破壊的な操作を実行していない。権限定義上、redirect、削除、`git apply`、git index/worktree 操作が可能である。

### 影響

「レビュー中はファイルを変更しない」という契約がプロンプト依存になる。レビュー対象の改変、意図しない commit/index 変更、レビュー結果と実際の差分の不一致が起こり得る。

### 修正案

- read-only agent から Bash を除外する。
- Bash が必要なら、許可コマンド、作業ディレクトリ、redirect、削除、git write を制限する wrapper を使う。
- review 開始前後の worktree/index 差分を比較し、変更があればレビューを失敗扱いにする。

## F-18: launcher `--bg` の fail-open

- 対象: `plugins/bluecore/src/bluecore/launcher.py:162-169`
- 原因: detached child の起動失敗・対象 module の失敗に関係なく、親 launcher が exit 0 で返る。

### 再現方法

```bash
PYTHONPATH=plugins/bluecore/src \
python3 plugins/bluecore/src/bluecore/launcher.py \
  --bg nonexistent.module
```

### 実測結果

親 process は exit 0。child の import/実行失敗は呼び出し元へ伝播しない。

### 影響

SessionStart、SessionEnd 等で background 処理が実行されたと誤認される。ログやメモリ更新が失敗しても、呼び出し側は成功と判断する。

### 条件付きとした理由

watchdog は追加されており、短時間の hang は軽減されている。また、background 処理を best-effort とする host 契約なら exit 0 自体が許容される可能性がある。しかし、現行 launcher には「起動受付成功」と「child の処理完了・成功」を区別する契約がない。

### 修正案

- child の PID、起動成功、終了コードを記録する。
- 起動直後の import/exec failure を親へ伝播する。
- 呼び出し元が非同期受付成功と処理完了を区別できる structured status を返す。
- best-effort 仕様なら、失敗を無視する対象とログ形式を明文化する。

## 最終結論

優先して扱うべき残存事項は F-04、F-07、F-09。これらはそれぞれ可用性、context 注入境界、read-only 安全契約に関わる。F-18 は host 契約次第だが、非同期処理の成否を隠すため、運用上の誤認を防ぐ明示的な状態契約が必要である。
