"""comply スクリプトで共有するユーティリティ。"""

from __future__ import annotations


def extract_yaml(text: str) -> str:
    """LLM出力からYAMLを抽出し、Markdownフェンスがあれば除去する。"""
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)
