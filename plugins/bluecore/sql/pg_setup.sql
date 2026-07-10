-- PostgreSQL セットアップスクリプト for mem チーム同期
-- 使用方法: psql -h <host> -U <user> -d <database> -f pg_setup.sql

-- 拡張機能の有効化（スーパーユーザー権限が必要な場合あり）
CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- pgvector は別途インストールが必要
CREATE EXTENSION IF NOT EXISTS vector;

-- memory_chunks テーブル
CREATE TABLE IF NOT EXISTS memory_chunks (
  id TEXT PRIMARY KEY,
  origin_user TEXT NOT NULL,
  session_id TEXT NOT NULL,
  project TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  content TEXT NOT NULL,
  tool_names TEXT,
  files_read TEXT,
  files_modified TEXT,
  user_prompt TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  created_at_epoch BIGINT NOT NULL,
  access_count INTEGER DEFAULT 0,
  last_accessed_epoch BIGINT,
  merged_generation INTEGER DEFAULT 0,
  merged_into TEXT REFERENCES memory_chunks(id),
  execution_status TEXT DEFAULT 'unknown',
  tool_error TEXT,
  ai_response_summary TEXT,
  tool_sequence TEXT DEFAULT '[]',
  synced_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(origin_user, session_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_origin ON memory_chunks(origin_user);
CREATE INDEX IF NOT EXISTS idx_chunks_session ON memory_chunks(session_id);
CREATE INDEX IF NOT EXISTS idx_chunks_project ON memory_chunks(project);
CREATE INDEX IF NOT EXISTS idx_chunks_epoch ON memory_chunks(created_at_epoch);
CREATE INDEX IF NOT EXISTS idx_chunks_content_trgm ON memory_chunks USING gin (content gin_trgm_ops);

-- RLS ポリシーは末尾の「RLS 実効化」セクションで一括設定する。

-- sessions テーブル
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  origin_user TEXT NOT NULL,
  session_id TEXT NOT NULL,
  project TEXT NOT NULL,
  started_at TIMESTAMPTZ DEFAULT NOW(),
  started_at_epoch BIGINT NOT NULL,
  chunk_count INTEGER DEFAULT 0,
  branch TEXT,
  commit_hash TEXT,
  uncommitted_count INTEGER DEFAULT 0,
  ended_at_epoch BIGINT,
  project_profile_id TEXT,
  synced_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(origin_user, session_id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_origin ON sessions(origin_user);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);

-- instincts テーブル
CREATE TABLE IF NOT EXISTS instincts (
  id TEXT PRIMARY KEY,
  origin_user TEXT NOT NULL,
  instinct_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  project_id TEXT,
  trigger_text TEXT,
  confidence REAL NOT NULL,
  domain TEXT,
  content TEXT NOT NULL,
  created_at_epoch BIGINT NOT NULL,
  updated_at_epoch BIGINT NOT NULL,
  observation_count INTEGER DEFAULT 0,
  confidence_reasons TEXT DEFAULT '[]',
  source_interaction_ids TEXT DEFAULT '[]',
  last_activated_epoch BIGINT,
  synced_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_instincts_origin ON instincts(origin_user);
CREATE INDEX IF NOT EXISTS idx_instincts_scope ON instincts(scope);
CREATE INDEX IF NOT EXISTS idx_instincts_project ON instincts(project_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_instincts_unique_key
  ON instincts(origin_user, instinct_id, scope, ((COALESCE(project_id, ''))));

-- adrs テーブル
CREATE TABLE IF NOT EXISTS adrs (
  id TEXT PRIMARY KEY,
  origin_user TEXT NOT NULL,
  project TEXT NOT NULL,
  adr_number INTEGER NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at_epoch BIGINT NOT NULL,
  updated_at_epoch BIGINT NOT NULL,
  synced_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(origin_user, project, adr_number)
);

CREATE INDEX IF NOT EXISTS idx_adrs_origin ON adrs(origin_user);
CREATE INDEX IF NOT EXISTS idx_adrs_project ON adrs(project);

-- event_logs テーブル
CREATE TABLE IF NOT EXISTS event_logs (
  id TEXT PRIMARY KEY,
  origin_user TEXT NOT NULL,
  event_type TEXT NOT NULL,
  project_id TEXT,
  content TEXT NOT NULL,
  created_at_epoch BIGINT NOT NULL,
  synced_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_events_origin ON event_logs(origin_user);
CREATE INDEX IF NOT EXISTS idx_events_type ON event_logs(event_type);
CREATE INDEX IF NOT EXISTS idx_events_epoch ON event_logs(created_at_epoch);
CREATE INDEX IF NOT EXISTS idx_events_project ON event_logs(project_id);

-- interaction_logs テーブル（スキル自動生成の原料）
CREATE TABLE IF NOT EXISTS interaction_logs (
  id TEXT PRIMARY KEY,
  origin_user TEXT NOT NULL,
  session_id TEXT NOT NULL,
  project TEXT NOT NULL,
  user_prompt_full TEXT NOT NULL,
  user_prompt_hash TEXT,
  ai_response_summary TEXT,
  ai_response_tool_plan TEXT,
  chunk_id TEXT REFERENCES memory_chunks(id),
  execution_outcome TEXT DEFAULT 'unknown',
  tool_error_count INTEGER DEFAULT 0,
  interaction_index INTEGER NOT NULL,
  created_at_epoch BIGINT NOT NULL,
  synced_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(origin_user, session_id, interaction_index)
);

CREATE INDEX IF NOT EXISTS idx_ilog_origin ON interaction_logs(origin_user);
CREATE INDEX IF NOT EXISTS idx_ilog_session ON interaction_logs(session_id);
CREATE INDEX IF NOT EXISTS idx_ilog_project ON interaction_logs(project);
CREATE INDEX IF NOT EXISTS idx_ilog_epoch ON interaction_logs(created_at_epoch);
CREATE INDEX IF NOT EXISTS idx_ilog_outcome ON interaction_logs(execution_outcome);
CREATE INDEX IF NOT EXISTS idx_ilog_hash ON interaction_logs(user_prompt_hash);

-- project_profiles テーブル（instinct の scope 判定に使用）
CREATE TABLE IF NOT EXISTS project_profiles (
  id TEXT PRIMARY KEY,
  origin_user TEXT NOT NULL,
  project TEXT NOT NULL,
  project_path TEXT,
  languages TEXT NOT NULL DEFAULT '[]',
  frameworks TEXT NOT NULL DEFAULT '[]',
  primary_language TEXT,
  test_command TEXT,
  build_command TEXT,
  scope_hint TEXT DEFAULT 'project',
  detected_at_epoch BIGINT NOT NULL,
  last_updated_epoch BIGINT NOT NULL,
  detection_confidence REAL DEFAULT 1.0,
  synced_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(origin_user, project)
);

CREATE INDEX IF NOT EXISTS idx_proj_prof_user ON project_profiles(origin_user);
CREATE INDEX IF NOT EXISTS idx_proj_prof_lang ON project_profiles(primary_language);

-- mem_item_runs テーブル（スキル・コマンド・エージェントの実行記録）
CREATE TABLE IF NOT EXISTS mem_item_runs (
  id TEXT PRIMARY KEY,
  origin_user TEXT NOT NULL,
  session_id TEXT NOT NULL,
  project TEXT NOT NULL,
  skill_name TEXT NOT NULL,
  skill_trigger TEXT,
  outcome TEXT DEFAULT 'unknown',
  tools_used TEXT DEFAULT '[]',
  files_modified_count INTEGER DEFAULT 0,
  duration_seconds INTEGER,
  interaction_log_id TEXT REFERENCES interaction_logs(id),
  created_at_epoch BIGINT NOT NULL,
  synced_at TIMESTAMPTZ DEFAULT NOW(),
  item_type TEXT NOT NULL DEFAULT 'skill'
);

CREATE INDEX IF NOT EXISTS idx_mir_skill ON mem_item_runs(skill_name);
CREATE INDEX IF NOT EXISTS idx_mir_project ON mem_item_runs(project);
CREATE INDEX IF NOT EXISTS idx_mir_epoch ON mem_item_runs(created_at_epoch);
CREATE INDEX IF NOT EXISTS idx_mir_outcome ON mem_item_runs(outcome, created_at_epoch);
CREATE INDEX IF NOT EXISTS idx_mir_item_type ON mem_item_runs(item_type);

-- session_digests テーブル（セッション要約の遡及/継続同期）
CREATE TABLE IF NOT EXISTS session_digests (
  id TEXT PRIMARY KEY,
  origin_user TEXT NOT NULL,
  session_id TEXT NOT NULL,
  project TEXT NOT NULL,
  summary TEXT NOT NULL,
  key_files TEXT DEFAULT '[]',
  key_decisions TEXT DEFAULT '[]',
  outcome TEXT DEFAULT 'unknown',
  harness TEXT DEFAULT 'unknown',
  source TEXT DEFAULT 'chunks',
  chunk_count INTEGER DEFAULT 0,
  started_at_epoch BIGINT NOT NULL,
  ended_at_epoch BIGINT,
  created_at_epoch BIGINT NOT NULL,
  synced_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(origin_user, session_id)
);

CREATE INDEX IF NOT EXISTS idx_digests_project_epoch ON session_digests(project, created_at_epoch);
CREATE INDEX IF NOT EXISTS idx_digests_origin ON session_digests(origin_user);

-- ベクトル検索テーブル（pgvector 拡張を有効にする必要がある）
CREATE TABLE IF NOT EXISTS memory_chunks_vec (
  chunk_id TEXT PRIMARY KEY REFERENCES memory_chunks(id),
  embedding vector(256)
);
CREATE INDEX IF NOT EXISTS idx_vec_embedding ON memory_chunks_vec USING ivfflat (embedding vector_l2_ops);

-- =====================================================================
-- RLS（行レベルセキュリティ）実効化 — 共有 DB での WRITE 所有モデル
-- =====================================================================
-- 方針:
--   * READ  : チーム全員が全行を参照可能（USING (true)）。team_search など
--             他ユーザーの経験横断検索を成立させるため read は開放する。
--   * WRITE : origin_user が current_setting('app.current_user') と一致する
--             自分の行のみ INSERT/UPDATE/DELETE 可能。
--   * app.current_user は PgDatabase が接続ごとに set_config(..., is_local=true)
--     で注入する git user.name。
--   * FORCE ROW LEVEL SECURITY: テーブル所有者にも RLS を適用して実効化する
--     （スーパーユーザーのみバイパス可能。app ロールには BYPASSRLS を与えない）。
--   * PUBLIC からは全権限を剥奪し、非所有者ロール bluecore_app にのみ DML を付与する。
--   * 空 identity ガード: current_setting が空/未設定なら NULLIF で NULL となり、
--     origin_user = NULL は成立しないため WRITE は全拒否される。
--
-- セキュリティ補足（memory_chunks_vec）:
--   目的は機密隔離ではなく WRITE 所有（他ユーザーの埋め込み行の上書き・詐称防止）。
--   READ はチーム共有プールの設計前提どおり全行に開放する（origin_user 列を
--   持たないため WRITE 所有者判定は memory_chunks 経由で行う）。
--
-- 監査: postgresql.conf で log_statement = 'mod' を設定し変更操作を記録する。
--       pg_dump / COPY による全件エクスポートは管理者ロールのみ許可する。

-- 非所有者アプリロール（全メンバーの同期クライアントが共有する単一 LOGIN 認証情報。
-- 各人の書き込み帰属は PG ロールでなくアプリ側 set_config('app.current_user', ...) の
-- RLS 判定で行うため、資格情報自体はチームで共有してよい設計）。
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bluecore_app') THEN
    CREATE ROLE bluecore_app LOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
END $$;

-- ポリシー是正は単一トランザクションで原子的に行う（DROP → CREATE を不可分に）
BEGIN;

-- 旧 owner ポリシー（current_user ベース）を撤去する
DROP POLICY IF EXISTS chunks_owner_policy ON memory_chunks;
DROP POLICY IF EXISTS digests_owner_policy ON session_digests;
DROP POLICY IF EXISTS vec_owner_policy ON memory_chunks_vec;

-- origin_user 列を持つ 9 テーブルへ統一ポリシーを付与
DO $$
DECLARE
  t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'memory_chunks', 'sessions', 'instincts', 'adrs', 'event_logs',
    'interaction_logs', 'project_profiles', 'mem_item_runs', 'session_digests'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('REVOKE ALL ON %I FROM PUBLIC', t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO bluecore_app', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_read', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_write', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR SELECT USING (true)', t || '_read', t);
    EXECUTE format(
      'CREATE POLICY %I ON %I FOR ALL '
      'USING (origin_user = NULLIF(current_setting(''app.current_user'', true), '''')) '
      'WITH CHECK (origin_user = NULLIF(current_setting(''app.current_user'', true), ''''))',
      t || '_write', t
    );
  END LOOP;
END $$;

-- memory_chunks_vec: origin_user 列が無いため memory_chunks 経由で WRITE 所有者判定
ALTER TABLE memory_chunks_vec ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_chunks_vec FORCE ROW LEVEL SECURITY;
REVOKE ALL ON memory_chunks_vec FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON memory_chunks_vec TO bluecore_app;
DROP POLICY IF EXISTS memory_chunks_vec_read ON memory_chunks_vec;
DROP POLICY IF EXISTS memory_chunks_vec_write ON memory_chunks_vec;
CREATE POLICY memory_chunks_vec_read ON memory_chunks_vec
  FOR SELECT USING (true);
CREATE POLICY memory_chunks_vec_write ON memory_chunks_vec
  FOR ALL
  USING (
    chunk_id IN (
      SELECT id FROM memory_chunks
      WHERE origin_user = NULLIF(current_setting('app.current_user', true), '')
    )
  )
  WITH CHECK (
    chunk_id IN (
      SELECT id FROM memory_chunks
      WHERE origin_user = NULLIF(current_setting('app.current_user', true), '')
    )
  );

COMMIT;

-- 完了メッセージ
DO $$
BEGIN
  RAISE NOTICE 'mem PostgreSQL setup completed successfully';
END $$;
