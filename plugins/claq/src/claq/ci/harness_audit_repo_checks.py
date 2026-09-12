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
    safe_parse_json,
    safe_read,
)

_HOOK_WRAPPER_BASENAMES = {"claq-hook", "claq-hook.cmd"}


def _hook_command_argv(command: str) -> tuple[str, ...] | None:
    """hooks.json 内のコマンド文字列から wrapper 起動後の引数列を返す。

    hooks.json の全エントリは ``runtime/claq-hook`` （cmd.exe ホストでは PATHEXT
    が同名の ``claq-hook.cmd`` へ解決する）を起動する。裸の ``python3`` 起動
    形式は廃止したため受け付けない。先頭の ``--bg``（非 Claude ハーネス向け
    detach フラグ）はコマンドの実体ではないため取り除く。

    受け取るのは ``command`` フィールドのみで、同じエントリの ``powershell``
    （PowerShell ホスト向けに ``claq-hook.cmd`` を明示的に名指す行）は対象外。
    ``powershell`` は call 演算子 ``&`` で始まり wrapper が先頭に来ないため、
    渡しても None になる。両フィールドのモジュール・引数一致は
    ``tests/ci/test_validate_hooks.py`` のパリティ検査が担当する。

    Args:
        command: hooks.json の ``command`` フィールドの生文字列

    Returns:
        wrapper 起動後の引数トークンのタプル（--bg 除去済み）。
        形式に合致しなければ None
    """
    try:
        command_tokens = shlex.split(command)
    except ValueError:
        return None

    if len(command_tokens) < 2:
        return None

    wrapper = command_tokens[0].replace("\\", "/").rsplit("/", 1)[-1]
    if wrapper not in _HOOK_WRAPPER_BASENAMES:
        return None

    argv = tuple(command_tokens[1:])
    if argv and argv[0] == "--bg":
        argv = argv[1:]
    return argv


def _iter_hook_commands(event_hooks: Any) -> Iterator[str]:
    """イベント配下の command 文字列を順に返す。

    matcher が辞書でない、hooks が配列でない、command が文字列でない
    エントリは飛ばす。event_hooks 自体が配列でなければ何も返さない。

    Args:
        event_hooks: hooks.json の特定イベントに紐づく matcher エントリ配列

    Yields:
        フック定義の command 文字列。
    """
    if not isinstance(event_hooks, list):
        return

    for matcher in event_hooks:
        if not isinstance(matcher, dict) or not isinstance(matcher.get("hooks"), list):
            continue
        for hook in matcher["hooks"]:
            if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                yield hook["command"]


def _event_has_matching_command(
    event_hooks: Any, argv_patterns: tuple[tuple[str, ...], ...]
) -> bool:
    """イベント配下のフック定義に、指定パターンに一致する起動引数があるかを返す。

    Args:
        event_hooks: hooks.json の特定イベントに紐づく matcher エントリ配列
        argv_patterns: launcher.py 起動後の引数列として許容するプレフィックス群

    Returns:
        いずれかのフックコマンドがいずれかのパターンに前方一致すれば True
    """
    for command in _iter_hook_commands(event_hooks):
        launcher_argv = _hook_command_argv(command)
        if launcher_argv is None:
            continue
        if any(launcher_argv[: len(pattern)] == pattern for pattern in argv_patterns):
            return True
    return False


