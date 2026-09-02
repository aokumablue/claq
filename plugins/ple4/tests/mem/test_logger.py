"""logger のテスト"""

import logging
from pathlib import Path

import pytest

import ple4.mem.logger as logger


def _flush() -> None:
    """ple4.mem ロガーの全ハンドラを flush する。"""
    for handler in logging.getLogger("ple4.mem").handlers:
        handler.flush()


class TestLogger:
    """ロガーのテスト"""

    def setup_method(self) -> None:
        logger.reset()

    def teardown_method(self) -> None:
        logger.reset()

    def test_get_returns_logger(self) -> None:
        log = logger.get("TEST")
        assert isinstance(log, logging.Logger)
        assert log.name == "ple4.mem.TEST"

    def test_setup_creates_handlers(self, tmp_path: Path) -> None:
        logger.setup(tmp_path, level="debug")
        root = logging.getLogger("ple4.mem")
        assert len(root.handlers) == 2  # ファイル出力 + stderr
        assert root.level == logging.DEBUG

    def test_setup_creates_log_file(self, tmp_path: Path) -> None:
        logger.setup(tmp_path, level="info")
        log_files = list(tmp_path.glob("mem-*.log"))
        assert len(log_files) == 1

    def test_setup_idempotent(self, tmp_path: Path) -> None:
        logger.setup(tmp_path, level="info")
        logger.setup(tmp_path, level="debug")  # 2回目は無視
        root = logging.getLogger("ple4.mem")
        assert len(root.handlers) == 2  # 増えない

    def test_setup_invalid_level_defaults_to_info(self, tmp_path: Path) -> None:
        logger.setup(tmp_path, level="nonexistent")
        root = logging.getLogger("ple4.mem")
        assert root.level == logging.INFO

    def test_reset_clears_state(self, tmp_path: Path) -> None:
        logger.setup(tmp_path)
        logger.reset()
        root = logging.getLogger("ple4.mem")
        assert len(root.handlers) == 0
        assert not logger._initialized

    def test_log_message_written_to_file(self, tmp_path: Path) -> None:
        logger.setup(tmp_path, level="info")
        log = logger.get("TEST")
        log.info("test message 12345")
        _flush()
        log_file = list(tmp_path.glob("mem-*.log"))[0]
        content = log_file.read_text()
        assert "test message 12345" in content

    def test_log_dir_chmod_0700(self, tmp_path: Path) -> None:
        """ログディレクトリは chmod 0700 で作成される。"""
        log_dir = tmp_path / "logs"
        logger.setup(log_dir, level="info")
        mode = log_dir.stat().st_mode & 0o777
        assert mode == 0o700, f"ログディレクトリ権限が期待 0o700 だが {oct(mode)}"

    def test_log_file_chmod_0600(self, tmp_path: Path) -> None:
        """ログファイルは chmod 0600 で作成される。"""
        logger.setup(tmp_path, level="info")
        log_files = list(tmp_path.glob("mem-*.log"))
        assert log_files
        mode = log_files[0].stat().st_mode & 0o777
        assert mode == 0o600, f"ログファイル権限が期待 0o600 だが {oct(mode)}"

    def test_redacting_formatter_masks_secret(self, tmp_path: Path) -> None:
        """ログに書かれたシークレットが [REDACTED] に置換される。"""
        logger.setup(tmp_path, level="info")
        log = logger.get("TEST")
        log.info("token=" + "sk-ant-" + "api03-" + "A" * 90)
        _flush()
        log_file = list(tmp_path.glob("mem-*.log"))[0]
        content = log_file.read_text()
        assert "[REDACTED]" in content
        assert "sk-ant-api03-" not in content

    def test_setup_can_retry_after_handler_construction_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ハンドラ構築が失敗しても初期化済みフラグが立たず、次の setup で復帰できる。

        `_initialized = True` を構築前に立てていた頃は、一度でも失敗すると
        「初期化済みだがハンドラが空」という復帰不能な状態がプロセス内に残った。
        """
        logger.reset()
        calls = {"n": 0}
        real_file_handler = logger._file_handler

        def flaky_file_handler(log_dir: Path):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return real_file_handler(log_dir)

        monkeypatch.setattr(logger, "_file_handler", flaky_file_handler)

        with pytest.raises(OSError):
            logger.setup(tmp_path, level="info")
        assert logger._initialized is False

        logger.setup(tmp_path, level="info")
        assert logging.getLogger("ple4.mem").handlers
