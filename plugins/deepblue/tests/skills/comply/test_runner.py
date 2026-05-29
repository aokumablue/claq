"""runner モジュールのテスト — LLM CLI によるシナリオ実行と出力解析。

デシジョンテーブル (_parse_stream_json):
  | # | 入力パターン                                              | 期待結果                                        |
  |---|----------------------------------------------------------|-------------------------------------------------|
  | 1 | assistant (tool_use) + user (tool_result) 対応あり        | ObservationEvent が1件生成される                |
  | 2 | 複数 tool_use と対応する tool_result                       | 複数 ObservationEvent がタイムスタンプ順に返る   |
  | 3 | assistant メッセージのみ（tool_result なし）               | pending 残りが空 output で ObservationEvent 化  |
  | 4 | 空文字列入力                                               | 空リスト                                        |
  | 5 | 不正 JSON 行が混在                                        | スキップされて有効なイベントのみ処理             |
  | 6 | tool_input が dict 型                                     | JSON文字列にシリアライズされる                  |
  | 7 | tool_input が string 型                                   | str() で変換される                              |
  | 8 | output が list 型                                         | JSON文字列にシリアライズされる                  |
  | 9 | output が string 型                                       | str() で変換される                              |
  | 10| user メッセージの content が list でない                   | tool_result のマッチングをスキップ               |

デシジョンテーブル (run_scenario):
  | # | 条件                              | 期待結果                         |
  |---|----------------------------------|----------------------------------|
  | 1 | model が ALLOWED_MODELS 外        | ValueError                        |
  | 2 | model が有効 + llm-cli 成功       | ScenarioRun が返る               |
  | 3 | llm-cli が returncode!=0          | RuntimeError                     |

デシジョンテーブル (_safe_sandbox_dir):
  | # | scenario_id                       | 期待動作                         |
  |---|----------------------------------|----------------------------------|
  | 1 | 英数字・ハイフン・アンダースコア   | そのまま使われる                 |
  | 2 | 特殊文字（/ . スペースなど）が含まれる | _ に置換される                |
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from devgear.skills.comply.parser import ObservationEvent
from devgear.skills.comply.runner import (
    SANDBOX_BASE,
    ScenarioRun,
    _parse_stream_json,
    _safe_sandbox_dir,
    _setup_sandbox,
    run_scenario,
)
from devgear.skills.comply.scenario_generator import Scenario


def _make_scenario(
    sid: str = "test-scenario",
    level: int = 1,
    setup_commands: tuple[str, ...] = (),
) -> Scenario:
    """テスト用シナリオファクトリ。"""
    return Scenario(
        id=sid,
        level=level,
        level_name="strict",
        description="desc",
        prompt="do something",
        setup_commands=setup_commands,
    )


def _assistant_line(tool_use_id: str, name: str, inp: object) -> str:
    """assistant メッセージ（tool_use）の stream-json 行を生成する。"""
    return json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": inp}]},
        }
    )


def _user_line(tool_use_id: str, output: object, session: str = "sess-001") -> str:
    """user メッセージ（tool_result）の stream-json 行を生成する。"""
    return json.dumps(
        {
            "type": "user",
            "session_id": session,
            "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": output}]},
        }
    )


# ========================
# _parse_stream_json テスト
# ========================


class TestParseStreamJson:
    """_parse_stream_json のデシジョンテーブルテスト。"""

    def test_single_tool_use_and_result(self) -> None:
        """ケース1: assistant (tool_use) + user (tool_result) で ObservationEvent が1件生成される。"""
        stdout = "\n".join(
            [
                _assistant_line("id1", "Read", {"file_path": "/foo.py"}),
                _user_line("id1", "file content"),
            ]
        )
        events = _parse_stream_json(stdout)
        assert len(events) == 1
        assert isinstance(events[0], ObservationEvent)
        assert events[0].tool == "Read"
        assert events[0].output == "file content"
        assert events[0].session == "sess-001"

    def test_multiple_tools_sorted_by_timestamp(self) -> None:
        """ケース2: 複数 tool_use がタイムスタンプ順（T0000, T0001）に返る。"""
        stdout = "\n".join(
            [
                _assistant_line("id1", "Write", {"file_path": "/a.py"}),
                _assistant_line("id2", "Bash", {"command": "pytest"}),
                _user_line("id1", "ok"),
                _user_line("id2", "passed"),
            ]
        )
        events = _parse_stream_json(stdout)
        assert len(events) == 2
        assert events[0].tool == "Write"
        assert events[1].tool == "Bash"
        assert events[0].timestamp == "T0000"
        assert events[1].timestamp == "T0001"

    def test_pending_without_result_emits_empty_output(self) -> None:
        """ケース3: tool_result が来なかった pending は空 output の ObservationEvent になる。"""
        stdout = _assistant_line("id_orphan", "Glob", {"pattern": "*.py"})
        events = _parse_stream_json(stdout)
        assert len(events) == 1
        assert events[0].tool == "Glob"
        assert events[0].output == ""
        assert events[0].session == "unknown"

    def test_empty_stdout_returns_empty_list(self) -> None:
        """ケース4: 空文字列では空リストが返る。"""
        events = _parse_stream_json("")
        assert events == []

    def test_invalid_json_lines_skipped(self) -> None:
        """ケース5: 不正 JSON 行はスキップされ有効イベントのみ処理される。"""
        stdout = "\n".join(
            [
                "{invalid json",
                _assistant_line("id1", "Read", {}),
                "also bad",
                _user_line("id1", "content"),
            ]
        )
        events = _parse_stream_json(stdout)
        assert len(events) == 1
        assert events[0].tool == "Read"

    def test_tool_input_dict_serialized_to_json(self) -> None:
        """ケース6: input が dict の場合 JSON 文字列にシリアライズされる。"""
        inp = {"file_path": "/foo.py", "content": "hello"}
        stdout = "\n".join(
            [
                _assistant_line("id1", "Write", inp),
                _user_line("id1", "done"),
            ]
        )
        events = _parse_stream_json(stdout)
        assert '"file_path"' in events[0].input
        assert '"content"' in events[0].input

    def test_tool_input_string_converted_to_str(self) -> None:
        """ケース7: input が string の場合 str() で変換される。"""
        stdout = "\n".join(
            [
                _assistant_line("id1", "Bash", "ls -la"),
                _user_line("id1", "output"),
            ]
        )
        events = _parse_stream_json(stdout)
        assert events[0].input == "ls -la"

    def test_output_list_serialized_to_json(self) -> None:
        """ケース8: output が list の場合 JSON 文字列にシリアライズされる。"""
        stdout = "\n".join(
            [
                _assistant_line("id1", "Read", {}),
                _user_line("id1", [{"text": "hello"}]),
            ]
        )
        events = _parse_stream_json(stdout)
        assert '"text"' in events[0].output

    def test_output_string_kept_as_string(self) -> None:
        """ケース9: output が string の場合 str のまま保持される。"""
        stdout = "\n".join(
            [
                _assistant_line("id1", "Read", {}),
                _user_line("id1", "plain output"),
            ]
        )
        events = _parse_stream_json(stdout)
        assert events[0].output == "plain output"

    def test_user_content_not_list_skipped(self) -> None:
        """ケース10: user メッセージの content が list でない場合 tool_result のマッチングをスキップ。"""
        stdout = "\n".join(
            [
                _assistant_line("id1", "Read", {}),
                json.dumps(
                    {
                        "type": "user",
                        "session_id": "s",
                        "message": {"content": "not a list"},
                    }
                ),
            ]
        )
        # pending の id1 は tool_result に対応しないため空 output で残る
        events = _parse_stream_json(stdout)
        assert len(events) == 1
        assert events[0].output == ""

    def test_input_truncated_at_5000_chars(self) -> None:
        """input が 5000 文字で切り詰められること。"""
        long_input = {"data": "x" * 10000}
        stdout = "\n".join(
            [
                _assistant_line("id1", "Write", long_input),
                _user_line("id1", "done"),
            ]
        )
        events = _parse_stream_json(stdout)
        assert len(events[0].input) <= 5000

    def test_output_truncated_at_5000_chars(self) -> None:
        """output が 5000 文字で切り詰められること。"""
        long_output = "y" * 10000
        stdout = "\n".join(
            [
                _assistant_line("id1", "Read", {}),
                _user_line("id1", long_output),
            ]
        )
        events = _parse_stream_json(stdout)
        assert len(events[0].output) <= 5000

    def test_non_assistant_non_user_type_ignored(self) -> None:
        """assistant/user 以外の type はイベントを生成しない。"""
        stdout = json.dumps({"type": "system", "message": {}})
        events = _parse_stream_json(stdout)
        assert events == []

    def test_tool_use_id_missing_defaults_to_empty(self) -> None:
        """tool_use_id が欠けている場合は空文字列キーとして扱われる。"""
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "tool_use", "name": "Read", "input": {}}
                                # id フィールドなし → "" がキーになる
                            ]
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "session_id": "s",
                        "message": {"content": [{"type": "tool_result", "tool_use_id": "", "content": "result"}]},
                    }
                ),
            ]
        )
        events = _parse_stream_json(stdout)
        # "" キーでマッチして1件生成されるはず
        assert len(events) == 1
        assert events[0].output == "result"


# ========================
# _safe_sandbox_dir テスト
# ========================


class TestSafeSandboxDir:
    """_safe_sandbox_dir のデシジョンテーブルテスト。"""

    def test_alphanumeric_id_unchanged(self) -> None:
        """英数字・ハイフン・アンダースコアの ID は変換されない。"""
        path = _safe_sandbox_dir("my-scenario_01")
        assert path.name == "my-scenario_01"
        assert str(path).startswith(str(SANDBOX_BASE))

    def test_special_chars_replaced_with_underscore(self) -> None:
        """スラッシュ・スペース・ドット等の特殊文字は _ に置換される。"""
        path = _safe_sandbox_dir("my/scenario.test")
        # / と . が _ に変換されること
        assert "/" not in path.name
        assert "." not in path.name

    def test_path_within_sandbox_base(self) -> None:
        """生成パスは SANDBOX_BASE 配下に収まる。"""
        path = _safe_sandbox_dir("safe-id")
        assert str(path).startswith(str(SANDBOX_BASE))


# ========================
# _setup_sandbox テスト
# ========================


class TestSetupSandbox:
    """_setup_sandbox のテスト。"""

    def test_creates_directory(self, tmp_path: Path) -> None:
        """sandbox_dir が作成されること。"""
        sandbox = tmp_path / "sandbox"
        scenario = _make_scenario()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _setup_sandbox(sandbox, scenario)

        assert sandbox.exists()

    def test_removes_existing_directory(self, tmp_path: Path) -> None:
        """既存の sandbox_dir を削除してから再作成すること。"""
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        existing_file = sandbox / "old.txt"
        existing_file.write_text("old content")
        scenario = _make_scenario()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _setup_sandbox(sandbox, scenario)

        # 古いファイルは削除されているはず
        assert not existing_file.exists()

    def test_runs_git_init(self, tmp_path: Path) -> None:
        """git init が実行されること。"""
        sandbox = tmp_path / "sandbox"
        scenario = _make_scenario()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _setup_sandbox(sandbox, scenario)

        calls = [str(c) for c in mock_run.call_args_list]
        assert any("git" in c and "init" in c for c in calls)

    def test_runs_setup_commands(self, tmp_path: Path) -> None:
        """setup_commands が順に実行されること。"""
        sandbox = tmp_path / "sandbox"
        scenario = _make_scenario(setup_commands=("echo hello", "echo world"))

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _setup_sandbox(sandbox, scenario)

        # git init + 2 コマンド = 3 回呼ばれる
        assert mock_run.call_count == 3


# ========================
# run_scenario テスト
# ========================


class TestRunScenario:
    """run_scenario のデシジョンテーブルテスト。"""

    def test_invalid_model_raises_value_error(self) -> None:
        """ケース1: 未知の model 名で ValueError が発生する。"""
        scenario = _make_scenario()
        with pytest.raises(ValueError, match="Unknown model"):
            run_scenario(scenario, model="gpt-4")

    def test_success_returns_scenario_run(self, tmp_path: Path) -> None:
        """ケース2: 正常実行では ScenarioRun が返る。"""
        scenario = _make_scenario()
        stdout = "\n".join(
            [
                _assistant_line("id1", "Read", {}),
                _user_line("id1", "content"),
            ]
        )
        mock_result = MagicMock(returncode=0, stdout=stdout, stderr="")

        with (
            patch("devgear.skills.comply.runner._safe_sandbox_dir", return_value=tmp_path / "sandbox"),
            patch("devgear.skills.comply.runner._setup_sandbox"),
            patch("subprocess.run", return_value=mock_result),
        ):
            run = run_scenario(scenario, model="haiku")

        assert isinstance(run, ScenarioRun)
        assert run.scenario is scenario
        assert isinstance(run.observations, tuple)

    def test_claude_failure_raises_runtime_error(self, tmp_path: Path) -> None:
        """ケース3: llm-cli が returncode != 0 の場合 RuntimeError が発生する。"""
        scenario = _make_scenario()
        mock_result = MagicMock(returncode=1, stdout="", stderr="error message")

        with (
            patch("devgear.skills.comply.runner._safe_sandbox_dir", return_value=tmp_path / "sandbox"),
            patch("devgear.skills.comply.runner._setup_sandbox"),
            patch("subprocess.run", return_value=mock_result),
        ):
            with pytest.raises(RuntimeError, match="llm-cli failed"):
                run_scenario(scenario, model="sonnet")

    def test_all_allowed_models_accepted(self, tmp_path: Path) -> None:
        """haiku / sonnet / opus の3モデルが ValueError なく受け入れられる。"""
        scenario = _make_scenario()
        mock_result = MagicMock(returncode=0, stdout="", stderr="")

        for model in ("haiku", "sonnet", "opus"):
            with (
                patch("devgear.skills.comply.runner._safe_sandbox_dir", return_value=tmp_path / "sandbox"),
                patch("devgear.skills.comply.runner._setup_sandbox"),
                patch("subprocess.run", return_value=mock_result),
            ):
                run = run_scenario(scenario, model=model)
            assert isinstance(run, ScenarioRun)

    def test_observations_parsed_from_stdout(self, tmp_path: Path) -> None:
        """stdout の tool_use/tool_result が ObservationEvent として格納される。"""
        scenario = _make_scenario()
        stdout = "\n".join(
            [
                _assistant_line("id1", "Write", {"file_path": "/a.py"}),
                _user_line("id1", "done"),
                _assistant_line("id2", "Bash", {"command": "pytest"}),
                _user_line("id2", "passed"),
            ]
        )
        mock_result = MagicMock(returncode=0, stdout=stdout, stderr="")

        with (
            patch("devgear.skills.comply.runner._safe_sandbox_dir", return_value=tmp_path / "sandbox"),
            patch("devgear.skills.comply.runner._setup_sandbox"),
            patch("subprocess.run", return_value=mock_result),
        ):
            run = run_scenario(scenario, model="haiku")

        assert len(run.observations) == 2
        assert run.observations[0].tool == "Write"
        assert run.observations[1].tool == "Bash"