def _has_memory_lifecycle_hooks(root_dir: str | Path) -> bool:
    """実際に使用されるメモリ永続化ライフサイクル定義が hooks.json にあるかを返す。

    ディレクトリの存在だけでなく、``hooks/hooks.json`` を実際にパースし、
    ``SessionStart``/``SessionEnd`` の各イベントが実在のメモリ永続化コマンド
    （``claq.mem.cli context``、``claq.mem.cli handoff``）を
    起動していることを確認する。

    Args:
        root_dir: 監査対象のルートディレクトリ

    Returns:
        全ライフサイクルイベントで実コマンドが確認できれば True
    """
    hooks_config = safe_parse_json(safe_read(root_dir, "hooks/hooks.json"))
    if not isinstance(hooks_config, dict) or not isinstance(hooks_config.get("hooks"), dict):
        return False

    hooks = hooks_config["hooks"]
    required_commands = (
        ("SessionStart", (("claq.mem.cli", "context"),)),
        ("SessionEnd", (("claq.mem.cli", "handoff"),)),
    )
    return all(
        _event_has_matching_command(hooks.get(event, []), patterns)
        for event, patterns in required_commands
    )


# 常時注入される frontmatter description の総量の上限（文字数）。description は
# セッションごとに必ず払うコストなので、ここだけが「常時オン」の実測対象になる。
_ALWAYS_ON_DESCRIPTION_BUDGET_CHARS = 4000

# 起動ごとに全文読まれる 1 定義あたりの上限（文字数）。
_ON_INVOKE_BUDGET_CHARS = 20000

_SURFACE_GLOBS = (("skills", "SKILL.md"), ("commands", "*.md"), ("agents", "*.md"))


def _iter_surface_files(root_dir: str | Path) -> Iterator[Path]:
    """常時オン/起動時コストを持つ定義ファイルを列挙する。"""
    root = Path(root_dir)
    for directory, pattern in _SURFACE_GLOBS:
        base = root / directory
        if base.is_dir():
            yield from sorted(base.rglob(pattern))


