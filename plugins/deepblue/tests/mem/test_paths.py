"""devgear.mem._paths のユニットテスト。

仕様は tests/model_build/test_paths.py と共有する。
両者は配布物として独立しているため、同一仕様のテストをコピーして維持する。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from devgear.mem._paths import safe_join, sha256_file, validate_sha256_format


class TestSafeJoin:
    """safe_join のテスト。"""

    def test_normal(self, tmp_path: Path) -> None:
        """通常のファイル名は base 配下のパスを返す。"""
        result = safe_join(tmp_path, "file.txt")
        assert result == (tmp_path / "file.txt").resolve()

    def test_dot_dot_rejected(self, tmp_path: Path) -> None:
        """.. を含む名前はエラー。"""
        with pytest.raises(ValueError, match="不正なパス"):
            safe_join(tmp_path, "../outside.txt")

    def test_absolute_path_rejected(self, tmp_path: Path) -> None:
        """絶対パスはエラー。"""
        with pytest.raises(ValueError, match="不正なパス"):
            safe_join(tmp_path, "/etc/passwd")

    def test_symlink_resolved(self, tmp_path: Path) -> None:
        """シンボリックリンクが base 内を指す場合は OK。"""
        real_file = tmp_path / "real.txt"
        real_file.write_text("hello")
        link = tmp_path / "link.txt"
        link.symlink_to(real_file)
        result = safe_join(tmp_path, "link.txt")
        assert result == real_file.resolve()

    def test_symlink_outside_rejected(self, tmp_path: Path) -> None:
        """シンボリックリンクが base 外を指す場合はエラー。"""
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("outside")
        link = tmp_path / "evil_link.txt"
        link.symlink_to(outside)
        with pytest.raises(ValueError, match="不正なパス"):
            safe_join(tmp_path, "evil_link.txt")

    def test_repr_used_in_error_message(self, tmp_path: Path) -> None:
        """エラーメッセージに repr(name) が使われる（制御文字の可視化）。"""
        name = "../evil\ttab"
        with pytest.raises(ValueError) as exc_info:
            safe_join(tmp_path, name)
        assert repr(name) in str(exc_info.value)


class TestValidateSha256Format:
    """validate_sha256_format のテスト。"""

    def test_valid_lowercase(self) -> None:
        """小文字の 64 桁 hex は通る。"""
        validate_sha256_format("a" * 64, "test")

    def test_valid_uppercase(self) -> None:
        """大文字の 64 桁 hex も通る（int(value,16) で正規化）。"""
        validate_sha256_format("A" * 64, "test")

    def test_valid_mixed(self) -> None:
        """大文字小文字混在の 64 桁 hex は通る。"""
        validate_sha256_format("aAbB" * 16, "test")

    def test_wrong_length_short(self) -> None:
        """63 桁はエラー。"""
        with pytest.raises(ValueError, match="長さ"):
            validate_sha256_format("a" * 63, "test")

    def test_wrong_length_long(self) -> None:
        """65 桁はエラー。"""
        with pytest.raises(ValueError, match="長さ"):
            validate_sha256_format("a" * 65, "test")

    def test_non_hex_char(self) -> None:
        """16進数でない文字はエラー。"""
        with pytest.raises(ValueError, match="16進数"):
            validate_sha256_format("g" * 64, "test")

    def test_label_in_error(self) -> None:
        """エラーメッセージにラベルが含まれる。"""
        with pytest.raises(ValueError, match="my_label"):
            validate_sha256_format("x" * 64, "my_label")


class TestSha256File:
    """sha256_file のテスト。"""

    def test_correct_hash(self, tmp_path: Path) -> None:
        """ファイルの SHA256 が hashlib と一致する。"""
        import hashlib

        data = b"hello world" * 1000
        f = tmp_path / "test.bin"
        f.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        assert sha256_file(f) == expected

    def test_empty_file(self, tmp_path: Path) -> None:
        """空ファイルの SHA256 は既知の値と一致する。"""
        import hashlib

        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        expected = hashlib.sha256(b"").hexdigest()
        assert sha256_file(f) == expected
