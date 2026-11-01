"""Git hosting service の共通ヘルパー。

`git remote get-url origin` の URL から github / gitlab を推測する。
"""

from __future__ import annotations

import re
from pathlib import Path

from claq.lib.subprocess_utils import run_text

GITHUB = "github"
GITLAB = "gitlab"

SERVICE_LABELS = {
    GITHUB: "GitHub",
    GITLAB: "GitLab",
}


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
    """
    stripped = url.strip()
    for pattern in (_URL_HOST_PATTERN, _SCP_HOST_PATTERN):
        match = pattern.match(stripped)
        if match:
            return match.group(1).lower()
    return ""


def detect_git_hosting_service(cwd: str | Path | None = None) -> str | None:
    """`git remote get-url origin` の URL から hosting service を推測する。

    判定は **URL 全体ではなくホスト部だけ**で行う。URL 全体の部分一致にすると
    `https://github.com/acme/gitlab-migration.git` のようにリポジトリ名へ他方の
    名前を含むだけで誤判定し、`gh pr create` の代わりに `glab mr create` を
    案内してしまう（先に評価される `gitlab` 側が勝つため）。
    ホスト部に `gitlab` を含めば gitlab、`github` を含めば github。

    subprocess は encoding を集約した `subprocess_utils.run_text` 経由で呼ぶ。
    locale 依存のデコードだと、`LC_ALL=C` 環境で origin URL に非 ASCII バイトが
    含まれる場合（日本語グループ名、IDN ホスト）に `UnicodeDecodeError` になる。

    Args:
        cwd: 判定対象の作業ディレクトリ。省略時は現在のディレクトリです。

    Returns:
        `github` か `gitlab`。git が無い・origin が無い・どちらのホストでもない（自前の Gitea、
        ローカルパス等）なら None（不明）。

    Raises:
        subprocess.TimeoutExpired: git が時間内に終わらない場合。
    """
    check_dir = str(cwd) if cwd is not None else "."
    try:
        result = run_text(["git", "-C", check_dir, "remote", "get-url", "origin"], timeout=5)
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None

    host = _remote_host(result.stdout)
    if "gitlab" in host:
        return GITLAB
    if "github" in host:
        return GITHUB
    return None


__all__ = [
    "GITHUB",
    "GITLAB",
    "SERVICE_LABELS",
    "detect_git_hosting_service",
]
