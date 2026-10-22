"""config_protection の CONDITIONALLY_PROTECTED_FILES のテスト。

pyproject.toml 等はファイル名だけでは保護できない（version bump 等の
正当な編集が頻繁なため）。編集内容が lint/format/coverage 設定を弱め
うる場合のみブロックする。判定は主（ディスク上のセクション解決）・
副（テキスト照合）の和集合。
"""

from __future__ import annotations

import pathlib
import time
from pathlib import Path
from typing import Any

import pytest

from claq.hooks import config_protection

from ._support import _run, _write_content


def _edit(file_path: str, old_string: str, new_string: str, tool_name: str = "Edit") -> dict[str, Any]:
    """Edit 系ツールの payload を組み立てる。"""
    return {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path, "old_string": old_string, "new_string": new_string},
    }


def _multi_edit(edits: list[dict[str, str]]) -> dict[str, Any]:
    """pyproject.toml への MultiEdit の payload を組み立てる。"""
    return {"tool_name": "MultiEdit", "tool_input": {"file_path": "pyproject.toml", "edits": edits}}


def _apply_patch(*body: str) -> dict[str, Any]:
    """pyproject.toml を更新する apply_patch（tool_input が生のパッチ文字列）の payload を組み立てる。"""
    patch = "*** Begin Patch\n*** Update File: pyproject.toml\n@@\n" + "".join(body) + "*** End Patch\n"
    return {"tool_name": "apply_patch", "tool_input": patch}


_VERSION_EDIT = {"old_string": 'version = "0.9.31"', "new_string": 'version = "0.9.32"'}

# (payload, ディスク上の現在の内容, 期待 exit code)。
# ディスク内容が None の行は cwd を動かさない（プラグインルートの実ファイルを主判定が読む経路のまま）。
# 文字列の行は tmp へ移って file_path にその内容を置き、主判定（所属セクションの解決）を効かせる。
_CASES = [
    # --- Write（新規全内容） ---
    pytest.param(_write_content("pyproject.toml", "[tool.ruff]\nignore=[]"), None, 2, id="write-lint-section-header"),
    pytest.param(_write_content("pyproject.toml", '[project]\nversion="0.9.32"'), None, 0, id="write-version-bump-only"),
    pytest.param(
        _write_content("setup.cfg", "[flake8]\nmax-line-length = 200"), None, 2, id="write-setup-cfg-flake8-section"
    ),
    pytest.param(
        _write_content("package.json", '{"eslintConfig": {"rules": {}}}'), None, 2, id="write-package-json-eslint-config"
    ),
    pytest.param(
        _write_content("package.json", '{"name": "x", "version": "1.0.1"}'),
        None,
        0,
        id="write-package-json-without-lint-keys",
    ),
    # content が無い（取得不能）場合は fail-closed。
    pytest.param(
        {"tool_name": "Write", "tool_input": {"file_path": "pyproject.toml"}}, None, 2, id="write-content-missing"
    ),
    # --- Edit（old_string/new_string の断片のみ） ---
    pytest.param(
        _edit(
            "pyproject.toml",
            "[tool.pytest.ini_options]\naddopts = '-v'",
            "[tool.pytest.ini_options]\naddopts = ''",
        ),
        None,
        2,
        id="edit-header-visible-in-new-string",
    ),
    # 見出しを含まない値行編集（fail_under 100→80）も副判定のキー照合で拾う。
    pytest.param(
        _edit("pyproject.toml", "fail_under = 100", "fail_under = 80"), None, 2, id="edit-value-line-key-match"
    ),
    pytest.param(_edit("pyproject.toml", **_VERSION_EDIT), None, 0, id="edit-unrelated-value"),
    # 見出しがスニペットに現れなくても、ディスク上の現在の内容から所属セクションを解決する（主判定）。
    pytest.param(
        _edit("pyproject.toml", "some_new_key = 1", "some_new_key = 2"),
        "[project]\nname = 'x'\n\n[tool.ruff.lint]\nselect = ['E']\nsome_new_key = 1\n",
        2,
        id="edit-resolves-to-lint-section-on-disk",
    ),
    pytest.param(
        _edit("pyproject.toml", "description = 'old'", "description = 'new'"),
        "[project]\nname = 'x'\ndescription = 'old'\n",
        0,
        id="edit-resolves-to-non-lint-section-on-disk",
    ),
    pytest.param(
        {"tool_name": "Edit", "tool_input": {"file_path": "pyproject.toml"}},
        None,
        2,
        id="edit-old-and-new-string-missing",
    ),
    # 既知の write/multiedit 以外（例: search_replace）は Edit 相当として扱う。
    pytest.param(
        _edit("pyproject.toml", "select = ['E']", "select = []", tool_name="search_replace"),
        None,
        2,
        id="unknown-write-tool-treated-as-edit",
    ),
    # --- tox.ini の `[testenv]`/`[testenv:*]` の commands 系キー ---
    pytest.param(
        _write_content("tox.ini", "[testenv]\ncommands = pytest --no-cov\n"), None, 2, id="tox-write-testenv-commands"
    ),
    # `[testenv:py312]` のようなサブ環境見出しも前方一致で拾う。
    pytest.param(
        _write_content("tox.ini", "[testenv:py312]\ncommands = pytest --no-cov\n"),
        None,
        2,
        id="tox-write-named-subenvironment",
    ),
    pytest.param(_write_content("tox.ini", "[tox]\ndescription = 'ci'\n"), None, 0, id="tox-write-description-only"),
    pytest.param(
        _write_content("tox.ini", "[testenv]\ncommands_pre = echo ok\n"), None, 2, id="tox-write-commands-pre"
    ),
    pytest.param(
        _write_content("tox.ini", "[testenv]\ncommands_post = echo ok\n"), None, 2, id="tox-write-commands-post"
    ),
    # 見出しを含まない値行編集（`commands = ...` のみ）も tox.ini 専用キー照合で拾う。
    pytest.param(
        _edit("tox.ini", "commands = pytest", "commands = pytest --no-cov"), None, 2, id="tox-edit-commands-value-line"
    ),
    # tox.ini 限定分岐のため、他ファイルの `commands` という語自体は誤検出しない。
    pytest.param(
        _edit("pyproject.toml", 'description = "old commands here"', 'description = "new commands here"'),
        None,
        0,
        id="pyproject-commands-word-not-blocked",
    ),
    pytest.param(
        _edit("tox.ini", "some_new_key = 1", "some_new_key = 2"),
        "[tox]\nenvlist = py312\n\n[testenv:py312]\ncommands = pytest\nsome_new_key = 1\n",
        2,
        id="tox-edit-resolves-to-testenv-on-disk",
    ),
    pytest.param(
        _edit("tox.ini", "description = 'old'", "description = 'new'"),
        "[tox]\nenvlist = py312\ndescription = 'old'\n",
        0,
        id="tox-edit-resolves-to-non-testenv-on-disk",
    ),
    # --- MultiEdit ---
    pytest.param(
        _multi_edit([_VERSION_EDIT, {"old_string": "ignore = ['E501']", "new_string": "ignore = ['E501', 'F401']"}]),
        None,
        2,
        id="multiedit-any-edit-touches-lint-key",
    ),
    pytest.param(_multi_edit([_VERSION_EDIT]), None, 0, id="multiedit-no-lint-signal"),
    pytest.param(
        {"tool_name": "MultiEdit", "tool_input": {"file_path": "pyproject.toml"}}, None, 2, id="multiedit-edits-missing"
    ),
    # --- apply_patch ---
    pytest.param(
        _apply_patch("-[tool.coverage.report]\n", "-fail_under = 100\n", "+[tool.coverage.report]\n", "+fail_under = 50\n"),
        None,
        2,
        id="apply-patch-lint-section",
    ),
    pytest.param(
        _apply_patch('-version = "0.9.31"\n', '+version = "0.9.32"\n'), None, 0, id="apply-patch-without-lint-signal"
    ),
]


