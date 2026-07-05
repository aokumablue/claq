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

-- RLS: 自ユーザーのチャンクのみアクセス可能にする
ALTER TABLE memory_chunks ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'memory_chunks' AND policyname = 'chunks_owner_policy'
  ) THEN
    EXECUTE $policy$
      CREATE POLICY chunks_owner_policy ON memory_chunks
        USING (origin_user = current_user)
    $policy$;
  END IF;
END $$;

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

-- RLS: 自ユーザーの digest のみアクセス可能にする
ALTER TABLE session_digests ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'session_digests' AND policyname = 'digests_owner_policy'
  ) THEN
    EXECUTE $policy$
      CREATE POLICY digests_owner_policy ON session_digests
        USING (origin_user = current_user)
    $policy$;
  END IF;
END $$;

-- ベクトル検索テーブル（pgvector 拡張を有効にする必要がある）
-- セキュリティ: 埋め込み反転攻撃（Vec2Text）対策として行レベルセキュリティ（RLS）を有効化する。
-- 埋め込みベクトルから元テキストの相当部分が復元可能なため、原文と同等の機密扱いとする。
CREATE TABLE IF NOT EXISTS memory_chunks_vec (
  chunk_id TEXT PRIMARY KEY REFERENCES memory_chunks(id),
  embedding vector(256)
);
CREATE INDEX IF NOT EXISTS idx_vec_embedding ON memory_chunks_vec USING ivfflat (embedding vector_l2_ops);

-- RLS: 自ユーザーが書き込んだチャンクのベクトルのみ参照可能にする
ALTER TABLE memory_chunks_vec ENABLE ROW LEVEL SECURITY;

-- 既存ポリシーの重複定義を防ぐ
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'memory_chunks_vec' AND policyname = 'vec_owner_policy'
  ) THEN
    EXECUTE $policy$
      CREATE POLICY vec_owner_policy ON memory_chunks_vec
        USING (
          chunk_id IN (
            SELECT id FROM memory_chunks WHERE origin_user = current_user
          )
        )
    $policy$;
  END IF;
END $$;

-- スーパーユーザーは RLS をバイパスできるため、通常の app ロールには BYPASSRLS を与えない。
-- pg_dump / COPY による全件エクスポートは管理者ロールのみ許可する。
-- 監査: log_statement = 'mod' を postgresql.conf で設定し、変更操作を記録する。
-- PUBLIC からアクセスを剥奪して RLS を実効化する（<app_role> は実際のロール名に差し替えること）
REVOKE ALL ON memory_chunks_vec FROM PUBLIC;
-- GRANT SELECT ON memory_chunks_vec TO <app_role>;

-- 完了メッセージ
DO $$
BEGIN
  RAISE NOTICE 'mem PostgreSQL setup completed successfully';
END $$;
