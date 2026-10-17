"""claq.lib.harness のうち、ツール名の正規化と tool_input の取り出し側のテスト。

ツール名の別名解決、JSON 文字列コンテナの解釈、Bash コマンドの取り出し、
入力コンテナ・ファイルパスの抽出、深いネストの JSON の扱いを検査する。
ユーザーメッセージの正規化は test_harness_user_message.py が検査する。
"""

from __future__ import annotations

import json

import pytest

from claq.lib import harness


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

    @pytest.mark.parametrize(
        ("grok_name", "expected"),
        [
            ("search_replace", "Edit"),
            ("run_terminal_command", "Bash"),
            ("spawn_subagent", "Agent"),
            ("read_file", "Read"),
            ("list_dir", "Glob"),
        ],
    )
    def test_grok_names_normalize_to_claude_code_form(self, grok_name, expected):
        """Grok 固有の runtime tool 名を Claude Code 表記へ正規化する。"""
        assert harness.normalize_tool_name(grok_name) == expected


class TestJsonStringContainerNormalization:
    """JSON 文字列コンテナの「JSON らしさ判定」と実パースを揃える。

    判定を lstrip 後の文字列で行い、パースを元の文字列で行うとずれる。実測では
    tool_input の JSON 文字列へ \x0c を 1 文字前置しただけで block_no_verify が
    exit 0 になった（正常な JSON なら exit 2）。lstrip は JSON が許容しない空白も
    剥がすため「{ で始まる」と判定されるが、元の文字列は json.loads が拒否し、
    生コマンド文字列として扱われる。その文字列の中では危険なコマンドが JSON の
    ダブルクォート内に入るため、トークナイザからは実行トークンに見えない。
    """

    @pytest.mark.parametrize("prefix", ["", " ", "\t", "\n", "\r", "\x0c", "\x0b"])
    def test_json_string_container_is_parsed_regardless_of_leading_whitespace(self, prefix: str) -> None:
        """先頭の空白種別によらず JSON 文字列コンテナをパースする。"""
        payload = {"tool_input": prefix + json.dumps({"command": "git commit --no-verify"})}

        assert harness.extract_tool_input(payload) == {"command": "git commit --no-verify"}
        assert list(harness.iter_bash_commands(payload)) == ["git commit --no-verify"]

    def test_unparseable_json_like_string_is_returned_as_is(self) -> None:
        """JSON に見えてパースできない文字列は生文字列のまま返す（既存契約）。"""
        assert harness.extract_tool_input({"tool_input": "{not-json"}) == "{not-json"

    @pytest.mark.parametrize("prefix", ["", " ", "\t", "\n", "\r", "\x0c", "\x0b"])
    def test_patch_container_is_parsed_regardless_of_leading_whitespace(self, prefix: str) -> None:
        """パッチ本文コンテナでも判定とパースの文字列を揃える。

        `extract_tool_input` と同じく、`lstrip` 前の文字列をパースすると
        `\\x0c` を 1 文字前置しただけで `apply_patch` 以外のツール名では
        生の JSON 文字列がそのままパス候補になり、本来の対象
        `.git/hooks/pre-commit` が候補から消える。
        """
        patch = "*** Begin Patch\n*** Update File: .git/hooks/pre-commit\n*** End Patch"
        tool_input = prefix + json.dumps({"input": patch})

        assert harness.extract_file_paths("str_replace_editor", tool_input) == [".git/hooks/pre-commit"]
        assert harness.extract_file_paths("apply_patch", tool_input) == [".git/hooks/pre-commit"]

    def test_unparseable_patch_container_falls_back_to_raw_string(self) -> None:
        """パッチ本文コンテナも、パース不能なら生文字列のまま返す（既存契約）。"""
        assert harness.extract_file_paths("str_replace_editor", "{not-json") == ["{not-json"]


