"""redux ローダ（loader.py）のユニットテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bluecore.redux import loader
from bluecore.redux.engine import ReduxEngine, apply_spec


def _write(path: Path, content: str) -> Path:
    """TOML 文字列をファイルに書いてパスを返す。"""
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _build_spec
# ---------------------------------------------------------------------------


class TestBuildSpec:
    def test_full_spec(self) -> None:
        spec = loader._build_spec(
            "demo",
            {
                "description": "d",
                "command_pattern": "^demo",
                "strip_ansi": True,
                "substitute": [{"pattern": r"\d+", "replacement": "N"}],
                "short_circuit": [{"pattern": "OK", "message": "ok", "unless": "ERR"}],
                "drop_lines": [r"^DEBUG"],
                "clip_width": 80,
                "head_lines": 5,
                "tail_lines": 5,
                "limit_lines": 30,
                "empty_message": "（空）",
                "strategies": ["smart_filter"],
            },
        )
        assert spec.name == "demo"
        assert spec.command_pattern.search("demo run")
        assert spec.strip_ansi is True
        assert spec.substitute[0].replacement == "N"
        assert spec.short_circuit[0].unless is not None
        assert spec.clip_width == 80
        assert spec.strategies == ["smart_filter"]
        # apply 経由で動作確認
        assert apply_spec(spec, "id 12 and 34") == "id N and N"

    def test_short_circuit_without_unless(self) -> None:
        spec = loader._build_spec(
            "x", {"command_pattern": ".", "short_circuit": [{"pattern": "OK", "message": "ok"}]}
        )
        assert spec.short_circuit[0].unless is None

    def test_keep_lines(self) -> None:
        spec = loader._build_spec("x", {"command_pattern": ".", "keep_lines": [r"ERROR"]})
        assert apply_spec(spec, "ok\nERROR\nok2") == "ERROR"

    def test_drop_keep_mutually_exclusive(self) -> None:
        with pytest.raises(ValueError, match="併用できない"):
            loader._build_spec("x", {"command_pattern": ".", "drop_lines": ["a"], "keep_lines": ["b"]})

    def test_unknown_strategy_rejected(self) -> None:
        with pytest.raises(ValueError, match="未知の戦略"):
            loader._build_spec("x", {"command_pattern": ".", "strategies": ["bogus"]})

    def test_long_command_pattern_rejected_when_untrusted(self) -> None:
        long_pat = "a" * (loader._MAX_USER_PATTERN_LEN + 1)
        with pytest.raises(ValueError, match="長すぎます"):
            loader._build_spec("x", {"command_pattern": long_pat}, trusted=False)

    def test_long_pattern_allowed_when_trusted(self) -> None:
        # 組込（trusted=True）は長さ検証をスキップする
        long_pat = "a" * (loader._MAX_USER_PATTERN_LEN + 1)
        spec = loader._build_spec("x", {"command_pattern": long_pat}, trusted=True)
        assert spec.name == "x"

    def test_pattern_length_check_scans_all_fields(self) -> None:
        # 全種別のパターン（短い）を含み検証を通過する（収集経路を網羅）
        spec = loader._build_spec(
            "x",
            {
                "command_pattern": "^x",
                "substitute": [{"pattern": r"\d", "replacement": "N"}],
                "short_circuit": [{"pattern": "OK", "message": "m", "unless": "ERR"}],
                "drop_lines": [r"^D"],
            },
            trusted=False,
        )
        assert spec.name == "x"

    def test_pattern_length_check_keep_and_no_unless(self) -> None:
        # keep_lines 経路と unless 無し short_circuit を網羅
        spec = loader._build_spec(
            "x",
            {
                "command_pattern": "^x",
                "short_circuit": [{"pattern": "OK", "message": "m"}],
                "keep_lines": [r"ERR"],
            },
            trusted=False,
        )
        assert spec.name == "x"


# ---------------------------------------------------------------------------
# _parse_toml
# ---------------------------------------------------------------------------


class TestParseToml:
    def test_parses_filters_and_cases(self, tmp_path: Path) -> None:
        toml_text = (
            "schema_version = 1\n"
            '[filters.ps]\n'
            'command_pattern = "^ps"\n'
            'limit_lines = 2\n'
            '[[cases.ps]]\n'
            'name = "trim"\n'
            'input = "a\\nb\\nc"\n'
            'expected = "a\\nb\\n... (1 行切り捨て)"\n'
        )
        path = _write(tmp_path / "f.toml", toml_text)
        specs, cases = loader._parse_toml(path)
        assert specs[0].name == "ps"
        assert cases[0].filter_name == "ps"
        assert cases[0].name == "trim"

    def test_bad_schema_version(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "f.toml", "schema_version = 99\n")
        with pytest.raises(ValueError, match="schema_version"):
            loader._parse_toml(path)


# ---------------------------------------------------------------------------
# パス解決 / 統合ロード
# ---------------------------------------------------------------------------


class TestPaths:
    def test_user_filter_paths_order(self) -> None:
        paths = loader._user_filter_paths()
        # プロジェクト（cwd）がユーザー全体（home）より先
        assert paths[0] == Path.cwd() / ".bluecore" / "redux" / "filters.toml"
        assert paths[1] == Path.home() / ".bluecore" / "redux" / "filters.toml"

    def test_builtin_paths_default_last(self) -> None:
        paths = loader.builtin_filter_paths()
        assert paths[-1].name == "default.toml"
        assert all(p.name != "default.toml" for p in paths[:-1])

    def test_builtin_paths_without_default(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _write(tmp_path / "a.toml", "schema_version = 1\n")
        monkeypatch.setattr(loader, "_BUILTIN_DIR", tmp_path)
        paths = loader.builtin_filter_paths()
        assert [p.name for p in paths] == ["a.toml"]


class TestLoadFilterSpecs:
    def test_builtin_only_when_no_user_files(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(loader, "_user_filter_paths", lambda: [])
        specs = loader.load_filter_specs()
        assert any(s.name == "default" for s in specs)
        # default は catch-all なので末尾
        assert specs[-1].name == "default"

    def test_user_file_prepended(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        user = _write(
            tmp_path / "filters.toml",
            'schema_version = 1\n[filters.mine]\ncommand_pattern = "^mycmd"\nlimit_lines = 1\n',
        )
        monkeypatch.setattr(loader, "_user_filter_paths", lambda: [user])
        specs = loader.load_filter_specs()
        # ユーザー定義が先頭
        assert specs[0].name == "mine"
        assert specs[-1].name == "default"

    def test_user_path_missing_is_skipped(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        missing = tmp_path / "nope.toml"
        monkeypatch.setattr(loader, "_user_filter_paths", lambda: [missing])
        specs = loader.load_filter_specs()
        assert all(s.name != "mine" for s in specs)

    def test_user_parse_error_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # 不正な TOML はスキップし、組込フィルタは無効化されない
        bad = _write(tmp_path / "filters.toml", "= not valid =")
        monkeypatch.setattr(loader, "_user_filter_paths", lambda: [bad])
        specs = loader.load_filter_specs()
        assert any(s.name == "default" for s in specs)
        assert "ユーザーフィルタを無視" in capsys.readouterr().err

    def test_user_long_pattern_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        long_pat = "a" * (loader._MAX_USER_PATTERN_LEN + 1)
        user = _write(
            tmp_path / "filters.toml",
            f'schema_version = 1\n[filters.mine]\ncommand_pattern = "{long_pat}"\n',
        )
        monkeypatch.setattr(loader, "_user_filter_paths", lambda: [user])
        specs = loader.load_filter_specs()
        assert all(s.name != "mine" for s in specs)
        assert "ユーザーフィルタを無視" in capsys.readouterr().err


class TestEngineLoad:
    def test_load_builds_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(loader, "_user_filter_paths", lambda: [])
        eng = ReduxEngine.load()
        assert any(s.name == "default" for s in eng.specs)
        # 未知コマンドは default の汎用戦略で圧縮
        out = eng.reduce("randomcmd", "npm warn deprecated\nkeep this")
        assert "npm warn" not in out
        assert "keep this" in out
