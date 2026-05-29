"""ハーネス監査の決定論的スコアリングを行う。"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from devgear.lib.git_hosting import (
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
REPO_CORE_MARKERS = [
    ".claude-plugin/plugin.json",
    "agents",
    "skills",
]
HARNESS_MARKERS = [
    "src/devgear/ci/harness_audit.py",
]
COMMAND_PARITY_PAIRS = [
    ("commands/c-harness-audit.md", ".opencode/commands/c-harness-audit.md"),
    ("commands/harness-audit.md", ".opencode/commands/harness-audit.md"),
]


def normalize_scope(scope: str | None) -> str:
    """scope を正規化する。"""
    value = (scope or "repo").lower()
    if value not in VALID_SCOPES:
        raise ValueError(f"Invalid scope: {scope}")
    return value


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
        arg = args[index]

        if arg in {"--help", "-h"}:
            parsed["help"] = True
            index += 1
            continue

        if arg == "--format":
            parsed["format"] = (args[index + 1] if index + 1 < len(args) else "").lower()
            index += 2
            continue

        if arg.startswith("--format="):
            parsed["format"] = arg.split("=", 1)[1].lower()
            index += 1
            continue

        if arg == "--scope":
            parsed["scope"] = normalize_scope(args[index + 1] if index + 1 < len(args) else None)
            index += 2
            continue

        if arg.startswith("--scope="):
            parsed["scope"] = normalize_scope(arg.split("=", 1)[1])
            index += 1
            continue

        if arg == "--root":
            parsed["root"] = Path(args[index + 1] if index + 1 < len(args) else os.getcwd()).resolve()
            index += 2
            continue

        if arg.startswith("--root="):
            parsed["root"] = Path(arg.split("=", 1)[1] or os.getcwd()).resolve()
            index += 1
            continue

        if arg.startswith("-"):
            raise ValueError(f"Unknown argument: {arg}")

        parsed["scope"] = normalize_scope(arg)
        index += 1

    if parsed["format"] not in VALID_FORMATS:
        raise ValueError(f"Invalid format: {parsed['format']}. Use text or json.")

    return parsed


def file_exists(root_dir: str | Path, relative_path: str) -> bool:
    """相対パスが存在するかを確認する。"""
    return Path(root_dir, relative_path).exists()


def read_text(root_dir: str | Path, relative_path: str) -> str:
    """テキストファイルを UTF-8 で読む。"""
    return Path(root_dir, relative_path).read_text(encoding="utf-8")


def _walk_dir(root_path: Path):
    stack = [root_path]
    while stack:
        current = stack.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                else:
                    yield entry


def count_files(root_dir: str | Path, relative_dir: str, extension: str | None) -> int:
    """指定ディレクトリ以下のファイル数を数える。"""
    dir_path = Path(root_dir, relative_dir)
    if not dir_path.exists():
        return 0

    count = 0
    for entry in _walk_dir(dir_path):
        if extension is None or entry.name.endswith(extension):
            count += 1
    return count


def safe_read(root_dir: str | Path, relative_path: str) -> str:
    """失敗しても空文字を返す安全な読み込み。"""
    try:
        return read_text(root_dir, relative_path)
    except OSError:
        return ""


def safe_parse_json(text: str) -> Any | None:
    """空文字や不正 JSON を None として扱う。"""
    if not text or not text.strip():
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _has_any_file(root_dir: str | Path, relative_paths: Sequence[str]) -> bool:
    """候補パスのどれか 1 つでも存在するかを調べる。"""
    return any(file_exists(root_dir, relative_path) for relative_path in relative_paths)


def _command_parity_matches(root_dir: str | Path) -> bool:
    """新旧コマンド名のどちらでもパリティが取れているかを確認する。"""
    for primary_path, parity_path in COMMAND_PARITY_PAIRS:
        primary = safe_read(root_dir, primary_path).strip()
        parity = safe_read(root_dir, parity_path).strip()
        if primary and primary == parity:
            return True
    return False


def has_file_with_extension(root_dir: str | Path, relative_dir: str, extensions: str | Sequence[str]) -> bool:
    """指定拡張子のファイルが 1 つでもあるかを調べる。"""
    dir_path = Path(root_dir, relative_dir)
    if not dir_path.exists():
        return False

    allowed = [extensions] if isinstance(extensions, str) else list(extensions)
    for entry in _walk_dir(dir_path):
        if any(entry.name.endswith(extension) for extension in allowed):
            return True
    return False


def detect_target_mode(root_dir: str | Path) -> str:
    """repo か consumer かを判定する。"""
    package_json = safe_parse_json(safe_read(root_dir, "package.json"))
    if isinstance(package_json, dict) and package_json.get("name") == "everything-claude-code":
        return "repo"

    if all(file_exists(root_dir, marker) for marker in REPO_CORE_MARKERS) and _has_any_file(root_dir, HARNESS_MARKERS):
        return "repo"

    return "consumer"


def _has_gitlab_security_scanning(root_dir: str | Path) -> bool:
    """GitLab CI に最低限のセキュリティスキャン設定があるかを確認する。"""
    content = safe_read(root_dir, ".gitlab-ci.yml")
    if not content:
        return False

    patterns = (
        r"(?mi)^\s*(dependency_scanning|sast|container_scanning|secret_detection|license_scanning)\s*:",
        r"(?mi)^\s*-\s*template:\s*Security/",
        r"(?mi)^\s*template:\s*Security/",
    )
    return any(re.search(pattern, content) for pattern in patterns)


def find_plugin_install(root_dir: str | Path) -> str | None:
    """ECC のインストール先を探す。"""
    home_dir = os.environ.get("HOME", "")
    candidates = [
        Path(root_dir) / ".claude" / "plugins" / "everything-claude-code" / ".claude-plugin" / "plugin.json",
        Path(root_dir) / ".claude" / "plugins" / "everything-claude-code" / "plugin.json",
        Path(home_dir) / ".claude" / "plugins" / "everything-claude-code" / ".claude-plugin" / "plugin.json"
        if home_dir
        else None,
        Path(home_dir) / ".claude" / "plugins" / "everything-claude-code" / "plugin.json" if home_dir else None,
    ]

    for candidate in candidates:
        if candidate is not None and candidate.exists():
            return str(candidate)
    return None


def get_repo_checks(root_dir: str | Path) -> list[dict[str, Any]]:
    """repo モード向けのチェック定義を返す。"""
    package_json = safe_parse_json(safe_read(root_dir, "package.json"))
    if not isinstance(package_json, dict):
        package_json = {}

    hooks_json = safe_read(root_dir, "hooks/hooks.json")

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
            "path": "scripts/hooks/",
            "description": "最低8個のフック実装スクリプトが存在する",
            "pass": count_files(root_dir, "scripts/hooks", ".js") >= 8,
            "fix": "Add missing hook implementations in scripts/hooks/.",
        },
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
            "path": ".opencode/commands/c-harness-audit.md",
            "description": "ハーネス監査コマンドのプライマリと OpenCode コマンドドック間でパリティが取れている",
            "pass": _command_parity_matches(root_dir),
            "fix": "Sync commands/c-harness-audit.md and .opencode/commands/c-harness-audit.md.",
        },
        {
            "id": "context-strategic-compact",
            "category": "Context Efficiency",
            "points": 3,
            "scopes": ["repo", "skills"],
            "path": "skills/s-slim/SKILL.md",
            "description": "コンテキスト最大圧縮スキルが存在する（LLMレスポンス・ファイルの原始人口調圧縮）",
            "pass": file_exists(root_dir, "skills/s-slim/SKILL.md"),
            "fix": "Add skills/s-slim/SKILL.md for maximum context compression.",
        },
        {
            "id": "context-suggest-compact-hook",
            "category": "Context Efficiency",
            "points": 3,
            "scopes": ["repo", "hooks"],
            "path": "scripts/hooks/suggest-compact.js",
            "description": "コンテキスト圧縮自動化フックが存在する（セッション中にコンテキスト圧縮提案）",
            "pass": file_exists(root_dir, "scripts/hooks/suggest-compact.js"),
            "fix": "Implement scripts/hooks/suggest-compact.js for context pressure hints.",
        },
        {
            "id": "context-model-route",
            "category": "Context Efficiency",
            "points": 2,
            "scopes": ["repo", "commands"],
            "path": "commands/c-plan.md",
            "description": "モデルルーティングコマンドが存在する（タスク複雑度に応じたモデル選択）",
            "pass": file_exists(root_dir, "commands/c-plan.md"),
            "fix": "Add c-plan command guidance in commands/c-plan.md.",
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
        {
            "id": "quality-test-runner",
            "category": "Quality Gates",
            "points": 3,
            "scopes": ["repo"],
            "path": "tests/run-all.js",
            "description": "一元化されたテストランナーが存在する",
            "pass": file_exists(root_dir, "tests/run-all.js"),
            "fix": "Add tests/run-all.js to enforce complete suite execution.",
        },
        {
            "id": "quality-ci-validations",
            "category": "Quality Gates",
            "points": 3,
            "scopes": ["repo"],
            "path": "package.json",
            "description": "テストスクリプトが検証チェーンを実行してからテストを実行する",
            "pass": isinstance(package_json.get("scripts"), dict)
            and isinstance(package_json["scripts"].get("test"), str)
            and "validate-commands.js" in package_json["scripts"]["test"]
            and "tests/run-all.js" in package_json["scripts"]["test"],
            "fix": "Update package.json test script to run validators plus tests/run-all.js.",
        },
        {
            "id": "quality-hook-tests",
            "category": "Quality Gates",
            "points": 2,
            "scopes": ["repo", "hooks"],
            "path": "tests/hooks/hooks.test.js",
            "description": "フックカバレッジテストファイルが存在する",
            "pass": file_exists(root_dir, "tests/hooks/hooks.test.js"),
            "fix": "Add tests/hooks/hooks.test.js for hook behavior validation.",
        },
        {
            "id": "quality-doctor-script",
            "category": "Quality Gates",
            "points": 2,
            "scopes": ["repo"],
            "path": "scripts/doctor.js",
            "description": "インストール状態チェック用ドクタースクリプトが存在する",
            "pass": file_exists(root_dir, "scripts/doctor.js"),
            "fix": "Add scripts/doctor.js for install-state integrity checks.",
        },
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
            "path": "scripts/hooks/session-start.js",
            "description": "セッション開始・終了時の永続化スクリプトが存在する",
            "pass": file_exists(root_dir, "scripts/hooks/session-start.js")
            and file_exists(root_dir, "scripts/hooks/session-end.js"),
            "fix": "Implement scripts/hooks/session-start.js and scripts/hooks/session-end.js.",
        },
        {
            "id": "memory-learning-skill",
            "category": "Memory Persistence",
            "points": 2,
            "scopes": ["repo", "skills"],
            "path": "skills/s-learn/SKILL.md",
            "description": "継続学習スキルが存在する（セッション観測→インスティンクト作成→スキル進化）",
            "pass": file_exists(root_dir, "skills/s-learn/SKILL.md"),
            "fix": "Add skills/s-learn/SKILL.md for memory evolution flow.",
        },
        {
            "id": "eval-skill",
            "category": "Eval Coverage",
            "points": 4,
            "scopes": ["repo", "skills"],
            "path": "commands/c-harness.md",
            "description": "品質監査コマンドが存在する（ハーネス監査・スキル棚卸し・遵守率測定）",
            "pass": file_exists(root_dir, "commands/c-harness.md"),
            "fix": "Add commands/c-harness.md for quality audit evaluation.",
        },
        {
            "id": "eval-commands",
            "category": "Eval Coverage",
            "points": 4,
            "scopes": ["repo", "commands"],
            "path": "commands/c-review.md",
            "description": "検証コマンドとプランコマンドが存在する",
            "pass": file_exists(root_dir, "commands/c-review.md")
            and file_exists(root_dir, "commands/c-plan.md"),
            "fix": "Add commands/c-review.md and commands/c-plan.md to standardize verification loops.",
        },
        {
            "id": "eval-tests-presence",
            "category": "Eval Coverage",
            "points": 2,
            "scopes": ["repo"],
            "path": "tests/",
            "description": "最低10個のテストファイルが存在する",
            "pass": count_files(root_dir, "tests", ".test.js") >= 10,
            "fix": "Increase automated test coverage across scripts/hooks/lib.",
        },
        {
            "id": "security-review-skill",
            "category": "Security Guardrails",
            "points": 3,
            "scopes": ["repo", "skills"],
            "path": "skills/s-secure/SKILL.md",
            "description": "セキュリティレビュースキルが存在する（認証・入力処理・シークレット管理）",
            "pass": file_exists(root_dir, "skills/s-secure/SKILL.md"),
            "fix": "Add skills/s-secure/SKILL.md for security checklist coverage.",
        },
        {
            "id": "security-agent",
            "category": "Security Guardrails",
            "points": 3,
            "scopes": ["repo", "agents"],
            "path": "agents/a-secure.md",
            "description": "セキュリティレビューエージェントが存在する",
            "pass": file_exists(root_dir, "agents/a-secure.md"),
            "fix": "Add agents/a-secure.md for delegated security audits.",
        },
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
            "path": "commands/c-review.md",
            "description": "セキュリティスキャンコマンドが存在する",
            "pass": file_exists(root_dir, "commands/c-review.md"),
            "fix": "Add commands/c-review.md with scan and remediation workflow.",
        },
        {
            "id": "cost-skill",
            "category": "Cost Efficiency",
            "points": 4,
            "scopes": ["repo", "skills"],
            "path": "skills/s-slim/SKILL.md",
            "description": "コスト最適化スキルが存在する（トークン削減による予算管理）",
            "pass": file_exists(root_dir, "skills/s-slim/SKILL.md"),
            "fix": "Add skills/s-slim/SKILL.md for budget-aware routing.",
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
            "path": "commands/c-plan.md",
            "description": "モデルルーティングコマンドが存在する（複雑度に応じたモデル選択ポリシー）",
            "pass": file_exists(root_dir, "commands/c-plan.md"),
            "fix": "Add commands/c-plan.md and route policies for cheap-default execution.",
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
    security_pass = file_exists(root_dir, "SECURITY.md")
    if hosting_service == "gitlab":
        security_pass = security_pass or _has_gitlab_security_scanning(root_dir)
    else:
        security_pass = (
            security_pass
            or file_exists(root_dir, ".github/dependabot.yml")
            or file_exists(root_dir, ".github/codeql.yml")
        )

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
        {
            "id": "consumer-hook-guardrails",
            "category": "Security Guardrails",
            "points": 2,
            "scopes": ["repo", "hooks"],
            "path": ".claude/settings.json",
            "description": "プロジェクトローカルフック設定がツール・プロンプトガードを参照している",
            "pass": "PreToolUse" in project_hooks
            or "beforeSubmitPrompt" in project_hooks
            or file_exists(root_dir, ".claude/hooks.json"),
            "fix": "Add project-local hook settings or hook definitions for prompt/tool guardrails.",
        },
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
Usage: python3 "${CLAUDE_PLUGIN_ROOT}/src/devgear/launcher.py" devgear.ci.harness_audit [scope] [--scope <repo|hooks|skills|commands|agents>] [--format <text|json>]
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
