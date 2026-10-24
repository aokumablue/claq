"""ホスト設定（settings.json / settings.local.json）の Edit/Write を、置換前後の全文で判定する検査。

保護フックを止められるのは top-level の ``hooks`` / ``enabledPlugins`` / ``disabledPlugins`` の部分木と、
``env`` のうちインタプリタ解決に効くキーだけなので、それらが変わる編集だけを deny し、``model`` /
``permissions`` / ``env`` の他のキーは通す。置換後が JSON object として読めない・全文を作れない・
走査上限を超える編集は、検査できないので deny する。

置換前の全文はディスクから読むため、各テストは対象ファイルを tmp_path に実際に置く（置かないと
「読めない JSON」の deny に落ち、間違った理由で緑になる）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from claq.hooks import config_protection

from ._support import _repo_scope as _repo_scope
from ._support import _run, _write_content

_BLOCKED_TEXT = "can disable the protection hooks"
_UNVERIFIABLE_TEXT = "could not be verified"


def _settings(tmp_path: Path, content: str, name: str = "settings.json") -> str:
    """``tmp_path/.claude/<name>`` に内容を置き、そのパスを返す。

    Args:
        tmp_path: pytest の一時ディレクトリ。
        content: ファイルへ書く内容。
        name: ファイル名。

    Returns:
        置いたファイルの絶対パス文字列。
    """
    path = tmp_path / ".claude" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


def _edit(file_path: str, old: str, new: str) -> dict[str, Any]:
    """Edit ツール呼び出しの payload を組み立てる。

    Args:
        file_path: 対象ファイルパス。
        old: 置換対象。
        new: 置換後の文字列。

    Returns:
        フック stdin へ渡す payload dict。
    """
    return {"tool_name": "Edit", "tool_input": {"file_path": file_path, "old_string": old, "new_string": new}}


def test_edit_disabling_plugin_inside_enabled_plugins_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``enabledPlugins`` の内側でプラグインを false にする Edit は、キー名が断片に無くても deny する。"""
    path = _settings(tmp_path, '{"enabledPlugins": {"claq@m": true}}')

    code = _run("config_protection", _edit(path, '"claq@m": true', '"claq@m": false'), monkeypatch)
    err = capsys.readouterr().err

    assert (code, _BLOCKED_TEXT in err, _UNVERIFIABLE_TEXT in err) == (2, True, False)


def test_edit_changing_enabled_plugin_value_type_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``true`` を ``1`` に変える Edit も変化として deny する（Python の ``True == 1`` で見逃さない）。"""
    path = _settings(tmp_path, '{"enabledPlugins": {"claq@m": true}}')

    assert _run("config_protection", _edit(path, '"claq@m": true', '"claq@m": 1'), monkeypatch) == 2


@pytest.mark.parametrize("name", ["PATH", "PYTHONPATH", "PYTHONHOME", "Path", "pythonpath"])
def test_edit_adding_interpreter_env_name_is_denied(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """インタプリタ解決に効く env のキーを足す Edit は、名前の大小を問わず deny する。"""
    path = _settings(tmp_path, '{"env": {"DEBUG": "1"}}')

    payload = _edit(path, '"DEBUG": "1"', f'"DEBUG": "1", "{name}": "/tmp/evil"')

    assert _run("config_protection", payload, monkeypatch) == 2


def test_edit_inside_hooks_subtree_without_key_name_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``hooks`` の内側の値だけを変える Edit（断片に ``"hooks"`` が出ない）も deny する。"""
    path = _settings(tmp_path, '{"hooks": {"PreToolUse": [{"matcher": "Bash"}]}}')

    assert _run("config_protection", _edit(path, '"Bash"', '"None"'), monkeypatch) == 2


def test_write_removing_guarded_subtree_is_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """保護対象の部分木を消す Write（結果にキー名が出ない）も deny する。"""
    path = _settings(tmp_path, '{"hooks": {"PreToolUse": []}, "model": "a"}')

    assert _run("config_protection", _write_content(path, '{"model": "a"}'), monkeypatch) == 2


_UNPARSEABLE_RESULTS = [
    pytest.param(lambda path: _edit(path, '"a"', '"a",'), id="trailing-comma"),
    pytest.param(lambda path: _write_content(path, "[]"), id="top-level-array"),
    pytest.param(lambda path: _write_content(path, '{"model": NaN}'), id="nan"),
    # 3.14 の C スキャナは 12 万段前後で RecursionError を出す（古い版はより浅く落ちる）。走査上限に収まる 20 万段で踏む。
    pytest.param(lambda path: _write_content(path, "[" * 200_000), id="deep-nesting"),
    pytest.param(lambda path: _write_content(path, ""), id="empty"),
]


