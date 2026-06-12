"""mem サブシステムの SQLite スキーマ定義（database.py から分離）。"""

from __future__ import annotations

_SCHEMA_SQL = """\
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS memory_chunks (
  id TEXT PRIMARY KEY,
  origin_user TEXT NOT NULL DEFAULT '',
  session_id TEXT NOT NULL,
  project TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  content TEXT NOT NULL,
  tool_names TEXT,
  files_read TEXT,
  files_modified TEXT,
  user_prompt TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  created_at_epoch INTEGER NOT NULL,
  access_count INTEGER DEFAULT 0,
  last_accessed_epoch INTEGER,
  merged_generation INTEGER DEFAULT 0,
  merged_into TEXT REFERENCES memory_chunks(id),
  execution_status TEXT DEFAULT 'unknown',
  tool_error TEXT,
  ai_response_summary TEXT,
  tool_sequence TEXT DEFAULT '[]',
  synced_at TEXT,
  UNIQUE(session_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_session ON memory_chunks(session_id);
CREATE INDEX IF NOT EXISTS idx_chunks_project ON memory_chunks(project);
CREATE INDEX IF NOT EXISTS idx_chunks_epoch ON memory_chunks(created_at_epoch);
CREATE INDEX IF NOT EXISTS idx_chunks_origin ON memory_chunks(origin_user);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  origin_user TEXT NOT NULL DEFAULT '',
  session_id TEXT NOT NULL,
  project TEXT NOT NULL,
  started_at TEXT DEFAULT (datetime('now')),
  started_at_epoch INTEGER NOT NULL,
  chunk_count INTEGER DEFAULT 0,
  branch TEXT,
  commit_hash TEXT,
  uncommitted_count INTEGER DEFAULT 0,
  ended_at_epoch INTEGER,
  project_profile_id TEXT,
  synced_at TEXT,
  UNIQUE(session_id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_origin ON sessions(origin_user);

-- インスティンクト
CREATE TABLE IF NOT EXISTS instincts (
  id TEXT PRIMARY KEY,
  origin_user TEXT NOT NULL DEFAULT '',
  instinct_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  project_id TEXT,
  trigger_text TEXT,
  confidence REAL NOT NULL,
  domain TEXT,
  content TEXT NOT NULL,
  created_at_epoch INTEGER NOT NULL,
  updated_at_epoch INTEGER NOT NULL,
  observation_count INTEGER DEFAULT 0,
  confidence_reasons TEXT DEFAULT '[]',
  source_interaction_ids TEXT DEFAULT '[]',
  last_activated_epoch INTEGER,
  synced_at TEXT,
  UNIQUE(origin_user, instinct_id, scope, project_id)
);

CREATE INDEX IF NOT EXISTS idx_instincts_user ON instincts(origin_user);
CREATE INDEX IF NOT EXISTS idx_instincts_scope ON instincts(scope);
CREATE INDEX IF NOT EXISTS idx_instincts_project ON instincts(project_id);

-- ADR
CREATE TABLE IF NOT EXISTS adrs (
  id TEXT PRIMARY KEY,
  origin_user TEXT NOT NULL DEFAULT '',
  project TEXT NOT NULL,
  adr_number INTEGER NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at_epoch INTEGER NOT NULL,
  updated_at_epoch INTEGER NOT NULL,
  synced_at TEXT,
  UNIQUE(origin_user, project, adr_number)
);

CREATE INDEX IF NOT EXISTS idx_adrs_user ON adrs(origin_user);
CREATE INDEX IF NOT EXISTS idx_adrs_project ON adrs(project);

-- 汎用イベントログ
CREATE TABLE IF NOT EXISTS event_logs (
  id TEXT PRIMARY KEY,
  origin_user TEXT NOT NULL DEFAULT '',
  event_type TEXT NOT NULL,
  project_id TEXT,
  content TEXT NOT NULL,
  created_at_epoch INTEGER NOT NULL,
  synced_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_type ON event_logs(event_type);
CREATE INDEX IF NOT EXISTS idx_events_epoch ON event_logs(created_at_epoch);
CREATE INDEX IF NOT EXISTS idx_events_project ON event_logs(project_id);

-- ユーザー指示と AI 応答のペア記録（スキル自動生成の最重要原料）
CREATE TABLE IF NOT EXISTS interaction_logs (
  id TEXT PRIMARY KEY,
  origin_user TEXT NOT NULL DEFAULT '',
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
  created_at_epoch INTEGER NOT NULL,
  synced_at TEXT,
  UNIQUE(session_id, interaction_index)
);

CREATE INDEX IF NOT EXISTS idx_ilog_session ON interaction_logs(session_id);
CREATE INDEX IF NOT EXISTS idx_ilog_project ON interaction_logs(project);
CREATE INDEX IF NOT EXISTS idx_ilog_epoch ON interaction_logs(created_at_epoch);
CREATE INDEX IF NOT EXISTS idx_ilog_outcome ON interaction_logs(execution_outcome);
CREATE INDEX IF NOT EXISTS idx_ilog_hash ON interaction_logs(user_prompt_hash);

-- プロジェクトの技術スタック情報（instinct の scope 判定に使用）
CREATE TABLE IF NOT EXISTS project_profiles (
  id TEXT PRIMARY KEY,
  origin_user TEXT NOT NULL DEFAULT '',
  project TEXT NOT NULL,
  project_path TEXT,
  languages TEXT NOT NULL DEFAULT '[]',
  frameworks TEXT NOT NULL DEFAULT '[]',
  primary_language TEXT,
  test_command TEXT,
  build_command TEXT,
  scope_hint TEXT DEFAULT 'project',
  detected_at_epoch INTEGER NOT NULL,
  last_updated_epoch INTEGER NOT NULL,
  detection_confidence REAL DEFAULT 1.0,
  synced_at TEXT,
  UNIQUE(origin_user, project)
);

CREATE INDEX IF NOT EXISTS idx_proj_prof_user ON project_profiles(origin_user);
CREATE INDEX IF NOT EXISTS idx_proj_prof_lang ON project_profiles(primary_language);

-- アイテム実行記録（スキル・コマンド・エージェント、ベストエフォート観測）
CREATE TABLE IF NOT EXISTS mem_item_runs (
  id TEXT PRIMARY KEY,
  origin_user TEXT NOT NULL DEFAULT '',
  session_id TEXT NOT NULL,
  project TEXT NOT NULL,
  skill_name TEXT NOT NULL,
  skill_trigger TEXT,
  outcome TEXT DEFAULT 'unknown',
  tools_used TEXT DEFAULT '[]',
  files_modified_count INTEGER DEFAULT 0,
  duration_seconds INTEGER,
  interaction_log_id TEXT REFERENCES interaction_logs(id),
  created_at_epoch INTEGER NOT NULL,
  synced_at TEXT,
  item_type TEXT NOT NULL DEFAULT 'skill'
);

CREATE INDEX IF NOT EXISTS idx_mir_skill ON mem_item_runs(skill_name);
CREATE INDEX IF NOT EXISTS idx_mir_project ON mem_item_runs(project);
CREATE INDEX IF NOT EXISTS idx_mir_epoch ON mem_item_runs(created_at_epoch);
CREATE INDEX IF NOT EXISTS idx_mir_outcome ON mem_item_runs(outcome, created_at_epoch);
CREATE INDEX IF NOT EXISTS idx_mir_item_type ON mem_item_runs(item_type);

-- マイグレーション適用済みバージョン管理（_MIGRATIONS の適用状態を記録）
CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at_epoch INTEGER NOT NULL
);
"""

