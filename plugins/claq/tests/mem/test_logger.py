"""claq.mem.logger のテスト。"""

import logging
from pathlib import Path

import pytest

import claq.mem.logger as logger


@pytest.fixture(autouse=True)
def _reset_logger() -> None:
    """各テストの前後でロガーの初期化状態とハンドラを消す。"""
    logger.reset()
    yield
    logger.reset()


def _mem_root() -> logging.Logger:
    """claq.mem のルートロガーを返す。"""
    return logging.getLogger("claq.mem")


def test_get_returns_child_logger() -> None:
    """get はコンポーネント名を付けた claq.mem 配下のロガーを返す。"""
    log = logger.get("TEST")
    assert isinstance(log, logging.Logger)
    assert log.name == "claq.mem.TEST"


@pytest.mark.parametrize(
    ("level", "expected"),
    [("debug", logging.DEBUG), ("info", logging.INFO), ("nonexistent", logging.INFO)],
)
def test_setup_adds_file_and_stderr_handlers_with_level(tmp_path: Path, level: str, expected: int) -> None:
    """ファイル出力と stderr の 2 ハンドラを付け、レベルを設定する（不正なレベルは INFO）。"""
    logger.setup(tmp_path, level=level)
    assert len(_mem_root().handlers) == 2
    assert _mem_root().level == expected


def test_setup_is_idempotent_and_reset_clears_state(tmp_path: Path) -> None:
    """2 回目の setup は無視され（ハンドラもレベルも変わらない）、reset でハンドラと初期化済みフラグが消える。"""
    logger.setup(tmp_path, level="info")
    logger.setup(tmp_path, level="debug")
    assert len(_mem_root().handlers) == 2
    assert _mem_root().level == logging.INFO
    logger.reset()
    assert _mem_root().handlers == []
    assert logger._initialized is False


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
    logger.setup(logs, level="info")
    existing = sorted(logs.glob("mem-*.log"))
    assert len(existing) == 1
    assert root.stat().st_mode & 0o777 == 0o700
    assert logs.stat().st_mode & 0o777 == 0o700
    assert existing[0].stat().st_mode & 0o777 == 0o600

    logger.reset()
    logger.setup(logs, level="info")
    assert sorted(logs.glob("mem-*.log")) == existing
    assert existing[0].stat().st_mode & 0o777 == 0o600


def test_messages_are_written_to_file_with_secrets_redacted(tmp_path: Path) -> None:
    """ログメッセージはファイルへ書かれ、シークレットは [REDACTED] に置換される。"""
    logger.setup(tmp_path, level="info")
    log = logger.get("TEST")
    log.info("test message 12345")
    log.info("token=" + "sk-ant-" + "api03-" + "A" * 90)
    for handler in _mem_root().handlers:
        handler.flush()
    content = next(tmp_path.glob("mem-*.log")).read_text()
    assert "test message 12345" in content
    assert "[REDACTED]" in content
    assert "sk-ant-api03-" not in content


def test_setup_can_retry_after_handler_construction_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ハンドラ構築が失敗しても初期化済みフラグが立たず、次の setup で復帰できる。

    `_initialized = True` を構築前に立てると、一度でも失敗したとき
    「初期化済みだがハンドラが空」という復帰不能な状態がプロセス内に残る。
    """
    real_file_handler = logger._file_handler
    failures = [OSError("disk full")]

    def flaky_file_handler(log_dir: Path) -> logging.Handler:
        """初回だけ OSError を送出し、以降は本物のハンドラを返す。"""
        if failures:
            raise failures.pop()
        return real_file_handler(log_dir)

    monkeypatch.setattr(logger, "_file_handler", flaky_file_handler)

    with pytest.raises(OSError, match="disk full"):
        logger.setup(tmp_path, level="info")
    assert logger._initialized is False

    logger.setup(tmp_path, level="info")
    assert _mem_root().handlers
