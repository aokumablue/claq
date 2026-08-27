# ADR-0007: 知識カードの active 化権限は payload から剥奪するが、`promote` と既存 active カードは維持する

**日付**: 2026-08-20  **ステータス**: accepted

## コンテキスト

v0.9.34 時点のランタイム監査レポートの H-01 は、`mem learn`（generic CLI）
の呼び出し元が JSON payload に
`source: "human"` / `status: "active"` と書く、または helper の
`--status active` を指定するだけで、真正性検証なしに
`status='active'` の知識カードを作成でき、SessionStart context への
永続注入を自己承認できてしまう問題を指摘した。

同レポート §8.3 はこの是正として次の 5 項目を挙げていた:

1. `mem/cli.py:_handle_learn` から `--status` を削除し、指定時は
   `UsageError`（exit 2）にする。
2. `mem/knowledge_input.py:parse_knowledge_payload` は payload の
   `source`/`status` を authority として扱わず、常に
   `source="agent"`/`status="pending"` に固定する。
3. `runtime/bluecore-helpers.sh` 等から active/archived 指定・promotion を
   agent が実行できると示す説明を除去する。
4. host capability が未実装の間、`mem/cli.py:_handle_promote` を agent /
   generic CLI から使用不可にする（exit 2 で拒否するか非公開化する）。
5. provenance を遡及判定できない既存の全 `active` カードを、backup /
   report の上で `pending` へ quarantine する。

## 決定

**上記のうち項目 1〜3（payload/CLI から `source`/`status` の authority を
剥奪する）だけを採用する。項目 4（`promote` の無効化）と項目 5（既存
active カードの一括隔離）は採用しない。**

採用した範囲（実装は本ラウンドで完了）:

- `knowledge_input.parse_knowledge_payload()` は常に
  `source="agent"`/`status="pending"` を書き込み、payload がそれ以外の値を
  明示した場合は `KnowledgeInputError` にする。
- `mem/cli.py` の `learn` サブコマンドは `--status` オプションそのものを
  拒否する（`CommandError`）。`list`/`search` の絞り込み用 `--status` は
  維持する。
- `runtime/bluecore-helpers.sh` の `bluecore_mem_learn` から
  `--status`/`--source` の option parsing を削除し、常に
  `source=agent`/`status=pending` で登録される旨をコメントへ明記した。
- `skills/learn/SKILL.md` / `commands/instinct.md` から「agent が active
  化できる」という記述を削除し、昇格は `mem promote <key>`
  （人間が実行するコマンド）だけである旨に統一した。

`_handle_promote`（`mem promote <key>`）は変更していない。既存の
`active` カードのマイグレーションも行っていない。

## 検討した代替案

### 代替案 1: v0.9.34 時点のランタイム監査レポート §8.3 の 5 項目を全面採用する

- 長所: 「caller が active を自己承認できる」という脅威を最も広く塞げる。
  `promote` 自体も無効化すれば、host-native な承認 capability が実装
  されるまで active 注入を完全に停止できる。
- 短所:
  - `promote` は `CLAUDE.md` のデータモデル前提に明記された正規の人間承認
    経路である（「`pending` は人間が `/instinct promote` で昇格させる
    まで注入されない」）。これを無効化すると、agent 由来の知識を人間が
    レビューして有効化する既存ワークフロー全体が機能しなくなる —
    「機能を壊すことで脅威を消す」という過剰対応になる。
  - このセッション自身の SessionStart context には、稼働中の `active`
    カードが 11 件前後注入されていた（`ランタイムは venv も install.sh も
    持たない` 等）。これらを一括で `pending` へ落とすと、蓄積済みの
    有用な知識が次回以降のセッションへ注入されなくなる。ADR-0002 が
    定義する脅威モデル（同一 OS ユーザーの敵対的回避は非対象）の外側にある
    リスクへの対処として、稼働中データを破壊するコストが大きすぎる。
  - host-mediated で agent が偽造できない承認 capability は、Copilot・
    Claude Code のいずれでも実証されていない。実装できない前提を機能停止
    の条件にすると、`promote` は無期限に使えなくなる。
- 却下理由: 実害（caller が payload だけで active を偽装できる）に対して
  必要十分な範囲は「payload/CLI からの authority を剥奪すること」であり、
  それ以上の「promote 自体の無効化」「既存データの破棄」は ADR-0002 の
  脅威モデルの外側の対応であり、得られる安全性の増分に対してコストが
  不釣り合いに大きい。

### 代替案 2: 何もしない（H-01 を non-issue として ADR 化する）

- 長所: 実装コストがゼロ。
- 短所: 実際に payload だけで active card を作成できることを実行して
  確認済み（`{"source":"human","status":"active"}` を stdin に渡した
  `learn` が active card を作っていた）。これは ADR-0002 の脅威モデル
  （同一 OS ユーザーの敵対的回避は非対象）の外側で発生しうる実害
  （agent が処理した外部入力がこの経路を悪用する）であり、修正不要とは
  言えない。
- 却下理由: 実害が確認されている以上、payload の authority 剥奪という
  低コストな修正を見送る理由がない。

## 結果

### 肯定的

- `mem learn` の generic 経路から `source`/`status` を偽装して active 化する
  経路は塞がれた。JSON `{"source":"human","status":"active"}` や
  `learn --status active` は exit 1（`KnowledgeInputError`/`CommandError`）
  になり、DB に書き込まれないことを実行して確認した。
- `promote <key>` による人間承認ワークフローと、稼働中の既存 active
  カードは無傷で維持される。

### 否定的

- `promote` 自体は引き続き agent が実行可能な CLI コマンドであり、
  「人間が意図して `/instinct promote` を叩いた」ことを技術的に強制する
  手段は無い（運用上の慣習に依存する）。これはレポート §8.3 が指摘した
  残存リスクとして受容する。

**2026-08-27 追記（F-21 対応）**: この受容判断そのものは再評価のうえ維持する。
同一 UID から `mem.db` を直接更新できる以上、CLI をいくら固めても保証にはならず、
`promote` を潰すのは「機能を壊してリスクを消す」過剰対応である。一方で、
**コードと文書が「人間承認のみ」という保証を主張していた点は是正した** —
`mem/cli.py` は `learn --status active` を「有効化は promote による人間承認のみです」
という理由で拒否していたが、その人間承認を担保する仕組みは存在しない。
README / `skills/learn/SKILL.md` / `commands/instinct.md` / CLI の文言を
「運用上の想定であり技術的強制ではない」へ改め、昇格時に
key / scope / source / 直前 status / uid を監査ログへ記録するようにした。
これは境界ではなく事後追跡である。

### リスク

- 将来、host-native な承認 capability（agent が偽造不能な単発トークン等）
  が Copilot / Claude 双方で実証された場合、`promote` をその capability
  必須の経路へ差し替える余地を残しておく必要がある。現状の
  `_handle_promote` はこの将来変更を妨げない形（単純な status 更新の
  ラッパー）のままにしてある。
- 既存の active カードのうち、実際には自己承認されたもの（本 ADR 以前に
  payload 経由で active 化されたもの）が紛れている可能性は残る。本ラウンド
  では遡及検知・監査を行っていない。
