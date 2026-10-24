"""claq.lib.harness のうち、ツール名の正規化と tool_input の取り出し側のテスト。

ツール名の別名解決、JSON 文字列コンテナの解釈、Bash コマンドの取り出し、
入力コンテナ・ファイルパスの抽出、深いネストの JSON の扱いを検査する。
ユーザーメッセージの正規化は test_harness_user_message.py が検査する。
"""

from __future__ import annotations

import json
import shlex

import pytest

from claq.lib import harness

_NO_VERIFY = "git commit --no-verify"
_PATCH_ADD_UPDATE = "*** Begin Patch\n*** Add File: new.py\n+x = 1\n*** Update File: mod.py\n@@\n*** End Patch"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # Codex / shell runtime
        ("apply_patch", "Edit"),
        ("shell", "Bash"),
        # マッピング対象外はそのまま
        ("Bash", "Bash"),
        ("Edit", "Edit"),
        ("Write", "Write"),
        ("MultiEdit", "MultiEdit"),
        ("Skill", "Skill"),
        ("", ""),
        # Copilot CLI の lowercase 名
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
        # Grok 固有名
        ("search_replace", "Edit"),
        ("run_terminal_command", "Bash"),
        ("spawn_subagent", "Agent"),
        ("read_file", "Read"),
        ("list_dir", "Glob"),
    ],
)
def test_normalize_tool_name(name: str, expected: str) -> None:
    """各ハーネスの runtime tool 名を Claude Code 表記へ正規化し、対象外はそのまま返す。"""
    assert harness.normalize_tool_name(name) == expected