@pytest.mark.parametrize("build", _UNPARSEABLE_RESULTS)
def test_unparseable_result_is_denied_and_asks_user(
    build: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """置換後が JSON object として読めない編集は検査できないので deny し、ユーザーに頼むよう促す。"""
    path = _settings(tmp_path, '{"model": "a"}')

    code = _run("config_protection", build(path), monkeypatch)
    err = capsys.readouterr().err.casefold()

    assert (code, _UNVERIFIABLE_TEXT in err, "ask the user" in err) == (2, True, True)


def test_recursion_error_while_comparing_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """解析・比較中の RecursionError は検査不能として deny する（実入力で踏める段数は版とスタックで変わる）。"""
    path = _settings(tmp_path, '{"model": "a"}')

    def _too_deep(_text: str) -> None:
        """深すぎる入れ子を模す。"""
        raise RecursionError

    monkeypatch.setattr(config_protection, "_parse_settings_object", _too_deep)

    code = _run("config_protection", _write_content(path, '{"model": "b"}'), monkeypatch)

    assert (code, _UNVERIFIABLE_TEXT in capsys.readouterr().err.casefold()) == (2, True)


def test_jsonc_vscode_settings_is_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """basename で判定するので、コメント入りの ``.vscode/settings.json`` も読めない JSON として deny する。"""
    path = tmp_path / ".vscode" / "settings.json"
    path.parent.mkdir()

    payload = _write_content(str(path), '// c\n{"editor.tabSize": 2}')

    assert _run("config_protection", payload, monkeypatch) == 2


def test_oversized_current_file_is_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """置換前が走査上限を超えるファイルへの Edit は、上限より後ろを見られないので deny する。"""
    path = _settings(tmp_path, json.dumps({"model": "x" * (300 * 1024)}))

    assert _run("config_protection", _edit(path, '"xxxx', '"yyyy'), monkeypatch) == 2


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda path: {
                "tool_name": "apply_patch",
                "tool_input": {"input": f'*** Update File: {path}\n-  "model": "a"\n+  "model": "b"\n'},
            },
            id="apply-patch",
        ),
        pytest.param(
            lambda path: {
                "tool_name": "MultiEdit",
                "tool_input": {"file_path": path, "edits": [{"old_string": '"absent"', "new_string": '"b"'}]},
            },
            id="multiedit-unmatched",
        ),
    ],
)
def test_unbuildable_result_is_denied(build: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """置換後の全文を作れない編集（パッチ本文・置換対象が見つからない MultiEdit）は deny する。"""
    path = _settings(tmp_path, '{"model": "a"}')

    assert _run("config_protection", build(path), monkeypatch) == 2


def test_oversized_result_is_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """置換後が走査上限を超える Write は deny する。"""
    path = _settings(tmp_path, '{"model": "a"}')

    payload = _write_content(path, json.dumps({"model": "x" * (300 * 1024)}))

    assert _run("config_protection", payload, monkeypatch) == 2


_UNGUARDED_SETTINGS = '{"model": "a", "permissions": {"allow": []}, "env": {"DEBUG": "1"}, "enabledPlugins": {"claq@m": true}}'


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param('"model": "a"', '"model": "b"', id="model"),
        pytest.param('"allow": []', '"allow": ["Bash(npm:*)"]', id="permissions"),
        pytest.param('"DEBUG": "1"', '"DEBUG": "0"', id="env-debug"),
    ],
)
def test_edit_to_unguarded_keys_is_allowed(
    old: str, new: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """保護対象を持つファイルでも、保護対象以外のキーの編集は通す。"""
    path = _settings(tmp_path, _UNGUARDED_SETTINGS)

    assert _run("config_protection", _edit(path, old, new), monkeypatch) == 0


def test_write_preserving_guarded_values_is_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """保護対象の値を変えずに書き直す Write は、結果にキー名が出ていても通す。"""
    guarded = {"hooks": {"PreToolUse": []}, "enabledPlugins": {"claq@m": True}, "env": {"CLAQ_PYTHON": "/usr/bin/python3"}}
    path = _settings(tmp_path, json.dumps({**guarded, "model": "a"}))

    payload = _write_content(path, json.dumps({**guarded, "model": "b"}, indent=2))

    assert _run("config_protection", payload, monkeypatch) == 0


def test_removing_env_without_guarded_names_is_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """保護対象の名前を持たない ``env`` を丸ごと消す Write は通す（``env`` が無いことは空と同じ）。"""
    path = _settings(tmp_path, '{"env": {"DEBUG": "1"}}')

    assert _run("config_protection", _write_content(path, "{}"), monkeypatch) == 0


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param('{"model": "x"}', 0, id="repair-allowed"),
        pytest.param('{"hooks": {}}', 2, id="guarded-key-denied"),
    ],
)
def test_broken_current_file(
    content: str, expected: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """置換前が読めないファイルは保護対象が無かったものとして扱い、置換後に保護対象が現れたときだけ deny する。"""
    path = _settings(tmp_path, "{broken")

    assert _run("config_protection", _write_content(path, content), monkeypatch) == expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param('{"env": "y"}', 2, id="changed"),
        pytest.param('{"env": "x", "model": "m"}', 0, id="unchanged"),
    ],
)
def test_non_object_env_is_compared_whole(
    content: str, expected: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``env`` が object でなければ値全体を比べる。"""
    path = _settings(tmp_path, '{"env": "x"}')

    assert _run("config_protection", _write_content(path, content), monkeypatch) == expected
