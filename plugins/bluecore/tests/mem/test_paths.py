"""bluecore.mem._paths のユニットテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bluecore.mem._paths import sha256_file, validate_sha256_format


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
