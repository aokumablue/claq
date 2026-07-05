"""mem サブシステムのデータクラス定義群（database.py から分離）。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


def generate_uuid() -> str:
    """UUID v4 を生成する"""
    return str(uuid.uuid4())


@dataclass
class MemoryChunk:
    """1チャンク分のメモリ（プロンプト・ツール・ファイル・実行品質）を表す。"""

    session_id: str
    project: str
    chunk_index: int
    content: str
    tool_names: list[str]
    files_read: list[str]
    files_modified: list[str]
    user_prompt: str
    created_at_epoch: int
    id: str | None = None
    origin_user: str = ""

    # Phase 1: コンテキスト品質向上
    access_count: int = 0
    last_accessed_epoch: int | None = None

    # Phase 2: メモリ圧縮
    merged_generation: int = 0
    merged_into: str | None = None

    # Phase 3: 実行品質トラッキング
    execution_status: str = "unknown"  # 'success'|'partial'|'failure'|'unknown'
    tool_error: str | None = None  # エラーメッセージ先頭500文字
    ai_response_summary: str | None = None  # AI応答の要約（最大500文字）
    tool_sequence: list[str] = field(default_factory=list)  # 順序保持・重複ありリスト


@dataclass
class Session:
    """1セッション分のメタ情報（プロジェクト・git 状態・チャンク数）を表す。"""

    session_id: str
    project: str
    started_at_epoch: int
    chunk_count: int = 0
    id: str | None = None
    origin_user: str = ""

    # git 状態（セッション開始時のスナップショット）
    branch: str | None = None
    commit_hash: str | None = None  # HEAD 先頭12文字
    uncommitted_count: int = 0
    ended_at_epoch: int | None = None
    project_profile_id: str | None = None  # project_profiles への参照


@dataclass
class Instinct:
    """インスティンクトデータ"""

    instinct_id: str
    scope: str
    confidence: float
    content: str
    created_at_epoch: int
    updated_at_epoch: int
    id: str | None = None
    origin_user: str = ""
    project_id: str | None = None
    trigger_text: str | None = None
    domain: str | None = None

    # 信頼度の根拠
    observation_count: int = 0
    confidence_reasons: list[dict] = field(default_factory=list)  # [{reason, weight}]
    source_interaction_ids: list[str] = field(default_factory=list)  # interaction_log IDs
    last_activated_epoch: int | None = None


@dataclass
class Adr:
    """ADR データ"""

    project: str
    adr_number: int
    title: str
    status: str
    content: str
    created_at_epoch: int
    updated_at_epoch: int
    id: str | None = None
    origin_user: str = ""


@dataclass
class EventLog:
    """汎用イベントログ"""

    event_type: str
    content: str
    created_at_epoch: int
    id: str | None = None
    origin_user: str = ""
    project_id: str | None = None


@dataclass
class InteractionLog:
    """ユーザー指示と AI 応答のペア記録（スキル自動生成の原料）"""

    session_id: str
    project: str
    user_prompt_full: str  # トランケートなし全文
    interaction_index: int  # セッション内通し番号
    created_at_epoch: int
    id: str | None = None
    origin_user: str = ""
    user_prompt_hash: str | None = None  # SHA256先頭16文字
    ai_response_summary: str | None = None  # 最大2000文字
    ai_response_tool_plan: str | None = None  # JSON配列（最大10件）
    chunk_id: str | None = None
    execution_outcome: str = "unknown"  # 'success'|'partial'|'failure'|'unknown'
    tool_error_count: int = 0


@dataclass
class ProjectProfile:
    """プロジェクトの技術スタック情報（instinct の scope 判定に使用）"""

    project: str
    detected_at_epoch: int
    last_updated_epoch: int
    id: str | None = None
    origin_user: str = ""
    project_path: str | None = None
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    primary_language: str | None = None
    test_command: str | None = None
    build_command: str | None = None
    scope_hint: str = "project"  # 'global'|'project'
    detection_confidence: float = 1.0


@dataclass
class SessionDigest:
    """セッション全体の要約（トランスクリプト/チャンクから生成する短期記憶の圧縮版）"""

    session_id: str
    project: str
    summary: str  # 最大500字要約
    started_at_epoch: int
    created_at_epoch: int
    id: str | None = None
    origin_user: str = ""
    key_files: list[str] = field(default_factory=list)
    key_decisions: list[str] = field(default_factory=list)
    outcome: str = "unknown"  # 'success'|'partial'|'failure'|'unknown'
    harness: str = "unknown"  # 'claude'|'codex'|'copilot'|'unknown'
    source: str = "chunks"  # 'transcript+chunks'|'chunks'
    chunk_count: int = 0
    ended_at_epoch: int | None = None


@dataclass
class MemItemRun:
    """メムサブシステムが観測したスキル・コマンド・エージェントの実行記録（ベストエフォート）"""

    session_id: str
    project: str
    skill_name: str
    created_at_epoch: int
    id: str | None = None
    origin_user: str = ""
    skill_trigger: str | None = None  # トリガープロンプト先頭200文字
    outcome: str = "unknown"  # 'success'|'partial'|'failure'|'unknown'
    tools_used: list[str] = field(default_factory=list)
    files_modified_count: int = 0
    duration_seconds: int | None = None
    interaction_log_id: str | None = None
    item_type: str = "skill"  # 'skill'|'command'|'agent'
