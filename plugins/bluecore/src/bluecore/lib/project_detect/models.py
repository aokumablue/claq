"""検出ルールと検出結果を表すデータクラス。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LanguageRule:
    """プログラミング言語を検出するためのルール。"""

    name: str
    extensions: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)


@dataclass
class FrameworkRule:
    """フレームワークを検出するためのルール。"""

    name: str
    language: str
    files: list[str] = field(default_factory=list)
    file_patterns: list[str] = field(default_factory=list)
    file_contents: list[dict[str, str]] = field(default_factory=list)
    package_json: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    cargo_toml: list[str] = field(default_factory=list)
    go_mod: list[str] = field(default_factory=list)
    gemfile: list[str] = field(default_factory=list)
    composer_json: list[str] = field(default_factory=list)
    pubspec: list[str] = field(default_factory=list)
    pom_xml: list[str] = field(default_factory=list)
    gradle: list[str] = field(default_factory=list)
    csproj: list[str] = field(default_factory=list)


@dataclass
class ProjectInfo:
    """検出されたプロジェクト情報。"""

    root: Path
    languages: list[str]
    frameworks: list[str]
    primary_language: str | None = None
