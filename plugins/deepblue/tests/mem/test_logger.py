"""logger のテスト"""

import logging
from pathlib import Path

import devgear.mem.logger as logger


class TestLogger:
    """ロガーのテスト"""

    def setup_method(self) -> None:
        logger.reset()

    def teardown_method(self) -> None:
        logger.reset()

    def test_get_returns_logger(self) -> None:
        log = logger.get("TEST")
        assert isinstance(log, logging.Logger)
        assert log.name == "devgear.mem.TEST"

    def test_setup_creates_handlers(self, tmp_path: Path) -> None:
        logger.setup(tmp_path, level="debug")
        root = logging.getLogger("devgear.mem")
        assert len(root.handlers) == 2  # ファイル出力 + stderr
        assert root.level == logging.DEBUG

    def test_setup_creates_log_file(self, tmp_path: Path) -> None:
        logger.setup(tmp_path, level="info")
        log_files = list(tmp_path.glob("mem-*.log"))
        assert len(log_files) == 1

    def test_setup_idempotent(self, tmp_path: Path) -> None:
        logger.setup(tmp_path, level="info")
        logger.setup(tmp_path, level="debug")  # 2回目は無視
        root = logging.getLogger("devgear.mem")
        assert len(root.handlers) == 2  # 増えない

    def test_setup_invalid_level_defaults_to_info(self, tmp_path: Path) -> None:
        logger.setup(tmp_path, level="nonexistent")
        root = logging.getLogger("devgear.mem")
        assert root.level == logging.INFO

    def test_reset_clears_state(self, tmp_path: Path) -> None:
        logger.setup(tmp_path)
        logger.reset()
        root = logging.getLogger("devgear.mem")
        assert len(root.handlers) == 0
        assert not logger._initialized

    def test_log_message_written_to_file(self, tmp_path: Path) -> None:
        logger.setup(tmp_path, level="info")
        log = logger.get("TEST")
        log.info("test message 12345")
        # バッファを flush
        for h in logging.getLogger("devgear.mem").handlers:
            h.flush()
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
        log.info("token=sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        for h in logging.getLogger("devgear.mem").handlers:
            h.flush()
        log_file = list(tmp_path.glob("mem-*.log"))[0]
        content = log_file.read_text()
        assert "[REDACTED]" in content
        assert "sk-ant-api03-" not in content
