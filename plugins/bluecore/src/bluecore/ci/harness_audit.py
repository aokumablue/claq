"""ハーネス監査の決定論的スコアリングを行う。"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bluecore.ci.harness_audit_repo_checks import get_repo_checks
from bluecore.ci.harness_audit_utils import (
    _command_parity_matches as _command_parity_matches,
)
from bluecore.ci.harness_audit_utils import (
    _has_any_file as _has_any_file,
)
from bluecore.ci.harness_audit_utils import (
    _has_gitlab_security_scanning,
    count_files,
    detect_target_mode,
    file_exists,
    find_plugin_install,
    has_file_with_extension,
    safe_parse_json,
    safe_read,
)
from bluecore.ci.harness_audit_utils import (
    read_text as read_text,
)
from bluecore.lib.git_hosting import (
    detect_git_hosting_service,
    get_git_hosting_service_label,
    normalize_git_hosting_service,
)

CATEGORIES = [
    "Tool Coverage",
    "Context Efficiency",
    "Quality Gates",
    "Memory Persistence",
    "Eval Coverage",
    "Security Guardrails",
    "Cost Efficiency",
]

VALID_SCOPES = {"repo", "hooks", "skills", "commands", "agents"}
VALID_FORMATS = {"text", "json"}


def normalize_scope(scope: str | None) -> str:
    """scope を正規化する。"""
    value = (scope or "repo").lower()
    if value not in VALID_SCOPES:
        raise ValueError(f"Invalid scope: {scope}")
    return value


def _apply_arg(parsed: dict[str, Any], args: list[str], index: int) -> int:
    """単一の CLI 引数を解釈して parsed を更新し、次のインデックスを返す。

    Args:
        parsed: 解析結果を蓄積する辞書（インプレース更新）
        args: 全引数のリスト
        index: 現在処理中の引数のインデックス

    Returns:
        次に処理すべき引数のインデックス

    Raises:
        ValueError: 不明な引数が渡された場合
    """
    arg = args[index]

    if arg in {"--help", "-h"}:
        parsed["help"] = True
        return index + 1

    if arg == "--format":
        parsed["format"] = (args[index + 1] if index + 1 < len(args) else "").lower()
        return index + 2

    if arg.startswith("--format="):
        parsed["format"] = arg.split("=", 1)[1].lower()
        return index + 1

    if arg == "--scope":
        parsed["scope"] = normalize_scope(args[index + 1] if index + 1 < len(args) else None)
        return index + 2

    if arg.startswith("--scope="):
        parsed["scope"] = normalize_scope(arg.split("=", 1)[1])
        return index + 1

    if arg == "--root":
        parsed["root"] = Path(args[index + 1] if index + 1 < len(args) else os.getcwd()).resolve()
        return index + 2

    if arg.startswith("--root="):
        parsed["root"] = Path(arg.split("=", 1)[1] or os.getcwd()).resolve()
        return index + 1

    if arg.startswith("-"):
        raise ValueError(f"Unknown argument: {arg}")

    parsed["scope"] = normalize_scope(arg)
    return index + 1


def parse_args(argv: Sequence[str] | None = None) -> dict[str, Any]:
    """CLI 引数を JS 実装と同じルールで解析する。"""
    args = list(sys.argv[1:] if argv is None else argv)
    parsed: dict[str, Any] = {
        "scope": "repo",
        "format": "text",
        "help": False,
        "root": Path(os.getcwd()).resolve(),
    }

    index = 0
    while index < len(args):
        index = _apply_arg(parsed, args, index)

    if parsed["format"] not in VALID_FORMATS:
        raise ValueError(f"Invalid format: {parsed['format']}. Use text or json.")

    return parsed


def _consumer_security_status(root_dir: str | Path, hosting_service: str) -> bool:
    """consumer プロジェクトのセキュリティポリシー有無を判定する。

    Args:
        root_dir: 監査対象のルートディレクトリ
        hosting_service: 正規化済みの Git ホスティングサービス名

    Returns:
        セキュリティポリシー・スキャン設定が存在すれば True

    Raises:
        例外は発生しません。
    """
    security_pass = file_exists(root_dir, "SECURITY.md")
    if hosting_service == "gitlab":
        return security_pass or _has_gitlab_security_scanning(root_dir)
    return (
        security_pass
        or file_exists(root_dir, ".github/dependabot.yml")
        or file_exists(root_dir, ".github/codeql.yml")
    )


def _consumer_tool_coverage_checks(root_dir: str | Path, plugin_install: str | None) -> list[dict[str, Any]]:
    """consumer モードの Tool Coverage カテゴリのチェック定義を返す。

    Args:
        root_dir: 監査対象のルートディレクトリ
        plugin_install: 検出されたプラグインのインストールパス（無ければ None）

    Returns:
        Tool Coverage チェック辞書のリスト

    Raises:
        例外は発生しません。
    """
    return [
        {
            "id": "consumer-plugin-install",
            "category": "Tool Coverage",
            "points": 4,
            "scopes": ["repo"],
            "path": "~/.claude/plugins/everything-claude-code/",
            "description": "プラグインがインストールされている",
            "pass": bool(plugin_install),
            "fix": "Install the ECC plugin for this user or project before auditing project-specific harness quality.",
        },
        {
            "id": "consumer-project-overrides",
            "category": "Tool Coverage",
            "points": 3,
            "scopes": ["repo", "hooks", "skills", "commands", "agents"],
            "path": ".claude/",
            "description": "プロジェクト固有のハーネスオーバーライドが .claude/ 配下に存在する",
            "pass": count_files(root_dir, ".claude/agents", ".md") > 0
            or count_files(root_dir, ".claude/skills", "SKILL.md") > 0
            or count_files(root_dir, ".claude/commands", ".md") > 0
            or file_exists(root_dir, ".claude/settings.json")
            or file_exists(root_dir, ".claude/hooks.json"),
            "fix": "Add project-local .claude hooks, commands, skills, or settings that tailor ECC to this repo.",
        },
    ]


def _consumer_context_efficiency_checks(root_dir: str | Path) -> list[dict[str, Any]]:
    """consumer モードの Context Efficiency カテゴリのチェック定義を返す。

    Args:
        root_dir: 監査対象のルートディレクトリ

    Returns:
        Context Efficiency チェック辞書のリスト

    Raises:
        例外は発生しません。
    """
    return [
        {
            "id": "consumer-instructions",
            "category": "Context Efficiency",
            "points": 3,
            "scopes": ["repo"],
            "path": "AGENTS.md",
            "description": "プロジェクトが明示的なエージェント・命令コンテキストを持つ",
            "pass": file_exists(root_dir, "AGENTS.md")
            or file_exists(root_dir, "CLAUDE.md")
            or file_exists(root_dir, ".claude/CLAUDE.md"),
            "fix": "Add AGENTS.md or CLAUDE.md so the harness has project-specific instructions.",
        },
        {
            "id": "consumer-project-config",
            "category": "Context Efficiency",
            "points": 2,
            "scopes": ["repo", "hooks"],
            "path": ".mcp.json",
            "description": "プロジェクトがローカル MCP・Claude 設定を宣言している",
            "pass": file_exists(root_dir, ".mcp.json")
            or file_exists(root_dir, ".claude/settings.json")
            or file_exists(root_dir, ".claude/settings.local.json"),
            "fix": "Add .mcp.json or .claude/settings.json so project-local tool configuration is explicit.",
        },
    ]


def _consumer_quality_gates_checks(
    root_dir: str | Path,
    package_json: dict[str, Any],
    ci_path: str,
    hosting_label: str,
    ci_pass: bool,
) -> list[dict[str, Any]]:
    """consumer モードの Quality Gates カテゴリのチェック定義を返す。

    Args:
        root_dir: 監査対象のルートディレクトリ
        package_json: 解析済みの package.json（無い場合は空辞書）
        ci_path: 表示用の CI 設定パス
        hosting_label: Git ホスティングサービスの表示ラベル
        ci_pass: CI 設定が存在するかの判定結果

    Returns:
        Quality Gates チェック辞書のリスト

    Raises:
        例外は発生しません。
    """
    return [
        {
            "id": "consumer-test-suite",
            "category": "Quality Gates",
            "points": 4,
            "scopes": ["repo"],
            "path": "tests/",
            "description": "プロジェクトが自動テストのエントリポイントを持つ",
            "pass": (
                isinstance(package_json.get("scripts"), dict) and isinstance(package_json["scripts"].get("test"), str)
            )
            or count_files(root_dir, "tests", ".test.js") > 0
            or has_file_with_extension(root_dir, ".", [".spec.js", ".spec.ts", ".test.ts"]),
            "fix": "Add a test script or checked-in tests so harness recommendations can be verified automatically.",
        },
        {
            "id": "consumer-ci-workflow",
            "category": "Quality Gates",
            "points": 3,
            "scopes": ["repo"],
            "path": ci_path,
            "description": f"プロジェクトが {hosting_label} CI 設定をチェックインしている",
            "pass": ci_pass,
            "fix": f"Add at least one CI configuration file for {hosting_label} so harness and test checks run outside local development.",
        },
    ]


def _consumer_memory_and_eval_checks(root_dir: str | Path) -> list[dict[str, Any]]:
    """consumer モードの Memory Persistence と Eval Coverage カテゴリのチェック定義を返す。

    Args:
        root_dir: 監査対象のルートディレクトリ

    Returns:
        Memory Persistence と Eval Coverage チェック辞書のリスト

    Raises:
        例外は発生しません。
    """
    return [
        {
            "id": "consumer-memory-notes",
            "category": "Memory Persistence",
            "points": 2,
            "scopes": ["repo"],
            "path": ".claude/memory.md",
            "description": "プロジェクトメモリ・恒久的なノートがチェックインされている",
            "pass": file_exists(root_dir, ".claude/memory.md") or count_files(root_dir, "docs/adr", ".md") > 0,
            "fix": "Add durable project memory such as .claude/memory.md or ADRs under docs/adr/.",
        },
        {
            "id": "consumer-eval-coverage",
            "category": "Eval Coverage",
            "points": 2,
            "scopes": ["repo"],
            "path": "evals/",
            "description": "プロジェクトが評価テストまたは複数の自動テストを持つ",
            "pass": count_files(root_dir, "evals", None) > 0 or count_files(root_dir, "tests", ".test.js") >= 3,
            "fix": "Add eval fixtures or at least a few focused automated tests for critical flows.",
        },
    ]


def _consumer_security_policy_checks(
    gitignore: str, security_path: str, hosting_label: str, security_pass: bool
) -> list[dict[str, Any]]:
    """consumer モードのセキュリティポリシー・シークレット衛生チェック2件を返す。"""
    return [
        {
            "id": "consumer-security-policy",
            "category": "Security Guardrails",
            "points": 2,
            "scopes": ["repo"],
            "path": security_path,
            "description": "プロジェクトがセキュリティポリシー・自動依存スキャンを公開している",
            "pass": security_pass,
            "fix": f"Add SECURITY.md or {hosting_label}-appropriate dependency/code scanning configuration to document the project security posture.",
        },
        {
            "id": "consumer-secret-hygiene",
            "category": "Security Guardrails",
            "points": 2,
            "scopes": ["repo"],
            "path": ".gitignore",
            "description": "プロジェクトが一般的なシークレット環境ファイルを無視している",
            "pass": ".env" in gitignore,
            "fix": "Ignore .env-style files in .gitignore so secrets do not land in the repo.",
        },
    ]


@dataclass(frozen=True)
class _GuardrailsCtx:
    """_consumer_security_guardrails_checks のセキュリティチェックコンテキスト。"""

    root_dir: str | Path
    gitignore: str
    project_hooks: str
    security_path: str
    hosting_label: str
    security_pass: bool


def _consumer_security_guardrails_checks(ctx: _GuardrailsCtx) -> list[dict[str, Any]]:
    """consumer モードの Security Guardrails カテゴリのチェック定義を返す。

    Args:
        root_dir: 監査対象のルートディレクトリ
        gitignore: .gitignore の生テキスト
        project_hooks: .claude/settings.json の生テキスト
        security_path: 表示用のセキュリティ設定パス
        hosting_label: Git ホスティングサービスの表示ラベル
        security_pass: セキュリティポリシーが存在するかの判定結果

    Returns:
        Security Guardrails チェック辞書のリスト
    """
    return _consumer_security_policy_checks(
        ctx.gitignore, ctx.security_path, ctx.hosting_label, ctx.security_pass
    ) + [
        {
            "id": "consumer-hook-guardrails",
            "category": "Security Guardrails",
            "points": 2,
            "scopes": ["repo", "hooks"],
            "path": ".claude/settings.json",
            "description": "プロジェクトローカルフック設定がツール・プロンプトガードを参照している",
            "pass": "PreToolUse" in ctx.project_hooks
            or "beforeSubmitPrompt" in ctx.project_hooks
            or file_exists(ctx.root_dir, ".claude/hooks.json"),
            "fix": "Add project-local hook settings or hook definitions for prompt/tool guardrails.",
        },
    ]


def get_consumer_checks(root_dir: str | Path, git_hosting_service: str = "github") -> list[dict[str, Any]]:
    """consumer project 向けのチェック定義を返す。"""
    package_json = safe_parse_json(safe_read(root_dir, "package.json"))
    if not isinstance(package_json, dict):
        package_json = {}

    gitignore = safe_read(root_dir, ".gitignore")
    project_hooks = safe_read(root_dir, ".claude/settings.json")
    plugin_install = find_plugin_install(root_dir)
    hosting_service = normalize_git_hosting_service(git_hosting_service)
    hosting_label = get_git_hosting_service_label(hosting_service)
    ci_path = ".gitlab-ci.yml" if hosting_service == "gitlab" else ".github/workflows/"
    security_path = ".gitlab-ci.yml" if hosting_service == "gitlab" else "SECURITY.md"
    ci_pass = (
        file_exists(root_dir, ".gitlab-ci.yml")
        if hosting_service == "gitlab"
        else has_file_with_extension(root_dir, ".github/workflows", [".yml", ".yaml"])
    )
    security_pass = _consumer_security_status(root_dir, hosting_service)

    return [
        *_consumer_tool_coverage_checks(root_dir, plugin_install),
        *_consumer_context_efficiency_checks(root_dir),
        *_consumer_quality_gates_checks(root_dir, package_json, ci_path, hosting_label, ci_pass),
        *_consumer_memory_and_eval_checks(root_dir),
        *_consumer_security_guardrails_checks(_GuardrailsCtx(
            root_dir=root_dir,
            gitignore=gitignore,
            project_hooks=project_hooks,
            security_path=security_path,
            hosting_label=hosting_label,
            security_pass=security_pass,
        )),
    ]


def summarize_category_scores(checks: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """カテゴリ別スコアを集計する。"""
    scores: dict[str, dict[str, int]] = {}
    for category in CATEGORIES:
        in_category = [check for check in checks if check["category"] == category]
        max_points = sum(check["points"] for check in in_category)
        earned_points = sum(check["points"] for check in in_category if check["pass"])
        normalized = 0 if max_points == 0 else round((earned_points / max_points) * 10)
        scores[category] = {"score": normalized, "earned": earned_points, "max": max_points}
    return scores


def build_report(scope: str, root_dir: str | Path | None = None, target_mode: str | None = None) -> dict[str, Any]:
    """監査レポートを組み立てる。"""
    resolved_root = Path(root_dir or os.getcwd()).resolve()
    resolved_mode = target_mode or detect_target_mode(resolved_root)
    hosting_service = detect_git_hosting_service(resolved_root)
    checks_source = (
        get_repo_checks(resolved_root)
        if resolved_mode == "repo"
        else get_consumer_checks(resolved_root, hosting_service)
    )
    checks = [check for check in checks_source if scope in check["scopes"]]
    category_scores = summarize_category_scores(checks)
    max_score = sum(check["points"] for check in checks)
    overall_score = sum(check["points"] for check in checks if check["pass"])

    failed_checks = [check for check in checks if not check["pass"]]
    failed_checks.sort(key=lambda check: check["points"], reverse=True)
    top_actions = [
        {
            "action": check["fix"],
            "path": check["path"],
            "category": check["category"],
            "points": check["points"],
        }
        for check in failed_checks[:3]
    ]

    return {
        "scope": scope,
        "root_dir": str(resolved_root),
        "target_mode": resolved_mode,
        "deterministic": True,
        "rubric_version": "2026-03-30",
        "overall_score": overall_score,
        "max_score": max_score,
        "categories": category_scores,
        "checks": [
            {
                "id": check["id"],
                "category": check["category"],
                "points": check["points"],
                "path": check["path"],
                "description": check["description"],
                "pass": check["pass"],
            }
            for check in checks
        ],
        "top_actions": top_actions,
    }


def print_text(report: dict[str, Any]) -> None:
    """テキスト形式で監査レポートを出力する。"""
    print(
        f"Harness Audit ({report['scope']}, {report['target_mode']}): {report['overall_score']}/{report['max_score']}"
    )
    print(f"Root: {report['root_dir']}")
    print()

    for category in CATEGORIES:
        data = report["categories"][category]
        if not data or data["max"] == 0:
            continue
        print(f"- {category}: {data['score']}/10 ({data['earned']}/{data['max']} pts)")

    failed = [check for check in report["checks"] if not check["pass"]]
    print()
    print(f"Checks: {len(report['checks'])} total, {len(failed)} failing")

    if failed:
        print()
        print("Top 3 Actions:")
        for index, action in enumerate(report["top_actions"], start=1):
            print(f"{index}) [{action['category']}] {action['action']} ({action['path']})")


def show_help(exit_code: int = 0) -> None:
    """ヘルプを表示して終了する。"""
    print(
        """
Usage: python3 "${CLAUDE_PLUGIN_ROOT}/src/bluecore/launcher.py" bluecore.ci.harness_audit [scope] [--scope <repo|hooks|skills|commands|agents>] [--format <text|json>]
       [--root <path>]

Deterministic harness audit based on explicit file/rule checks.
Audits the current working directory by default and auto-detects repo vs consumer-project mode.
"""
    )
    raise SystemExit(exit_code)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI のエントリポイント。"""
    try:
        args = parse_args(argv)

        if args["help"]:
            show_help(0)

        report = build_report(args["scope"], root_dir=args["root"])

        if args["format"] == "json":
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print_text(report)

        if any(not check["pass"] for check in report["checks"]):
            return 1
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