class TestIterBashCommands:
    """iter_bash_commands / iter_tool_input_containers のテスト。"""

    def test_tool_input_dict_command(self):
        """Claude 形式 tool_input.command を返す。"""
        assert list(harness.iter_bash_commands({"tool_input": {"command": "ls -la"}})) == ["ls -la"]

    def test_tool_args_json_string(self):
        """Copilot camelCase toolArgs（JSON 文字列）をパースして command を返す。"""
        payload = {"toolArgs": json.dumps({"command": "git status"})}
        assert list(harness.iter_bash_commands(payload)) == ["git status"]

    def test_tool_args_dict(self):
        """toolArgs が既に dict の場合も command を返す。"""
        assert list(harness.iter_bash_commands({"toolArgs": {"command": "pwd"}})) == ["pwd"]

    def test_tool_input_string_not_json(self):
        """tool_input が非 JSON 文字列ならそのまま返す。"""
        assert list(harness.iter_bash_commands({"tool_input": "echo hi"})) == ["echo hi"]

    def test_tool_input_malformed_json_object_string(self):
        """{ で始まるが JSON 不正なら生文字列を返す。"""
        assert list(harness.iter_bash_commands({"tool_input": "{not-json"})) == ["{not-json"]

    def test_tool_input_list_without_commands_yields_nothing(self):
        """list の要素から command が 1 つも取れなければ何も返さない。"""
        assert list(harness.iter_bash_commands({"tool_input": [1, 2]})) == []

    def test_tool_input_list_of_dicts_is_extracted(self):
        """list 形状のコンテナも要素ごとに command を取り出す。

        取り出せないと `extract_tool_input` が list を返すため必須フィールド
        欠落の fail-closed を素通りし、フックが静かに許可する
        （`git commit --no-verify` を list に包むだけで通る）。
        """
        payload = {"tool_input": [{"command": "ls"}, {"command": "git commit --no-verify"}]}

        assert list(harness.iter_bash_commands(payload)) == ["ls", "git commit --no-verify"]

    def test_every_field_alias_is_scanned(self):
        """command と cmd が同居する場合は両方を返す。

        先勝ちで最初の 1 つだけを返すと、無害な command を足すだけで cmd の
        危険なコマンドが検査から外れる（実測: 4 フックすべてが exit 0）。
        """
        payload = {"tool_input": {"command": "echo safe", "cmd": "git commit --no-verify -m x"}}

        assert list(harness.iter_bash_commands(payload)) == ["echo safe", "git commit --no-verify -m x"]

    def test_tool_input_list_of_strings_is_extracted(self):
        """要素が生文字列でも取り出す。"""
        assert list(harness.iter_bash_commands({"tool_input": ["git status"]})) == ["git status"]

    def test_tool_input_nested_list_is_flattened(self):
        """入れ子の list も再帰的に取り出す。"""
        assert list(harness.iter_bash_commands({"tool_input": [["git status"]]})) == ["git status"]

    def test_argv_list_also_yields_the_joined_command(self):
        """全要素が str の argv 配列は、要素ごとの走査に加えて 1 コマンドへ結合した文字列も返す。

        要素ごとにしか検査しないと ``["git", "commit", "--no-verify"]`` の各要素は単独では
        無害に見え、全フックを素通りする。結合した 1 文字列が検査対象に入って初めて
        フラグが ``git commit`` と同じコマンドの引数として読める。
        """
        payload = {"tool_input": {"command": ["git", "commit", "--no-verify", "-m", "x"]}}

        assert list(harness.iter_bash_commands(payload)) == [
            "git",
            "commit",
            "--no-verify",
            "-m",
            "x",
            "git commit --no-verify -m x",
        ]

    def test_argv_join_quotes_elements_that_contain_spaces(self):
        """空白を含む要素は ``shlex.join`` が引用し、1 語のまま読めること。"""
        payload = {"tool_input": {"cmd": ["git", "commit", "-m", "a b"]}}

        assert list(harness.iter_bash_commands(payload))[-1] == "git commit -m 'a b'"

    def test_bare_and_nested_argv_lists_are_joined(self):
        """``tool_input`` 直下の配列と入れ子の配列も、階層ごとに結合する。"""
        assert list(harness.iter_bash_commands({"tool_input": ["rm", "ruff.toml"]})) == [
            "rm",
            "ruff.toml",
            "rm ruff.toml",
        ]
        assert list(harness.iter_bash_commands({"tool_input": [["rm", "ruff.toml"]]})) == [
            "rm",
            "ruff.toml",
            "rm ruff.toml",
        ]

    def test_single_element_list_is_not_joined_again(self):
        """要素が 1 つなら結合しても情報が増えない（引用が付くだけ）ので返さない。"""
        assert list(harness.iter_bash_commands({"tool_input": {"command": ["git status"]}})) == ["git status"]

    def test_list_with_non_string_element_is_not_joined(self):
        """str 以外を含む配列は結合しない（要素ごとの走査だけを行う）。"""
        payload = {"tool_input": {"command": ["git", 1, "status"]}}

        assert list(harness.iter_bash_commands(payload)) == ["git", "status"]

    def test_argv_list_is_one_group_ending_with_the_joined_command(self):
        """全 str の argv 配列は（要素…, 結合）の 1 グループになる。

        フラット化すると呼び出し側が「同じ 1 コマンドの別表現」と「別コマンド」を
        区別できない。``fix: git commit handling`` のように引数の文字列に ``git commit``
        を含む commit が、要素と結合の 2 件として数えられてしまう。
        """
        payload = {"tool_input": {"command": ["git", "commit", "-m", "fix: git commit handling"]}}

        groups = list(harness.iter_bash_command_groups(payload))

        assert groups == [
            ("git", "commit", "-m", "fix: git commit handling", "git commit -m 'fix: git commit handling'")
        ]
        assert list(harness.iter_bash_commands(payload)) == list(groups[0])

    def test_plain_strings_are_singleton_groups(self):
        """文字列と、argv 配列でない list の要素は 1 要素ずつのグループになる。"""
        payload = {"tool_input": [{"command": "ls"}, {"cmd": "pwd"}]}

        assert list(harness.iter_bash_command_groups(payload)) == [("ls",), ("pwd",)]

    def test_nested_argv_lists_are_separate_groups(self):
        """入れ子の argv 配列はそれぞれが別のグループ（別のコマンド）になる。"""
        payload = {"tool_input": [["git", "commit", "-m", "a"], ["git", "commit", "-m", "b"]]}

        assert list(harness.iter_bash_command_groups(payload)) == [
            ("git", "commit", "-m", "a", "git commit -m a"),
            ("git", "commit", "-m", "b", "git commit -m b"),
        ]

    def test_flat_command_strings_list_is_one_group(self):
        """複数コマンド文字列を並べた配列も argv 配列と同じ形（要素…, 結合）を取る。"""
        payload = {"tool_input": ["git commit -m a", "git commit -m b"]}

        groups = list(harness.iter_bash_command_groups(payload))

        assert len(groups) == 1
        assert groups[0][:2] == ("git commit -m a", "git commit -m b")

    def test_every_container_key_yields_its_own_groups(self):
        """コンテナキーごとにグループを返す（先頭が無害でも後続を落とさない）。"""
        payload = {
            "tool_input": {"command": "echo safe"},
            "toolArgs": {"command": ["git", "commit", "--no-verify"]},
        }

        assert list(harness.iter_bash_command_groups(payload)) == [
            ("echo safe",),
            ("git", "commit", "--no-verify", "git commit --no-verify"),
        ]

    @pytest.mark.parametrize(
        "argv",
        [
            ["git", "commit", "-m", "a b"],
            ["echo", "it's"],
            ["echo", 'say "hi"'],
            ["printf", "a\nb", "line1\nline2"],
            ["echo", "$HOME", "`id`", "$(id)"],
            ["echo", "日本語", "\u00e9\u200b"],
            ["echo", "", "x"],
            ["echo", "a;b", "c&&d", "e|f", "#g"],
            ["x", "\\"],
        ],
    )
    def test_joined_command_round_trips_to_the_argv(self, argv):
        """結合文字列は ``shlex.split`` で元の argv に戻る（引用が 1 語を保つ）。"""
        import shlex

        group = next(harness.iter_bash_command_groups({"tool_input": {"command": argv}}))

        assert shlex.split(group[-1]) == argv

    def test_cmd_alias_field(self):
        """command が無く cmd があればそれを使う。"""
        assert list(harness.iter_bash_commands({"tool_input": {"cmd": "whoami"}})) == ["whoami"]

    def test_missing_yields_nothing(self):
        """キーが無ければ何も返さない。"""
        assert list(harness.iter_bash_commands({})) == []

    def test_tool_input_dict_without_string_command(self):
        """tool_input が dict でも command/cmd が無い・非文字列なら返さない。"""
        assert list(harness.iter_bash_commands({"tool_input": {}})) == []
        assert list(harness.iter_bash_commands({"tool_input": {"command": 1}})) == []

    def test_every_container_key_is_scanned(self):
        """複数コンテナキーを持つ payload は全キー分の command を返す。

        先勝ちで 1 キーだけ見ると、無害な側だけを検査して後続キーの危険な
        コマンドを素通りさせる（実測: `bash_config_protection` /
        `pre_bash_commit_quality` が exit 0 のまま通した）。
        """
        payload = {
            "tool_input": {"command": "echo safe"},
            "toolInput": {"command": "echo safe2"},
            "toolArgs": {"command": "git commit --no-verify -m x"},
            "tool_args": {"command": "echo safe3"},
        }

        assert list(harness.iter_bash_commands(payload)) == [
            "echo safe",
            "echo safe2",
            "git commit --no-verify -m x",
            "echo safe3",
        ]

    def test_iter_containers_yields_normalized_values(self):
        """iter_tool_input_containers は各キーの正規化済み値を順に返す。"""
        payload = {"tool_input": {"file_path": "a"}, "toolArgs": json.dumps({"file_path": "b"})}

        assert list(harness.iter_tool_input_containers(payload)) == [
            {"file_path": "a"},
            {"file_path": "b"},
        ]


