"""devgear.ci.check_unicode_safety のテスト。"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

from devgear.ci import check_unicode_safety


def test_unicode_helper_functions() -> None:
    ranges = sorted(check_unicode_safety.DANGEROUS_INVISIBLE_RANGES)
    starts = [start for start, _ in ranges]
    assert check_unicode_safety._in_ranges(0x200B, ranges, starts)
    assert not check_unicode_safety._in_ranges(0x61, ranges, starts)
    assert check_unicode_safety._is_dangerous_invisible(0xFEFF)
    assert not check_unicode_safety._is_dangerous_invisible(0x61)
    assert check_unicode_safety._is_emoji_like(ord("🙂"))
    assert not check_unicode_safety._is_emoji_like(ord("a"))
    assert check_unicode_safety._code_point_hex(0x1F642) == "U+1F642"


def test_text_and_file_helpers(tmp_path: Path) -> None:
    assert check_unicode_safety.should_skip(tmp_path / "node_modules" / "x.md")
    assert check_unicode_safety.should_skip(tmp_path / ".git" / "x.md")
    assert check_unicode_safety.is_text_file("x.MD")
    assert not check_unicode_safety.is_text_file("x.bin")
    assert check_unicode_safety.can_auto_write("x.txt")
    assert not check_unicode_safety.can_auto_write("x.py")

    (tmp_path / "keep").mkdir()
    (tmp_path / "keep" / "a.md").write_text("a\n", encoding="utf-8")
    (tmp_path / "keep" / "b.txt").write_text("b\n", encoding="utf-8")
    (tmp_path / "keep" / "c.bin").write_bytes(b"bin")
    (tmp_path / "keep" / "node_modules").mkdir()
    (tmp_path / "keep" / "node_modules" / "ignored.md").write_text("x\n", encoding="utf-8")

    files = check_unicode_safety.list_files(tmp_path)
    assert sorted(Path(item).name for item in files) == ["a.md", "b.txt"]


def test_sanitize_and_collect_matches() -> None:
    text = "  ⚠️  Zero\u200bWidth\n🙂\n©\n"
    assert check_unicode_safety.strip_dangerous_invisible_chars(text) == "  ⚠  ZeroWidth\n🙂\n©\n"
    sanitized = check_unicode_safety.sanitize_text(text)
    assert "WARNING:" in sanitized
    assert "ZeroWidth" in sanitized
    assert "🙂" not in sanitized

    invisible_matches = check_unicode_safety.collect_dangerous_invisible_matches("a\n\uFEFFb")
    assert invisible_matches[0]["line"] == 2
    assert invisible_matches[0]["column"] == 1
    assert invisible_matches[0]["codePoint"] == "U+FEFF"

    emoji_matches = check_unicode_safety.collect_emoji_matches("a\n🙂")
    assert emoji_matches[0]["line"] == 2
    assert emoji_matches[0]["column"] == 1
    assert emoji_matches[0]["codePoint"] == "U+1F642"


def test_validate_unicode_safety_write_and_violation_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clean_root = tmp_path / "clean"
    clean_root.mkdir()
    (clean_root / "README.md").write_text("plain text\n", encoding="utf-8")
    assert check_unicode_safety.validate_unicode_safety(clean_root) == 0
    assert "Unicode 安全性チェックに合格しました。" in capsys.readouterr().out

    write_root = tmp_path / "write"
    write_root.mkdir()
    doc = write_root / "README.md"
    doc.write_text("⚠️  Zero\u200bWidth ✅\n", encoding="utf-8")
    assert check_unicode_safety.validate_unicode_safety(write_root, write_mode=True) == 0
    output = capsys.readouterr()
    assert "1 個のファイルをサニタイズしました" in output.out
    assert doc.read_text(encoding="utf-8").startswith("WARNING:")
    assert "PASS:" in doc.read_text(encoding="utf-8")

    violation_root = tmp_path / "violations"
    violation_root.mkdir()
    (violation_root / "data.json").write_text("🙂\u200b\n", encoding="utf-8")
    assert check_unicode_safety.validate_unicode_safety(violation_root) == 1
    assert "Unicode 安全性の違反が検出されました:" in capsys.readouterr().err


def test_unicode_list_files_normalize_and_main_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert check_unicode_safety._normalize_relative_path(tmp_path / "a/b") == str(tmp_path / "a/b")

    class FakeEntry:
        def __init__(self, path: str, kind: str) -> None:
            self.path = path
            self._kind = kind

        def is_dir(self, follow_symlinks: bool = False) -> bool:  # noqa: ARG002
            if self._kind == "error":
                raise OSError("boom")
            return self._kind == "dir"

        def is_file(self, follow_symlinks: bool = False) -> bool:  # noqa: ARG002
            return self._kind == "file"

    def fake_scandir(_path: Path) -> list[FakeEntry]:
        return [FakeEntry(str(tmp_path / "good.md"), "file"), FakeEntry(str(tmp_path / "broken"), "error")]

    monkeypatch.setattr(check_unicode_safety.os, "scandir", fake_scandir)
    assert check_unicode_safety.list_files(tmp_path) == [str(tmp_path / "good.md")]

    monkeypatch.setattr(check_unicode_safety, "validate_unicode_safety", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    assert check_unicode_safety.main(["--root", str(tmp_path)]) == 1
    assert "エラー: boom" in capsys.readouterr().err


def test_validate_unicode_safety_skips_unreadable_files_and_entrypoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "README.md").write_text("safe\n", encoding="utf-8")

    original_read_text = check_unicode_safety.Path.read_text

    def fake_read_text(self, *args, **kwargs):  # noqa: ANN001
        if self == root / "README.md":
            raise OSError("boom")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(check_unicode_safety.Path, "read_text", fake_read_text)
    assert check_unicode_safety.validate_unicode_safety(root) == 0

    monkeypatch.setattr(
        sys,
        "argv",
        ["check_unicode_safety.py", "--root", str(root)],
    )

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("devgear.ci.check_unicode_safety", run_name="__main__")

    assert excinfo.value.code == 0
