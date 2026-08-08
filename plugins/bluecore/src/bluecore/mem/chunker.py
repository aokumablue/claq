"""ルールベースのチャンク分割 — LLM不要でツール使用をメモリチャンクに変換"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from bluecore.lib.repo_paths import filter_safe_paths
from bluecore.mem.database import MemoryChunk
from bluecore.mem.redaction import redact
from bluecore.mem.tag_stripping import strip_tags

_FILE_WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}

# tool_response が入力（編集・書き込み内容）をそのままエコーする書き込み系ツール。
# これらの response は input_summary と情報が重複し、生 dict（structuredPatch 等）で
# 肥大化するため、保存時に短い抜粋へ切り詰める（埋め込み・注入密度のコスト削減）。
_ECHO_RESPONSE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# tool_output の最大文字数（超過時はトランケート）
_MAX_OUTPUT_LEN = 1500
_ECHO_RESPONSE_OUTPUT_LEN = 200  # エコー系ツール response の短縮上限
_TRUNCATE_KEEP = 500  # 先頭/末尾それぞれ保持する文字数



@dataclass(frozen=True)
class ToolUseParams:
    """1回のツール使用をチャンクに蓄積するためのパラメータ。"""

    tool_name: str
    tool_input: dict | str | None
    tool_response: str | None
    chunk_max_length: int = 2000


def _parse_tool_input(tool_input: dict | str | None) -> dict:
    """tool_input を dict に正規化する。"""
    if isinstance(tool_input, dict):
        return tool_input
    if isinstance(tool_input, str):
        try:
            parsed = json.loads(tool_input)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


@dataclass
class ChunkAccumulator:
    """1ユーザープロンプト分のツール使用を蓄積する"""

    session_id: str
    project: str
    chunk_index: int
    tool_names: list[str] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    _content_parts: list[str] = field(default_factory=list)

    def add_tool_use(self, params: ToolUseParams) -> None:
        """ツール使用を蓄積する"""
        tool_name = params.tool_name
        if tool_name not in self.tool_names:
            self.tool_names.append(tool_name)

        inp = _parse_tool_input(params.tool_input)

        # ファイルパスの抽出（危険なパス — トラバーサル・制御文字・
        # コンテキストタグ偽装 — は永続化前に除外する）
        files = filter_safe_paths(_extract_file_paths(tool_name, inp))
        if tool_name in _FILE_WRITE_TOOLS:
            self.files_modified.extend(f for f in files if f not in self.files_modified)
        elif files:
            self.files_read.extend(f for f in files if f not in self.files_read)

        # コンテンツの組み立て
        input_summary = _summarize_input(tool_name, inp, params.tool_input)
        output_max_len = _ECHO_RESPONSE_OUTPUT_LEN if tool_name in _ECHO_RESPONSE_TOOLS else _MAX_OUTPUT_LEN
        output_summary = _truncate(strip_tags(str(params.tool_response or "")), max_len=output_max_len)
        part = f"[{tool_name}] {input_summary}"
        if output_summary:
            part += f"\n{output_summary}"

        # チャンク最大長に収まるよう制限
        if sum(len(p) for p in self._content_parts) + len(part) > params.chunk_max_length:
            return
        self._content_parts.append(part)

    def to_chunk(self) -> MemoryChunk:
        """蓄積した内容を redact・タグ除去した MemoryChunk に変換する。"""
        content = "\n\n".join(self._content_parts)
        return MemoryChunk(
            session_id=self.session_id,
            project=self.project,
            chunk_index=self.chunk_index,
            content=redact(strip_tags(content)),
            tool_names=self.tool_names,
            files_read=self.files_read,
            files_modified=self.files_modified,
            created_at_epoch=int(time.time()),
        )


def build_chunk_from_tool_use(
    session_id: str,
    project: str,
    chunk_index: int,
    params: ToolUseParams,
) -> MemoryChunk:
    """単一ツール使用から即座にチャンクを生成する（PostToolUse 毎の呼び出し）"""
    acc = ChunkAccumulator(
        session_id=session_id,
        project=project,
        chunk_index=chunk_index,
    )
    acc.add_tool_use(params)
    return acc.to_chunk()


# --- 内部ヘルパー ---


def _extract_file_paths(tool_name: str, inp: dict) -> list[str]:
    """パース済み tool_input からファイルパスを抽出する"""
    paths: list[str] = []

    # Read / Write / Edit の場合は file_path を抽出
    if fp := inp.get("file_path"):
        paths.append(str(fp))
    # Grep の場合は path を抽出
    if p := inp.get("path"):
        if p != "." and "/" in str(p):
            paths.append(str(p))
    # Glob の場合は pattern を抽出（パスを含むものだけ。Grep の pattern は検索条件なので除外）
    if tool_name == "Glob" and (pattern := inp.get("pattern")) and "/" in str(pattern):
        paths.append(str(pattern))
    # Bash の command からファイルパスを抽出
    if tool_name == "Bash" and (cmd := inp.get("command")):
        paths.extend(re.findall(r'(?:^|\s)(/[^\s"\']+)', str(cmd)))

    return paths


def _summarize_input(tool_name: str, inp: dict, raw_input: dict | str | None) -> str:
    """tool_input の要約を生成する"""
    if not inp and not raw_input:
        return ""

    match tool_name:
        case "Read" | "Write":
            return inp.get("file_path", "")
        case "Edit":
            fp = inp.get("file_path", "")
            old = _truncate(inp.get("old_string", ""), max_len=80)
            return f"{fp} ({old} → ...)"
        case "Bash":
            return _truncate(inp.get("command", ""), max_len=200)
        case "Glob":
            return inp.get("pattern", "")
        case "Grep":
            return f"{inp.get('pattern', '')} in {inp.get('path', '.')}"
        case _:
            if inp:
                return _truncate(json.dumps(inp, ensure_ascii=False), max_len=200)
            return _truncate(str(raw_input), max_len=200)


def _truncate(text: str, max_len: int = _MAX_OUTPUT_LEN) -> str:
    """長すぎるテキストを先頭/末尾保持でトランケートする"""
    if len(text) <= max_len:
        return text
    keep = min(_TRUNCATE_KEEP, max_len // 2)
    return f"{text[:keep]}\n... ({len(text) - 2 * keep} chars truncated) ...\n{text[-keep:]}"