def always_on_description_chars(root_dir: str | Path) -> int:
    """全 surface の frontmatter ``description`` の合計文字数を返す。

    「トークン最適化ドキュメントが存在するか」はハーネスの性質を何も測らない。
    実際に毎セッション払うコストは frontmatter の ``description`` だけなので、
    そちらを直接測る。

    測定は frontmatter をパースした結果の値に対して行う。``description:`` で
    始まる行を 1 行だけ数えていた頃は、YAML のブロックスカラー
    （``description: >`` に続く折り返し行）の中身が丸ごと計上から漏れ、
    ``description: >`` の行そのものが残す 2 文字しか加算されなかった。
    どれだけ長い description を折り返しで書いても
    `context-always-on-budget`（予算 4,000 文字）を通過できたということで、
    行ではなく値を測れば折り返しの書き方に依らず実コストに比例する。

    本リポジトリはブロックスカラーの description を 1 件も持たないため、この
    穴が実害として顕在化してはいなかった（実測: surface 35 ファイルで旧算法
    3,672 文字 / 新算法 3,637 文字。どちらも予算内で合否は変わらない）。
    合否が入力に応じて反転することは
    `tests/ci/test_harness_audit_score_pairs.py` の対の fixture が固定する。

    ``disable-model-invocation: true`` の surface は**計上しない**。この宣言は
    モデルによる自動発火を止めると同時に description をモデルのコンテキストから
    外すので、毎セッション払うコストがゼロになる。計上すると、払っていない
    コストで予算を圧迫して他の surface の description を削らせることになり、
    予算の意味が逆転する。スラッシュ起動専用の skill を足すたびに always-on
    予算が減る状態は、この関数が測っているつもりの量とは別物である。

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
    """起動ごとの読み込みコストが上限を超える定義ファイル名を返す。

    Args:
        root_dir: 監査対象のルートディレクトリ

    Returns:
        上限超過した定義の相対パス一覧
    """
    root = Path(root_dir)
    return [
        str(path.relative_to(root))
        for path in _iter_surface_files(root_dir)
        if len(path.read_text(encoding="utf-8", errors="replace")) > _ON_INVOKE_BUDGET_CHARS
    ]


def _hook_modules_resolve(root_dir: str | Path) -> bool:
    """hooks.json が参照する claq フックモジュールがすべて実在するかを判定する。

    「最低 N 個のモジュールがある」という個数条件は品質を測っていない。実際の
    契約は「宣言したものが実在する」であり、こちらは破損を確実に検出できる。

    Args:
        root_dir: 監査対象のルートディレクトリ

    Returns:
        参照モジュールがすべて実在すれば True
    """
    raw = safe_read(root_dir, "hooks/hooks.json")
    if not raw:
        return False
    modules = set(re.findall(r"claq\.hooks\.([A-Za-z_][A-Za-z0-9_]*)", raw))
    if not modules:
        return False
    return all(file_has_content(root_dir, f"src/claq/hooks/{name}.py") for name in modules)


def _manifest_surfaces_resolve(root_dir: str | Path) -> bool:
    """plugin.json が宣言するディレクトリがすべて実在するかを判定する。

    Args:
        root_dir: 監査対象のルートディレクトリ

    Returns:
        宣言された surface がすべて実在すれば True
    """
    manifest = safe_parse_json(safe_read(root_dir, ".claude-plugin/plugin.json"))
    if not isinstance(manifest, dict):
        return False
    declared = [
        entry
        for key in ("skills", "commands", "agents")
        for entry in (manifest.get(key) or [])
        if isinstance(entry, str)
    ]
    if not declared:
        return False
    # 宣言値は `./skills/` のようなディレクトリパス。ここだけは存在検査のままにする
    # （ディレクトリに「中身が空でない」を要求すると常に False になる）。surface が
    # 空になっていないかは `tool-skill-count` が SKILL.md の件数で別に見る。
    return all(file_exists(root_dir, entry.lstrip("./").rstrip("/")) for entry in declared)


_COVERAGE_GATE_MINIMUM = 1
"""ゲートとして機能するとみなす ``fail_under`` の下限。"""

_COVERAGE_GATE_KEY_PATH = ("tool", "coverage", "report", "fail_under")
"""pyproject.toml 内でカバレッジ閾値へ至るキー列。"""


def _coverage_gate_configured(root_dir: str | Path) -> bool:
    """カバレッジ閾値（fail_under）がゲートとして機能する値で設定されているかを判定する。

    以前は ``"fail_under" in pyproject.toml`` の部分文字列照合だった。実測で
    ``fail_under = 0``（何も落とさない）も ``# fail_under = 100 (disabled)``
    （コメントアウト）も True を返し、`eval-tests-presence`（2pts）がゲート無効の
    ままで満点になった。docs/adr/verification-scope-release-gates.md の「skip されるゲートはゲートとして機能しない」
    と同じく、宣言の字面ではなく有効な値を条件にする。

    ``bool`` を明示的に弾くのは ``isinstance(True, int)`` が True になるため。
    ``fail_under = true`` は TOML としては通るが閾値ではない。

    Args:
        root_dir: 監査対象のルートディレクトリ

    Returns:
        ``[tool.coverage.report].fail_under`` が数値かつ 1 以上なら True

    Raises:
        UnicodeDecodeError: ``pyproject.toml`` が UTF-8 で読めない場合。
            `safe_read` は `OSError` しか捕捉しないため素通りする。TOML として
            読めないだけの場合（`tomllib.TOMLDecodeError`）は False として扱う。
    """
    try:
        threshold: Any = tomllib.loads(safe_read(root_dir, "pyproject.toml"))
    except tomllib.TOMLDecodeError:
        return False
    for key in _COVERAGE_GATE_KEY_PATH:
        if not isinstance(threshold, dict):
            return False
        threshold = threshold.get(key)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        return False
    return threshold >= _COVERAGE_GATE_MINIMUM


def _repo_tool_coverage_hooks_checks(root_dir: str | Path) -> list[dict[str, Any]]:
    """Tool Coverage のフック関連チェック2件を返す。"""
    return [
        {
            "id": "tool-hooks-config",
            "category": "Tool Coverage",
            "points": 2,
            "scopes": ["repo", "hooks"],
            "path": "hooks/hooks.json",
            "description": "フック設定ファイルが存在する",
            "pass": file_has_content(root_dir, "hooks/hooks.json"),
            "fix": "Create hooks/hooks.json and define baseline hook events.",
        },
        {
            "id": "tool-hooks-manifest-consistent",
            "category": "Tool Coverage",
            "points": 2,
            "scopes": ["repo", "hooks"],
            "path": "hooks/hooks.json",
            "description": "hooks.json が参照する実装モジュールがすべて実在する（個数ではなく整合を見る）",
            "pass": _hook_modules_resolve(root_dir),
            "fix": "Fix hooks.json entries that point at modules which do not exist under src/claq/hooks/.",
        },
    ]


def _repo_tool_coverage_checks(root_dir: str | Path) -> list[dict[str, Any]]:
    """repo モードの Tool Coverage カテゴリのチェック定義を返す。

    Args:
        root_dir: 監査対象のルートディレクトリ

    Returns:
        Tool Coverage チェック辞書のリスト
    """
    return _repo_tool_coverage_hooks_checks(root_dir) + [
        {
            "id": "tool-agent-count",
            "category": "Tool Coverage",
            "points": 2,
            "scopes": ["repo", "agents"],
            "path": "agents/",
            "description": "エージェント定義の surface が失われていない（破損検知の下限。数は品質指標ではない — docs/adr/definition-and-subagent-design.md）",
            "pass": count_files(root_dir, "agents", ".md") >= 5,
            "fix": "Restore agent definitions under agents/ if the surface was emptied. Do NOT split agents to raise this count.",
        },
        {
            "id": "tool-skill-count",
            "category": "Tool Coverage",
            "points": 2,
            "scopes": ["repo", "skills"],
            "path": "skills/",
            "description": "スキル定義の surface が失われていない（破損検知の下限。数は品質指標ではない — docs/adr/definition-and-subagent-design.md）",
            "pass": count_files(root_dir, "skills", "SKILL.md") >= 1,
            "fix": "Restore skill directories under skills/ if the surface was emptied. Do NOT split skills to raise this count.",
        },
        {
            "id": "tool-manifest-surface-consistent",
            "category": "Tool Coverage",
            "points": 2,
            "scopes": ["repo", "commands"],
            "path": ".claude-plugin/plugin.json",
            "description": "manifest が宣言する surface とディスク上の実体が一致している",
            "pass": _manifest_surfaces_resolve(root_dir),
            "fix": "Make .claude-plugin/plugin.json declarations match the directories that actually exist.",
        },
    ]


def _repo_context_efficiency_checks(root_dir: str | Path) -> list[dict[str, Any]]:
    """repo モードの Context Efficiency カテゴリのチェック定義を返す。

    Args:
        root_dir: 監査対象のルートディレクトリ

    Returns:
        Context Efficiency チェック辞書のリスト
    """
    return [
        {
            "id": "context-model-route",
            "category": "Context Efficiency",
            "points": 2,
            "scopes": ["repo", "commands"],
            "path": "commands/plan.md",
            "description": "モデルルーティングコマンドが存在する（タスク複雑度に応じたモデル選択）",
            "pass": file_has_content(root_dir, "commands/plan.md"),
            "fix": "Add plan command guidance in commands/plan.md.",
        },
        {
            "id": "context-always-on-budget",
            "category": "Context Efficiency",
            "points": 2,
            "scopes": ["repo"],
            "path": "skills/, commands/, agents/",
            "description": (
                "常時注入される frontmatter description の総量が予算内"
                f"（{_ALWAYS_ON_DESCRIPTION_BUDGET_CHARS} 文字）"
            ),
            "pass": always_on_description_chars(root_dir) <= _ALWAYS_ON_DESCRIPTION_BUDGET_CHARS,
            "fix": "Shorten skill/command/agent frontmatter descriptions; only they are paid on every session.",
        },
    ]


def _repo_quality_test_checks(root_dir: str | Path) -> list[dict[str, Any]]:
    """Quality Gates のテスト基盤チェック2件を返す。"""
    pyproject_toml = safe_read(root_dir, "pyproject.toml")
    validators_test = safe_read(root_dir, "tests/ci/test_validators.py")
    return [
        {
            "id": "quality-test-runner",
            "category": "Quality Gates",
            "points": 3,
            "scopes": ["repo"],
            "path": "pyproject.toml",
            "description": "一元化されたテストランナー設定が存在する（pytest testpaths）",
            "pass": "[tool.pytest.ini_options]" in pyproject_toml and "testpaths" in pyproject_toml,
            "fix": "Add [tool.pytest.ini_options] with testpaths in pyproject.toml to enforce complete suite execution.",
        },
        {
            "id": "quality-ci-validations",
            "category": "Quality Gates",
            "points": 3,
            "scopes": ["repo"],
            "path": "tests/ci/test_validators.py",
            "description": "検証(validate_*)チェーンが pytest 実行に組み込まれている",
            "pass": "validate_commands" in validators_test and "validate_agents" in validators_test,
            "fix": "Wire validate_*.py checks into tests/ci/test_validators.py so validation runs inside the standard pytest suite.",
        },
    ]


def _repo_quality_gates_checks(root_dir: str | Path) -> list[dict[str, Any]]:
    """repo モードの Quality Gates カテゴリのチェック定義を返す。

    Args:
        root_dir: 監査対象のルートディレクトリ

    Returns:
        Quality Gates チェック辞書のリスト
    """
    return _repo_quality_test_checks(root_dir) + [
        {
            "id": "quality-hook-tests",
            "category": "Quality Gates",
            "points": 2,
            "scopes": ["repo", "hooks"],
            "path": "tests/hooks/test_hook_edge_cases.py",
            "description": "フックカバレッジテストファイルが存在する",
            "pass": file_has_content(root_dir, "tests/hooks/test_hook_edge_cases.py"),
            "fix": "Add tests/hooks/test_hook_edge_cases.py for hook behavior validation.",
        },
    ]


def _repo_memory_persistence_checks(root_dir: str | Path) -> list[dict[str, Any]]:
    """repo モードの Memory Persistence カテゴリのチェック定義を返す。

    Args:
        root_dir: 監査対象のルートディレクトリ

    Returns:
        Memory Persistence チェック辞書のリスト

    Raises:
        例外は発生しません。
    """
    return [
        {
            "id": "memory-hooks-lifecycle",
            "category": "Memory Persistence",
            "points": 4,
            "scopes": ["repo", "hooks"],
            "path": "hooks/hooks.json",
            "description": "実際に使用されるメモリ永続化ライフサイクル定義が hooks/hooks.json に存在する",
            "pass": _has_memory_lifecycle_hooks(root_dir),
            "fix": "Wire real memory lifecycle commands (claq.mem.cli context/handoff) into "
            "hooks/hooks.json's SessionStart/SessionEnd events.",
        },
        {
            "id": "memory-session-hooks",
            "category": "Memory Persistence",
            "points": 4,
            "scopes": ["repo", "hooks"],
            "path": "src/claq/mem/cli.py",
            "description": "セッション永続化を担う mem CLI 実装が存在する",
            "pass": file_has_content(root_dir, "src/claq/mem/cli.py"),
            "fix": "Implement src/claq/mem/cli.py for session persistence (context/handoff commands).",
        },
        {
            "id": "memory-learning-skill",
            "category": "Memory Persistence",
            "points": 2,
            "scopes": ["repo", "skills"],
            "path": "skills/learn/SKILL.md",
            "description": "継続学習スキルが存在する（セッション観測→インスティンクト作成→スキル進化）",
            "pass": file_has_content(root_dir, "skills/learn/SKILL.md"),
            "fix": "Add skills/learn/SKILL.md for memory evolution flow.",
        },
    ]


def _repo_eval_coverage_checks(root_dir: str | Path) -> list[dict[str, Any]]:
    """repo モードの Eval Coverage カテゴリのチェック定義を返す。

    Args:
        root_dir: 監査対象のルートディレクトリ

    Returns:
        Eval Coverage チェック辞書のリスト

    Raises:
        例外は発生しません。
    """
    return [
        {
            "id": "eval-skill",
            "category": "Eval Coverage",
            "points": 4,
            "scopes": ["repo", "skills"],
            "path": "commands/harness.md",
            "description": "品質監査コマンドが存在する（ハーネス監査・スキル棚卸し・遵守率測定）",
            "pass": file_has_content(root_dir, "commands/harness.md"),
            "fix": "Add commands/harness.md for quality audit evaluation.",
        },
        {
            "id": "eval-commands",
            "category": "Eval Coverage",
            "points": 4,
            "scopes": ["repo", "commands"],
            "path": "commands/review.md",
            "description": "検証コマンドとプランコマンドが存在する",
            "pass": file_has_content(root_dir, "commands/review.md")
            and file_has_content(root_dir, "commands/plan.md"),
            "fix": "Add commands/review.md and commands/plan.md to standardize verification loops.",
        },
        {
            "id": "eval-tests-presence",
            "category": "Eval Coverage",
            "points": 2,
            "scopes": ["repo"],
            "path": "tests/",
            "description": "カバレッジゲートが設定され、テスト surface が失われていない（数は品質指標ではない）",
            "pass": count_files(root_dir, "tests", ".py") >= 1 and _coverage_gate_configured(root_dir),
            "fix": "Keep tests/ populated and configure a coverage threshold (fail_under) in pyproject.toml.",
        },
    ]


def _repo_security_core_checks(root_dir: str | Path) -> list[dict[str, Any]]:
    """Security Guardrails のスキル・エージェントチェック2件を返す。"""
    return [
        {
            "id": "security-review-skill",
            "category": "Security Guardrails",
            "points": 3,
            "scopes": ["repo", "skills"],
            "path": "skills/secure/SKILL.md",
            "description": "セキュリティレビュースキルが存在する（認証・入力処理・シークレット管理）",
            "pass": file_has_content(root_dir, "skills/secure/SKILL.md"),
            "fix": "Add skills/secure/SKILL.md for security checklist coverage.",
        },
        {
            "id": "security-agent",
            "category": "Security Guardrails",
            "points": 3,
            "scopes": ["repo", "agents"],
            "path": "agents/security-auditor.md",
            "description": "セキュリティレビューエージェントが存在する",
            "pass": file_has_content(root_dir, "agents/security-auditor.md"),
            "fix": "Add agents/security-auditor.md for delegated security audits.",
        },
    ]


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
    """hooks.json が実行前ガードのモジュールを実際に起動しているかを判定する。

    元は生テキストへの部分文字列照合（``"PreToolUse" in hooks_json``）で、
    description の散文に語が現れるだけで合格した。次に「エントリが 1 件以上
    ある」へ厳格化したが、これも起動されるコマンドの中身を見ないため、実測で
    何もしない ``{"type": "command", "command": "true"}`` が True を返し、
    `security-prompt-hook`（2pts）が保護ゼロで満点になった。

    同じファイルの `_event_has_matching_command` が argv 照合を既に実装しており、
    `_has_memory_lifecycle_hooks` はそちらを使っている。セキュリティ側だけ
    「有無」に留まる非対称を解消し、実ガードモジュールの起動を要求する。

    イベント間は ``any`` で結ぶ。本チェックの契約は「実行前ガードが含まれている」
    であり、両イベントの同時宣言ではない（``all`` にすると、ガードが片方の
    イベントへ寄っただけで満点を失う）。

    Args:
        hooks_json: hooks/hooks.json の生テキスト。

    Returns:
        実行前ガードのイベントが実ガードモジュールを起動していれば True。

    Raises:
        例外は発生しません（パース不能は False として扱う）。
    """
    parsed = safe_parse_json(hooks_json)
    if not isinstance(parsed, dict):
        return False
    hooks = parsed.get("hooks")
    if not isinstance(hooks, dict):
        return False
    return any(
        _event_has_matching_command(hooks.get(event), _PREFLIGHT_GUARD_ARGV_PATTERNS)
        for event in _PREFLIGHT_EVENTS
    )


def _repo_security_guardrails_checks(root_dir: str | Path, hooks_json: str) -> list[dict[str, Any]]:
    """repo モードの Security Guardrails カテゴリのチェック定義を返す。

    Args:
        root_dir: 監査対象のルートディレクトリ
        hooks_json: hooks/hooks.json の生テキスト

    Returns:
        Security Guardrails チェック辞書のリスト
    """
    return _repo_security_core_checks(root_dir) + [
        {
            "id": "security-prompt-hook",
            "category": "Security Guardrails",
            "points": 2,
            "scopes": ["repo", "hooks"],
            "path": "hooks/hooks.json",
            "description": "フックにプロンプト送信・ツール実行時のセキュリティガードが含まれている",
            "pass": _declares_preflight_hook(hooks_json),
            "fix": "Add prompt/tool preflight security guards in hooks/hooks.json.",
        },
        {
            "id": "security-scan-command",
            "category": "Security Guardrails",
            "points": 2,
            "scopes": ["repo", "commands"],
            "path": "commands/review.md",
            "description": "セキュリティスキャンコマンドが存在する",
            "pass": file_has_content(root_dir, "commands/review.md"),
            "fix": "Add commands/review.md with scan and remediation workflow.",
        },
    ]


def _repo_cost_efficiency_checks(root_dir: str | Path) -> list[dict[str, Any]]:
    """repo モードの Cost Efficiency カテゴリのチェック定義を返す。

    Args:
        root_dir: 監査対象のルートディレクトリ

    Returns:
        Cost Efficiency チェック辞書のリスト

    Raises:
        例外は発生しません。
    """
    return [
        {
            "id": "cost-no-oversized-surface",
            "category": "Cost Efficiency",
            "points": 3,
            "scopes": ["repo"],
            "path": "skills/, commands/",
            "description": (
                "起動ごとに読まれる定義が 1 件も肥大化していない"
                f"（各 {_ON_INVOKE_BUDGET_CHARS} 文字以内）"
            ),
            "pass": not oversized_surfaces(root_dir),
            "fix": "Split or trim the oversized SKILL.md / command definitions; each is paid in full on every invocation.",
        },
        {
            "id": "cost-model-route-command",
            "category": "Cost Efficiency",
            "points": 3,
            "scopes": ["repo", "commands"],
            "path": "commands/plan.md",
            "description": "モデルルーティングコマンドが存在する（複雑度に応じたモデル選択ポリシー）",
            "pass": file_has_content(root_dir, "commands/plan.md"),
            "fix": "Add commands/plan.md and route policies for cheap-default execution.",
        },
    ]


def get_repo_checks(root_dir: str | Path) -> list[dict[str, Any]]:
    """repo モード向けのチェック定義を返す。"""
    hooks_json = safe_read(root_dir, "hooks/hooks.json")

    return [
        *_repo_tool_coverage_checks(root_dir),
        *_repo_context_efficiency_checks(root_dir),
        *_repo_quality_gates_checks(root_dir),
        *_repo_memory_persistence_checks(root_dir),
        *_repo_eval_coverage_checks(root_dir),
        *_repo_security_guardrails_checks(root_dir, hooks_json),
        *_repo_cost_efficiency_checks(root_dir),
    ]