# FTS5 と sqlite-vec は別途作成（拡張依存のため）
_FTS5_SQL = """\
CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_fts USING fts5(
  chunk_id UNINDEXED,
  content,
  user_prompt,
  tool_names,
  files_read,
  files_modified,
  ai_response_summary,
  tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON memory_chunks BEGIN
  INSERT INTO memory_chunks_fts(chunk_id, content, user_prompt, tool_names, files_read, files_modified, ai_response_summary)
  VALUES (new.id, new.content, new.user_prompt, new.tool_names, new.files_read, new.files_modified, new.ai_response_summary);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON memory_chunks BEGIN
  DELETE FROM memory_chunks_fts WHERE chunk_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON memory_chunks BEGIN
  UPDATE memory_chunks_fts
  SET content = new.content,
      user_prompt = new.user_prompt,
      tool_names = new.tool_names,
      files_read = new.files_read,
      files_modified = new.files_modified,
      ai_response_summary = new.ai_response_summary
  WHERE chunk_id = new.id;
END;
"""

_VEC_SQL = """\
CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_vec USING vec0(
  chunk_id TEXT PRIMARY KEY,
  embedding FLOAT[256]
);
"""

# --- マイグレーション ---

_MIGRATIONS: list[tuple[str, list[str]]] = []
