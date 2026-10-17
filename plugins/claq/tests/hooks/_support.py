"""tests/hooks の複数のファイルが使う補助（secret scan 予算の deadline 生成・コミットメッセージ検証のスパイ）。"""

from __future__ import annotations

import pytest

from claq.hooks import commit_quality_scanner as commit_quality_scanner
from claq.hooks import pre_bash_commit_quality as pre_bash_commit_quality


def _spy_commit_message_validation(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`validate_commit_message` を「問題なし」(None) を返すスパイに替え、受け取ったコマンドの記録を返す。

    コミットメッセージ検証を無関係な観点のテストから切り離すためのスタブ。None を返すだけだと、
    パッチが外れても本物が None を返す場面では結果が変わらず、差し替えが `evaluate` の経路へ
    届いたかを判別できない。呼び出しの記録を assert すれば、スタブが実際に検証段へ入ったことが固定される。
    """
    validated: list[str] = []

    def _spy(command: str) -> None:
        """受け取ったコマンドを記録し、検証結果なし(None)を返す。"""
        validated.append(command)

    monkeypatch.setattr(pre_bash_commit_quality, "validate_commit_message", _spy)
    return validated


def _scan_deadline() -> float:
    """テストから `find_file_issues` へ渡す secret scan 予算 1 本分の deadline を作る。

    本番ではフック 1 回の起動につき 1 度だけ `new_secret_scan_deadline()` を呼び、
    その 1 本を全ファイルで共有する。単発の `find_file_issues` を検査するテストは
    ファイルごとに満額の予算で構わないため、呼び出しごとに新しい deadline を作る。

    Returns:
        `_monotonic()` 基準で走査を打ち切るべき時刻（秒）。

    Raises:
        例外は発生しません。
    """
    return commit_quality_scanner.new_secret_scan_deadline()