@pytest.mark.parametrize(("payload", "disk", "expected"), _CASES)
def test_conditional_protection(
    payload: dict[str, Any], disk: str | None, expected: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """編集内容が lint 設定を弱めうるときだけ deny する。"""
    if disk is not None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / payload["tool_input"]["file_path"]).write_text(disk, encoding="utf-8")

    assert _run("config_protection", payload, monkeypatch) == expected


@pytest.mark.parametrize(
    ("tool_name", "container", "expected"),
    [
        pytest.param("apply_patch", {"input": "*** patch ***"}, ["*** patch ***"], id="apply-patch-dict-with-input"),
        pytest.param("apply_patch", {"other": "x"}, None, id="apply-patch-dict-without-input"),
        pytest.param("apply_patch", 123, None, id="apply-patch-wrong-type"),
        # Edit 系で container が dict でない（想定外の shape）場合は判定不能扱い。
        pytest.param("Edit", "raw string, not dict", None, id="edit-container-not-dict"),
    ],
)
def test_editable_text(tool_name: str, container: Any, expected: list[str] | None) -> None:
    """_editable_text の分岐（実 JSON 経路では組み立てにくい枝）。"""
    assert config_protection._editable_text(tool_name, container) == expected


def test_resolve_lint_section_empty_snippet_is_undetermined() -> None:
    """空の断片はディスク上のセクションを判定できない。"""
    assert config_protection._resolve_lint_section_from_disk("pyproject.toml", "") is None


