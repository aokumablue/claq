"""ple4.ci.validate_hooks のテスト。"""

from __future__ import annotations

import json
from pathlib import Path

from ple4.ci.ci_common import REPO_ROOT
from ple4.ci.validate_hooks import REQUIRED_EVENTS, validate_hooks

REPO_HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"


def complete_hooks(**events: object) -> dict:
    """検証対象イベント以外の必須イベントをダミーで埋めた hooks 設定を返す。

    `validate_hooks` は「完成した hooks マニフェスト」を検証する契約であり、
    必須イベント（PreToolUse / PreCompact / SessionStart / SessionEnd）が
    揃っていなければ失敗する（F-03）。1 つのマッチャーの構造だけを見たい
    テストでも、残りは構造的に正しいダミーで埋めて完成形にする。
    """
    filler = [{"matcher": "*", "hooks": [{"type": "command", "command": "true", "timeout": 5}]}]
    filled: dict[str, object] = dict.fromkeys(REQUIRED_EVENTS, filler)
    filled.update(events)
    return {"hooks": filled}


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def test_validate_hooks_fails_when_missing(tmp_path, capsys):
    """hooks.json が無い場合は失敗すること（F-03）。

    「見つからないのでスキップします」と表示して 0 を返していた頃は、
    宣言がまるごと失われた破損を成功として報告していた。
    """
    result = validate_hooks(tmp_path / "hooks.json")

    assert result == 1
    assert "見つかりません" in capsys.readouterr().err


def test_validate_hooks_skips_when_missing_and_optional(tmp_path, capsys):
    """--optional を明示した場合だけスキップして 0 を返すこと。"""
    result = validate_hooks(tmp_path / "hooks.json", optional=True)

    assert result == 0
    assert "検証をスキップします" in capsys.readouterr().out


def test_validate_hooks_rejects_empty_hooks_object(tmp_path, capsys):
    """`{"hooks": {}}` を「0 個検証しました」と成功扱いしないこと（F-03）。"""
    hooks_file = tmp_path / "hooks.json"
    write_json(hooks_file, {"hooks": {}})

    assert validate_hooks(hooks_file) == 1
    assert "必須イベントが宣言されていません" in capsys.readouterr().err


def test_validate_hooks_rejects_invalid_json(tmp_path, capsys):
    """不正な JSON は即座に失敗すること。"""
    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text("{ invalid json", encoding="utf-8")

    result = validate_hooks(hooks_file)
    captured = capsys.readouterr()

    assert result == 1
    assert "JSON 形式が不正です" in captured.err


def test_validate_hooks_valid_command_hook(tmp_path, capsys):
    """有効なコマンドフックはオブジェクト形式で通ること。"""
    hooks_file = tmp_path / "hooks.json"
    write_json(
        hooks_file,
        complete_hooks(
            PreToolUse=[
                {
                    "matcher": "test",
                    "hooks": [{"type": "command", "command": 'node -e "console.log(1+2)"'}],
                }
            ]
        ),
    )

    assert validate_hooks(hooks_file) == 0
    assert "個のフックマッチャーを検証しました" in capsys.readouterr().out


def test_validate_hooks_accepts_inline_js_strings(tmp_path, capsys):
    """旧式のインライン JS コマンドは不透明な文字列として受け入れられること。"""
    hooks_file = tmp_path / "hooks.json"
    write_json(
        hooks_file,
        complete_hooks(
            PreToolUse=[
                {
                    "matcher": "test",
                    "hooks": [{"type": "command", "command": 'node -e "function {"'}],
                }
            ]
        ),
    )

    result = validate_hooks(hooks_file)

    assert result == 0
    assert "個のフックマッチャーを検証しました" in capsys.readouterr().out


class TestRepoHooksJsonStaticChecks:
    """実リポジトリの hooks/hooks.json に対する静的検証（Phase 6）。

    §7-3（実 install/update + host 登録の smoke test）は本タスクのスコープを
    大きく超えるため実施しない。代わりに、hooks.json 自体が全経路を宣言し、
    かつ全エントリが timeout を持つことを静的に検証する。
    """

    _REQUIRED_PRE_TOOL_USE_MODULES = (
        "ple4.hooks.block_no_verify",
        "ple4.hooks.pre_bash_commit_quality",
        "ple4.hooks.bash_config_protection",
        "ple4.hooks.config_protection",
    )
    _REQUIRED_EVENTS = ("PreToolUse", "PreCompact", "SessionStart", "SessionEnd")

    @staticmethod
    def _load_hooks() -> dict:
        return json.loads(REPO_HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]

    def test_declares_valid_schema(self, capsys) -> None:
        """既存の validate_hooks（構造検証）も実 hooks.json に対して通ること。"""
        assert validate_hooks(REPO_HOOKS_JSON) == 0
        assert "検証しました" in capsys.readouterr().out

    def test_declares_all_required_events(self) -> None:
        hooks = self._load_hooks()
        missing = [event for event in self._REQUIRED_EVENTS if event not in hooks or not hooks[event]]
        assert missing == []

    def test_pre_tool_use_declares_all_required_modules(self) -> None:
        hooks = self._load_hooks()
        commands = " ".join(
            str(hook.get("command", ""))
            for entry in hooks.get("PreToolUse", [])
            for hook in entry.get("hooks", [])
        )
        missing = [module for module in self._REQUIRED_PRE_TOOL_USE_MODULES if module not in commands]
        assert missing == []

    def test_every_hook_entry_has_a_timeout(self) -> None:
        """全イベントの全 hook エントリが timeout を持つこと（現状 PreCompact のみ未指定だった）。"""
        hooks = self._load_hooks()
        missing_timeout = [
            (event, hook.get("command"))
            for event, entries in hooks.items()
            for entry in entries
            for hook in entry.get("hooks", [])
            if "timeout" not in hook
        ]
        assert missing_timeout == []