@pytest.mark.parametrize("prefix", ["", " ", "\t", "\n", "\r", "\x0c", "\x0b"])
def test_json_string_container_is_parsed_regardless_of_leading_whitespace(prefix: str) -> None:
    """先頭の空白種別によらず JSON 文字列コンテナ（コマンド・パッチ本文）をパースする。

    判定を lstrip 後の文字列で、パースを元の文字列で行うとずれる。実測では \\x0c を
    1 文字前置しただけで json.loads が拒否して生文字列扱いになり、block_no_verify が
    exit 0 になった（危険なコマンドが JSON のクォート内に入り実行トークンに見えない）。
    パッチ側では本来の対象 `.git/hooks/pre-commit` がパス候補から消える。
    """
    payload = {"tool_input": prefix + json.dumps({"command": _NO_VERIFY})}
    patch = "*** Begin Patch\n*** Update File: .git/hooks/pre-commit\n*** End Patch"
    patch_input = prefix + json.dumps({"input": patch})

    assert harness.extract_tool_input(payload) == {"command": _NO_VERIFY}
    assert list(harness.iter_bash_commands(payload)) == [_NO_VERIFY]
    assert harness.extract_file_paths("str_replace_editor", patch_input) == [".git/hooks/pre-commit"]
    assert harness.extract_file_paths("apply_patch", patch_input) == [".git/hooks/pre-commit"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param({"tool_input": {"command": "ls -la"}}, ["ls -la"], id="tool_input-dict"),
        pytest.param({"toolArgs": json.dumps({"command": "git status"})}, ["git status"], id="toolArgs-json-string"),
        pytest.param({"toolArgs": {"command": "pwd"}}, ["pwd"], id="toolArgs-dict"),
        # Grok の camelCase toolInput を落とすと PreToolUse が全拒否になる。
        pytest.param(
            {"toolName": "run_terminal_command", "toolInput": {"command": _NO_VERIFY}}, [_NO_VERIFY], id="toolInput"
        ),
        pytest.param({"tool_input": "echo hi"}, ["echo hi"], id="non-json-string"),
        pytest.param({"tool_input": "{not-json"}, ["{not-json"], id="malformed-json-string"),
        pytest.param({"tool_input": {"cmd": "whoami"}}, ["whoami"], id="cmd-alias"),
        # 先勝ちで command だけ返すと、無害な command を足すだけで cmd が外れる（実測: 4 フックが exit 0）。
        pytest.param(
            {"tool_input": {"command": "echo safe", "cmd": _NO_VERIFY + " -m x"}},
            ["echo safe", _NO_VERIFY + " -m x"],
            id="command-and-cmd-both",
        ),
        pytest.param({}, [], id="missing"),
        pytest.param({"tool_input": {}}, [], id="dict-without-command"),
        pytest.param({"tool_input": {"command": 1}}, [], id="non-string-command"),
        pytest.param({"tool_input": [1, 2]}, [], id="list-without-commands"),
        # list を取りこぼすと extract_tool_input の必須フィールド欠落 fail-closed を素通りする。
        pytest.param(
            {"tool_input": [{"command": "ls"}, {"command": _NO_VERIFY}]}, ["ls", _NO_VERIFY], id="list-of-dicts"
        ),
        pytest.param({"tool_input": ["git status"]}, ["git status"], id="list-of-strings"),
        pytest.param({"tool_input": [["git status"]]}, ["git status"], id="nested-list"),
        # 要素だけでは無害に見える argv も、結合した 1 文字列でフラグが git commit の引数として読める。
        pytest.param(
            {"tool_input": {"command": ["git", "commit", "--no-verify", "-m", "x"]}},
            ["git", "commit", "--no-verify", "-m", "x", "git commit --no-verify -m x"],
            id="argv-also-yields-joined",
        ),
        pytest.param(
            {"tool_input": {"cmd": ["git", "commit", "-m", "a b"]}},
            ["git", "commit", "-m", "a b", "git commit -m 'a b'"],
            id="argv-join-quotes-spaces",
        ),
        pytest.param({"tool_input": ["rm", "ruff.toml"]}, ["rm", "ruff.toml", "rm ruff.toml"], id="bare-argv"),
        pytest.param({"tool_input": [["rm", "ruff.toml"]]}, ["rm", "ruff.toml", "rm ruff.toml"], id="nested-argv"),
        # 要素が 1 つなら結合しても引用が付くだけなので返さない。
        pytest.param({"tool_input": {"command": ["git status"]}}, ["git status"], id="single-element-not-joined"),
        pytest.param({"tool_input": {"command": ["git", 1, "status"]}}, ["git", "status"], id="non-string-not-joined"),
        # 先勝ちで 1 キーだけ見ると後続キーの危険なコマンドを素通りさせる（実測: 2 フックが exit 0）。
        pytest.param(
            {
                "tool_input": {"command": "echo safe"},
                "toolInput": {"command": "echo safe2"},
                "toolArgs": {"command": _NO_VERIFY + " -m x"},
                "tool_args": {"command": "echo safe3"},
            },
            ["echo safe", "echo safe2", _NO_VERIFY + " -m x", "echo safe3"],
            id="every-container-key",
        ),
        pytest.param({"tool_input": {"command": "a"}, "toolInput": {"command": "b"}}, ["a", "b"], id="snake-and-camel"),
    ],
)
def test_iter_bash_commands(payload: dict, expected: list[str]) -> None:
    """全コンテナキー・全フィールド別名・list 形状から command を取り出し、argv は結合も返す。"""
    assert list(harness.iter_bash_commands(payload)) == expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # フラット化すると「同じ 1 コマンドの別表現」と「別コマンド」を区別できない。
        pytest.param(
            {"tool_input": {"command": ["git", "commit", "-m", "fix: git commit handling"]}},
            [("git", "commit", "-m", "fix: git commit handling", "git commit -m 'fix: git commit handling'")],
            id="argv-is-one-group",
        ),
        pytest.param({"tool_input": [{"command": "ls"}, {"cmd": "pwd"}]}, [("ls",), ("pwd",)], id="singleton-groups"),
        pytest.param(
            {"tool_input": [["git", "commit", "-m", "a"], ["git", "commit", "-m", "b"]]},
            [("git", "commit", "-m", "a", "git commit -m a"), ("git", "commit", "-m", "b", "git commit -m b")],
            id="nested-argv-separate-groups",
        ),
        pytest.param(
            {"tool_input": ["git commit -m a", "git commit -m b"]},
            [("git commit -m a", "git commit -m b", "'git commit -m a' 'git commit -m b'")],
            id="flat-command-strings-one-group",
        ),
        pytest.param(
            {"tool_input": {"command": "echo safe"}, "toolArgs": {"command": ["git", "commit", "--no-verify"]}},
            [("echo safe",), ("git", "commit", "--no-verify", _NO_VERIFY)],
            id="group-per-container-key",
        ),
    ],
)
def test_iter_bash_command_groups(payload: dict, expected: list[tuple[str, ...]]) -> None:
    """argv 配列は（要素…, 結合）の 1 グループ、文字列は 1 要素グループになり、平坦化が iter_bash_commands。"""
    groups = list(harness.iter_bash_command_groups(payload))

    assert groups == expected
    assert list(harness.iter_bash_commands(payload)) == [command for group in groups for command in group]


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
def test_joined_command_round_trips_to_the_argv(argv: list[str]) -> None:
    """結合文字列は ``shlex.split`` で元の argv に戻る（引用が 1 語を保つ）。"""
    group = next(harness.iter_bash_command_groups({"tool_input": {"command": argv}}))

    assert shlex.split(group[-1]) == argv


