"""bluecore.lib.harness のテスト。"""

from __future__ import annotations

import json
import os

import pytest

from bluecore.lib import harness


class TestDetectHarness:
    """detect_harness のテスト。"""

    def test_claudecode_env_returns_claude(self, monkeypatch):
        """CLAUDECODE 設定時は claude を返す。"""
        monkeypatch.setenv("CLAUDECODE", "1")
        assert harness.detect_harness() == "claude"

    def test_claudecode_takes_precedence(self, monkeypatch):
        """CLAUDECODE は他の判定材料より優先される。"""
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("PLUGIN_DATA", "/tmp/plugin-data")
        assert harness.detect_harness() == "claude"

    def test_plugin_data_returns_codex(self, monkeypatch):
        """PLUGIN_DATA 設定時は codex を返す。"""
        monkeypatch.setenv("PLUGIN_DATA", "/tmp/plugin-data")
        assert harness.detect_harness() == "codex"

    def test_codex_prefix_env_returns_codex(self, monkeypatch):
        """CODEX_ プレフィックス環境変数で codex を返す。"""
        monkeypatch.setenv("CODEX_HOME", "/home/u/.codex")
        assert harness.detect_harness() == "codex"

    def test_copilot_prefix_env_returns_copilot(self, monkeypatch):
        """COPILOT_ プレフィックス環境変数で copilot を返す。"""
        monkeypatch.setenv("COPILOT_AGENT_PROMPT", "do something")
        assert harness.detect_harness() == "copilot"

    def test_copilot_plugin_root_path_returns_copilot(self, monkeypatch):
        """CLAUDE_PLUGIN_ROOT が copilot キャッシュ配下なら copilot を返す。"""
        monkeypatch.setenv(
            "CLAUDE_PLUGIN_ROOT", "/home/u/.copilot/installed-plugins/bluecore/bluecore"
        )
        assert harness.detect_harness() == "copilot"

    def test_no_markers_returns_unknown(self, monkeypatch):
        """判定材料が無ければ unknown を返す。"""
        # ホストに GROK_* / COPILOT_* が残っていても unknown になるよう除去
        for key in list(os.environ):
            if key.startswith(("GROK_", "COPILOT_", "CODEX_")) or key in {
                "CLAUDECODE",
                "PLUGIN_DATA",
            }:
                monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/home/u/dev/repo")
        harness.detect_harness.cache_clear()
        assert harness.detect_harness() == "unknown"

    def test_grok_plugin_root_path_returns_grok(self, monkeypatch):
        """CLAUDE_PLUGIN_ROOT が ~/.grok/installed-plugins 配下なら grok を返す。"""
        monkeypatch.setenv(
            "CLAUDE_PLUGIN_ROOT",
            "/Users/u/.grok/installed-plugins/bluecore-abc123",
        )
        assert harness.detect_harness() == "grok"

    def test_grok_plugins_path_returns_grok(self, monkeypatch):
        """CLAUDE_PLUGIN_ROOT が ~/.grok/plugins 配下なら grok を返す。"""
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/Users/u/.grok/plugins/bluecore")
        assert harness.detect_harness() == "grok"

    def test_grok_prefix_env_returns_grok(self, monkeypatch):
        """GROK_ プレフィックス環境変数で grok を返す。"""
        monkeypatch.setenv("GROK_SESSION_ID", "sess-1")
        assert harness.detect_harness() == "grok"

    def test_result_is_memoized(self, monkeypatch):
        """判定結果はメモ化され環境変更後も維持される。"""
        monkeypatch.setenv("CLAUDECODE", "1")
        assert harness.detect_harness() == "claude"
        monkeypatch.delenv("CLAUDECODE")
        assert harness.detect_harness() == "claude"


class TestNormalizeToolName:
    """normalize_tool_name のテスト。"""

    def test_apply_patch_maps_to_edit(self):
        """Codex の apply_patch は Edit に正規化される。"""
        assert harness.normalize_tool_name("apply_patch") == "Edit"

    def test_shell_maps_to_bash(self):
        """shell runtime 名は Bash に正規化される。"""
        assert harness.normalize_tool_name("shell") == "Bash"

    @pytest.mark.parametrize("name", ["Bash", "Edit", "Write", "MultiEdit", "Skill", ""])
    def test_other_names_pass_through(self, name):
        """マッピング対象外のツール名はそのまま返す。"""
        assert harness.normalize_tool_name(name) == name

    @pytest.mark.parametrize(
        ("copilot_name", "expected"),
        [
            ("write", "Write"),
            ("edit", "Edit"),
            ("bash", "Bash"),
            ("shell", "Bash"),
            ("multiedit", "MultiEdit"),
            ("read", "Read"),
            ("view", "Read"),
            ("glob", "Glob"),
            ("grep", "Grep"),
            ("task", "Agent"),
            ("agent", "Agent"),
            ("notebookedit", "NotebookEdit"),
        ],
    )
    def test_copilot_lowercase_names_normalize_to_claude_code_form(self, copilot_name, expected):
        """Copilot CLI が渡す lowercase runtime tool 名を Claude Code 表記へ正規化する。"""
        assert harness.normalize_tool_name(copilot_name) == expected


