"""ハーネス監査の repo モード向けチェック定義群。"""

from __future__ import annotations

import re
import shlex
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from claq.ci.ci_common import extract_frontmatter
from claq.ci.harness_audit_utils import (
    count_files,
    file_exists,
    file_has_content,
    make_check,
    safe_parse_json,
    safe_read,
)

_HOOK_WRAPPER_BASENAMES = {"claq-hook", "claq-hook.cmd"}


def _hook_command_argv(command: str) -> tuple[str, ...] | None:
    """hooks.json 内のコマンド文字列から wrapper 起動後の引数列を返す。

    hooks.json の全エントリは ``runtime/claq-hook`` （cmd.exe ホストでは PATHEXT
    が同名の ``claq-hook.cmd`` へ解決する）を起動する。裸の ``python3`` 起動は
    受け付けない。先頭の ``--bg``（非 Claude ハーネス向け detach フラグ）は
    コマンドの実体ではないため取り除く。

    同じエントリの ``powershell`` は call 演算子 ``&`` で始まり wrapper が先頭に
    来ないため、渡しても None になる。両フィールドのモジュール・引数一致は
    ``tests/ci/test_validate_hooks.py`` のパリティ検査が担当する。

    Args:
        command: hooks.json の ``command`` フィールドの生文字列

    Returns:
        wrapper 起動後の引数トークンのタプル（--bg 除去済み）。
        形式に合致しなければ None
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if len(tokens) < 2 or tokens[0].replace("\\", "/").rsplit("/", 1)[-1] not in _HOOK_WRAPPER_BASENAMES:
        return None
    argv = tuple(tokens[1:])
    return argv[1:] if argv[0] == "--bg" else argv


def _iter_hook_commands(event_hooks: Any) -> Iterator[str]:
    """イベント配下の command 文字列を順に返す。

    matcher が辞書でない、hooks が配列でない、command が文字列でない
    エントリは飛ばす。event_hooks 自体が配列でなければ何も返さない。
    """
    if not isinstance(event_hooks, list):
        return
    for matcher in event_hooks:
        if not isinstance(matcher, dict) or not isinstance(matcher.get("hooks"), list):
            continue
        for hook in matcher["hooks"]:
            if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                yield hook["command"]


def _event_has_matching_command(event_hooks: Any, argv_patterns: tuple[tuple[str, ...], ...]) -> bool:
    """イベント配下のフック定義に、指定パターンに前方一致する起動引数があるかを返す。

    Args:
        event_hooks: hooks.json の特定イベントに紐づく matcher エントリ配列
        argv_patterns: wrapper 起動後の引数列として許容するプレフィックス群
    """
    for command in _iter_hook_commands(event_hooks):
        argv = _hook_command_argv(command)
        if argv is not None and any(argv[: len(pattern)] == pattern for pattern in argv_patterns):
            return True
    return False


def _hook_events(hooks_json: str) -> dict[str, Any] | None:
    """hooks.json の生テキストからイベント名→定義の辞書を返す。形が違えば None。"""
    parsed = safe_parse_json(hooks_json)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("hooks"), dict):
        return None
    return parsed["hooks"]


_MEMORY_LIFECYCLE_COMMANDS = (
    ("SessionStart", (("claq.mem.cli", "context"),)),
    ("SessionEnd", (("claq.mem.cli", "handoff"),)),
)


def _has_memory_lifecycle_hooks(root_dir: str | Path) -> bool:
    """SessionStart / SessionEnd が実在のメモリ永続化コマンドを起動しているかを返す。

    ディレクトリの存在ではなく ``hooks/hooks.json`` をパースし、各イベントが
    ``claq.mem.cli context`` / ``claq.mem.cli handoff`` を起動していることを見る。
    """
    hooks = _hook_events(safe_read(root_dir, "hooks/hooks.json"))
    return hooks is not None and all(
        _event_has_matching_command(hooks.get(event), patterns) for event, patterns in _MEMORY_LIFECYCLE_COMMANDS
    )


# 常時注入される frontmatter description の総量の上限（文字数）。description は
# セッションごとに必ず払うコストなので、ここだけが「常時オン」の実測対象になる。
_ALWAYS_ON_DESCRIPTION_BUDGET_CHARS = 4000

# 起動ごとに全文読まれる 1 定義あたりの上限（文字数）。
_ON_INVOKE_BUDGET_CHARS = 20000

_SURFACE_GLOBS = (("skills", "SKILL.md"), ("commands", "*.md"), ("agents", "*.md"))


def _iter_surface_files(root_dir: str | Path) -> Iterator[Path]:
    """常時オン/起動時コストを持つ定義ファイルを列挙する。"""
    for directory, pattern in _SURFACE_GLOBS:
        base = Path(root_dir) / directory
        if base.is_dir():
            yield from sorted(base.rglob(pattern))


def always_on_description_chars(root_dir: str | Path) -> int:
    """全 surface の frontmatter ``description`` の合計文字数を返す。

    毎セッション払うコストは frontmatter の ``description`` だけなので、それを
    直接測る。``description:`` の行ではなくパース後の値を測るのは、YAML の
    ブロックスカラー（``description: >`` に続く折り返し行）の中身を計上から
    漏らさないため。合否が入力に応じて反転することは
    `tests/ci/test_harness_audit_score_pairs.py` の対の fixture が固定する。

    ``disable-model-invocation: true`` の surface は計上しない。この宣言は
    description をモデルのコンテキストから外すので毎セッションのコストが
    ゼロになり、計上すると払っていないコストで予算を圧迫する。

    Args:
        root_dir: 監査対象のルートディレクトリ

    Returns:
        description の合計文字数。frontmatter が無い・``description`` が文字列で
        ない・``disable-model-invocation: true`` のファイルは 0 として扱う。
    """
    total = 0
    for path in _iter_surface_files(root_dir):
        frontmatter = extract_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        if not frontmatter or frontmatter.get("disable-model-invocation") is True:
            continue
        description = frontmatter.get("description")
        if isinstance(description, str):
            total += len(description)
    return total


def oversized_surfaces(root_dir: str | Path) -> list[str]:
    """起動ごとの読み込みコストが上限を超える定義ファイルの相対パスを返す。"""
    return [
        str(path.relative_to(Path(root_dir)))
        for path in _iter_surface_files(root_dir)
        if len(path.read_text(encoding="utf-8", errors="replace")) > _ON_INVOKE_BUDGET_CHARS
    ]


def _hook_modules_resolve(root_dir: str | Path, hooks_json: str) -> bool:
    """hooks.json が参照する claq フックモジュールがすべて実在するかを判定する。

    個数ではなく「宣言したものが実在する」を契約にする。こちらは破損を確実に
    検出できる。
    """
    modules = set(re.findall(r"claq\.hooks\.([A-Za-z_][A-Za-z0-9_]*)", hooks_json))
    return bool(modules) and all(file_has_content(root_dir, f"src/claq/hooks/{name}.py") for name in modules)


def _manifest_surfaces_resolve(root_dir: str | Path) -> bool:
    """plugin.json が宣言するディレクトリがすべて実在するかを判定する。"""
    manifest = safe_parse_json(safe_read(root_dir, ".claude-plugin/plugin.json"))
    if not isinstance(manifest, dict):
        return False
    declared = [
        entry
        for key in ("skills", "commands", "agents")
        for entry in (manifest.get(key) or [])
        if isinstance(entry, str)
    ]
    # 宣言値は `./skills/` のようなディレクトリパス。ここだけは存在検査のままにする
    # （ディレクトリに「中身が空でない」を要求すると常に False になる）。surface が
    # 空になっていないかは `tool-skill-count` が SKILL.md の件数で別に見る。
    return bool(declared) and all(file_exists(root_dir, entry.lstrip("./").rstrip("/")) for entry in declared)


def _coverage_gate_configured(root_dir: str | Path) -> bool:
    """``[tool.coverage.report].fail_under`` がゲートとして機能する値（数値かつ 1 以上）かを判定する。

    部分文字列照合では ``fail_under = 0``（何も落とさない）もコメントアウトも
    合格してしまう。docs/adr/verification-scope-release-gates.md の「skip される
    ゲートはゲートとして機能しない」と同じく、字面ではなく有効な値を条件にする。
    ``bool`` を弾くのは ``isinstance(True, int)`` が True になるため
    （``fail_under = true`` は TOML としては通るが閾値ではない）。

    Raises:
        UnicodeDecodeError: ``pyproject.toml`` が UTF-8 で読めない場合。
            `safe_read` は `OSError` しか捕捉しないため素通りする。TOML として
            読めないだけの場合（`tomllib.TOMLDecodeError`）は False として扱う。
    """
    try:
        threshold: Any = tomllib.loads(safe_read(root_dir, "pyproject.toml"))
    except tomllib.TOMLDecodeError:
        return False
    for key in ("tool", "coverage", "report", "fail_under"):
        if not isinstance(threshold, dict):
            return False
        threshold = threshold.get(key)
    return isinstance(threshold, int | float) and not isinstance(threshold, bool) and threshold >= 1


_PREFLIGHT_EVENTS = ("PreToolUse", "beforeSubmitPrompt")
"""実行前ガードが載りうるフックイベント。"""

_PREFLIGHT_GUARD_ARGV_PATTERNS = (
    ("claq.hooks.block_no_verify",),
    ("claq.hooks.pre_bash_commit_quality",),
    ("claq.hooks.bash_config_protection",),
    ("claq.hooks.config_protection",),
)
"""実行前ガードとして数える wrapper 起動後の引数列。"""


def _declares_preflight_hook(hooks_json: str) -> bool:
    """hooks.json の実行前イベントが実ガードモジュールを起動しているかを判定する。

    語の出現やエントリの有無ではなく起動されるモジュールを照合する。そうしないと
    何もしない ``{"type": "command", "command": "true"}`` で
    `security-prompt-hook` が保護ゼロのまま満点になる。

    イベント間は ``any`` で結ぶ。契約は「実行前ガードが含まれている」であり、
    ``all`` にするとガードが片方のイベントへ寄っただけで満点を失う。
    """
    hooks = _hook_events(hooks_json)
    return hooks is not None and any(
        _event_has_matching_command(hooks.get(event), _PREFLIGHT_GUARD_ARGV_PATTERNS) for event in _PREFLIGHT_EVENTS
    )


def get_repo_checks(root_dir: str | Path) -> list[dict[str, Any]]:
    """repo モード向けのチェック定義を返す。"""
    hooks_json = safe_read(root_dir, "hooks/hooks.json")
    tool, context, quality = "Tool Coverage", "Context Efficiency", "Quality Gates"
    memory, evals, security, cost = "Memory Persistence", "Eval Coverage", "Security Guardrails", "Cost Efficiency"
    checks = [
        make_check(
            "tool-hooks-config", tool, 2, ["repo", "hooks"], "hooks/hooks.json",
            "フック設定ファイルが存在する",
            file_has_content(root_dir, "hooks/hooks.json"),
            "Create hooks/hooks.json and define baseline hook events.",
        ),
        make_check(
            "tool-hooks-manifest-consistent", tool, 2, ["repo", "hooks"], "hooks/hooks.json",
            "hooks.json が参照する実装モジュールがすべて実在する（個数ではなく整合を見る）",
            _hook_modules_resolve(root_dir, hooks_json),
            "Fix hooks.json entries that point at modules which do not exist under src/claq/hooks/.",
        ),
        make_check(
            "tool-agent-count", tool, 2, ["repo", "agents"], "agents/",
            "エージェント定義の surface が失われていない（破損検知の下限。数は品質指標ではない — docs/adr/definition-and-subagent-design.md）",
            count_files(root_dir, "agents", ".md") >= 5,
            "Restore agent definitions under agents/ if the surface was emptied. Do NOT split agents to raise this count.",
        ),
        make_check(
            "tool-skill-count", tool, 2, ["repo", "skills"], "skills/",
            "スキル定義の surface が失われていない（破損検知の下限。数は品質指標ではない — docs/adr/definition-and-subagent-design.md）",
            count_files(root_dir, "skills", "SKILL.md") >= 1,
            "Restore skill directories under skills/ if the surface was emptied. Do NOT split skills to raise this count.",
        ),
        make_check(
            "tool-manifest-surface-consistent", tool, 2, ["repo", "commands"], ".claude-plugin/plugin.json",
            "manifest が宣言する surface とディスク上の実体が一致している",
            _manifest_surfaces_resolve(root_dir),
            "Make .claude-plugin/plugin.json declarations match the directories that actually exist.",
        ),
        make_check(
            "context-model-route", context, 2, ["repo", "commands"], "commands/plan.md",
            "モデルルーティングコマンドが存在する（タスク複雑度に応じたモデル選択）",
            file_has_content(root_dir, "commands/plan.md"),
            "Add plan command guidance in commands/plan.md.",
        ),
        make_check(
            "context-always-on-budget", context, 2, ["repo"], "skills/, commands/, agents/",
            f"常時注入される frontmatter description の総量が予算内（{_ALWAYS_ON_DESCRIPTION_BUDGET_CHARS} 文字）",
            always_on_description_chars(root_dir) <= _ALWAYS_ON_DESCRIPTION_BUDGET_CHARS,
            "Shorten skill/command/agent frontmatter descriptions; only they are paid on every session.",
        ),
    ]
    pyproject_toml = safe_read(root_dir, "pyproject.toml")
    validators_test = safe_read(root_dir, "tests/ci/test_validators.py")
    checks += [
        make_check(
            "quality-test-runner", quality, 3, ["repo"], "pyproject.toml",
            "一元化されたテストランナー設定が存在する（pytest testpaths）",
            "[tool.pytest.ini_options]" in pyproject_toml and "testpaths" in pyproject_toml,
            "Add [tool.pytest.ini_options] with testpaths in pyproject.toml to enforce complete suite execution.",
        ),
        make_check(
            "quality-ci-validations", quality, 3, ["repo"], "tests/ci/test_validators.py",
            "検証(validate_*)チェーンが pytest 実行に組み込まれている",
            "validate_commands" in validators_test and "validate_agents" in validators_test,
            "Wire validate_*.py checks into tests/ci/test_validators.py so validation runs inside the standard pytest suite.",
        ),
        make_check(
            "quality-hook-tests", quality, 2, ["repo", "hooks"], "tests/hooks/test_hook_edge_cases.py",
            "フックカバレッジテストファイルが存在する",
            file_has_content(root_dir, "tests/hooks/test_hook_edge_cases.py"),
            "Add tests/hooks/test_hook_edge_cases.py for hook behavior validation.",
        ),
        make_check(
            "memory-hooks-lifecycle", memory, 4, ["repo", "hooks"], "hooks/hooks.json",
            "実際に使用されるメモリ永続化ライフサイクル定義が hooks/hooks.json に存在する",
            _has_memory_lifecycle_hooks(root_dir),
            "Wire real memory lifecycle commands (claq.mem.cli context/handoff) into "
            "hooks/hooks.json's SessionStart/SessionEnd events.",
        ),
        make_check(
            "memory-session-hooks", memory, 4, ["repo", "hooks"], "src/claq/mem/cli.py",
            "セッション永続化を担う mem CLI 実装が存在する",
            file_has_content(root_dir, "src/claq/mem/cli.py"),
            "Implement src/claq/mem/cli.py for session persistence (context/handoff commands).",
        ),
        make_check(
            "memory-learning-skill", memory, 2, ["repo", "skills"], "skills/learn/SKILL.md",
            "継続学習スキルが存在する（セッション観測→インスティンクト作成→スキル進化）",
            file_has_content(root_dir, "skills/learn/SKILL.md"),
            "Add skills/learn/SKILL.md for memory evolution flow.",
        ),
        make_check(
            "eval-skill", evals, 4, ["repo", "skills"], "commands/harness.md",
            "品質監査コマンドが存在する（ハーネス監査・スキル棚卸し・遵守率測定）",
            file_has_content(root_dir, "commands/harness.md"),
            "Add commands/harness.md for quality audit evaluation.",
        ),
        make_check(
            "eval-commands", evals, 4, ["repo", "commands"], "commands/review.md",
            "検証コマンドとプランコマンドが存在する",
            file_has_content(root_dir, "commands/review.md") and file_has_content(root_dir, "commands/plan.md"),
            "Add commands/review.md and commands/plan.md to standardize verification loops.",
        ),
        make_check(
            "eval-tests-presence", evals, 2, ["repo"], "tests/",
            "カバレッジゲートが設定され、テスト surface が失われていない（数は品質指標ではない）",
            count_files(root_dir, "tests", ".py") >= 1 and _coverage_gate_configured(root_dir),
            "Keep tests/ populated and configure a coverage threshold (fail_under) in pyproject.toml.",
        ),
        make_check(
            "security-review-skill", security, 3, ["repo", "skills"], "skills/secure/SKILL.md",
            "セキュリティレビュースキルが存在する（認証・入力処理・シークレット管理）",
            file_has_content(root_dir, "skills/secure/SKILL.md"),
            "Add skills/secure/SKILL.md for security checklist coverage.",
        ),
        make_check(
            "security-agent", security, 3, ["repo", "agents"], "agents/security-auditor.md",
            "セキュリティレビューエージェントが存在する",
            file_has_content(root_dir, "agents/security-auditor.md"),
            "Add agents/security-auditor.md for delegated security audits.",
        ),
        make_check(
            "security-prompt-hook", security, 2, ["repo", "hooks"], "hooks/hooks.json",
            "フックにプロンプト送信・ツール実行時のセキュリティガードが含まれている",
            _declares_preflight_hook(hooks_json),
            "Add prompt/tool preflight security guards in hooks/hooks.json.",
        ),
        make_check(
            "security-scan-command", security, 2, ["repo", "commands"], "commands/review.md",
            "セキュリティスキャンコマンドが存在する",
            file_has_content(root_dir, "commands/review.md"),
            "Add commands/review.md with scan and remediation workflow.",
        ),
        make_check(
            "cost-no-oversized-surface", cost, 3, ["repo"], "skills/, commands/",
            f"起動ごとに読まれる定義が 1 件も肥大化していない（各 {_ON_INVOKE_BUDGET_CHARS} 文字以内）",
            not oversized_surfaces(root_dir),
            "Split or trim the oversized SKILL.md / command definitions; each is paid in full on every invocation.",
        ),
        make_check(
            "cost-model-route-command", cost, 3, ["repo", "commands"], "commands/plan.md",
            "モデルルーティングコマンドが存在する（複雑度に応じたモデル選択ポリシー）",
            file_has_content(root_dir, "commands/plan.md"),
            "Add commands/plan.md and route policies for cheap-default execution.",
        ),
    ]
    return checks
