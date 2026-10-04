"""旧仕様の API 名・機構の走査（smoke_static.py の legacy_api_hits と、smoke_check.py の test_no_legacy_api）のテスト。"""

from __future__ import annotations

import os
import types
import warnings
from pathlib import Path
from typing import Any

import pytest

from ._support import _SKILL_DIR, _TEMPLATE


def test_assets_have_no_legacy_api(smoke_static: types.ModuleType) -> None:
    """スケルトン一式が、smoke_static の旧仕様パターン（正本）に 1 件も当たらない（パターンを持つ検査自身の 2 ファイルは除く）。"""
    skip = (_TEMPLATE / "smoke_check.py", _TEMPLATE / "smoke_static.py")
    assert smoke_static.legacy_api_hits(_SKILL_DIR / "assets", skip=skip) == ([], [])


class TestLegacyApiScan:
    """旧仕様の API 名・機構の走査。"""

    def test_finds_hits_and_skips_outside_files(self, smoke_static: types.ModuleType, tmp_path: Path) -> None:
        """生成先のファイルの当たりを返し、仮想環境・キャッシュ・バイナリ・検査自身（smoke_check.py と smoke_static.py）は見ない。"""
        legacy = "from mcp.server import Fast" + "MCP\n"
        (tmp_path / "server.py").write_text("print('ok')\n" + legacy, encoding="utf-8")
        (tmp_path / "README.md").write_text("ctx" + ".info(x)\n", encoding="utf-8")
        for skipped in (".venv", "venv", "__pycache__", "pkg.egg-info"):
            (tmp_path / skipped).mkdir()
            (tmp_path / skipped / "x.py").write_text(legacy, encoding="utf-8")
        (tmp_path / "blob.pyc").write_bytes(b"\xff\xfe" + legacy.encode())
        checkers = (tmp_path / "smoke_check.py", tmp_path / "smoke_static.py")
        for checker in checkers:
            checker.write_text(legacy, encoding="utf-8")
        assert smoke_static.legacy_api_hits(tmp_path, skip=checkers) == (
            ["README.md:1: ctx" + ".info(x)", "server.py:2: " + legacy.strip()],
            [],
        )

    def test_clean_tree_has_no_hits(self, smoke_static: types.ModuleType, tmp_path: Path) -> None:
        """旧仕様の名前が無ければ、当たりも走査しなかったファイルも空を返す。"""
        (tmp_path / "server.py").write_text("from mcp.server import MCPServer\n", encoding="utf-8")
        assert smoke_static.legacy_api_hits(tmp_path, skip=(tmp_path / "smoke_check.py",)) == ([], [])

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO を作れない OS")
    def test_reports_files_it_did_not_scan(self, smoke_static: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """通常のファイルでない（FIFO・シンボリックリンク）・大きすぎる・読めないファイルは、開かずに走査しなかったものとして返す。"""
        legacy = "Fast" + "MCP\n"
        (tmp_path / "server.py").write_text(legacy, encoding="utf-8")
        os.mkfifo(tmp_path / "pipe")
        (tmp_path / "link.py").symlink_to(tmp_path / "server.py")
        (tmp_path / "big.txt").write_text(legacy * 10, encoding="utf-8")
        (tmp_path / "locked.txt").write_text(legacy, encoding="utf-8")
        monkeypatch.setattr(smoke_static, "MAX_SCANNED_BYTES", len(legacy))
        original = Path.read_text

        def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
            """locked.txt だけ、権限の無いファイルとして読めなくする。"""
            if self.name == "locked.txt":
                raise PermissionError(13, "Permission denied", str(self))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)
        hits, unscanned = smoke_static.legacy_api_hits(tmp_path, skip=(tmp_path / "smoke_check.py",))
        assert hits == ["server.py:1: " + legacy.strip()]
        assert [entry.split(" ")[0] for entry in unscanned] == ["big.txt", "link.py", "locked.txt", "pipe"]
        assert "通常のファイルではない" in unscanned[1] and "通常のファイルではない" in unscanned[3]
        assert "Permission denied" in unscanned[2]

    def test_no_legacy_api_warns_unscanned(self, smoke_check: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        """smoke_check.py のディレクトリを、検査自身の 2 ファイル（smoke_check.py と smoke_static.py）を除いて走査する。

        走査しなかったファイルは黙って飛ばさず、SmokeWarning で 1 件ずつ知らせる（検査は落とさない）。
        """
        scanned: list[tuple[Path, set[Path]]] = []

        def legacy_api_hits(root: Path, *, skip: Any) -> tuple[list[str], list[str]]:
            """走査の起点と除くファイルを記録し、走査しなかったファイルを 2 件返す。"""
            scanned.append((root, {Path(path).resolve() for path in skip}))
            return [], ["big.txt (1 MiB を超える)", "pipe (通常のファイルではない)"]

        monkeypatch.setattr(smoke_check.smoke_static, "legacy_api_hits", legacy_api_hits)
        with pytest.warns(smoke_check.SmokeWarning) as caught:
            smoke_check.test_no_legacy_api()
        assert [str(warning.message) for warning in caught] == [
            "旧仕様の名前を走査していません: big.txt (1 MiB を超える)",
            "旧仕様の名前を走査していません: pipe (通常のファイルではない)",
        ]
        own_files = {(_TEMPLATE / "smoke_check.py").resolve(), (_TEMPLATE / "smoke_static.py").resolve()}
        assert scanned == [(_TEMPLATE.resolve(), own_files)]

    def test_main_reports_smoke_warnings(
        self, smoke_check: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """main は SmokeWarning を WARN <検査名>: <文面> と表示し、それ以外の警告は元どおり showwarning に渡す。"""
        shown: list[str] = []

        def noisy() -> None:
            """知らせを 2 種類出して通る。"""
            warnings.warn("見ていないファイル", smoke_check.SmokeWarning, stacklevel=1)
            warnings.warn("別の警告", UserWarning, stacklevel=1)

        monkeypatch.setattr(smoke_check, "CHECKS", (noisy,))
        monkeypatch.setattr(smoke_check, "sdk_protocol_warning", lambda: None)
        monkeypatch.setattr(warnings, "showwarning", lambda message, *args, **kwargs: shown.append(str(message)))
        assert smoke_check.main() == 0
        assert capsys.readouterr().err.splitlines()[1:] == ["PASS noisy", "WARN noisy: 見ていないファイル"]
        assert shown == ["別の警告"]
