---
name: instinct
description: 蓄積された知識（knowledge）の一覧・閲覧・検索・昇格・アーカイブ・追加を行う統合コマンド。
command: /instinct
---

# 知識管理

`knowledge` テーブルに蓄積された **知識カード** の棚卸しを扱う。
知識モデル・kind の使い分け・記録基準は `../skills/learn/SKILL.md` が正。

中心となるワークフローは **昇格**: `learn` で入れた候補は常に `status='pending'` で登録され
（H-01 対応: `source`/`status` は caller が指定しても採用されず、常に `source=agent`/
`status=pending` に固定される）、SessionStart に注入されない。ここでレビューして
`promote` した知識だけが `status='active'` になり、以後の全セッションへ注入される。

`promote` が人間によって実行されたことは技術的に強制されていない（同一 UID から
`mem.db` を直接更新できるため。ADR-0007 が受容した残存リスク）。昇格は key・scope・
source・直前 status とともに監査ログへ記録される。

## 永続メモリ

- 注入: SessionStart の `mem context` が `<bluecore-memory>` を自動投入（`status='active'` のみ）
- 参照: `bluecore_run bluecore.mem.cli search "..."`（`. "$HOME/.bluecore/env.sh"` 前提。クエリ例 `{棚卸し対象の domain}` / `{key}`）→ 本文が要る key だけ `bluecore_run bluecore.mem.cli show <key>`
- 記録: 本コマンド自身の実行結果は記録しない（棚卸しはセッション限りの作業でありノイズになる）。記録基準は `../skills/learn/SKILL.md` の「記録する / しない」

## ステップ1: サブコマンド確定

明示サブコマンドあり → そのまま実行。

明示サブコマンドなし → プロンプトキーワード照合で自動判定:

- 一覧/棚卸し/確認 → `list`
- 中身/本文/詳細 → `show`
- 検索/探す → `search`
- 昇格/有効化/採用 → `promote`
- 削除/整理/忘れ/廃止 → `forget`
- 追加/登録/覚え → `learn`

推論結果は実行前に1行表示。複数一致 / 該当なしの場合は、候補サブコマンドと推奨を 1 行で提示してユーザーに確定を促す（grillme は起動しない — 確定に要るのは 1 問であり、徹底質問セッションではない）。

## ステップ2: 実行

```bash
. "$HOME/.bluecore/env.sh" || exit 127
bluecore_run bluecore.mem.cli <subcommand> [args...]
```

### list

知識カードの title を 1 件 1 行（`- [kind] title (key)`）で出す。`body` は出ない。
既定は「このリポジトリ + global」「`status='active'`」「20 件」。

```bash
. "$HOME/.bluecore/env.sh" || exit 127
bluecore_run bluecore.mem.cli list                          # 有効な知識の棚卸し
bluecore_run bluecore.mem.cli list --status pending         # 昇格待ちの候補（レビュー対象）
bluecore_run bluecore.mem.cli list --global --kind pitfall  # global の罠だけ
```

オプション: `--global` / `--repo`（排他）・`--status active|pending|archived`・
`--kind convention|decision|pitfall|howto|fact|preference`・`--limit N`・`--json`。

0 件なら 1 文字も出力されない（「見つかりません」も出ない）。

### show `<key>`

知識カード 1 件を全項目表示する。**`body` を読める唯一の口**。
key は「このリポジトリの repo スコープ → global スコープ」の順で解決する。

### search `<query>`

title / key / domain / body へのヒットを重み付けし、confidence と新しさで補正して上位順に出す。
出力は `list` と同じ 1 行形式で `body` は含まない（既定 5 件）。
本文が要るカードだけ key を `show` に渡す。

### promote `<key>`

`status` を `active` にする。以後 SessionStart で注入される。

**レビュー手順**: `list --status pending` で候補を出す → 気になる key を `show` で読む →
`../skills/learn/SKILL.md` の「記録する / しない」に照らして採否を決める → 採用分だけ `promote`。
昇格は注入枠を消費するので、一覧をそのまま全件昇格しない。

### forget `<key>` [`--superseded-by <new-key>`]

`status` を `archived` にする（行は消さない）。
新しい知識で置き換えた場合は `--superseded-by <new-key>` を付けて置換関係を残す。

対象: 前提が変わって成り立たなくなった知識・重複・title が曖昧で検索に引っかからない知識。

### learn

stdin の JSON から知識カードを 1 件登録する。ヘルパ経由が簡単:

```bash
. "$HOME/.bluecore/env.sh" || exit 127
bluecore_mem_learn --key sqlite-wal-sidecars --kind pitfall --scope repo \
  --title "WAL モードの接続は -wal/-shm を残す" \
  --domain sqlite --confidence 0.8 \
  --body "close 時に自動削除されない SQLite ビルドがあるため、DB 再作成後は明示的に unlink する。"
```

同じ `key` への `learn` は上書き更新になる（重複行は作られない）。
`--status` フラグは無い（H-01: 常に `status=pending` で登録され、JSON で
`status`/`source` を明示しても採用されず usage error になる）。

## ステップ3: 結果報告

実行したサブコマンドと対象 key を 1 行で報告する。`promote` / `forget` は
「昇格/アーカイブした key」と「見送った key + 理由」を分けて提示する。

## 引数

- 位置 #1: `<subcommand>` = `list | show <key> | search "<query>" | promote <key> | forget <key> | learn`
- 位置 #2 以降: サブコマンドの引数・オプション（上記各節を参照）
