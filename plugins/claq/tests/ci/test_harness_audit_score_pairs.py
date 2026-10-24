"""採点述語ごとに「満点になる fixture」と「ならない fixture」を対で固定する。

知識カード `audit-score-measures-existence-not-effectiveness` が要求する形式。
片側（不合格になる fixture）だけを置くと、全部を 0 点にする実装でも緑になり、
逆に片側（合格する fixture）だけだと、全部を満点にする実装でも緑になる。両方を
同じ表に並べて初めて「述語が入力に応じて動いている」ことの証跡になる。

判定は述語ヘルパーではなく `get_repo_checks` / `build_report` が返すチェックの
``pass`` に対して行う。ヘルパー単体を検査しても、将来そのヘルパーを呼ばなくなる
配線変更が緑のまま通ってしまい、カードが指摘している穴がそのまま残るため。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import claq.ci.harness_audit as harness_audit
from claq.ci.harness_audit_repo_checks import get_repo_checks

from ._support import _isolate_home as _isolate_home
from ._support import _write_files

_WRAPPER = '"${CLAUDE_PLUGIN_ROOT}/runtime/claq-hook"'
_FOLDED = "\n".join(f"  {'あ' * 80}" for _ in range(60))
_TEST_PY = "def test_x(): pass\n"
_SPEC_JS = "it('x', () => {});\n"


def _repo_check(root_dir: Path, check_id: str) -> dict:
    """repo モードのチェック 1 件を返す。"""
    return next(check for check in get_repo_checks(root_dir) if check["id"] == check_id)


def _consumer_check_pass(root_dir: Path, check_id: str) -> bool:
    """consumer モードのチェック 1 件の合否を返す。"""
    report = harness_audit.build_report("repo", root_dir=root_dir, target_mode="consumer")
    return next(check for check in report["checks"] if check["id"] == check_id)["pass"]


def _entry(command: str) -> dict:
    """hooks.json のイベント配下に置く、コマンド 1 件の matcher エントリを返す。"""
    return {"hooks": [{"type": "command", "command": command}]}


def test_real_repo_passes_every_repo_check() -> None:
    """本リポジトリが自身の repo 監査を全項目通過すること。

    監査全体の合否を見るのはこのテストだけである（`max_score` を読むテストは
    監査の不合格では落ちない）。これが無いと不合格の check が見過ごされるため、
    ここで**落ちた check の id を名指しして**報告する。hooks.json の厳格化や
    Python 構造前提のチェック（memory-hooks-lifecycle・security-prompt-hook 等）の
    巻き込みもここで検出する。
    """
    plugin_root = Path(__file__).resolve().parents[2]
    failing = sorted(check["id"] for check in get_repo_checks(plugin_root) if not check["pass"])

    assert failing == [], f"repo 監査の不合格 check: {failing}"


@pytest.mark.parametrize(
    ("frontmatter", "expected"),
    [
        ("name: small\ndescription: 短い説明", True),
        (f"name: huge\ndescription: >\n{_FOLDED}", False),
        (f"name: huge\ndisable-model-invocation: true\ndescription: >\n{_FOLDED}", True),
    ],
    ids=["short", "block-scalar-over-budget", "slash-only-not-counted"],
)
def test_context_always_on_budget(tmp_path: Path, frontmatter: str, expected: bool) -> None:
    """`context-always-on-budget`（2pts）は description を行ではなく値で測る。

    ``description: >`` の行だけを見て 2 文字と数えると、超過 description が満点で通る。
    ``disable-model-invocation: true`` は description をモデルのコンテキストから外し毎セッションの
    コストがゼロになるため計上しない（計上するとスラッシュ起動専用の skill を足すたびに他の
    description を削る羽目になる）。不合格行と同じ長さを使い、差が宣言 1 行だけであることを固定する。
    """
    _write_files(tmp_path, {"skills/s/SKILL.md": f"---\n{frontmatter}\n---\n\n本文\n"})

    assert _repo_check(tmp_path, "context-always-on-budget")["pass"] is expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("[tool.coverage.report]\nfail_under = 100\n", True),
        ("[tool.coverage.report]\nfail_under = 99.5\n", True),
        ("[tool.coverage.report]\nfail_under = 0\n", False),
        ("[tool.coverage.report]\n# fail_under = 100 (disabled)\n", False),
        ("[tool.coverage.report]\nfail_under = true\n", False),
        ("[tool.other]\nfail_under = 100\n", False),
        ("[tool.coverage.report\nfail_under = 100\n", False),
    ],
    ids=["effective", "float", "gate-disabled", "commented-out", "bool-is-not-threshold", "other-section", "broken-toml"],
)
def test_eval_tests_presence_requires_effective_coverage_gate(tmp_path: Path, body: str, expected: bool) -> None:
    """`eval-tests-presence`（2pts）はカバレッジ閾値の有効値を要求する。

    ``"fail_under" in pyproject.toml`` のような部分文字列照合では、ゲートが無効（0）でも
    コメントアウトでも満点になる。coverage.py が受ける float 閾値は有効値として扱う。
    """
    _write_files(tmp_path, {"pyproject.toml": body, "tests/test_app.py": _TEST_PY})

    assert _repo_check(tmp_path, "eval-tests-presence")["pass"] is expected


@pytest.mark.parametrize(
    ("hooks", "expected"),
    [
        ({"PreToolUse": [_entry(f"{_WRAPPER} claq.hooks.block_no_verify")]}, True),
        ({"beforeSubmitPrompt": [_entry(f"{_WRAPPER} claq.hooks.config_protection")]}, True),
        ({"PreToolUse": [_entry("true")]}, False),
        ({"PreToolUse": [_entry(f"{_WRAPPER} claq.hooks.pre_compact")]}, False),
        ({"PreToolUse": []}, False),
        ({"SessionStart": [_entry(f"{_WRAPPER} claq.hooks.config_protection")]}, False),
    ],
    ids=["real-guard", "beforesubmitprompt-alone", "noop-command", "unrelated-module", "empty", "not-pre-execution"],
)
def test_security_prompt_hook_requires_real_guard_module(tmp_path: Path, hooks: dict, expected: bool) -> None:
    """`security-prompt-hook`（2pts）は実ガードモジュールの起動を要求する。

    エントリの有無だけを見ると、何もしない ``command: "true"`` でも満点になる。
    イベント間を ``all`` で結ぶと、ガードが beforeSubmitPrompt だけに寄っただけで満点を失う
    （契約は「実行前ガードが含まれている」であって両イベントの同時宣言ではない）。
    """
    _write_files(tmp_path, {"hooks/hooks.json": json.dumps({"hooks": hooks})})

    assert _repo_check(tmp_path, "security-prompt-hook")["pass"] is expected


_FULL_LIFECYCLE = {
    "SessionStart": [_entry(f"{_WRAPPER} claq.mem.cli context"), _entry(f"{_WRAPPER} claq.hooks.session_start")],
    "Stop": [_entry(f"{_WRAPPER} --bg claq.hooks.session_end")],
    "SessionEnd": [_entry(f"{_WRAPPER} --bg claq.mem.cli handoff")],
}


@pytest.mark.parametrize(
    ("hooks_json", "expected"),
    [
        (None, False),
        ("not-json", False),
        (json.dumps({"hooks": ["not", "a", "dict"]}), False),
        (
            json.dumps(
                {"hooks": {"SessionStart": [_entry("claq-hook claq.hooks.unrelated")], "Stop": [], "SessionEnd": []}}
            ),
            False,
        ),
        (json.dumps({"hooks": {"SessionStart": [_entry(f"{_WRAPPER} claq.mem.cli context")]}}), False),
        (json.dumps({"hooks": _FULL_LIFECYCLE}), True),
    ],
    ids=["missing", "malformed-json", "hooks-not-dict", "stale-commands", "partial-lifecycle", "real-lifecycle"],
)
def test_memory_hooks_lifecycle_requires_real_commands(tmp_path: Path, hooks_json: str | None, expected: bool) -> None:
    """`memory-hooks-lifecycle` は hooks.json が実在のメモリ永続化コマンドを起動しているときだけ通る。

    ディレクトリの存在だけで合格させると、hooks.json が欠落・空・不正でも満点になる回帰があった。
    そのため hooks.json を置く行ではフックディレクトリ相当（memory-persistence）も残しておく。
    合格行は実際の hooks.json と同じく Stop / SessionEnd を --bg 付きで登録する。
    """
    if hooks_json is not None:
        (tmp_path / "hooks" / "memory-persistence").mkdir(parents=True)
        _write_files(tmp_path, {"hooks/hooks.json": hooks_json})

    check = _repo_check(tmp_path, "memory-hooks-lifecycle")

    assert (check["pass"], check["path"]) == (expected, "hooks/hooks.json")


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"src/a.spec.js": _SPEC_JS}, True),
        ({"tests/test_app.py": _TEST_PY}, True),
        ({"package.json": json.dumps({"name": "p", "scripts": {"test": "x"}})}, True),
        ({"package.json": json.dumps({"name": "my-project"})}, False),
        ({"node_modules/dep/a.spec.js": _SPEC_JS}, False),
        ({"vendor/dep/a.spec.js": _SPEC_JS}, False),
        ({".venv/dep/a.spec.js": _SPEC_JS}, False),
        ({"node_modules/dep/test_dep.py": _TEST_PY}, False),
    ],
    ids=[
        "project-spec", "pytest-only", "package-test-script", "package-without-scripts",
        "node_modules-spec", "vendor-spec", "venv-spec", "vendor-python",
    ],
)
def test_consumer_test_suite_ignores_vendor_trees(tmp_path: Path, files: dict[str, str], expected: bool) -> None:
    """`consumer-test-suite`（4pts）はプロジェクト自身のテスト入口で通り、ベンダーツリーのテストは数えない。

    `_walk_dir` に枝刈りが無いと、`node_modules/dep/a.spec.js` を 1 個置いただけのツリーが
    `has_file_with_extension` 経由で満点を取れる。JS の規約を持たない pytest 専用リポジトリでも通る。
    """
    _write_files(tmp_path, files)

    assert _consumer_check_pass(tmp_path, "consumer-test-suite") is expected


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({f"tests/test_mod{index}.py": _TEST_PY for index in range(2)}, False),
        ({f"tests/test_mod{index}.py": _TEST_PY for index in range(3)}, True),
        ({"src/app.py": "x = 1\n"}, False),
        ({"evals/case.yaml": "name: case\n"}, True),
    ],
    ids=["two-pytest-files", "three-pytest-files", "no-tests-or-evals", "evals-directory"],
)
def test_consumer_eval_coverage_counts_python_tests_at_js_threshold(
    tmp_path: Path, files: dict[str, str], expected: bool
) -> None:
    """`consumer-eval-coverage` は pytest も JS と同じ 3 件の閾値で数え、`evals/` 経路も残す。

    `tests/*.test.js` >= 3 しか見ないと、大量の pytest を持つリポジトリでも `evals/` が無ければ 0 点になる。
    """
    _write_files(tmp_path, files)

    assert _consumer_check_pass(tmp_path, "consumer-eval-coverage") is expected
