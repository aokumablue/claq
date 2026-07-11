# PostgreSQL RLS 受け入れ手順

`pg_setup.sql` が実効化する RLS（行レベルセキュリティ）は **READ 共有 + WRITE 所有**
モデル。目的は機密性（READ 隔離）ではなく完全性（書き込み帰属）——
他ユーザーの `origin_user` を詐称した INSERT や、他者行の UPDATE/DELETE を
防止することであり、`team_search` / ダッシュボード集計が前提とする
「全員が全行を READ できる」設計は維持される。

本書はモックでは検証できない実 PostgreSQL 上での適用・切替・受け入れ確認手順を示す。

## 1. 適用

管理者（テーブル所有者）権限を持つ接続で一度だけ実行する。`pg_setup.sql` は
`IF NOT EXISTS` / `DROP POLICY IF EXISTS` により冪等なので再実行しても安全。

```bash
psql "<admin_url>" -f pg_setup.sql
```

`<admin_url>` は既存のテーブル所有者ロールへの接続文字列（現行の
`sync.postgres_url` と同じもので良い）。

## 2. ロール切替運用（コード変更ではなく運用手順）

適用後、実際の同期クライアントの接続先を非所有者ロール `bluecore_app` へ
切り替える。これはコード変更ではなく設定・運用の変更のみで完結する。

1. PostgreSQL 管理者が `bluecore_app` にログインパスワードを設定する
   （`pg_setup.sql` は資格情報をスクリプトに埋め込まない設計のため、
   別途 `ALTER ROLE bluecore_app PASSWORD '...'` を管理者が実行する）。
2. 各メンバーの `settings.json` の `mem.sync.postgres_url` を、
   ロール部分だけ `bluecore_app` に差し替えた接続文字列に更新する
   （ホスト・ポート・DB 名は変更しない）。
3. パスワードは平文で `settings.json` に残さない。
   `Settings.load()`（`settings.json` 読み込み時）が自動で `<data_dir>/.pgpass` へ
   分離するため、パスワード付き URL を一度設定すれば以降は `.pgpass` から解決される
   （`bluecore.mem.settings.Settings.load` → `_migrate_postgres_url` / `pgpass_path()` 参照。
   `PgDatabase` は分離済みの `.pgpass` を `passfile` 接続パラメータで参照するだけで、
   分離処理自体は行わない）。
4. **無停止移行が成立する理由**: `pg_setup.sql` は対象10テーブル全てに
   `FORCE ROW LEVEL SECURITY` を適用しているため、切替前の所有者ロール接続でも
   （所有者は本来 RLS をバイパスできるところを FORCE が上書きするため）
   切替後の `bluecore_app` 接続と同じ WRITE 制約が即座に有効になる。
   よって「まず全メンバーが所有者接続のまま RLS の恩恵を受け、後日
   `bluecore_app` へ緩やかに切替える」という段階移行が可能。
5. 所有者ロール・`bluecore_app` のいずれにも **`BYPASSRLS` を付与しない**こと。
   `BYPASSRLS` を持つロールは `FORCE ROW LEVEL SECURITY` があっても RLS を
   バイパスできてしまい、本モデルの前提が崩れる。

## 3. 実 PostgreSQL 受け入れ検証（手動）

モックでは PostgreSQL 自体の RLS 強制ロジックを検証できないため、
適用後は実 DB に対して以下を手動確認する。

- [ ] `bluecore_app` で接続し `SELECT set_config('app.current_user', 'alice', true)`
      を実行した後、`origin_user='alice'` で INSERT すると成功する。
- [ ] 同じセッションで `origin_user='bob'`（他者になりすまし）を指定して
      INSERT すると、ポリシー違反（`new row violates row-level security policy`）
      で拒否される。
- [ ] `bob` として接続（`set_config('app.current_user', 'bob', true)`）し、
      `alice` が所有する既存行に対し `UPDATE ... SET content='x' WHERE id=<alice の行>`
      を実行しても影響行数 0（他者行を書き換えられない）。
- [ ] 同様に `bob` として `alice` の既存行に `DELETE ... WHERE id=<alice の行>`
      を実行しても影響行数 0（他者行を削除できない）。
      ※この UPDATE/DELETE 拒否は `_read`(USING true) と `_write`(FOR ALL) の
      共存下での可視性判定（SELECT ポリシーの USING が UPDATE/DELETE の既存行判定に
      AND 結合される PostgreSQL 意味論）に依存する非自明な部分のため、実 DB で必ず確認する。
- [ ] `set_config` を一切呼ばずに INSERT すると、
      `current_setting('app.current_user', true)` が NULL のため
      `origin_user = NULL` は決して真にならず拒否される（フェイルクローズ）。
- [ ] 全10テーブルに対する `SELECT` で、他ユーザーの `origin_user` を持つ行が
      通常に見える（`team_search` / `vec_search` / ダッシュボード集計が
      壊れないことの確認）。
- [ ] `memory_chunks_vec` も同様に、他者の `chunk_id` 行が READ できるが、
      他者の `chunk_id` への embedding の INSERT/UPDATE/DELETE は拒否される。
- [ ] テーブル所有者ロールで直接接続した場合も、`FORCE ROW LEVEL SECURITY`
      により `bluecore_app` と同じ WRITE 制約が働く（所有者だからといって
      RLS を素通りしない）。
- [ ] `git config user.name` が未設定（`origin_user=""`）のメンバーは
      同期の書き込みが全て拒否される（設計どおりの動作。同期したい場合は
      `git config --global user.name` の設定が必要である旨をエラーメッセージや
      オンボーディング手順で案内すること）。

## 4. 監視指標の推奨

- 同期成功率（`upsert_*` 系呼び出しの成功/失敗比率）
- INSERT/UPDATE/DELETE のポリシー違反エラー率
  （`row-level security policy` を含む DB エラーの発生頻度）
- ダッシュボード・`team_search` の空結果率（READ 開放が壊れていないかの間接指標）

## 5. ロールバック手順

RLS 適用後に問題が発覚した場合の切り戻し手順（優先度順）。

1. **接続先を所有者ロールへ戻す**: `settings.json` の
   `mem.sync.postgres_url` を元の所有者ロール接続文字列に戻す。
   ただし `FORCE ROW LEVEL SECURITY` は所有者接続にも適用されるため、
   これだけでは WRITE 制約は解除されない。
2. **FORCE を解除する**（一時的な緊急措置）:
   ```sql
   ALTER TABLE <table> NO FORCE ROW LEVEL SECURITY;
   ```
   対象10テーブルそれぞれに実行すると、所有者ロール接続に限り RLS が
   バイパスされる（`bluecore_app` など非所有者ロールには引き続き適用される）。
3. **一時的に全 READ/WRITE 開放ポリシーへ退避する**（さらに深刻な場合）:
   ```sql
   DROP POLICY IF EXISTS <table>_write ON <table>;
   CREATE POLICY <table>_write_open ON <table> FOR ALL USING (true) WITH CHECK (true);
   ```
   復旧後は当該ポリシーを `DROP` し、`pg_setup.sql` を再適用して
   本来の WRITE 所有ポリシーに戻すこと。
4. いずれの切り戻しも一時的な緊急対応であり、恒久適用しない
   （`BYPASSRLS` の付与や `ENABLE ROW LEVEL SECURITY` 自体の無効化は行わない）。
