"""mem サブシステムの SQLite スキーマ定義（database.py から分離）。

``repos`` / ``sessions`` / ``knowledge`` の 3 テーブルのみで構成する。
``knowledge.session_id`` が ``sessions`` を参照するため、DDL は
``repos`` → ``sessions`` → ``knowledge`` の順に並べる。
"""

from __future__ import annotations

_SCHEMA_SQL = """\
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

-- リポジトリ台帳。知識のスコープ境界。
-- id は人間可読スラッグ、正体キーは identity_key に分離する。
CREATE TABLE IF NOT EXISTS repos (
  id            TEXT PRIMARY KEY,          -- 'ple4-dev'（同名衝突時のみ '-2' を付す）
  identity_key  TEXT NOT NULL UNIQUE,      -- 正規化 remote URL、無ければ repo root 絶対パス
  root_path     TEXT NOT NULL,             -- 最後に観測した絶対パス（worktree でも本体に寄せる）
  remote_url    TEXT,                      -- userinfo除去済みremote（無ければNULL。§7-4）
  first_seen_at TEXT NOT NULL,             -- ISO8601
  last_seen_at  TEXT NOT NULL
);

-- セッション履歴。knowledge の出所であり、次セッションへの引き継ぎを持つ。
CREATE TABLE IF NOT EXISTS sessions (
  id          INTEGER PRIMARY KEY,
  session_uid TEXT NOT NULL UNIQUE,        -- ハーネスが渡す session_id
  repo_id     TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  harness     TEXT NOT NULL DEFAULT 'unknown',
  handoff     TEXT NOT NULL DEFAULT '',    -- 次セッションへの引き継ぎ（人間可読の散文）
  started_at  TEXT NOT NULL,
  ended_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_repo
  ON sessions(repo_id, started_at DESC);

-- 知識カード。1行 = 1つの再利用可能な言明。
-- title だけ読んで意味が通ることを必須とする（注入は原則 title のみ）。
CREATE TABLE IF NOT EXISTS knowledge (
  id            INTEGER PRIMARY KEY,
  key           TEXT NOT NULL,             -- kebab-case スラッグ。重複投入の防止キー
  scope         TEXT NOT NULL CHECK (scope IN ('global','repo')),
  repo_id       TEXT REFERENCES repos(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL CHECK (kind IN
                  ('convention','decision','pitfall','howto','fact','preference')),
  title         TEXT NOT NULL,             -- 1行。これ単体で意味が通ること
  body          TEXT NOT NULL DEFAULT '',  -- why / how の補足。空でよい
  domain        TEXT,                      -- 'testing' 'git' 'build' 等。任意
  confidence    REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  -- 既定は 'pending'（＝注入されない側）。H-01「agent 由来カードは人間の
  -- promote を経なければ注入されない」を支えるのが Python 検証 1 層だけだと、
  -- status を省略する経路が 1 本増えた瞬間に破れる。現状そんな経路は無いが、
  -- 多層防御として最も危険な値を既定に据えない（省略時は fail-safe 側へ倒す）。
  status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('active','pending','archived')),
  source        TEXT NOT NULL CHECK (source IN ('agent','observer','human')),
  source_ref    TEXT,                      -- 出所の自由記述（ファイルパス等）
  session_id    INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
  superseded_by INTEGER REFERENCES knowledge(id) ON DELETE SET NULL,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  -- scope と repo_id の整合を強制する
  CHECK ((scope = 'global' AND repo_id IS NULL)
      OR (scope = 'repo'   AND repo_id IS NOT NULL))
);

-- global 行は repo_id が NULL のため、式インデックスで一意性を担保する
CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_key
  ON knowledge(COALESCE(repo_id, ''), key);
CREATE INDEX IF NOT EXISTS idx_knowledge_inject
  ON knowledge(status, scope, repo_id);
"""