def test_iter_containers_yields_normalized_values() -> None:
    """iter_tool_input_containers は各キーの正規化済み値を順に返す。"""
    payload = {"tool_input": {"file_path": "a"}, "toolArgs": json.dumps({"file_path": "b"})}

    assert list(harness.iter_tool_input_containers(payload)) == [{"file_path": "a"}, {"file_path": "b"}]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param({"tool_input": {"file_path": "sample.py"}}, {"file_path": "sample.py"}, id="tool_input-dict"),
        pytest.param(
            {"toolName": "run_terminal_command", "toolInput": {"command": "ls -la"}},
            {"command": "ls -la"},
            id="toolInput-dict",
        ),
        pytest.param({"toolArgs": {"file_path": "sample.py"}}, {"file_path": "sample.py"}, id="toolArgs-dict"),
        pytest.param({"tool_args": {"file_path": "sample.py"}}, {"file_path": "sample.py"}, id="tool_args-dict"),
        pytest.param(
            {"toolArgs": json.dumps({"file_path": "sample.py"})}, {"file_path": "sample.py"}, id="json-object-string"
        ),
        pytest.param({"toolArgs": '["raw"]'}, ["raw"], id="json-array-string"),
        pytest.param({"toolArgs": "raw command"}, "raw command", id="plain-string"),
        pytest.param({"toolArgs": "{bad"}, "{bad", id="malformed-json-string"),
        pytest.param({"tool_input": "{not-json"}, "{not-json", id="malformed-json-in-tool_input"),
        pytest.param(
            {"tool_input": {"file_path": "canonical.py"}, "toolArgs": {"file_path": "native.py"}},
            {"file_path": "canonical.py"},
            id="tool_input-wins-over-toolArgs",
        ),
        pytest.param({"tool_input": None, "toolArgs": {"file_path": "sample.py"}}, None, id="tool_input-none-wins"),
        pytest.param({}, None, id="missing"),
        pytest.param({"toolArgs": None}, None, id="none"),
        pytest.param({"toolArgs": [1, 2]}, [1, 2], id="list"),
        pytest.param({"toolArgs": 3}, 3, id="number"),
    ],
)
def test_extract_tool_input(payload: dict, expected: object) -> None:
    """存在するコンテナを優先順に 1 つ選び、JSON 文字列は decode し、それ以外は型を保って返す。"""
    assert harness.extract_tool_input(payload) == expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param({"tool_name": "Edit"}, "Edit", id="tool_name"),
        pytest.param({"toolName": "edit"}, "edit", id="toolName-not-normalized"),
        pytest.param({"tool_name": "Write", "toolName": "edit"}, "Write", id="prefer-tool_name"),
        pytest.param({"tool_name": 123, "toolName": "edit"}, "edit", id="nonstring-falls-through"),
        pytest.param({"toolName": 123}, "", id="toolName-nonstring"),
        pytest.param({}, "", id="missing"),
        pytest.param({"tool_name": "", "toolName": "edit"}, "edit", id="empty-tool_name-falls-through"),
        pytest.param({"tool_name": "", "toolName": ""}, "", id="both-empty"),
    ],
)
def test_extract_raw_tool_name(payload: dict, expected: str) -> None:
    """tool_name / toolName の有効な文字列を正規化せず返す（空文字・非文字列は無効）。"""
    assert harness.extract_raw_tool_name(payload) == expected