class TestExtractToolInput:
    """extract_tool_input のコンテナ優先順位と JSON デコード。"""

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
        """存在優先・非 object JSON・型保持を固定する。"""
        assert harness.extract_tool_input(payload) == expected


class TestExtractRawToolName:
    """extract_raw_tool_name は normalize 前の生文字列を返す。"""

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


class TestExtractFilePaths:
    """extract_file_paths のテスト。"""

    def test_file_path_field(self):
        """Edit/Write 系は file_path フィールドを返す。"""
        assert harness.extract_file_paths("Edit", {"file_path": "/a/b.py"}) == ["/a/b.py"]

    def test_missing_file_path_returns_empty(self):
        """file_path 不在時は空リストを返す（対象ファイル無し）。"""
        assert harness.extract_file_paths("Bash", {"command": "ls"}) == []

    def test_non_string_file_path_is_undecidable(self):
        """file_path がパスとして解釈できない値なら判定不能（None）を返す。

        空リストを返すと、呼び出し側が「対象ファイルが 1 つも無い
        書き込み」として静かに許可してしまう。判定できない入力は fail-closed へ
        倒す（docs/adr/shell-analysis-boundary.md: 誤検出 > 誤通過）。
        """
        assert harness.extract_file_paths("Edit", {"file_path": 123}) is None

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

    def test_non_apply_patch_unsupported_input_returns_empty(self):
        """apply_patch 以外で dict / list / 文字列のいずれでもない入力は空リスト。"""
        assert harness.extract_file_paths("Edit", None) == []
        assert harness.extract_file_paths("Edit", 123) == []
        assert harness.extract_file_paths("Edit", "") == []

    def test_bare_string_container_is_treated_as_path(self):
        """生文字列コンテナはパス候補として返す。

        空リストを返すと config_protection が「対象 0 件」として静かに
        許可し、``{"tool_input": "ruff.toml"}`` が保護を素通りする（実測 exit 0）。
        """
        assert harness.extract_file_paths("Write", "ruff.toml") == ["ruff.toml"]

    def test_list_of_dicts_is_expanded(self):
        """listdict 形状は要素ごとに展開する（保護素通りの回帰防止）。"""
        assert harness.extract_file_paths(
            "Write", [{"file_path": "ruff.toml"}, {"file_path": "a.py"}]
        ) == ["ruff.toml", "a.py"]

    def test_list_of_strings_is_expanded(self):
        """liststr 形状は要素をそのままパス候補として展開する。"""
        assert harness.extract_file_paths("Write", ["ruff.toml", "a.py"]) == [
            "ruff.toml",
            "a.py",
        ]

    def test_legacy_file_key_is_accepted(self):
        """旧 ``file`` キーも file_path の別名として受理する（dict / list とも）。"""
        assert harness.extract_file_paths("Edit", {"file": "ruff.toml"}) == ["ruff.toml"]
        assert harness.extract_file_paths("Edit", [{"file": "ruff.toml"}]) == ["ruff.toml"]

    def test_both_file_path_and_legacy_file_key_are_scanned(self):
        """file_path と file が同居する場合は両方を検査対象にする。

        先勝ちで file_path だけを返すと、無害な file_path を 1 つ足すだけで
        file 側の保護対象パスが検査から外れる（実測: config_protection が
        exit 0）。どちらが実際に編集されるかは実行前に確定できないため、
        両方を保護対象として扱う。
        """
        assert harness.extract_file_paths("Edit", {"file_path": "new.py", "file": "old.py"}) == [
            "new.py",
            "old.py",
        ]

    def test_list_with_undeterminable_element_is_fail_closed(self):
        """list 要素が 1 つでも判定不能なら全体を判定不能（None）にする。"""
        assert harness.extract_file_paths("apply_patch", [{"input": "garbage"}]) is None

    def test_declared_patch_tool_with_empty_list_is_fail_closed(self):
        """apply_patch と明示された空 list は対象不明として None を返す。"""
        assert harness.extract_file_paths("apply_patch", []) is None

    def test_non_patch_tool_with_empty_list_returns_empty(self):
        """apply_patch 以外の空 list は対象 0 件として空リストを返す。"""
        assert harness.extract_file_paths("Write", []) == []

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
        """tool_name 不一致かつマーカーが無い文字列は、パッチではなくパス候補として扱う。

        誤ブロックの抑止はここで候補を捨てることではなく、呼び出し側
        （config_protection）の basename 一致と repo スコープ判定が担う。
        ここで捨てると保護対象パスを運ぶ生文字列形状ごと素通りする。
        """
        assert harness.extract_file_paths("edit", "echo *** not a patch ***") == [
            "echo *** not a patch ***"
        ]

    def test_mismatched_tool_name_dict_with_file_path_prefers_file_path(self):
        """tool_name 不一致でも file_path フィールドを持つ dict は最優先で使う。"""
        assert harness.extract_file_paths("unknown-harness-tool", {"file_path": "/a/b.py"}) == [
            "/a/b.py"
        ]


