"""ハーネス監査の repo モード向けチェック定義群。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bluecore.ci.harness_audit_utils import (
    _command_parity_matches,
    count_files,
    file_exists,
    safe_read,
)


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
            "pass": file_exists(root_dir, "hooks/hooks.json"),
            "fix": "Create hooks/hooks.json and define baseline hook events.",
        },
        {
            "id": "tool-hooks-impl-count",
            "category": "Tool Coverage",
            "points": 2,
            "scopes": ["repo", "hooks"],
            "path": "src/bluecore/hooks/",
            "description": "最低20個のフック実装モジュールが存在する",
            "pass": count_files(root_dir, "src/bluecore/hooks", ".py") >= 20,
            "fix": "Add missing hook implementations in src/bluecore/hooks/.",
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
            "description": "最低10個のエージェント定義が存在する",
            "pass": count_files(root_dir, "agents", ".md") >= 10,
            "fix": "Add or restore agent definitions under agents/.",
        },
        {
            "id": "tool-skill-count",
            "category": "Tool Coverage",
            "points": 2,
            "scopes": ["repo", "skills"],
            "path": "skills/",
            "description": "最低20個のスキル定義が存在する",
            "pass": count_files(root_dir, "skills", "SKILL.md") >= 20,
            "fix": "Add missing skill directories with SKILL.md definitions.",
        },
        {
            "id": "tool-command-parity",
            "category": "Tool Coverage",
            "points": 2,
            "scopes": ["repo", "commands"],
            "path": ".opencode/commands/harness.md",
            "description": "ハーネス監査コマンドのプライマリと OpenCode コマンドドック間でパリティが取れている",
            "pass": _command_parity_matches(root_dir),
            "fix": "Sync commands/harness.md and .opencode/commands/harness.md.",
        },
    ]


def _repo_context_compact_checks(root_dir: str | Path) -> list[dict[str, Any]]:
    """Context Efficiency のコンパクト関連チェック2件を返す。"""
    return [
        {
            "id": "context-strategic-compact",
            "category": "Context Efficiency",
            "points": 3,
            "scopes": ["repo", "skills"],
            "path": "output-styles/slim.md",
            "description": "コンテキスト最大圧縮 output-style が存在する（LLMレスポンス・ファイルの原始人口調圧縮）",
            "pass": file_exists(root_dir, "output-styles/slim.md"),
            "fix": "Add output-styles/slim.md for maximum context compression.",
        },
        {
            "id": "context-suggest-compact-hook",
            "category": "Context Efficiency",
            "points": 3,
            "scopes": ["repo", "hooks"],
            "path": "src/bluecore/hooks/suggest_compact.py",
            "description": "コンテキスト圧縮自動化フックが存在する（セッション中にコンテキスト圧縮提案）",
            "pass": file_exists(root_dir, "src/bluecore/hooks/suggest_compact.py"),
            "fix": "Implement src/bluecore/hooks/suggest_compact.py for context pressure hints.",
        },
    ]


def _repo_context_efficiency_checks(root_dir: str | Path) -> list[dict[str, Any]]:
    """repo モードの Context Efficiency カテゴリのチェック定義を返す。

    Args:
        root_dir: 監査対象のルートディレクトリ

    Returns:
        Context Efficiency チェック辞書のリスト
    """
    return _repo_context_compact_checks(root_dir) + [
        {
            "id": "context-model-route",
            "category": "Context Efficiency",
            "points": 2,
            "scopes": ["repo", "commands"],
            "path": "commands/plan.md",
            "description": "モデルルーティングコマンドが存在する（タスク複雑度に応じたモデル選択）",
            "pass": file_exists(root_dir, "commands/plan.md"),
            "fix": "Add plan command guidance in commands/plan.md.",
        },
        {
            "id": "context-token-doc",
            "category": "Context Efficiency",
            "points": 2,
            "scopes": ["repo"],
            "path": "docs/token-optimization.md",
            "description": "トークン最適化ドキュメントが存在する",
            "pass": file_exists(root_dir, "docs/token-optimization.md"),
            "fix": "Add docs/token-optimization.md with concrete context-cost controls.",
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
            "pass": file_exists(root_dir, "tests/hooks/test_hook_edge_cases.py"),
            "fix": "Add tests/hooks/test_hook_edge_cases.py for hook behavior validation.",
        },
        {
            "id": "quality-doctor-script",
            "category": "Quality Gates",
            "points": 2,
            "scopes": ["repo"],
            "path": "src/bluecore/hooks/session_install.py",
            "description": "インストール状態チェック用ドクタースクリプトが存在する",
            "pass": file_exists(root_dir, "src/bluecore/hooks/session_install.py"),
            "fix": "Add src/bluecore/hooks/session_install.py for install-state integrity checks.",
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
            "id": "memory-hooks-dir",
            "category": "Memory Persistence",
            "points": 4,
            "scopes": ["repo", "hooks"],
            "path": "hooks/memory-persistence/",
            "description": "メモリ永続化フックディレクトリが存在する",
            "pass": file_exists(root_dir, "hooks/memory-persistence"),
            "fix": "Add hooks/memory-persistence with lifecycle hook definitions.",
        },
        {
            "id": "memory-session-hooks",
            "category": "Memory Persistence",
            "points": 4,
            "scopes": ["repo", "hooks"],
            "path": "src/bluecore/hooks/session_start.py",
            "description": "セッション開始・終了時の永続化モジュールが存在する",
            "pass": file_exists(root_dir, "src/bluecore/hooks/session_start.py")
            and file_exists(root_dir, "src/bluecore/hooks/session_end.py"),
            "fix": "Implement src/bluecore/hooks/session_start.py and src/bluecore/hooks/session_end.py.",
        },
        {
            "id": "memory-learning-skill",
            "category": "Memory Persistence",
            "points": 2,
            "scopes": ["repo", "skills"],
            "path": "skills/learn/SKILL.md",
            "description": "継続学習スキルが存在する（セッション観測→インスティンクト作成→スキル進化）",
            "pass": file_exists(root_dir, "skills/learn/SKILL.md"),
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
            "pass": file_exists(root_dir, "commands/harness.md"),
            "fix": "Add commands/harness.md for quality audit evaluation.",
        },
        {
            "id": "eval-commands",
            "category": "Eval Coverage",
            "points": 4,
            "scopes": ["repo", "commands"],
            "path": "commands/review.md",
            "description": "検証コマンドとプランコマンドが存在する",
            "pass": file_exists(root_dir, "commands/review.md")
            and file_exists(root_dir, "commands/plan.md"),
            "fix": "Add commands/review.md and commands/plan.md to standardize verification loops.",
        },
        {
            "id": "eval-tests-presence",
            "category": "Eval Coverage",
            "points": 2,
            "scopes": ["repo"],
            "path": "tests/",
            "description": "最低100個のテストファイル(.py)が存在する",
            "pass": count_files(root_dir, "tests", ".py") >= 100,
            "fix": "Increase automated test coverage across src/bluecore modules.",
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
            "pass": file_exists(root_dir, "skills/secure/SKILL.md"),
            "fix": "Add skills/secure/SKILL.md for security checklist coverage.",
        },
        {
            "id": "security-agent",
            "category": "Security Guardrails",
            "points": 3,
            "scopes": ["repo", "agents"],
            "path": "agents/security-auditor.md",
            "description": "セキュリティレビューエージェントが存在する",
            "pass": file_exists(root_dir, "agents/security-auditor.md"),
            "fix": "Add agents/security-auditor.md for delegated security audits.",
        },
    ]


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
            "pass": "beforeSubmitPrompt" in hooks_json or "PreToolUse" in hooks_json,
            "fix": "Add prompt/tool preflight security guards in hooks/hooks.json.",
        },
        {
            "id": "security-scan-command",
            "category": "Security Guardrails",
            "points": 2,
            "scopes": ["repo", "commands"],
            "path": "commands/review.md",
            "description": "セキュリティスキャンコマンドが存在する",
            "pass": file_exists(root_dir, "commands/review.md"),
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
            "id": "cost-skill",
            "category": "Cost Efficiency",
            "points": 4,
            "scopes": ["repo", "skills"],
            "path": "output-styles/slim.md",
            "description": "コスト最適化 output-style が存在する（トークン削減による予算管理）",
            "pass": file_exists(root_dir, "output-styles/slim.md"),
            "fix": "Add output-styles/slim.md for budget-aware routing.",
        },
        {
            "id": "cost-doc",
            "category": "Cost Efficiency",
            "points": 3,
            "scopes": ["repo"],
            "path": "docs/token-optimization.md",
            "description": "コスト最適化ドキュメントが存在する",
            "pass": file_exists(root_dir, "docs/token-optimization.md"),
            "fix": "Create docs/token-optimization.md with target settings and tradeoffs.",
        },
        {
            "id": "cost-model-route-command",
            "category": "Cost Efficiency",
            "points": 3,
            "scopes": ["repo", "commands"],
            "path": "commands/plan.md",
            "description": "モデルルーティングコマンドが存在する（複雑度に応じたモデル選択ポリシー）",
            "pass": file_exists(root_dir, "commands/plan.md"),
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