# 判定不能は None（fail-closed）。空リストへ丸めると呼び出し側（config_protection）が
# 「対象ファイルが 1 つも無い書き込み」として静かに許可する（誤検出 > 誤通過）。
@pytest.mark.parametrize(
    ("tool_name", "tool_input", "expected"),
    [
        pytest.param("Edit", {"file_path": "/a/b.py"}, ["/a/b.py"], id="file_path"),
        pytest.param("Bash", {"command": "ls"}, [], id="no-file_path"),
        pytest.param("Edit", {"file_path": 123}, None, id="non-string-file_path"),
        pytest.param("Edit", {"file": "ruff.toml"}, ["ruff.toml"], id="legacy-file-key"),
        pytest.param("Edit", [{"file": "ruff.toml"}], ["ruff.toml"], id="legacy-file-key-in-list"),
        # どちらが編集されるかは実行前に確定できない。先勝ちだと無害な file_path で file が外れる。
        pytest.param("Edit", {"file_path": "new.py", "file": "old.py"}, ["new.py", "old.py"], id="file_path-and-file"),
        pytest.param("Write", {"file_path": ["ruff.toml"]}, ["ruff.toml"], id="list-valued-file_path"),
        pytest.param("Write", {"file_path": ""}, [], id="empty-file_path"),
        pytest.param("Write", {"file_path": 123}, None, id="undecidable-int"),
        pytest.param("Write", {"file_path": {"nested": "ruff.toml"}}, None, id="undecidable-dict"),
        pytest.param("Write", {"file_path": ["ruff.toml", 123]}, None, id="undecidable-mixed-list"),
        pytest.param(
            "unknown-harness-tool", {"file_path": "/a/b.py"}, ["/a/b.py"], id="unknown-tool-file_path-first"
        ),
        # 生文字列・list 形状（空リストだと {"tool_input": "ruff.toml"} が保護を素通りする）
        pytest.param("Write", "ruff.toml", ["ruff.toml"], id="bare-string"),
        pytest.param(
            "Write", [{"file_path": "ruff.toml"}, {"file_path": "a.py"}], ["ruff.toml", "a.py"], id="list-of-dicts"
        ),
        pytest.param("Write", ["ruff.toml", "a.py"], ["ruff.toml", "a.py"], id="list-of-strings"),
        pytest.param("Write", [], [], id="empty-list"),
        pytest.param("Edit", None, [], id="none-input"),
        pytest.param("Edit", 123, [], id="int-input"),
        pytest.param("Edit", "", [], id="empty-string-input"),
        pytest.param("str_replace_editor", "{not-json", ["{not-json"], id="unparseable-container-is-raw-path"),
        # apply_patch のマーカー
        pytest.param(
            "apply_patch",
            {"input": "*** Begin Patch\n*** Update File: src/x.py\n@@\n-a\n+b\n*** End Patch"},
            ["src/x.py"],
            id="patch-update-marker",
        ),
        pytest.param(
            "apply_patch",
            {"input": "*** Begin Patch\n*** Add File: new.py\n+x = 1\n*** Update File: mod.py\n@@\n"
             "*** Delete File: old.py\n*** End Patch"},
            ["new.py", "mod.py", "old.py"],
            id="patch-add-update-delete",
        ),
        pytest.param(
            "apply_patch",
            "*** Begin Patch\n*** Update File: /tmp/example.txt\n@@\n-old\n+new\n*** End Patch\n",
            ["/tmp/example.txt"],
            id="patch-raw-string",
        ),
        pytest.param(
            "apply_patch",
            json.dumps({"input": "*** Begin Patch\n*** Add File: new.py\n+x = 1\n*** End Patch"}),
            ["new.py"],
            id="patch-json-string",
        ),
        pytest.param("apply_patch", {"input": "garbage"}, None, id="patch-without-markers"),
        pytest.param("apply_patch", {"input": None}, None, id="patch-input-none"),
        pytest.param("apply_patch", {}, None, id="patch-input-missing"),
        pytest.param("apply_patch", "{not valid json", None, id="patch-malformed-json-is-raw-without-markers"),
        pytest.param("apply_patch", json.dumps({"input": 123}), None, id="patch-json-non-string-input"),
        pytest.param("apply_patch", None, None, id="patch-none-input"),
        pytest.param("apply_patch", 123, None, id="patch-int-input"),
        pytest.param("apply_patch", [{"input": "garbage"}], None, id="list-with-undecidable-element"),
        pytest.param("apply_patch", [], None, id="patch-empty-list"),
        # ツール名の表記ゆれで保護対象が漏れないよう、パッチは内容ベースでも検出する
        pytest.param(
            "edit", "*** Update File: model.json\n@@\n-old\n+new\n", ["model.json"], id="mismatched-name-raw-patch"
        ),
        pytest.param(
            "unknown-harness-tool", {"input": _PATCH_ADD_UPDATE}, ["new.py", "mod.py"], id="mismatched-name-dict-patch"
        ),
        pytest.param(
            "Edit",
            json.dumps({"input": "*** Update File: eslint.config.js\n@@\n-a\n+b\n"}),
            ["eslint.config.js"],
            id="mismatched-name-json-patch",
        ),
        # マーカーが無ければパス候補として扱う。誤ブロックの抑止は呼び出し側の basename・repo 判定が担う。
        pytest.param(
            "edit", "echo *** not a patch ***", ["echo *** not a patch ***"], id="mismatched-name-without-markers"
        ),
    ],
)
def test_extract_file_paths(tool_name: str, tool_input: object, expected: list[str] | None) -> None:
    """フィールド・生文字列・list・パッチマーカーから対象パスを取り出し、判定不能なら None を返す。"""
    assert harness.extract_file_paths(tool_name, tool_input) == expected


