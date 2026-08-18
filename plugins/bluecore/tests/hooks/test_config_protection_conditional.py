"""config_protection の CONDITIONALLY_PROTECTED_FILES（R-07）のテスト。

pyproject.toml 等はファイル名だけでは保護できない（version bump 等の
正当な編集が頻繁なため）。編集内容が lint/format/coverage 設定を弱め
うる場合のみブロックする。判定は主（ディスク上のセクション解決）・
副（テキスト照合）の和集合。
"""

from __future__ import annotations

import json

import pytest

from bluecore.hooks import config_protection


def _run(monkeypatch: pytest.MonkeyPatch, payload: dict) -> int:
    """config_protection.main() を実行し終了コードを返す。"""
    monkeypatch.setattr(config_protection, "read_raw_stdin_with_truncation", lambda: (json.dumps(payload), False))
    return config_protection.main()


class TestWrite:
    """Write（新規全内容）に対する判定。"""

    def test_blocks_content_with_lint_section_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _run(
            monkeypatch,
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "pyproject.toml", "content": "[tool.ruff]\nignore=[]"},
            },
        )
        assert code == 2

    def test_allows_version_bump_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _run(
            monkeypatch,
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "pyproject.toml", "content": '[project]\nversion="0.9.32"'},
            },
        )
        assert code == 0

    def test_blocks_setup_cfg_flake8_section(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _run(
            monkeypatch,
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "setup.cfg", "content": "[flake8]\nmax-line-length = 200"},
            },
        )
        assert code == 2

    def test_blocks_package_json_eslint_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _run(
            monkeypatch,
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "package.json", "content": '{"eslintConfig": {"rules": {}}}'},
            },
        )
        assert code == 2

    def test_allows_package_json_without_lint_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _run(
            monkeypatch,
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "package.json", "content": '{"name": "x", "version": "1.0.1"}'},
            },
        )
        assert code == 0

    def test_blocks_when_content_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """content が無い（取得不能）場合は fail-closed で deny する。"""
        code = _run(
            monkeypatch,
            {"tool_name": "Write", "tool_input": {"file_path": "pyproject.toml"}},
        )
        assert code == 2


class TestEdit:
    """Edit（old_string/new_string の断片のみ）に対する判定。"""

    def test_blocks_header_visible_in_new_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _run(
            monkeypatch,
            {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "pyproject.toml",
                    "old_string": "[tool.pytest.ini_options]\naddopts = '-v'",
                    "new_string": "[tool.pytest.ini_options]\naddopts = ''",
                },
            },
        )
        assert code == 2

    def test_blocks_value_line_without_header_via_key_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """見出しを含まない値行編集（fail_under 100→80）も副判定のキー照合で拾う。"""
        code = _run(
            monkeypatch,
            {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "pyproject.toml",
                    "old_string": "fail_under = 100",
                    "new_string": "fail_under = 80",
                },
            },
        )
        assert code == 2

    def test_allows_unrelated_value_edit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _run(
            monkeypatch,
            {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "pyproject.toml",
                    "old_string": 'version = "0.9.31"',
                    "new_string": 'version = "0.9.32"',
                },
            },
        )
        assert code == 0

    def test_blocks_when_old_string_resolves_to_lint_section_on_disk(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """見出しがスニペットに現れなくても、ディスク上の現在の内容から所属セクションを解決する（主判定）。"""
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "pyproject.toml"
        target.write_text(
            "[project]\nname = 'x'\n\n[tool.ruff.lint]\nselect = ['E']\nsome_new_key = 1\n",
            encoding="utf-8",
        )
        code = _run(
            monkeypatch,
            {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "pyproject.toml",
                    "old_string": "some_new_key = 1",
                    "new_string": "some_new_key = 2",
                },
            },
        )
        assert code == 2

    def test_allows_when_old_string_resolves_to_non_lint_section_on_disk(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """ディスク上で lint 系以外のセクションに属すると判定できれば、その断片は主判定でブロックしない。"""
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "pyproject.toml"
        target.write_text("[project]\nname = 'x'\ndescription = 'old'\n", encoding="utf-8")
        code = _run(
            monkeypatch,
            {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "pyproject.toml",
                    "old_string": "description = 'old'",
                    "new_string": "description = 'new'",
                },
            },
        )
        assert code == 0

    def test_blocks_when_old_string_and_new_string_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _run(
            monkeypatch,
            {"tool_name": "Edit", "tool_input": {"file_path": "pyproject.toml"}},
        )
        assert code == 2


