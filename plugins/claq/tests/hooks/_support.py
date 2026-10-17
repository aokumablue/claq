"""tests/hooks の複数のファイルが使う補助。

- secret scan 予算の deadline 生成・コミットメッセージ検証のスパイ
- 保護フックの `main()` をインプロセスで呼ぶ土台（`_HOOKS` / `_run` / payload 組み立て / `_repo_scope`）
- 保護語彙の期待値表（`_EXPECTED_*`、`_EXPECTED_VOCABULARIES`、`_EXTERNALLY_PINNED_VOCABULARIES`）。
  保護ファイル名の全数検査（`test_protection_bypass_regressions.py`）と語彙集合の固定
  （`test_protection_vocabulary_declarations.py`）が同じ表を使うため、ここに 1 つだけ置く。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from claq.hooks import bash_config_protection, block_no_verify, config_protection, hook_common, stdin_io
from claq.hooks import commit_quality_scanner as commit_quality_scanner
from claq.hooks import pre_bash_commit_quality as pre_bash_commit_quality


def _spy_commit_message_validation(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`validate_commit_message` を「問題なし」(None) を返すスパイに替え、受け取ったコマンドの記録を返す。

    コミットメッセージ検証を無関係な観点のテストから切り離すためのスタブ。None を返すだけだと、
    パッチが外れても本物が None を返す場面では結果が変わらず、差し替えが `evaluate` の経路へ
    届いたかを判別できない。呼び出しの記録を assert すれば、スタブが実際に検証段へ入ったことが固定される。
    """
    validated: list[str] = []

    def _spy(command: str) -> None:
        """受け取ったコマンドを記録し、検証結果なし(None)を返す。"""
        validated.append(command)

    monkeypatch.setattr(pre_bash_commit_quality, "validate_commit_message", _spy)
    return validated


def _scan_deadline() -> float:
    """テストから `find_file_issues` へ渡す secret scan 予算 1 本分の deadline を作る。

    本番ではフック 1 回の起動につき 1 度だけ `new_secret_scan_deadline()` を呼び、
    その 1 本を全ファイルで共有する。単発の `find_file_issues` を検査するテストは
    ファイルごとに満額の予算で構わないため、呼び出しごとに新しい deadline を作る。

    Returns:
        `_monotonic()` 基準で走査を打ち切るべき時刻（秒）。

    Raises:
        例外は発生しません。
    """
    return commit_quality_scanner.new_secret_scan_deadline()


# 検査対象フック名 → main() を持つモジュール。
_HOOKS = {
    "block_no_verify": block_no_verify,
    "bash_config_protection": bash_config_protection,
    "config_protection": config_protection,
    "pre_bash_commit_quality": pre_bash_commit_quality,
}


def _bash(command: str) -> dict[str, Any]:
    """Bash ツール呼び出しの payload を組み立てる。

    Args:
        command: フックへ渡すシェルコマンド文字列。

    Returns:
        フック stdin へ渡す payload dict。
    """
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _write(file_path: Any) -> dict[str, Any]:
    """Write ツール呼び出しの payload を組み立てる。

    Args:
        file_path: `file_path` フィールドに載せる値（文字列でなくてもよい）。

    Returns:
        フック stdin へ渡す payload dict。
    """
    return {"tool_name": "Write", "tool_input": {"file_path": file_path, "content": "x"}}


def _write_content(file_path: str, content: str) -> dict[str, Any]:
    """内容を指定した Write ツール呼び出しの payload を組み立てる。

    条件付き保護（`CONDITIONALLY_PROTECTED_FILES`）は書き込み内容に lint 兆候が
    無ければ allow なので、`_write` の固定内容では検査軸を動かせない。

    Args:
        file_path: `file_path` フィールドに載せる値。
        content: `content` フィールドに載せる書き込み内容。

    Returns:
        フック stdin へ渡す payload dict。
    """
    return {"tool_name": "Write", "tool_input": {"file_path": file_path, "content": content}}


_NO_VERIFY = "git commit --no-verify -m x"


@pytest.fixture(autouse=True)
def _repo_scope(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """cwd と repo ルートを使い捨てディレクトリへ固定する。

    `bash_config_protection` の repo スコープ判定が実行環境の git
    リポジトリに依存しないようにする。`cd sub` の到達先も用意する。

    Args:
        monkeypatch: pytest の monkeypatch フィクスチャ。
        tmp_path: pytest の一時ディレクトリ。

    Returns:
        repo ルートとして固定した一時ディレクトリ。
    """
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bash_config_protection, "resolve_repo_root", lambda: tmp_path)
    # `pre_bash_commit_quality` は staged files を git へ問い合わせる。使い捨て
    # ディレクトリは git リポジトリではないため、素のままだと「staged を特定
    # できない」の fail-closed で全ケースが exit 2 になり、mutation ガードの
    # 判定軸がまったく効いていなくても表が緑になる。空 staged を与えて、
    # `_compound_commit_risk` の結論だけが exit code に出る状態にする。
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: [])
    return tmp_path


