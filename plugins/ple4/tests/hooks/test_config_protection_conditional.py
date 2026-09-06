"""config_protection の CONDITIONALLY_PROTECTED_FILES（R-07）のテスト。

pyproject.toml 等はファイル名だけでは保護できない（version bump 等の
正当な編集が頻繁なため）。編集内容が lint/format/coverage 設定を弱め
うる場合のみブロックする。判定は主（ディスク上のセクション解決）・
副（テキスト照合）の和集合。
"""

from __future__ import annotations

import json
import pathlib
import time

import pytest

from ple4.hooks import config_protection


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


class TestToxIni:
    """A-04: tox.ini の `[testenv]`/`[testenv:*]` の commands 系キー検出。"""

    def test_blocks_write_with_testenv_commands(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _run(
            monkeypatch,
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "tox.ini", "content": "[testenv]\ncommands = pytest --no-cov\n"},
            },
        )
        assert code == 2

    def test_blocks_named_subenvironment_via_prefix_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`[testenv:py312]` のようなサブ環境見出しも前方一致で拾う。"""
        code = _run(
            monkeypatch,
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "tox.ini",
                    "content": "[testenv:py312]\ncommands = pytest --no-cov\n",
                },
            },
        )
        assert code == 2

    def test_allows_version_or_description_only_edit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """正当な非コマンド編集（description 等）は許可する。"""
        code = _run(
            monkeypatch,
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "tox.ini", "content": "[tox]\ndescription = 'ci'\n"},
            },
        )
        assert code == 0

    def test_blocks_commands_pre_and_commands_post(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in ("commands_pre", "commands_post"):
            code = _run(
                monkeypatch,
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": "tox.ini", "content": f"[testenv]\n{key} = echo ok\n"},
                },
            )
            assert code == 2, key

    def test_blocks_commands_value_line_without_header_via_key_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """見出しを含まない値行編集（`commands = ...` のみ）も tox.ini 専用キー照合で拾う。"""
        code = _run(
            monkeypatch,
            {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "tox.ini",
                    "old_string": "commands = pytest",
                    "new_string": "commands = pytest --no-cov",
                },
            },
        )
        assert code == 2

    def test_pyproject_toml_commands_key_is_not_blocked_globally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """tox.ini 限定分岐のため、他ファイルの `commands` という語自体は誤検出しない。"""
        code = _run(
            monkeypatch,
            {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "pyproject.toml",
                    "old_string": 'description = "old commands here"',
                    "new_string": 'description = "new commands here"',
                },
            },
        )
        assert code == 0

    def test_blocks_when_old_string_resolves_to_testenv_section_on_disk(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """見出しがスニペットに現れなくても、ディスク上の現在の内容から `[testenv:*]` 所属を解決する（主判定）。"""
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "tox.ini"
        target.write_text(
            "[tox]\nenvlist = py312\n\n[testenv:py312]\ncommands = pytest\nsome_new_key = 1\n",
            encoding="utf-8",
        )
        code = _run(
            monkeypatch,
            {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "tox.ini",
                    "old_string": "some_new_key = 1",
                    "new_string": "some_new_key = 2",
                },
            },
        )
        assert code == 2

    def test_allows_when_old_string_resolves_to_non_testenv_section_on_disk(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "tox.ini"
        target.write_text("[tox]\nenvlist = py312\ndescription = 'old'\n", encoding="utf-8")
        code = _run(
            monkeypatch,
            {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "tox.ini",
                    "old_string": "description = 'old'",
                    "new_string": "description = 'new'",
                },
            },
        )
        assert code == 0


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
        """読み取りが失敗したら判定不能（読み取りは open + 上限付き read で行う）。"""
        target = tmp_path / "pyproject.toml"
        target.write_text("[project]\nname = 'x'\n", encoding="utf-8")

        def _boom(*args, **kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr("pathlib.Path.open", _boom)
        assert config_protection._resolve_lint_section_from_disk(str(target), "name") is None

    def test_read_is_capped_at_max_stdin_bytes(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """読み取り量に上限があること（無制限だと hook timeout を踏んで allow へ倒れる）。"""
        target = tmp_path / "pyproject.toml"
        target.write_text("[tool.ruff]\nignore = ['E']\n", encoding="utf-8")
        requested: list[int | None] = []
        real_open = pathlib.Path.open

        def _spy_open(self, *args, **kwargs):
            handle = real_open(self, *args, **kwargs)
            real_read = handle.read

            def _read(size=None):
                requested.append(size)
                return real_read(size)

            handle.read = _read
            return handle

        monkeypatch.setattr("pathlib.Path.open", _spy_open)
        config_protection._resolve_lint_section_from_disk(str(target), "ignore")

        assert requested == [config_protection.MAX_STDIN_BYTES]

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


class TestLintKeyPatternIsLinear:
    """行頭キー照合が入力長に対して線形であること（H-7a）。

    修正前の `(?m)^\\s*(...)\\s*=` は `\\s` が改行を含むため、**一致しない**
    空白主体テキストに対して「各行頭 × 残り空白長」で後退し二次オーダーに
    なっていた（実測: 改行だけの入力で N=5,000 → 0.105s、N=20,000 → 1.619s、
    N=50,000 → 10.187s）。config_protection の hooks.json timeout は 15 秒
    なので約 60,000 バイトの改行だけで超過し、host にフックを殺されれば
    allow へ倒れる＝保護そのものが無効化される。

    判定は入力 2 サイズ間の比ではなく**絶対的な壁時計の上限**で行う。比の
    アサートは小さい N ではノイズ支配で他マシンで flake するため。
    """

    # 修正後は 200,000 改行で 0.01 秒未満。修正前は同じ入力で 100 秒超に
    # なるため、200 倍以上の余裕を取っても二次オーダーへの回帰は必ず捕まる。
    _CEILING_SECONDS = 2.0
    _SIZE = 200_000

    @pytest.mark.parametrize(
        "pattern_name", ["_LINT_KEY_PATTERN", "_TOX_COMMAND_KEY_PATTERN"]
    )
    def test_whitespace_only_input_returns_quickly(self, pattern_name: str) -> None:
        """改行だけの入力（最悪ケース）が上限時間内に一致なしで返る。"""
        pattern = getattr(config_protection, pattern_name)
        text = "\n" * self._SIZE

        started = time.monotonic()
        match = pattern.search(text)
        elapsed = time.monotonic() - started

        assert match is None
        assert elapsed < self._CEILING_SECONDS, (
            f"{pattern_name} took {elapsed:.3f}s for {self._SIZE} newlines "
            f"(二次オーダーへ回帰した疑い)"
        )

    def test_mixed_indent_input_returns_quickly(self) -> None:
        """行頭インデント付きの非一致テキストでも線形であること。"""
        text = ("    \t" * 20 + "\n") * 4_000

        started = time.monotonic()
        match = config_protection._LINT_KEY_PATTERN.search(text)
        elapsed = time.monotonic() - started

        assert match is None
        assert elapsed < self._CEILING_SECONDS

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("ignore = []", True),
            ("  fail_under = 80", True),
            ("\tselect = [\"E\"]", True),
            ("addopts\t=\t-q", True),
            ("nothing = 1", False),
        ],
    )
    def test_detection_is_preserved(self, text: str, expected: bool) -> None:
        """行頭空白を `[ \\t]` へ絞っても行頭キーの検出は変わらないこと。

        TOML / INI ともキーと `=` は同じ行に無ければならないため、改行を
        跨ぐ形を落としても正当な設定行の取りこぼしは生じない。
        """
        assert bool(config_protection._LINT_KEY_PATTERN.search(text)) is expected


class TestLintSignalTextLengthCap:
    """`_text_has_lint_signal` の走査量上限（H-7a）。"""

    def test_over_cap_text_blocks_fail_closed(self) -> None:
        """上限超過のテキストは走査せず True（ブロック）を返す。

        打ち切って走査を続ける（truncate）形にすると、上限より後ろに lint
        キーを置くだけで条件付き保護を素通りできる新しいバイパスになるため、
        fail-closed 側へ倒す。
        """
        text = "x" * (config_protection._LINT_SIGNAL_MAX_TEXT_BYTES + 1)

        assert config_protection._text_has_lint_signal(text, "pyproject.toml") is True

    def test_large_but_harmless_text_under_cap_is_not_blocked(self) -> None:
        """上限内の大きな非 lint テキストは従来どおり False（陰性対照）。

        上限を入れたことで「大きいだけの正当な書き込み」まで deny へ倒れて
        いないことを固定する。
        """
        text = "x" * (config_protection._LINT_SIGNAL_MAX_TEXT_BYTES - 1)

        assert config_protection._text_has_lint_signal(text, "pyproject.toml") is False