class TestMultiEdit:
    def test_blocks_when_any_edit_touches_lint_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _run(
            monkeypatch,
            {
                "tool_name": "MultiEdit",
                "tool_input": {
                    "file_path": "pyproject.toml",
                    "edits": [
                        {"old_string": 'version = "0.9.31"', "new_string": 'version = "0.9.32"'},
                        {"old_string": "ignore = ['E501']", "new_string": "ignore = ['E501', 'F401']"},
                    ],
                },
            },
        )
        assert code == 2

    def test_allows_when_no_edit_touches_lint_signal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _run(
            monkeypatch,
            {
                "tool_name": "MultiEdit",
                "tool_input": {
                    "file_path": "pyproject.toml",
                    "edits": [
                        {"old_string": 'version = "0.9.31"', "new_string": 'version = "0.9.32"'},
                    ],
                },
            },
        )
        assert code == 0

    def test_blocks_when_edits_field_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _run(
            monkeypatch,
            {"tool_name": "MultiEdit", "tool_input": {"file_path": "pyproject.toml"}},
        )
        assert code == 2


class TestApplyPatch:
    def test_blocks_patch_text_with_lint_section(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_text = (
            "*** Begin Patch\n"
            "*** Update File: pyproject.toml\n"
            "@@\n"
            "-[tool.coverage.report]\n"
            "-fail_under = 100\n"
            "+[tool.coverage.report]\n"
            "+fail_under = 50\n"
            "*** End Patch\n"
        )
        code = _run(monkeypatch, {"tool_name": "apply_patch", "tool_input": patch_text})
        assert code == 2

    def test_allows_patch_text_without_lint_signal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_text = (
            "*** Begin Patch\n"
            "*** Update File: pyproject.toml\n"
            "@@\n"
            '-version = "0.9.31"\n'
            '+version = "0.9.32"\n'
            "*** End Patch\n"
        )
        code = _run(monkeypatch, {"tool_name": "apply_patch", "tool_input": patch_text})
        assert code == 0


class TestEditableTextUnitLevel:
    """_editable_text の分岐を直接叩く単体テスト（実 JSON 経路では組み立てにくい枝）。"""

    def test_apply_patch_dict_container_with_input_key(self) -> None:
        result = config_protection._editable_text("apply_patch", {"input": "*** patch ***"})
        assert result == ["*** patch ***"]

    def test_apply_patch_dict_container_without_input_key(self) -> None:
        assert config_protection._editable_text("apply_patch", {"other": "x"}) is None

    def test_apply_patch_container_wrong_type(self) -> None:
        assert config_protection._editable_text("apply_patch", 123) is None

    def test_non_apply_patch_container_not_dict(self) -> None:
        """Edit 系で container が dict でない（想定外の shape）場合は判定不能扱い。"""
        assert config_protection._editable_text("Edit", "raw string, not dict") is None


class TestResolveLintSectionFromDiskUnitLevel:
    """_resolve_lint_section_from_disk の分岐を直接叩く単体テスト。"""

    def test_empty_snippet_is_undetermined(self) -> None:
        assert config_protection._resolve_lint_section_from_disk("pyproject.toml", "") is None

    def test_read_failure_is_undetermined(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        target = tmp_path / "pyproject.toml"
        target.write_text("[project]\nname = 'x'\n", encoding="utf-8")

        def _boom(*args, **kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr("pathlib.Path.read_text", _boom)
        assert config_protection._resolve_lint_section_from_disk(str(target), "name") is None

    def test_snippet_before_any_section_header_is_not_lint(self, tmp_path) -> None:
        """先頭セクション見出しより前（トップレベル）に見つかった場合は lint 系ではない。"""
        target = tmp_path / "setup.cfg"
        target.write_text("# leading comment\ntop_level_marker\n[flake8]\nmax-line-length = 100\n", encoding="utf-8")
        result = config_protection._resolve_lint_section_from_disk(str(target), "top_level_marker")
        assert result is False


class TestUnknownTool:
    def test_unrecognized_write_tool_shape_is_treated_as_edit_and_denied_when_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """既知の write/multiedit 以外（例: search_replace）は Edit 相当として扱う。"""
        code = _run(
            monkeypatch,
            {
                "tool_name": "search_replace",
                "tool_input": {
                    "file_path": "pyproject.toml",
                    "old_string": "select = ['E']",
                    "new_string": "select = []",
                },
            },
        )
        assert code == 2
