"""bluecore.lib.harness のテスト。"""

from __future__ import annotations

import json

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
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/home/u/dev/repo")
        assert harness.detect_harness() == "unknown"

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


class TestResolveSessionId:
    """resolve_session_id のテスト。"""

    def test_payload_session_id_wins(self, monkeypatch):
        """ペイロードの session_id が最優先。"""
        monkeypatch.setenv("CLAUDE_SESSION_ID", "env-id")
        assert harness.resolve_session_id({"session_id": "payload-id"}) == "payload-id"

    def test_env_fallback(self, monkeypatch):
        """ペイロードに無ければ CLAUDE_SESSION_ID を使う。"""
        monkeypatch.setenv("CLAUDE_SESSION_ID", "env-id")
        assert harness.resolve_session_id({}) == "env-id"

    def test_default_fallback(self):
        """どちらも無ければ default を返す。"""
        assert harness.resolve_session_id({"session_id": ""}) == "default"

    def test_unsafe_session_id_falls_back_to_default(self):
        """ファイル名に安全でない session_id は fail-closed で default に倒す。"""
        assert harness.resolve_session_id({"session_id": "../../etc/passwd"}) == "default"
        assert harness.resolve_session_id({"session_id": "a/b"}) == "default"
        assert harness.resolve_session_id({"session_id": "x" * 129}) == "default"

    def test_unsafe_env_session_id_falls_back_to_default(self, monkeypatch):
        """環境変数由来でも不正形式は default に倒す。"""
        monkeypatch.setenv("CLAUDE_SESSION_ID", "bad id with spaces")
        assert harness.resolve_session_id({}) == "default"


class TestResolveProjectDir:
    """resolve_project_dir のテスト。"""

    def test_env_wins(self, monkeypatch):
        """CLAUDE_PROJECT_DIR が最優先。"""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/proj")
        assert harness.resolve_project_dir({"cwd": "/payload"}) == "/proj"

    def test_payload_cwd_fallback(self):
        """環境変数が無ければペイロードの cwd を使う。"""
        assert harness.resolve_project_dir({"cwd": "/payload"}) == "/payload"

    def test_getcwd_fallback(self, monkeypatch, tmp_path):
        """どちらも無ければカレントディレクトリを返す。"""
        monkeypatch.chdir(tmp_path)
        assert harness.resolve_project_dir({"cwd": ""}) == str(tmp_path)