class TestExtractBashCommand:
    """extract_bash_command / extract_tool_input のテスト。"""

    def test_tool_input_dict_command(self):
        """Claude 形式 tool_input.command を返す。"""
        assert harness.extract_bash_command({"tool_input": {"command": "ls -la"}}) == "ls -la"

    def test_tool_args_json_string(self):
        """Copilot camelCase toolArgs（JSON 文字列）をパースして command を返す。"""
        payload = {"toolArgs": json.dumps({"command": "git status"})}
        assert harness.extract_bash_command(payload) == "git status"

    def test_tool_args_dict(self):
        """toolArgs が既に dict の場合も command を返す。"""
        assert harness.extract_bash_command({"toolArgs": {"command": "pwd"}}) == "pwd"

    def test_tool_input_string_not_json(self):
        """tool_input が非 JSON 文字列ならそのまま返す。"""
        assert harness.extract_bash_command({"tool_input": "echo hi"}) == "echo hi"

    def test_tool_input_malformed_json_object_string(self):
        """{ で始まるが JSON 不正なら生文字列を返す。"""
        assert harness.extract_bash_command({"tool_input": "{not-json"}) == "{not-json"

    def test_tool_input_non_dict_does_not_raise(self):
        """tool_input が list 等でも AttributeError にならず空文字。"""
        assert harness.extract_bash_command({"tool_input": [1, 2]}) == ""

    def test_cmd_alias_field(self):
        """command が無く cmd があればそれを使う。"""
        assert harness.extract_bash_command({"tool_input": {"cmd": "whoami"}}) == "whoami"

    def test_missing_returns_empty(self):
        """キーが無ければ空文字。"""
        assert harness.extract_bash_command({}) == ""


class TestExtractToolInput:
    """DT-01: extract_tool_input のコンテナ優先順位と JSON デコード。"""

    def test_extract_tool_input_accepts_tool_args_dict(self) -> None:
        """toolArgs / tool_args が dict ならそのまま返す。"""
        expected = {"file_path": "sample.py"}
        assert harness.extract_tool_input({"toolArgs": expected}) == expected
        assert harness.extract_tool_input({"tool_args": expected}) == expected

    def test_extract_tool_input_decodes_tool_args_json_object(self) -> None:
        """toolArgs が JSON オブジェクト文字列なら dict に decode する。"""
        payload = {"toolArgs": json.dumps({"file_path": "sample.py"})}
        assert harness.extract_tool_input(payload) == {"file_path": "sample.py"}

    def test_extract_tool_input_preserves_malformed_json_string(self) -> None:
        """不正 JSON 文字列は例外を出さず元の文字列を返す。"""
        assert harness.extract_tool_input({"toolArgs": "{bad"}) == "{bad"

    def test_extract_tool_input_prefers_tool_input_over_tool_args(self) -> None:
        """tool_input と toolArgs が両方あるときは tool_input を優先する。"""
        payload = {
            "tool_input": {"file_path": "canonical.py"},
            "toolArgs": {"file_path": "native.py"},
        }
        assert harness.extract_tool_input(payload) == {"file_path": "canonical.py"}

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"tool_input": {"file_path": "sample.py"}}, {"file_path": "sample.py"}),
            ({"toolArgs": '["raw"]'}, ["raw"]),
            ({"toolArgs": "raw command"}, "raw command"),
            (
                {"tool_input": None, "toolArgs": {"file_path": "sample.py"}},
                None,
            ),
            ({}, None),
            ({"toolArgs": None}, None),
            ({"toolArgs": [1, 2]}, [1, 2]),
            ({"toolArgs": 3}, 3),
        ],
        ids=[
            "dt01-1-tool_input-dict",
            "dt01-5-json-array-string",
            "dt01-7-plain-string",
            "dt01-9-tool_input-none-wins",
            "dt01-10-missing",
            "dt01-11-none",
            "dt01-11-list",
            "dt01-11-number",
        ],
    )
    def test_extract_tool_input_remaining_dt01_rows(
        self, payload: dict, expected: object
    ) -> None:
        """DT-01 の残行（存在優先・非 object JSON・型保持）を固定する。"""
        assert harness.extract_tool_input(payload) == expected