def test_resolve_lint_section_read_failure_is_undetermined(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """読み取りが失敗したら判定不能（読み取りは open + 上限付き read で行う）。"""
    target = tmp_path / "pyproject.toml"
    target.write_text("[project]\nname = 'x'\n", encoding="utf-8")

    def _boom(*args, **kwargs):
        """open を常に失敗させる。"""
        raise OSError("permission denied")

    monkeypatch.setattr("pathlib.Path.open", _boom)
    assert config_protection._resolve_lint_section_from_disk(str(target), "name") is None


def test_resolve_lint_section_read_is_capped_at_max_stdin_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """読み取り量に上限があること（無制限だと hook timeout を踏んで allow へ倒れる）。"""
    target = tmp_path / "pyproject.toml"
    target.write_text("[tool.ruff]\nignore = ['E']\n", encoding="utf-8")
    requested: list[int | None] = []
    real_open = pathlib.Path.open

    def _spy_open(self, *args, **kwargs):
        """open したハンドルの read に渡された上限を記録する。"""
        handle = real_open(self, *args, **kwargs)
        real_read = handle.read

        def _read(size=None):
            """要求サイズを記録して本物の read へ渡す。"""
            requested.append(size)
            return real_read(size)

        handle.read = _read
        return handle

    monkeypatch.setattr("pathlib.Path.open", _spy_open)
    config_protection._resolve_lint_section_from_disk(str(target), "ignore")

    assert requested == [config_protection.MAX_STDIN_BYTES]


def test_resolve_lint_section_snippet_before_any_header_is_not_lint(tmp_path: Path) -> None:
    """先頭セクション見出しより前（トップレベル）に見つかった場合は lint 系ではない。"""
    target = tmp_path / "setup.cfg"
    target.write_text("# leading comment\ntop_level_marker\n[flake8]\nmax-line-length = 100\n", encoding="utf-8")
    assert config_protection._resolve_lint_section_from_disk(str(target), "top_level_marker") is False


class TestLintKeyPatternIsLinear:
    """行頭キー照合が入力長に対して線形であること。

    `(?m)^\\s*(...)\\s*=` のような形は `\\s` が改行を含むため、**一致しない**
    空白主体テキストに対して「各行頭 × 残り空白長」で後退し二次オーダーに
    なる（実測: 改行だけの入力で N=5,000 → 0.105s、N=20,000 → 1.619s、
    N=50,000 → 10.187s）。config_protection の hooks.json timeout は 15 秒
    なので約 60,000 バイトの改行だけで超過し、host にフックを殺されれば
    allow へ倒れる＝保護そのものが無効化される。

    判定は入力 2 サイズ間の比ではなく**絶対的な壁時計の上限**で行う。比の
    アサートは小さい N ではノイズ支配で他マシンで flake するため。
    """

    # 線形なら 200,000 改行で 0.01 秒未満。二次オーダーなら同じ入力で 100 秒超に
    # なるため、200 倍以上の余裕を取っても二次オーダーへの回帰は必ず捕まる。
    _CEILING_SECONDS = 2.0
    _SIZE = 200_000

    @pytest.mark.parametrize(
        ("pattern_name", "text"),
        [
            # 改行だけの入力（最悪ケース）。
            ("_LINT_KEY_PATTERN", "\n" * _SIZE),
            ("_TOX_COMMAND_KEY_PATTERN", "\n" * _SIZE),
            # 行頭インデント付きの非一致テキスト。
            ("_LINT_KEY_PATTERN", ("    \t" * 20 + "\n") * 4_000),
        ],
        ids=["lint-key-newlines", "tox-command-key-newlines", "lint-key-mixed-indent"],
    )
    def test_non_matching_input_returns_quickly(self, pattern_name: str, text: str) -> None:
        """一致しない空白主体の入力が上限時間内に一致なしで返る。"""
        pattern = getattr(config_protection, pattern_name)

        started = time.monotonic()
        match = pattern.search(text)
        elapsed = time.monotonic() - started

        assert match is None
        assert elapsed < self._CEILING_SECONDS, f"{pattern_name} took {elapsed:.3f}s (二次オーダーへ回帰した疑い)"

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


@pytest.mark.parametrize(
    ("excess", "expected"),
    [
        # 上限超過は走査せず True（ブロック）。打ち切って走査を続ける（truncate）形にすると、
        # 上限より後ろに lint キーを置くだけで条件付き保護を素通りできるため fail-closed 側へ倒す。
        pytest.param(1, True, id="over-cap-blocks-fail-closed"),
        # 陰性対照: 上限を入れたことで「大きいだけの正当な書き込み」まで deny へ倒れていない。
        pytest.param(-1, False, id="large-harmless-text-under-cap"),
    ],
)
def test_lint_signal_text_length_cap(excess: int, expected: bool) -> None:
    """`_text_has_protected_signal` は走査量の上限を超えるテキストを fail-closed で扱う。"""
    text = "x" * (config_protection._LINT_SIGNAL_MAX_TEXT_BYTES + excess)

    assert config_protection._text_has_protected_signal(text, "pyproject.toml") is expected
