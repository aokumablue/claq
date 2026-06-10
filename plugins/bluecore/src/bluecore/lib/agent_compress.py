"""
エージェント定義 Markdown の読み込みと圧縮を行います。
frontmatter からメタデータを抽出し、カタログ・要約・全文の3形式に変換します。
必要に応じて名前の安全性を検証し、単一ファイルの遅延読み込みも提供します。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any


def parse_frontmatter(content: str) -> dict[str, Any]:
    """Markdown 文字列から YAML frontmatter を解析する。

    Args:
        content: content の値

    Returns:
        dict[str, Any]: 解析結果を返します。

    Raises:
        例外は発生しません。
    """
    match = re.match(r"^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n([\s\S]*))?$", content)
    if not match:
        return {"frontmatter": {}, "body": content}

    frontmatter: dict[str, Any] = {}
    for line in match.group(1).split("\n"):
        colon_idx = line.find(":")
        if colon_idx == -1:
            continue

        key = line[:colon_idx].strip()
        value: Any = line[colon_idx + 1 :].strip()

        # JSON 配列を処理する（例: tools: ["Read", "Grep"]）
        if value.startswith("[") and value.endswith("]"):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass  # 文字列のまま保持

        # 前後の引用符を削除する
        if isinstance(value, str) and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]

        frontmatter[key] = value

    return {"frontmatter": frontmatter, "body": match.group(2) or ""}


def _is_skippable_line(trimmed: str) -> bool:
    """Markdown の見出し・リスト・表の行など本文に含めない行か判定する。"""
    return (
        trimmed.startswith("#")
        or trimmed.startswith("- ")
        or trimmed.startswith("* ")
        or bool(re.match(r"^\d+\.\s", trimmed))
        or trimmed.startswith("|")
    )


def _collect_paragraphs(lines: list[str]) -> list[str]:
    """行リストをコードブロックや Markdown 記法を除きつつ段落単位に集約する。"""
    paragraphs: list[str] = []
    current: list[str] = []
    in_code_block = False

    for line in lines:
        trimmed = line.strip()
        if trimmed.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if trimmed == "":
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        if _is_skippable_line(trimmed):
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        current.append(trimmed)

    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def extract_summary(body: str, max_sentences: int = 1) -> str:
    """エージェント本文から意味のある最初の段落を要約として抽出する。
    見出し、リスト項目、コードブロック、表の行は除外する。

    Args:
        body: 本文
        max_sentences: 抽出する文の最大数

    Returns:
        str: 処理結果を返します。

    Raises:
        例外は発生しません。
    """
    paragraphs = _collect_paragraphs(body.split("\n"))
    first_paragraph = next((p for p in paragraphs if p), None)
    if not first_paragraph:
        return ""
    sentences = re.findall(r"[^.!?]+[.!?]+", first_paragraph) or [first_paragraph]
    return " ".join(s.strip() for s in sentences[:max_sentences]).strip()


def load_agent(file_path: str | Path) -> dict[str, Any]:
    """単一のエージェントファイルを読み込み、解析する。

    Args:
        file_path: ファイルパス

    Returns:
        dict[str, Any]: 処理結果を返します。

    Raises:
        OSError: ファイルが読み込めない場合。
    """
    path = Path(file_path)
    content = path.read_text(encoding="utf-8")
    parsed = parse_frontmatter(content)
    frontmatter = parsed["frontmatter"]
    file_name = path.stem

    tools = frontmatter.get("tools", [])
    if not isinstance(tools, list):
        tools = []

    return {
        "fileName": file_name,
        "name": frontmatter.get("name", file_name),
        "description": frontmatter.get("description", ""),
        "tools": tools,
        "model": frontmatter.get("model", "sonnet"),
        "body": parsed["body"],
        "byteSize": len(content.encode("utf-8")),
    }


def load_agents(agents_dir: str | Path) -> list[dict[str, Any]]:
    """ディレクトリからすべてのエージェントを読み込む。

    Args:
        agents_dir: エージェント .md ファイルが配置されたディレクトリパス。

    Returns:
        list[dict[str, Any]]: エージェント定義 dict の一覧。
            ディレクトリが存在しない場合は例外を出さず空リストを返す。

    Raises:
        OSError: ディレクトリ内のエージェントファイルが読み込めない場合
            （load_agent の read_text から伝播）。
    """
    path = Path(agents_dir)
    return [load_agent(f) for f in sorted(path.glob("*.md"))]


def compress_to_catalog(agent: dict[str, Any]) -> dict[str, Any]:
    """エージェントをカタログエントリに圧縮する（メタデータのみ）。

    Args:
        agent: agent の値

    Returns:
        dict[str, Any]: 処理結果を返します。

    Raises:
        例外は発生しません。
    """
    return {
        "name": agent["name"],
        "description": agent["description"],
        "tools": agent["tools"],
        "model": agent["model"],
    }


def compress_to_summary(agent: dict[str, Any]) -> dict[str, Any]:
    """エージェントを要約エントリに圧縮する（メタデータ + 最初の段落）。

    Args:
        agent: agent の値

    Returns:
        dict[str, Any]: 処理結果を返します。

    Raises:
        例外は発生しません。
    """
    return {
        **compress_to_catalog(agent),
        "summary": extract_summary(agent["body"]),
    }


ALLOWED_MODES = ("catalog", "summary", "full")


def _compress_agents(agents: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    """モードに応じてエージェントリストを圧縮する。"""
    if mode == "catalog":
        return [compress_to_catalog(a) for a in agents]
    if mode == "summary":
        return [compress_to_summary(a) for a in agents]
    return [
        {"name": a["name"], "description": a["description"], "tools": a["tools"], "model": a["model"], "body": a["body"]}
        for a in agents
    ]


def build_agent_catalog(
    agents_dir: str | Path,
    *,
    mode: str = "catalog",
    filter_fn: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """
    エージェントのディレクトリから圧縮カタログを構築する。

    モード:
     - 'catalog': 名前、説明、ツール、モデルのみ（27 エージェントで約 2〜3k トークン）
     - 'summary': カタログ + 最初の段落の要約（約 4〜5k トークン）
     - 'full':    圧縮せず、本文全体を含める

    Args:
        agents_dir: agents_dir の値
        mode: mode の値
        filter_fn: filter_fn の値

    Returns:
        dict[str, Any]: 処理結果を返します。

    Raises:
        ValueError: 入力の不正や処理失敗時に発生します。
    """
    if mode not in ALLOWED_MODES:
        raise ValueError(f'Invalid mode "{mode}". Allowed modes: {", ".join(ALLOWED_MODES)}')

    agents = load_agents(agents_dir)
    if filter_fn is not None:
        agents = [a for a in agents if filter_fn(a)]

    original_bytes = sum(a["byteSize"] for a in agents)
    compressed = _compress_agents(agents, mode)
    compressed_json = json.dumps(compressed)
    # おおよそのトークン見積もり: 英語テキストでは約 4 文字で 1 トークン
    compressed_token_estimate = len(compressed_json) // 4 + 1

    return {
        "agents": compressed,
        "stats": {
            "totalAgents": len(agents),
            "originalBytes": original_bytes,
            "compressedBytes": len(compressed_json.encode("utf-8")),
            "compressedTokenEstimate": compressed_token_estimate,
            "mode": mode,
        },
    }


def lazy_load_agent(agents_dir: str | Path, agent_name: str) -> dict[str, Any] | None:
    """名前を指定して単一エージェントの完全な内容を遅延読み込みする。
    見つからない場合は None を返す。

    Args:
        agents_dir: agents_dir の値
        agent_name: agent_name の値

    Returns:
        dict[str, Any] | None: dict[str, Any を返します。見つからない場合は None です。

    Raises:
        例外は発生しません。
    """
    # agentName を検証する: 英数字、ハイフン、アンダースコアのみ許可
    if not re.match(r"^[\w-]+$", agent_name):
        return None

    agents_path = Path(agents_dir).resolve()
    file_path = (agents_path / f"{agent_name}.md").resolve()

    # 解決したパスがまだ agentsDir 配下にあることを確認する
    try:
        file_path.relative_to(agents_path)
    except ValueError:
        return None

    if not file_path.exists():
        return None

    return load_agent(file_path)


__all__ = [
    "ALLOWED_MODES",
    "build_agent_catalog",
    "compress_to_catalog",
    "compress_to_summary",
    "extract_summary",
    "lazy_load_agent",
    "load_agent",
    "load_agents",
    "parse_frontmatter",
]