class TestExtractRawToolName:
    """DT-01: extract_raw_tool_name は normalize 前の生文字列を返す。"""

    def test_extract_raw_tool_name_accepts_snake_and_camel_case_fields(self) -> None:
        """tool_name と toolName の有効な文字列を吸収し、正規化はしない。"""
        assert harness.extract_raw_tool_name({"tool_name": "Edit"}) == "Edit"
        assert harness.extract_raw_tool_name({"toolName": "edit"}) == "edit"

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"tool_name": "Write", "toolName": "edit"}, "Write"),
            ({"tool_name": 123, "toolName": "edit"}, "edit"),
            ({"toolName": 123}, ""),
            ({}, ""),
            ({"tool_name": "", "toolName": "edit"}, "edit"),
            ({"tool_name": "", "toolName": ""}, ""),
        ],
        ids=[
            "dt01-raw-3-prefer-tool_name",
            "dt01-raw-4-nonstring-fallback",
            "dt01-raw-5-toolname-nonstring",
            "dt01-raw-6-missing",
            "empty-tool_name-falls-through",
            "both-empty",
        ],
    )
    def test_extract_raw_tool_name_remaining_dt01_rows(
        self, payload: dict, expected: str
    ) -> None:
        """空文字は無効、非文字列は fallback、両方なしは空文字。"""
        assert harness.extract_raw_tool_name(payload) == expected


class TestExtractToolResultText:
    """extract_tool_result_text のテスト。"""

    def test_claude_tool_response_stdout(self):
        """Claude の tool_response.stdout を返す。"""
        text, resp = harness.extract_tool_result_text(
            {"tool_response": {"stdout": "hello\n", "stderr": ""}}
        )
        assert text == "hello\n"
        assert resp["stdout"] == "hello\n"

    def test_copilot_tool_result_text(self):
        """Copilot camelCase toolResult.textResultForLlm を返す。"""
        text, resp = harness.extract_tool_result_text(
            {"toolResult": {"resultType": "success", "textResultForLlm": "out"}}
        )
        assert text == "out"
        assert resp["stdout"] == "out"

    def test_vscode_tool_result_snake(self):
        """VS Code tool_result.text_result_for_llm を返す。"""
        text, _resp = harness.extract_tool_result_text(
            {"tool_result": {"text_result_for_llm": "snake"}}
        )
        assert text == "snake"

    def test_tool_result_raw_string(self):
        """toolResult が生文字列でも返す。"""
        text, resp = harness.extract_tool_result_text({"toolResult": "plain"})
        assert text == "plain"
        assert resp == {"stdout": "plain"}

    def test_empty_when_missing(self):
        """フィールドが無ければ空。"""
        assert harness.extract_tool_result_text({}) == ("", {})


