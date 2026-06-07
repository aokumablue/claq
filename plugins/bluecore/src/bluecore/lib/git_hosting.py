"""Git hosting service の共通ヘルパー。

`git remote get-url origin` の URL から github / gitlab を推測する。
settings.json 依存は廃止済み。
"""

from __future__ import annotations

import re
import subprocess
import warnings
from pathlib import Path
from typing import Any

GITHUB = "github"
GITLAB = "gitlab"
VALID_GIT_HOSTING_SERVICES = frozenset({GITHUB, GITLAB})

SERVICE_LABELS = {
    GITHUB: "GitHub",
    GITLAB: "GitLab",
}

SERVICE_CLI_NAMES = {
    GITHUB: "gh",
    GITLAB: "glab",
}

SERVICE_ITEM_LABELS = {
    GITHUB: "Pull Request",
    GITLAB: "Merge Request",
}

SERVICE_ITEM_SHORT_LABELS = {
    GITHUB: "PR",
    GITLAB: "MR",
}

SERVICE_CREATE_COMMANDS = {
    GITHUB: "gh pr create",
    GITLAB: "glab mr create",
}

SERVICE_REVIEW_COMMANDS = {
    GITHUB: "gh pr review",
    GITLAB: "glab mr view",
}

SERVICE_ITEM_URL_PATTERNS = {
    GITHUB: re.compile(r"https?://github\.com/(?P<repo>[^/\s]+/[^/\s]+)/pull/(?P<number>\d+)", re.IGNORECASE),
    GITLAB: re.compile(r"https?://gitlab\.com/(?P<repo>.+?)/-/merge_requests/(?P<number>\d+)", re.IGNORECASE),
}


def normalize_git_hosting_service(value: Any, default: str = GITHUB) -> str:
    """Git hosting service 名を正規化する。

    Args:
        value: 正規化対象の値です。
        default: 未設定または不正値のときに返す既定値です。

    Returns:
        `github` か `gitlab` を返します。

    Raises:
        例外は発生しません。
    """
    normalized = str(value or "").strip().lower()
    if normalized in VALID_GIT_HOSTING_SERVICES:
        return normalized

    fallback = str(default or "").strip().lower()
    if fallback in VALID_GIT_HOSTING_SERVICES:
        if normalized:
            warnings.warn(
                f"Invalid git-hosting-service '{value}', falling back to '{fallback}'",
                UserWarning,
                stacklevel=2,
            )
        return fallback
    if normalized:
        warnings.warn(
            f"Invalid git-hosting-service '{value}', falling back to '{GITHUB}'",
            UserWarning,
            stacklevel=2,
        )
    return GITHUB


def detect_git_hosting_service(cwd: str | Path | None = None, default: str = GITHUB) -> str:
    """`git remote get-url origin` の URL から hosting service を推測する。

    URL に `gitlab` を含めば gitlab、それ以外（github.com, 他）は github フォールバック。
    git コマンド失敗時も default を返す。

    Args:
        cwd: 判定対象の作業ディレクトリ。省略時は現在のディレクトリです。
        default: 推測できないときに返す既定値です。

    Returns:
        `github` か `gitlab` を返します。

    Raises:
        例外は発生しません。
    """
    check_dir = str(cwd) if cwd is not None else "."
    try:
        result = subprocess.run(
            ["git", "-C", check_dir, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return normalize_git_hosting_service(default)

    if result.returncode != 0:
        return normalize_git_hosting_service(default)

    url = result.stdout.strip().lower()
    if "gitlab" in url:
        return GITLAB
    if "github" in url:
        return GITHUB
    return normalize_git_hosting_service(default)


def get_git_hosting_service_label(service: str) -> str:
    """hosting service の表示名を返す。"""
    return SERVICE_LABELS.get(normalize_git_hosting_service(service), SERVICE_LABELS[GITHUB])


def get_git_hosting_cli_name(service: str) -> str:
    """hosting service で使う CLI 名を返す。"""
    return SERVICE_CLI_NAMES.get(normalize_git_hosting_service(service), SERVICE_CLI_NAMES[GITHUB])


def get_git_hosting_item_label(service: str) -> str:
    """Pull Request / Merge Request の名称を返す。"""
    return SERVICE_ITEM_LABELS.get(normalize_git_hosting_service(service), SERVICE_ITEM_LABELS[GITHUB])


def get_git_hosting_item_short_label(service: str) -> str:
    """PR / MR の短縮表記を返す。"""
    return SERVICE_ITEM_SHORT_LABELS.get(normalize_git_hosting_service(service), SERVICE_ITEM_SHORT_LABELS[GITHUB])


def get_git_hosting_create_command(service: str) -> str:
    """作成コマンドを返す。"""
    return SERVICE_CREATE_COMMANDS.get(normalize_git_hosting_service(service), SERVICE_CREATE_COMMANDS[GITHUB])


def get_git_hosting_review_command(service: str) -> str:
    """レビュー用コマンドを返す。"""
    return SERVICE_REVIEW_COMMANDS.get(normalize_git_hosting_service(service), SERVICE_REVIEW_COMMANDS[GITHUB])


def build_git_hosting_item_url(service: str, repo: str, number: str) -> str:
    """PR / MR の URL を組み立てる。"""
    normalized = normalize_git_hosting_service(service)
    if normalized == GITLAB:
        return f"https://gitlab.com/{repo}/-/merge_requests/{number}"
    return f"https://github.com/{repo}/pull/{number}"


def extract_git_hosting_item_details(service: str, text: str) -> tuple[str, str] | None:
    """作成結果の出力から repo と番号を抜き出す。"""
    pattern = SERVICE_ITEM_URL_PATTERNS.get(normalize_git_hosting_service(service))
    if pattern is None:
        return None

    match = pattern.search(text)
    if not match:
        return None
    return match.group("repo"), match.group("number")


__all__ = [
    "GITHUB",
    "GITLAB",
    "VALID_GIT_HOSTING_SERVICES",
    "build_git_hosting_item_url",
    "detect_git_hosting_service",
    "extract_git_hosting_item_details",
    "get_git_hosting_cli_name",
    "get_git_hosting_create_command",
    "get_git_hosting_item_label",
    "get_git_hosting_item_short_label",
    "get_git_hosting_review_command",
    "get_git_hosting_service_label",
    "normalize_git_hosting_service",
]