class TestGrokCamelCaseToolInput:
    """Grok の camelCase toolInput を受理する（PreToolUse 全拒否の回帰防止）。"""

    def test_tool_input_camel_case_is_accepted(self):
        """toolInput から tool 入力を取り出せる。"""
        payload = {"toolName": "run_terminal_command", "toolInput": {"command": "ls -la"}}
        assert harness.extract_tool_input(payload) == {"command": "ls -la"}

    def test_bash_command_from_camel_case_tool_input(self):
        """toolInput.command が Bash コマンドとして取り出せる。"""
        payload = {"toolName": "run_terminal_command", "toolInput": {"command": "git commit --no-verify"}}
        assert list(harness.iter_bash_commands(payload)) == ["git commit --no-verify"]

    def test_snake_case_and_camel_case_are_both_scanned(self):
        """両方のコンテナキーがある場合は宣言順で両方返す（片側だけ検査しない）。"""
        payload = {"tool_input": {"command": "a"}, "toolInput": {"command": "b"}}
        assert list(harness.iter_bash_commands(payload)) == ["a", "b"]

    def test_container_scanning_is_the_single_source(self):
        """コンテナキーの定義と全キー走査は harness の 1 か所だけに置く。

        フック側が走査を手書きすると片側だけ緩い状態が再発する（実測: 走査を
        手書きしていない 2 フックが複数コンテナキー payload を素通りさせた）。
        """
        from claq.hooks import bash_config_protection, block_no_verify, config_protection

        assert "toolInput" in harness.INPUT_CONTAINER_KEYS
        assert config_protection.iter_tool_input_containers is harness.iter_tool_input_containers
        for module in (block_no_verify, bash_config_protection):
            assert module.iter_bash_commands is harness.iter_bash_commands