class TestExtractFilePaths:
    """extract_file_paths のテスト。"""

    def test_file_path_field(self):
        """Edit/Write 系は file_path フィールドを返す。"""
        assert harness.extract_file_paths("Edit", {"file_path": "/a/b.py"}) == ["/a/b.py"]

    def test_missing_file_path_returns_empty(self):
        """file_path 不在時は空リストを返す（対象ファイル無し）。"""
        assert harness.extract_file_paths("Bash", {"command": "ls"}) == []

    def test_non_string_file_path_returns_empty(self):
        """file_path が文字列以外なら空リストを返す。"""
        assert harness.extract_file_paths("Edit", {"file_path": 123}) == []

    def test_apply_patch_update_marker(self):
        """apply_patch の Update File マーカーをパースする。"""
        patch = "*** Begin Patch\n*** Update File: src/x.py\n@@\n-a\n+b\n*** End Patch"
        assert harness.extract_file_paths("apply_patch", {"input": patch}) == ["src/x.py"]

    def test_apply_patch_multiple_markers(self):
        """Add/Update/Delete の複数マーカーをすべて抽出する。"""
        patch = (
            "*** Begin Patch\n"
            "*** Add File: new.py\n"
            "+x = 1\n"
            "*** Update File: mod.py\n"
            "@@\n"
            "*** Delete File: old.py\n"
            "*** End Patch"
        )
        assert harness.extract_file_paths("apply_patch", {"input": patch}) == [
            "new.py",
            "mod.py",
            "old.py",
        ]

    def test_apply_patch_without_markers_returns_none(self):
        """マーカーが無いパッチは None（判定不能 → fail-closed）。"""
        assert harness.extract_file_paths("apply_patch", {"input": "garbage"}) is None

    def test_apply_patch_non_string_input_returns_none(self):
        """input が文字列以外なら None（判定不能 → fail-closed）。"""
        assert harness.extract_file_paths("apply_patch", {"input": None}) is None
        assert harness.extract_file_paths("apply_patch", {}) is None

    def test_apply_patch_raw_string_input(self):
        """tool_input が生文字列（dict ではなく）でもパッチをパースできる。"""
        patch_text = (
            "*** Begin Patch\n"
            "*** Update File: /tmp/example.txt\n"
            "@@\n"
            "-old\n"
            "+new\n"
            "*** End Patch\n"
        )
        assert harness.extract_file_paths("apply_patch", patch_text) == ["/tmp/example.txt"]

    def test_apply_patch_json_encoded_string_input(self):
        """tool_input が JSON 文字列化された dict（{"input": "..."}）でも input を復元できる。"""
        patch_text = "*** Begin Patch\n*** Add File: new.py\n+x = 1\n*** End Patch"
        wrapped = json.dumps({"input": patch_text})
        assert harness.extract_file_paths("apply_patch", wrapped) == ["new.py"]

    def test_apply_patch_malformed_json_string_falls_back_to_raw(self):
        """{ で始まるが JSON として不正な文字列は、そのまま生パッチとしてマーカー探索する。"""
        assert harness.extract_file_paths("apply_patch", "{not valid json") is None

    def test_apply_patch_json_string_without_string_input_field_falls_back_to_raw(self):
        """JSON dict だが input フィールドが文字列でない場合は、生文字列としてマーカー探索する。"""
        wrapped = json.dumps({"input": 123})
        assert harness.extract_file_paths("apply_patch", wrapped) is None

    def test_apply_patch_non_dict_non_string_input_returns_none(self):
        """tool_input が dict でも str でもない場合は判定不能で None。"""
        assert harness.extract_file_paths("apply_patch", None) is None
        assert harness.extract_file_paths("apply_patch", 123) is None

    def test_non_apply_patch_non_dict_input_returns_empty(self):
        """apply_patch 以外で tool_input が dict でない場合は空リストを返す。"""
        assert harness.extract_file_paths("Bash", "ls -la") == []
        assert harness.extract_file_paths("Edit", None) == []

    def test_mismatched_tool_name_raw_patch_string_is_still_detected(self):
        """tool_name が "apply_patch" と一致しなくても、生入力が構造化パッチ
        マーカーを含む文字列なら内容ベースで検出し、ファイル一覧を返す
        （ツール名の表記ゆれで保護対象ファイルが漏れる fail-open の回帰防止）。
        """
        patch_text = "*** Update File: model.json\n@@\n-old\n+new\n"
        assert harness.extract_file_paths("edit", patch_text) == ["model.json"]

    def test_mismatched_tool_name_dict_wrapped_patch_is_still_detected(self):
        """tool_name が未知の値でも、{"input": ...} 形式で構造化パッチ本文を
        運ぶ dict なら内容ベースでマーカーをパースする。
        """
        patch = (
            "*** Begin Patch\n"
            "*** Add File: new.py\n"
            "+x = 1\n"
            "*** Update File: mod.py\n"
            "@@\n"
            "*** End Patch"
        )
        assert harness.extract_file_paths("unknown-harness-tool", {"input": patch}) == [
            "new.py",
            "mod.py",
        ]

    def test_mismatched_tool_name_json_encoded_patch_is_still_detected(self):
        """tool_name 不一致でも JSON 文字列化された {"input": ...} 形式を復元し、
        マーカーをパースする。
        """
        patch_text = "*** Update File: eslint.config.js\n@@\n-a\n+b\n"
        wrapped = json.dumps({"input": patch_text})
        assert harness.extract_file_paths("Edit", wrapped) == ["eslint.config.js"]

    def test_mismatched_tool_name_without_markers_falls_back_to_normal_handling(self):
        """tool_name 不一致かつ内容にパッチマーカーが無い文字列は、
        通常の非パッチ入力として空リストを返す（Bash コマンド等の誤ブロック防止）。
        """
        assert harness.extract_file_paths("edit", "echo *** not a patch ***") == []

    def test_mismatched_tool_name_dict_with_file_path_prefers_file_path(self):
        """tool_name 不一致でも file_path フィールドを持つ dict は最優先で使う。"""
        assert harness.extract_file_paths("unknown-harness-tool", {"file_path": "/a/b.py"}) == [
            "/a/b.py"
        ]


class TestResolveSessionId:
    """resolve_session_id のテスト。"""

class TestResolveProjectDir:
    """resolve_project_dir のテスト。"""

