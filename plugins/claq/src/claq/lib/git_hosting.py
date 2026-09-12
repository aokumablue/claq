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

from claq.lib.subprocess_utils import run_text

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
    if fallback not in VALID_GIT_HOSTING_SERVICES:
        fallback = GITHUB
    if normalized:
        warnings.warn(
            f"Invalid git-hosting-service '{value}', falling back to '{fallback}'",
            UserWarning,
            stacklevel=2,
        )
    return fallback


# origin URL からホスト部だけを取り出すためのパターン。
#
# 1 本目: `scheme://[user@]host[:port]/path`（http/https/ssh/git）。IPv6 リテラルの
# `[::1]` も 1 つのホストとして拾う。
# 2 本目: scp 形式 `[user@]host:path`（`git@gitlab.example.com:group/repo.git`）。
# scheme が無いためこちらを別に持つ必要がある —— urlsplit だけで済ませると
# 自前ホストの SSH remote が軒並みホスト無しになり、既定値へ落ちる。
# `:` の直後が `/` の場合は scheme 付き URL なので 1 本目に譲る。
_URL_HOST_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://(?:[^/@]*@)?(\[[^\]]+\]|[^/:?#]+)")
_SCP_HOST_PATTERN = re.compile(r"^(?:[^/@]*@)?([^/:]+):(?!/)")


def _remote_host(url: str) -> str:
    """remote URL からホスト部を小文字で取り出す。

    ホストを特定できない形（ローカルパス等）では空文字を返す。

    Args:
        url: `git remote get-url origin` が返した URL。

    Returns:
        小文字化したホスト部。取り出せなければ空文字。

    Raises:
        例外は発生しません。
    """
    stripped = url.strip()
    for pattern in (_URL_HOST_PATTERN, _SCP_HOST_PATTERN):
        match = pattern.match(stripped)
        if match:
            return match.group(1).lower()
    return ""


def detect_git_hosting_service(cwd: str | Path | None = None, default: str = GITHUB) -> str:
    """`git remote get-url origin` の URL から hosting service を推測する。

    判定は **URL 全体ではなくホスト部だけ**で行う。URL 全体の部分一致にすると
    `https://github.com/acme/gitlab-migration.git` のようにリポジトリ名へ他方の
    名前を含むだけで誤判定し、`gh pr create` の代わりに `glab mr create` を
    案内してしまう（先に評価される `gitlab` 側が勝つため）。
    ホスト部に `gitlab` を含めば gitlab、`github` を含めば github、
    どちらでもない・ホストを取り出せない（ローカルパス等）なら default。
    git コマンド失敗時も default を返す。

    subprocess は `subprocess_utils.run_text` 経由で呼ぶ。`text=True` だけで
    encoding を指定しないと locale 依存のデコードになり、`LC_ALL=C`
    （preferred encoding = US-ASCII）環境で origin URL に非 ASCII バイトが
    含まれる場合（自前 GitLab の日本語グループ名、IDN ホスト）に
    `UnicodeDecodeError` が下の except を貫通して本 docstring の
    「例外は発生しません」が破れる。`run_text` は `encoding="utf-8",
    errors="replace"` を集約済み。

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
        result = run_text(["git", "-C", check_dir, "remote", "get-url", "origin"], timeout=5)
    except (OSError, subprocess.SubprocessError):
        return normalize_git_hosting_service(default)

    if result.returncode != 0:
        return normalize_git_hosting_service(default)

    host = _remote_host(result.stdout)
    if "gitlab" in host:
        return GITLAB
    if "github" in host:
        return GITHUB
    return normalize_git_hosting_service(default)


def _lookup_service(table: dict[str, str], service: str) -> str:
    """正規化した service で table を引き、未知なら GitHub の値を返す。"""
    return table.get(normalize_git_hosting_service(service), table[GITHUB])


def get_git_hosting_service_label(service: str) -> str:
    """hosting service の表示名を返す。"""
    return _lookup_service(SERVICE_LABELS, service)


def get_git_hosting_cli_name(service: str) -> str:
    """hosting service で使う CLI 名を返す。"""
    return _lookup_service(SERVICE_CLI_NAMES, service)


def get_git_hosting_item_label(service: str) -> str:
    """Pull Request / Merge Request の名称を返す。"""
    return _lookup_service(SERVICE_ITEM_LABELS, service)


def get_git_hosting_item_short_label(service: str) -> str:
    """PR / MR の短縮表記を返す。"""
    return _lookup_service(SERVICE_ITEM_SHORT_LABELS, service)


def get_git_hosting_create_command(service: str) -> str:
    """作成コマンドを返す。"""
    return _lookup_service(SERVICE_CREATE_COMMANDS, service)


def get_git_hosting_review_command(service: str) -> str:
    """レビュー用コマンドを返す。"""
    return _lookup_service(SERVICE_REVIEW_COMMANDS, service)


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
