"""claq.mem.logger のテスト。"""

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

import claq.mem.logger as logger

from ._support import reset_mem_logger


@pytest.fixture(autouse=True)
def _reset_logger() -> Iterator[None]:
    """各テストの前後でロガーのハンドラを外す。"""
    reset_mem_logger()
    yield
    reset_mem_logger()


def _mem_root() -> logging.Logger:
    """claq.mem のルートロガーを返す。"""
    return logging.getLogger("claq.mem")


def test_get_returns_child_logger() -> None:
    """get はコンポーネント名を付けた claq.mem 配下のロガーを返す。"""
    log = logger.get("TEST")
    assert isinstance(log, logging.Logger)
    assert log.name == "claq.mem.TEST"


def test_setup_adds_two_handlers_at_info(tmp_path: Path) -> None:
    """ファイル出力と stderr の 2 ハンドラを INFO で付ける。"""
    logger.setup(tmp_path)
    assert len(_mem_root().handlers) == 2
    assert _mem_root().level == logging.INFO


def test_setup_closes_dirs_and_reuses_log_file(tmp_path: Path) -> None:
    """ログ用ディレクトリと**その親**を 0700、日次ログファイルを 0600 で作り、再 setup では同じファイルを使う。

    `mkdir(parents=True)` は mode を親へ適用しない。ロガーは
    `_load_settings_or_raise()` の中でどの ``Database()`` よりも先に走るため、
    DB を開かずに終わる経路（``mem list --kind bogus``）では `~/.claq` を最初に
    作るのがロガーになる。そこで 0700 にしないと ``drwxr-xr-x``（0755）のまま残り、
    CLAUDE.md が定める「`~/.claq` の 0700」を割る。
    """
    root = tmp_path / "claq-root"
    logs = root / "logs"
    logger.setup(logs)
    existing = sorted(logs.glob("mem-*.log"))
    assert len(existing) == 1
    assert root.stat().st_mode & 0o777 == 0o700
    assert logs.stat().st_mode & 0o777 == 0o700
    assert existing[0].stat().st_mode & 0o777 == 0o600

    reset_mem_logger()
    logger.setup(logs)
    assert sorted(logs.glob("mem-*.log")) == existing
    assert existing[0].stat().st_mode & 0o777 == 0o600


def test_messages_are_written_to_file_with_secrets_redacted(tmp_path: Path) -> None:
    """ログメッセージはファイルへ書かれ、シークレットは [REDACTED] に置換される。"""
    logger.setup(tmp_path)
    log = logger.get("TEST")
    log.info("test message 12345")
    log.info("token=" + "sk-ant-" + "api03-" + "A" * 90)
    for handler in _mem_root().handlers:
        handler.flush()
    content = next(tmp_path.glob("mem-*.log")).read_text()
    assert "test message 12345" in content
    assert "[REDACTED]" in content
    assert "sk-ant-api03-" not in content


def test_file_only_records_skip_stderr_but_reach_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``FILE_ONLY`` を付けた記録はファイルにだけ書かれ、付けない WARNING 以上は stderr にも出る。

    呼び出し側が利用者向けの 1 行を自分で stderr へ出す経路で、同じ失敗が 2 行にならないこと。
    """
    logger.setup(tmp_path)
    log = logger.get("TEST")
    log.error("file only failure", extra=logger.FILE_ONLY)
    log.error("shared failure")
    for handler in _mem_root().handlers:
        handler.flush()

    stderr = capsys.readouterr().err
    content = next(tmp_path.glob("mem-*.log")).read_text()
    assert "file only failure" in content
    assert "shared failure" in content
    assert "file only failure" not in stderr
    assert "shared failure" in stderr