class TestExtractFilePathsNestedValue:
    """extract_file_paths のフィールド値展開のテスト。"""

    def test_list_valued_file_path_is_expanded(self):
        """`file_path` の値が list でも対象ファイルとして取り出される。

        文字列以外を捨てると「対象ファイルが 1 つも無い書き込み」として
        config_protection が素通りする。
        """
        assert harness.extract_file_paths("Write", {"file_path": ["ruff.toml"]}) == ["ruff.toml"]

    def test_empty_string_value_yields_no_path(self):
        """空文字の `file_path` は対象ファイルとして数えない。"""
        assert harness.extract_file_paths("Write", {"file_path": ""}) == []

    @pytest.mark.parametrize("value", [123, {"nested": "ruff.toml"}, ["ruff.toml", 123]])
    def test_undecidable_value_propagates_none(self, value):
        """文字列でも文字列 list でもない値は判定不能として None を返す。

        ここで空リストへ丸めると、呼び出し側（config_protection）は「対象
        ファイルが 1 つも無い書き込み」として静かに許可してしまう。
        """
        assert harness.extract_file_paths("Write", {"file_path": value}) is None


class TestDeepNestingIsParseFailureNotCrash:
    """深いネストの JSON が例外で貫通しないこと。

    CPython の JSON デコーダは再帰下降で、深くネストした配列/オブジェクトに
    対して `JSONDecodeError` ではなく `RecursionError` を送出する。これを
    取りこぼすと保護フックが例外で異常終了し、PreToolUse の exit 1
    （non-blocking error = ツールはそのまま実行される）へ倒れる。
    """

    @staticmethod
    def _deep_json_text(depth: int = 200_000) -> str:
        """`json.loads` が RecursionError を送出する深さの JSON テキストを作る。"""
        return "[" * depth + "]" * depth

    def test_recursion_error_is_in_parse_failure_tuple(self):
        """RecursionError が JSON パース失敗として扱われる集合に入っている。"""
        assert RecursionError in harness.JSON_PARSE_FAILURES

    def test_deep_nesting_actually_raises_recursion_error(self):
        """前提の実測: この入力は json.loads が RecursionError を投げる。

        これが成り立たなくなった（CPython が反復パーサへ変わった等）場合、
        以下の 2 テストは何も守らなくなるため前提自体を固定する。
        """
        with pytest.raises(RecursionError):
            json.loads(self._deep_json_text())

    def test_extract_tool_input_returns_raw_string(self):
        """深いネストのコンテナ文字列はパース失敗として生文字列で返る。"""
        text = self._deep_json_text()
        assert harness.extract_tool_input({"toolArgs": text}) == text

    def test_extract_patch_text_returns_raw_string(self):
        """同型の json.loads を持つ `_extract_patch_text` も貫通しない。

        片側だけ塞ぐと同じ穴が別の呼び出し箇所に残るため対で固定する。
        """
        text = "{" + self._deep_json_text() + "}"
        assert harness._extract_patch_text(text) == text