def test_container_scanning_is_the_single_source() -> None:
    """コンテナキーの定義と全キー走査は harness の 1 か所だけに置く。

    フック側が走査を手書きすると片側だけ緩い状態が再発する（実測: 走査を
    手書きしていない 2 フックが複数コンテナキー payload を素通りさせた）。
    """
    from claq.hooks import bash_config_protection, block_no_verify, config_protection

    assert "toolInput" in harness.INPUT_CONTAINER_KEYS
    assert config_protection.iter_tool_input_containers is harness.iter_tool_input_containers
    for module in (block_no_verify, bash_config_protection):
        assert module.iter_bash_commands is harness.iter_bash_commands


class TestDeepNestingIsParseFailureNotCrash:
    """深いネストの JSON が例外で貫通しないこと。

    CPython の JSON デコーダは再帰下降で、深くネストした配列/オブジェクトに
    対して `JSONDecodeError` ではなく `RecursionError` を送出する。これを
    取りこぼすと保護フックが例外で異常終了し、PreToolUse の exit 1
    （non-blocking error = ツールはそのまま実行される）へ倒れる。
    """

    _DEEP_JSON_TEXT = "[" * 200_000 + "]" * 200_000

    def test_recursion_error_is_in_parse_failure_tuple(self):
        """RecursionError が JSON パース失敗として扱われる集合に入っている。"""
        assert RecursionError in harness.JSON_PARSE_FAILURES

    def test_deep_nesting_actually_raises_recursion_error(self):
        """前提の実測: この入力は json.loads が RecursionError を投げる（崩れると下のテストが何も守らない）。"""
        with pytest.raises(RecursionError):
            json.loads(self._DEEP_JSON_TEXT)

    def test_both_json_loads_call_sites_return_raw_string(self):
        """`extract_tool_input` と `_extract_patch_text` の両方が生文字列を返す（片側だけ塞ぐと穴が残る）。"""
        text = self._DEEP_JSON_TEXT
        assert harness.extract_tool_input({"toolArgs": text}) == text
        assert harness._extract_patch_text("{" + text + "}") == "{" + text + "}"
