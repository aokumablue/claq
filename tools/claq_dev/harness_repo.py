"""claq 自身のリポジトリ（プラグイン提供元）を repo モードで採点する開発用 CLI。

採点の仕組み（scope の絞り込み・集計・表示・CLI）は配布物の ``claq.ci.harness_audit`` と
共有し、ここはチェック定義（``claq_dev.harness_repo_checks``）を渡すだけにする。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from claq.ci.harness_audit import assemble_report, run
from claq_dev.harness_repo_checks import get_repo_checks

_USAGE = """
Usage: python3 -m claq_dev.harness_repo [scope] [--scope <repo|hooks|skills|commands|agents>] [--format <text|json>]
       [--root <path>]

Deterministic harness audit of claq's own plugin directory (repo mode). Point --root at plugins/claq.
"""


def build_report(scope: str, root_dir: Path) -> dict[str, Any]:
    """claq のプラグインディレクトリを repo モードで採点したレポートを組み立てる。

    Args:
        scope: 監査スコープ。
        root_dir: 監査対象のプラグインディレクトリ（``plugins/claq``。`run` が解決済みの Path を渡す）。
    """
    return assemble_report(scope, root_dir, "repo", get_repo_checks(root_dir))


def main(argv: Sequence[str] | None = None) -> int:
    """CLI のエントリポイント。"""
    return run(argv, build_report, _USAGE)


if __name__ == "__main__":
    raise SystemExit(main())