def _run(hook: str, payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> int:
    """payload を stdin として与えてフックの main() を実行する。

    Args:
        hook: `_HOOKS` のキー。
        payload: フック stdin へ渡す payload。
        monkeypatch: pytest の monkeypatch フィクスチャ。

    Returns:
        main() の終了コード。
    """

    def _read() -> tuple[str, bool]:
        """stdin 読み取りの差し替え実装。

        Returns:
            (payload の JSON 文字列, 切り捨て無し) のタプル。
        """
        return json.dumps(payload), False

    module = _HOOKS[hook]
    # `pre_bash_commit_quality.main()` は `stdin_io` から関数内 import する
    # ため、モジュール束縛の差し替えだけでは効かない。両方を差し替える。
    monkeypatch.setattr(stdin_io, "read_raw_stdin_with_truncation", _read)
    if hasattr(module, "read_raw_stdin_with_truncation"):
        monkeypatch.setattr(module, "read_raw_stdin_with_truncation", _read)
    return module.main()


# `config_protection.PROTECTED_FILES` の期待内容（層2）。
_EXPECTED_PROTECTED_FILES = frozenset({
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    "eslint.config.ts",
    "eslint.config.mts",
    "eslint.config.cts",
    ".prettierrc",
    ".prettierrc.js",
    ".prettierrc.cjs",
    ".prettierrc.json",
    ".prettierrc.yml",
    ".prettierrc.yaml",
    "prettier.config.js",
    "prettier.config.cjs",
    "prettier.config.mjs",
    "biome.json",
    "biome.jsonc",
    ".ruff.toml",
    "ruff.toml",
    ".shellcheckrc",
    ".stylelintrc",
    ".stylelintrc.json",
    ".stylelintrc.yml",
    ".stylelintrc.yaml",
    ".markdownlint.json",
    ".markdownlint.yaml",
    ".markdownlint.yml",
    ".markdownlintrc",
    ".pre-commit-config.yaml",
    ".pre-commit-config.yml",
})

# `config_protection.CONDITIONALLY_PROTECTED_FILES` の期待内容（層2）。
_EXPECTED_CONDITIONALLY_PROTECTED_FILES = frozenset({
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    "package.json",
    # ホスト設定。保護フックを止めるキーだけを条件にする（`env` 全般ではない —
    # `update-config` skill の `DEBUG=true` や `.vscode/settings.json` の
    # `terminal.integrated.env.*` を巻き込むため）。
    "settings.json",
    "settings.local.json",
})

# `bash_config_protection._DIRECTORY_CHANGE_COMMANDS` の期待内容（層2）。
_EXPECTED_DIRECTORY_CHANGE_COMMANDS = frozenset({"cd", "pushd", "popd", "chdir"})

# `config_protection._LINT_SECTION_HEADERS` の期待内容（層2）。順序も含めて固定する
# （前方一致で判定するため、要素の順序は結果に影響しないが定義の差分を見やすくする）。
_EXPECTED_LINT_SECTION_HEADERS = (
    "[tool.ruff",
    "[tool.coverage",
    "[tool.pytest",
    "[flake8]",
    "[mypy]",
    "[pycodestyle]",
    "[testenv",
)

# `config_protection._LINT_KEYS` の期待内容（層2）。
_EXPECTED_LINT_KEYS = ("ignore", "select", "per-file-ignores", "exclude", "fail_under", "addopts")


# `bash_config_protection._RAW_TEXT_RISK_INDICATORS` の構成要素（層2）。
_EXPECTED_REMOVE_COMMANDS = frozenset({
    "clc",
    "clear-content",
    "del",
    "erase",
    "mi",
    "move-item",
    "mv",
    "rd",
    "remove-item",
    "ri",
    "rm",
    "shred",
    "truncate",
    "unlink",
})
_EXPECTED_MODE_COMMANDS = frozenset({"chflags", "chgrp", "chmod", "chown"})
_EXPECTED_TOUCH_COMMANDS = frozenset({"touch"})

# `config_protection._PROTECTED_PATH_SEGMENTS` の期待内容（層2）。
_EXPECTED_PROTECTED_PATH_SEGMENTS = ((".git", "hooks"), ("hooks", "hooks.json"))

# `config_protection._GIT_CONFIG_REPO_SEGMENTS` の期待内容（層2）。``*`` は worktree 名。
_EXPECTED_GIT_CONFIG_REPO_SEGMENTS = (
    (".git", "config"),
    (".git", "config.worktree"),
    (".git", "worktrees", "*", "config"),
    (".git", "worktrees", "*", "config.worktree"),
)

# `git_config_text.GLOBAL_GIT_CONFIG_DISPLAY_NAMES` の期待内容（層2）。
_EXPECTED_GLOBAL_GIT_CONFIG_DISPLAY_NAMES = frozenset(
    {"~/.gitconfig", "$XDG_CONFIG_HOME/git/config", "~/.config/git/config"}
)


def _folded(names: frozenset[str]) -> frozenset[str]:
    """保護名の畳み込みを期待値側でも同じ規則で行う。

    Args:
        names: 畳む前の名前集合。

    Returns:
        大小無視で畳んだ名前集合。
    """
    return frozenset(hook_common.normalize_protected_name(name) for name in names)


# 語彙集合の期待内容（層2）。キーは ``<モジュール名>.<定数名>``。
#
# **実装からコピーせず、変更のたびに人間がここを直す**のが目的の設計である。
# 保護語彙は「壊れても誰も困らないから気付かれない」種類のデータで、外部に
# 突き合わせ先を持たない。ここへ複製することで、語彙の増減がレビュー対象へ上がる。
# 派生値（畳み込み・和集合・連結）だけは、実装と同じ式を**期待値定数の上で**
# 組み立てる。実装側から作ると「派生の入力ごと壊れた」場合に緑のまま通るため。
_EXPECTED_VOCABULARIES: dict[str, Any] = {
    # --- bash_config_protection ---
    "bash_config_protection._BASH_TOOL_NAMES": frozenset({"bash"}),
    "bash_config_protection._REDIRECT_OPERATORS": frozenset(
        {">", ">>", "&>", "&>>", ">&", ">|", "1>", "2>", "1>>", "2>>"}
    ),
    "bash_config_protection._LAST_ARG_WRITE_COMMANDS": frozenset(
        {"copy", "copy-item", "cp", "cpi", "install", "mi", "move", "move-item", "mv"}
    ),
    "bash_config_protection._REMOVE_COMMANDS": _EXPECTED_REMOVE_COMMANDS,
    "bash_config_protection._MODE_COMMANDS": _EXPECTED_MODE_COMMANDS,
    "bash_config_protection._TEE_COMMANDS": frozenset(
        {"ac", "add-content", "new-item", "ni", "out-file", "sc", "set-content", "tee"}
    ),
    "bash_config_protection._INPLACE_EDIT_COMMANDS": frozenset({"perl", "ruby", "sed"}),
    "bash_config_protection._LN_COMMANDS": frozenset({"ln"}),
    "bash_config_protection._DD_COMMANDS": frozenset({"dd"}),
    "bash_config_protection._TOUCH_COMMANDS": _EXPECTED_TOUCH_COMMANDS,
    "bash_config_protection._DIRECTORY_CHANGE_COMMANDS": _EXPECTED_DIRECTORY_CHANGE_COMMANDS,
    "bash_config_protection._COMMAND_POSITION_WRAPPERS": frozenset(
        {
            "chrt",
            "command",
            "doas",
            "env",
            "ionice",
            "nice",
            "nohup",
            "setsid",
            "stdbuf",
            "sudo",
            "taskset",
            "time",
            "timeout",
            "xargs",
        }
    ),
    "bash_config_protection._WRAPPER_VALUE_SHORT_OPTIONS": frozenset({"-u"}),
    "bash_config_protection._HOME_LOG_PATH_SEGMENTS": ((".claq", "logs"),),
    # 派生: 保護 basename の和集合。
    "bash_config_protection._ALL_PROTECTED_BASENAMES": (
        _folded(_EXPECTED_PROTECTED_FILES) | _folded(_EXPECTED_CONDITIONALLY_PROTECTED_FILES)
    ),
    # 派生: malformed JSON fallback の指標列（順序も固定する）。
    "bash_config_protection._RAW_TEXT_RISK_INDICATORS": (
        (">", "tee", "-i")
        + tuple(sorted(_EXPECTED_REMOVE_COMMANDS))
        + tuple(sorted(_EXPECTED_MODE_COMMANDS))
        + tuple(sorted(_EXPECTED_TOUCH_COMMANDS))
        + ("ln",)
    ),
    # --- block_no_verify ---
    "block_no_verify._VALUE_LONG_OPTIONS": frozenset(
        {
            "--attr-source",
            "--author",
            "--cleanup",
            "--config-env",
            "--date",
            "--exec-path",
            "--file",
            "--fixup",
            "--git-dir",
            "--message",
            "--namespace",
            "--pathspec-from-file",
            "--reedit-message",
            "--reuse-message",
            "--squash",
            "--super-prefix",
            "--template",
            "--trailer",
            "--work-tree",
        }
    ),
    "block_no_verify._BOOLEAN_GLOBAL_LONG_OPTIONS": frozenset(
        {
            "--bare",
            "--glob-pathspecs",
            "--help",
            "--html-path",
            "--icase-pathspecs",
            "--info-path",
            "--literal-pathspecs",
            "--man-path",
            "--no-advice",
            "--no-lazy-fetch",
            "--no-optional-locks",
            "--no-pager",
            "--no-replace-objects",
            "--noglob-pathspecs",
            "--paginate",
            "--version",
        }
    ),
    "block_no_verify._VALUE_SHORT_OPTIONS": frozenset("CcmFt"),
    "block_no_verify._BOOLEAN_GLOBAL_SHORT_OPTIONS": frozenset("pPvh"),
    "block_no_verify._OPTIONAL_VALUE_SHORT_OPTIONS": frozenset("uS"),
    "block_no_verify._SENSITIVE_CONFIG_KEY_PREFIXES": (
        "core.hookspath",
        "include.path",
        "includeif.",
        "alias.",
    ),
    "block_no_verify._GIT_CONFIG_INJECTION_ENV_NAMES": frozenset(
        {
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_SYSTEM",
            "HOME",
            "XDG_CONFIG_HOME",
        }
    ),
    # ラッパ名とそのオプションの語彙は持たない。列挙して読み飛ばす設計は、列挙が
    # 漏れた瞬間に**素通り側へ倒れる**（`env -v` / `env -C` / `command -p` /
    # `sudo -E` / `nohup env` など）。前置トークンを全部走査するので語彙が要らず、
    # 語彙を持たない実装は語彙の陳腐化で壊れない。
    "block_no_verify._GIT_CONFIG_READ_ONLY_FLAGS": frozenset(
        {"--get", "--get-all", "--get-regexp", "--list", "--show-origin"}
    ),
    "block_no_verify._GIT_CONFIG_WRITE_FLAGS": frozenset(
        {"--add", "--replace-all", "--unset", "--unset-all"}
    ),
    "block_no_verify._GIT_CONFIG_NEW_READ_OPS": frozenset({"get", "list"}),
    "block_no_verify._GIT_CONFIG_NEW_WRITE_OPS": frozenset({"add", "set", "unset"}),
    # --- commit_quality_scanner ---
    "commit_quality_scanner._LINTABLE_SUFFIXES": {".js", ".jsx", ".ts", ".tsx", ".py", ".go", ".rs"},
    "commit_quality_scanner._MINIFIED_SUFFIXES": (".min.js", ".min.css"),
    "commit_quality_scanner._SECRET_SCAN_EXCLUDED_FILENAMES": {
        "Cargo.lock",
        "Pipfile.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "yarn.lock",
    },
    # --- config_protection ---
    "config_protection._WRITE_TOOL_NAMES": frozenset({"edit", "multiedit", "write"}),
    "config_protection.PROTECTED_FILES": _EXPECTED_PROTECTED_FILES,
    "config_protection.CONDITIONALLY_PROTECTED_FILES": _EXPECTED_CONDITIONALLY_PROTECTED_FILES,
    "config_protection.PROTECTED_FILES_FOLDED": _folded(_EXPECTED_PROTECTED_FILES),
    "config_protection.CONDITIONALLY_PROTECTED_FILES_FOLDED": _folded(
        _EXPECTED_CONDITIONALLY_PROTECTED_FILES
    ),
    # ホスト設定で保護フックを止められるキー。`env` 全般を条件にすると
    # `update-config` skill の `DEBUG=true` や `.vscode/settings.json` の
    # `terminal.integrated.env.*` を巻き込むため、`CLAQ_PYTHON` の出現そのものと
    # 保護を外せる 3 キーだけに絞る。
    "config_protection._SETTINGS_GUARD_KEYS": (
        "CLAQ_PYTHON",
        '"hooks"',
        '"enabledPlugins"',
        '"disabledPlugins"',
    ),
    "config_protection._PROTECTED_PATH_SEGMENTS": _EXPECTED_PROTECTED_PATH_SEGMENTS,
    "config_protection._PROTECTED_PATH_SEGMENTS_FOLDED": tuple(
        tuple(hook_common.normalize_protected_name(part) for part in segments)
        for segments in _EXPECTED_PROTECTED_PATH_SEGMENTS
    ),
    "config_protection._GIT_CONFIG_REPO_SEGMENTS": _EXPECTED_GIT_CONFIG_REPO_SEGMENTS,
    "config_protection._GIT_CONFIG_REPO_SEGMENTS_FOLDED": tuple(
        tuple(hook_common.normalize_protected_name(part) for part in segments)
        for segments in _EXPECTED_GIT_CONFIG_REPO_SEGMENTS
    ),
    "config_protection.GIT_CONFIG_DISPLAY_NAMES": frozenset("/".join(segments) for segments in _EXPECTED_GIT_CONFIG_REPO_SEGMENTS)
    | _EXPECTED_GLOBAL_GIT_CONFIG_DISPLAY_NAMES,
    "git_config_text.GLOBAL_GIT_CONFIG_DISPLAY_NAMES": _EXPECTED_GLOBAL_GIT_CONFIG_DISPLAY_NAMES,
    "config_protection._LINT_SECTION_HEADERS": _EXPECTED_LINT_SECTION_HEADERS,
    "config_protection._LINT_KEYS": _EXPECTED_LINT_KEYS,
    "config_protection._PACKAGE_JSON_LINT_KEYS": ("eslintConfig", "prettier"),
    "config_protection._TOX_COMMAND_KEYS": ("commands", "commands_pre", "commands_post"),
    # --- shell_tokens ---
    "shell_tokens._SHELL_SEPARATORS": frozenset({"&&", "||", ";", "|", "&", "(", ")"}),
    "shell_tokens.SHELL_WRAPPER_EXECUTABLES": frozenset({"sh", "bash", "zsh", "dash"}),
    "shell_tokens._BODY_EXECUTING_BUILTINS": frozenset({".", "source", "eval"}),
    "shell_tokens._COMMENT_PRECEDING_CHARS": frozenset(" \t\n;|&()<>"),
    "shell_tokens._HEREDOC_CONTINUATION_SUFFIXES": ("\\", "|", "&"),
    # --- hook_common ---
    "hook_common.ALWAYS_MUTATING_EDIT_EXECUTABLES": frozenset({"ed", "red", "ex", "sponge"}),
    # --- commit_risk ---
    "commit_risk._INDEX_MUTATING_GIT_SUBCOMMANDS": frozenset(
        {
            "add",
            "am",
            "apply",
            "checkout",
            "cherry-pick",
            "merge",
            "mv",
            "pull",
            "rebase",
            "reset",
            "restore",
            "revert",
            "rm",
            "stash",
            "switch",
        }
    ),
    "commit_risk._WORKTREE_MUTATING_EXECUTABLES": frozenset(
        {
            "chmod",
            "chown",
            "cp",
            "dd",
            "install",
            "ln",
            "mkdir",
            "mv",
            "patch",
            "rm",
            "shred",
            "tee",
            "touch",
            "truncate",
            "unlink",
        }
    ),
    "commit_risk._INPLACE_EDIT_EXECUTABLES": frozenset({"perl", "sed"}),
    "commit_risk._MUTATING_REDIRECT_OPERATORS": frozenset(
        {">", ">>", "&>", "&>>", ">|", "1>", "2>", "1>>", "2>>", ">&"}
    ),
}

# 完全一致で固定**しない**語彙と、その代わりに何が固定しているか。
#
# `_SECRET_PATTERNS` は 14 本の正規表現リテラルで、期待値へ複製すると同じ綴りを
# 2 か所で保守することになる。この集合には外部の突き合わせ先（`claq.mem.redaction`）
# があり、`tests/mem/test_redaction.py::TestVendorPatternSync` が「同じ検体に両者が
# 反応するか」という振る舞いで全ベンダ形式を固定している。「外部に抽出元が
# ある場合はそちらを使う」方針どおり、そちらを単一の関門にする。
_EXTERNALLY_PINNED_VOCABULARIES: dict[str, str] = {
    "commit_quality_scanner._SECRET_PATTERNS": (
        "tests/mem/test_redaction.py::TestVendorPatternSync が redaction 側との"
        "振る舞い一致で固定する（正規表現リテラルの二重保守を避ける）"
    ),
}
