"""ルールベースのチャンク分割 — LLM不要でツール使用をメモリチャンクに変換"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from devgear.mem.database import MemoryChunk
from devgear.mem.redaction import redact
from devgear.mem.tag_stripping import strip_tags

_FILE_WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}

# tool_output の最大文字数（超過時はトランケート）
_MAX_OUTPUT_LEN = 1500
_TRUNCATE_KEEP = 500  # 先頭/末尾それぞれ保持する文字数

# AI 応答の要約: 先頭400 + 末尾100 = 計500文字
_AI_RESPONSE_HEAD = 400
_AI_RESPONSE_TAIL = 100


def _summarize_ai_response(response: str) -> str:
    """AI 応答を先頭+末尾で最大500文字に要約する。"""
    if len(response) <= _AI_RESPONSE_HEAD + _AI_RESPONSE_TAIL:
        return response
    return response[:_AI_RESPONSE_HEAD] + response[-_AI_RESPONSE_TAIL:]


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
    user_prompt: str
    chunk_index: int
    tool_names: list[str] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    _content_parts: list[str] = field(default_factory=list)

    # 実行品質トラッキング
    _tool_sequence: list[str] = field(default_factory=list)  # 順序保持・重複あり
    _error_count: int = 0
    _last_error: str | None = None
    _ai_response_summary: str | None = None

    def add_tool_use(
        self,
        tool_name: str,
        tool_input: dict | str | None,
        tool_response: str | None,
        chunk_max_length: int = 2000,
        is_error: bool = False,
        ai_response: str | None = None,
    ) -> None:
        """ツール使用を蓄積する"""
        # 重複排除なし（呼び出し順序を保持）
        self._tool_sequence.append(tool_name)

        # tool_names は重複排除（後方互換）
        if tool_name not in self.tool_names:
            self.tool_names.append(tool_name)

        inp = _parse_tool_input(tool_input)

        # ファイルパスの抽出
        files = _extract_file_paths(tool_name, inp)
        if tool_name in _FILE_WRITE_TOOLS:
            self.files_modified.extend(f for f in files if f not in self.files_modified)
        elif files:
            self.files_read.extend(f for f in files if f not in self.files_read)

        # エラー記録
        if is_error:
            self._error_count += 1
            if tool_response:
                from devgear.mem.redaction import redact

                self._last_error = redact(tool_response[:500])

        # AI 応答の要約を保存（最後のものを上書き）
        if ai_response:
            self._ai_response_summary = _summarize_ai_response(ai_response)

        # コンテンツの組み立て
        input_summary = _summarize_input(tool_name, inp, tool_input)
        output_summary = _truncate(strip_tags(str(tool_response or "")))
        part = f"[{tool_name}] {input_summary}"
        if output_summary:
            part += f"\n{output_summary}"

        # チャンク最大長に収まるよう制限
        if sum(len(p) for p in self._content_parts) + len(part) > chunk_max_length:
            return
        self._content_parts.append(part)

    def to_chunk(self) -> MemoryChunk:
        content = "\n\n".join(self._content_parts)
        # エラーが1件以上あれば failure、それ以外は success
        execution_status = "failure" if self._error_count > 0 else "success"
        return MemoryChunk(
            session_id=self.session_id,
            project=self.project,
            chunk_index=self.chunk_index,
            content=redact(strip_tags(content)),
            tool_names=self.tool_names,
            files_read=self.files_read,
            files_modified=self.files_modified,
            user_prompt=redact(strip_tags(self.user_prompt)),
            created_at_epoch=int(time.time()),
            execution_status=execution_status,
            tool_error=self._last_error,
            ai_response_summary=self._ai_response_summary,
            tool_sequence=list(self._tool_sequence),
        )


def build_chunk_from_tool_use(
    session_id: str,
    project: str,
    chunk_index: int,
    user_prompt: str,
    tool_name: str,
    tool_input: dict | str | None,
    tool_response: str | None,
    chunk_max_length: int = 2000,
    is_error: bool = False,
    ai_response: str | None = None,
) -> MemoryChunk:
    """単一ツール使用から即座にチャンクを生成する（PostToolUse 毎の呼び出し）"""
    acc = ChunkAccumulator(
        session_id=session_id,
        project=project,
        user_prompt=user_prompt,
        chunk_index=chunk_index,
    )
    acc.add_tool_use(tool_name, tool_input, tool_response, chunk_max_length, is_error, ai_response)
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
