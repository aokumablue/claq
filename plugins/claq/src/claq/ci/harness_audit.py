"""ハーネス監査の決定論的スコアリングを行う。"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from claq.ci.harness_audit_repo_checks import get_repo_checks
from claq.ci.harness_audit_utils import (
    _has_any_file as _has_any_file,
)
from claq.ci.harness_audit_utils import (
    _has_gitlab_security_scanning,
    count_files,
    detect_target_mode,
    file_has_content,
    find_plugin_install,
    has_file_with_extension,
    has_python_tests,
    make_check,
    safe_parse_json,
    safe_read,
)
from claq.ci.harness_audit_utils import (
    read_text as read_text,
)
from claq.lib.git_hosting import (
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
VALID_TARGET_KINDS = {"repo", "consumer"}


RUBRIC_VERSION = "2026-09-06"
"""採点ルーブリックの版。

スコアはこの版の中でのみ比較可能で、版が上がった時点で過去のベースラインとは
比較不能になる。同じ文字列を ``commands/harness.md`` の散文と
``agents/harness-tuner.md`` のサンプル JSON が書いており、片方だけ更新すると
「比較不能なスコア比較」が静かに通る。そのため 3 箇所すべての一致を
tests/test_md_references.py が機械照合する。
"""


def normalize_scope(scope: str | None) -> str:
    """scope を正規化する。"""
    value = (scope or "repo").lower()
    if value not in VALID_SCOPES:
        raise ValueError(f"Invalid scope: {scope}")
    return value


def _normalize_target_kind(value: str) -> str:
    """--target-kind の値を正規化する。"""
    normalized = value.lower()
    if normalized not in VALID_TARGET_KINDS:
        raise ValueError(f"Invalid target-kind: {value}. Use repo or consumer.")
    return normalized


_VALUE_OPTIONS: tuple[tuple[str, str, Callable[[str], Any]], ...] = (
    ("--format", "format", str.lower),
    ("--scope", "scope", normalize_scope),
    ("--root", "root", lambda value: Path(value).resolve()),
    ("--target-kind", "target_kind", _normalize_target_kind),
)
"""値を取るフラグ名・格納先キー・値の変換。"""


def _option_value(args: list[str], index: int, name: str) -> tuple[str, int]:
    """`--name VALUE` または `--name=VALUE` の値と次インデックスを返す。

    値が無い・空文字・``-`` 始まり（＝次のフラグを値として食っている）の 3 つを
    まとめてエラーにする（そうしないと ``--root --help`` は ``root=<cwd>/--help``
    かつ ``help=False`` になり、``--scope``（値なし）は黙って ``repo`` へ落ちる）。
    取得の唯一の経路である本関数へ置くのは、フラグを増やすたびにガードを
    書き忘れる余地を残さないため。

    Raises:
        ValueError: 値が無い、空文字、または ``-`` 始まりの場合。
    """
    arg = args[index]
    if arg.startswith(f"{name}="):
        value, next_index = arg.split("=", 1)[1], index + 1
    else:
        value, next_index = (args[index + 1] if index + 1 < len(args) else ""), index + 2
    if not value or value.startswith("-"):
        raise ValueError(f"{name} requires a non-empty value that is not another flag")
    return value, next_index


def _apply_arg(parsed: dict[str, Any], args: list[str], index: int) -> int:
    """単一の CLI 引数を解釈して parsed を更新し、次のインデックスを返す。

    Raises:
        ValueError: 不明な引数、または値が不正な場合
    """
    arg = args[index]
    if arg in {"--help", "-h"}:
        parsed["help"] = True
        return index + 1
    for name, key, convert in _VALUE_OPTIONS:
        if arg == name or arg.startswith(f"{name}="):
            value, next_index = _option_value(args, index, name)
            parsed[key] = convert(value)
            return next_index
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
        "target_kind": None,
    }
    index = 0
    while index < len(args):
        index = _apply_arg(parsed, args, index)
    if parsed["format"] not in VALID_FORMATS:
        raise ValueError(f"Invalid format: {parsed['format']}. Use text or json.")
    return parsed


def _hosting_layout(root_dir: str | Path, hosting_service: str) -> tuple[str, str, bool, bool]:
    """consumer 向けの (CI パス, セキュリティパス, CI 有無, セキュリティポリシー有無) を返す。"""
    if hosting_service == "gitlab":
        return (
            ".gitlab-ci.yml",
            ".gitlab-ci.yml",
            file_has_content(root_dir, ".gitlab-ci.yml"),
            file_has_content(root_dir, "SECURITY.md") or _has_gitlab_security_scanning(root_dir),
        )
    return (
        ".github/workflows/",
        "SECURITY.md",
        has_file_with_extension(root_dir, ".github/workflows", [".yml", ".yaml"]),
        file_has_content(root_dir, "SECURITY.md")
        or file_has_content(root_dir, ".github/dependabot.yml")
        or file_has_content(root_dir, ".github/codeql.yml"),
    )


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
    ci_path, security_path, ci_pass, security_pass = _hosting_layout(root_dir, hosting_service)

    return [
        make_check(
            "consumer-plugin-install", "Tool Coverage", 4, ["repo"], "~/.claude/plugins/",
            "プラグインがインストールされている",
            bool(plugin_install),
            "Install the claq plugin for this user or project before auditing project-specific harness quality.",
        ),
        make_check(
            "consumer-project-overrides", "Tool Coverage", 3, ["repo", "hooks", "skills", "commands", "agents"],
            ".claude/",
            "プロジェクト固有のハーネスオーバーライドが .claude/ 配下に存在する",
            count_files(root_dir, ".claude/agents", ".md") > 0
            or count_files(root_dir, ".claude/skills", "SKILL.md") > 0
            or count_files(root_dir, ".claude/commands", ".md") > 0
            or file_has_content(root_dir, ".claude/settings.json")
            or file_has_content(root_dir, ".claude/hooks.json"),
            "Add project-local .claude hooks, commands, skills, or settings that tailor claq to this repo.",
        ),
        make_check(
            "consumer-instructions", "Context Efficiency", 3, ["repo"], "AGENTS.md",
            "プロジェクトが明示的なエージェント・命令コンテキストを持つ",
            file_has_content(root_dir, "AGENTS.md")
            or file_has_content(root_dir, "CLAUDE.md")
            or file_has_content(root_dir, ".claude/CLAUDE.md"),
            "Add AGENTS.md or CLAUDE.md so the harness has project-specific instructions.",
        ),
        make_check(
            "consumer-project-config", "Context Efficiency", 2, ["repo", "hooks"], ".mcp.json",
            "プロジェクトがローカル MCP・Claude 設定を宣言している",
            file_has_content(root_dir, ".mcp.json")
            or file_has_content(root_dir, ".claude/settings.json")
            or file_has_content(root_dir, ".claude/settings.local.json"),
            "Add .mcp.json or .claude/settings.json so project-local tool configuration is explicit.",
        ),
        make_check(
            "consumer-test-suite", "Quality Gates", 4, ["repo"], "tests/",
            "プロジェクトが自動テストのエントリポイントを持つ",
            # 最初のヒットで打ち切る判定（has_python_tests / has_file_with_extension）を
            # 先に置く。count_files はヒットしても数え上げを続けるため最後に回す。
            (isinstance(package_json.get("scripts"), dict) and isinstance(package_json["scripts"].get("test"), str))
            or has_python_tests(root_dir, minimum=1)
            or has_file_with_extension(root_dir, ".", [".spec.js", ".spec.ts", ".test.ts"])
            or count_files(root_dir, "tests", ".test.js") > 0,
            "Add a test script or checked-in tests (package.json scripts.test, *.spec.ts, "
            "or pytest test_*.py) so harness recommendations can be verified automatically.",
        ),
        make_check(
            "consumer-ci-workflow", "Quality Gates", 3, ["repo"], ci_path,
            f"プロジェクトが {hosting_label} CI 設定をチェックインしている",
            ci_pass,
            f"Add at least one CI configuration file for {hosting_label} so harness and test checks run outside local development.",
        ),
        make_check(
            "consumer-memory-notes", "Memory Persistence", 2, ["repo"], ".claude/memory.md",
            "プロジェクトメモリ・恒久的なノートがチェックインされている",
            file_has_content(root_dir, ".claude/memory.md") or count_files(root_dir, "docs/adr", ".md") > 0,
            "Add durable project memory such as .claude/memory.md or ADRs under docs/adr/.",
        ),
        make_check(
            "consumer-eval-coverage", "Eval Coverage", 2, ["repo"], "evals/",
            "プロジェクトが評価テストまたは複数の自動テストを持つ",
            # 「複数の自動テスト」は Python のテストも数え、閾値は JS 側と揃えて 3 件。
            # 判定順は consumer-test-suite と同じ理由で、短絡する has_python_tests を
            # 全数え上げの count_files より前に置く。`evals/` を先頭に残すのは、
            # この判定を落とすリポジトリには `evals/` が無く即 0 が返るため。
            count_files(root_dir, "evals", None) > 0
            or has_python_tests(root_dir, minimum=3)
            or count_files(root_dir, "tests", ".test.js") >= 3,
            "Add eval fixtures or at least a few focused automated tests for critical flows.",
        ),
        make_check(
            "consumer-security-policy", "Security Guardrails", 2, ["repo"], security_path,
            "プロジェクトがセキュリティポリシー・自動依存スキャンを公開している",
            security_pass,
            f"Add SECURITY.md or {hosting_label}-appropriate dependency/code scanning configuration to document the project security posture.",
        ),
        make_check(
            "consumer-secret-hygiene", "Security Guardrails", 2, ["repo"], ".gitignore",
            "プロジェクトが一般的なシークレット環境ファイルを無視している",
            ".env" in gitignore,
            "Ignore .env-style files in .gitignore so secrets do not land in the repo.",
        ),
        make_check(
            "consumer-hook-guardrails", "Security Guardrails", 2, ["repo", "hooks"], ".claude/settings.json",
            "プロジェクトローカルフック設定がツール・プロンプトガードを参照している",
            "PreToolUse" in project_hooks
            or "beforeSubmitPrompt" in project_hooks
            or file_has_content(root_dir, ".claude/hooks.json"),
            "Add project-local hook settings or hook definitions for prompt/tool guardrails.",
        ),
    ]


def summarize_category_scores(checks: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """カテゴリ別スコアを集計する。

    キー名で単位を明示する。同じ dict の中で ``score`` が 0..10 の正規化値、
    ``max`` が生配点という異なる単位を持つと、``score > max`` や
    「カテゴリ score の合計が overall と一致しない」が正常値になり、JSON を
    機械利用すると誤読しやすい（テキスト出力の ``score/10 (earned/max pts)``
    と同じく書き分ける）。
    """
    scores: dict[str, dict[str, int]] = {}
    for category in CATEGORIES:
        in_category = [check for check in checks if check["category"] == category]
        max_points = sum(check["points"] for check in in_category)
        earned_points = sum(check["points"] for check in in_category if check["pass"])
        scores[category] = {
            "earned_points": earned_points,
            "max_points": max_points,
            "normalized_score": 0 if max_points == 0 else round((earned_points / max_points) * 10),
        }
    return scores


def _plugin_provider_hint(root_dir: Path) -> str:
    """root 配下に plugin 提供元ディレクトリがあれば、`--root` の指定を促す 1 文を返す。

    プラグイン提供元リポジトリのルートは、それ自体はプラグインではないので
    consumer と判定される。「repo モードで測りたいのに consumer の点数が出る」を
    自力で解けるよう、実際に指すべきパスを提示する。該当が無ければ空文字列。
    """
    providers = sorted((root_dir / "plugins").glob("*/.claude-plugin/plugin.json"))
    if not providers:
        return ""
    paths = " ".join(str(path.parent.parent.relative_to(root_dir)) for path in providers)
    return f" このルートには plugin 提供元ディレクトリがあります: --root {paths}"


def build_report(scope: str, root_dir: str | Path | None = None, target_mode: str | None = None) -> dict[str, Any]:
    """監査レポートを組み立てる。

    `target_mode` を明示した場合、自動判定（`detect_target_mode`）と不一致なら
    ValueError を送出する。誤った作業ルートを監査していることに、明示指定と
    自動判定の食い違いで気付けるようにするため、黙って明示値を信用しない。

    Args:
        scope: 監査スコープ。
        root_dir: 監査対象のルート。None なら cwd。
        target_mode: ``"repo"`` / ``"consumer"`` を明示する場合に指定する。
            None なら自動判定に任せる。

    Raises:
        ValueError: 明示した target_mode が自動判定と食い違う場合。
    """
    resolved_root = Path(root_dir or os.getcwd()).resolve()
    detected_mode = detect_target_mode(resolved_root)
    if target_mode is not None and target_mode != detected_mode:
        raise ValueError(
            f"--target-kind {target_mode} does not match the auto-detected mode "
            f"({detected_mode}) for root {resolved_root}. Pass the correct root, "
            "or omit --target-kind to use auto-detection."
            + _plugin_provider_hint(resolved_root)
        )
    resolved_mode = target_mode or detected_mode
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

    # 単位を分けた以上、合計が一致することは機械的に保証できる。集計経路が
    # 壊れたら（カテゴリの取りこぼし・二重計上）ここで落とす。
    category_total = sum(data["earned_points"] for data in category_scores.values())
    if category_total != overall_score:
        raise AssertionError(
            f"カテゴリ earned_points の合計 {category_total} が overall_score {overall_score} と一致しません"
        )

    failed_checks = sorted((check for check in checks if not check["pass"]), key=lambda c: c["points"], reverse=True)
    return {
        "scope": scope,
        "root_dir": str(resolved_root),
        "target_mode": resolved_mode,
        "deterministic": True,
        "rubric_version": RUBRIC_VERSION,
        "overall_score": overall_score,
        "max_score": max_score,
        "categories": category_scores,
        "checks": [
            {key: check[key] for key in ("id", "category", "points", "path", "description", "pass")}
            for check in checks
        ],
        "top_actions": [
            {"action": check["fix"], "path": check["path"], "category": check["category"], "points": check["points"]}
            for check in failed_checks[:3]
        ],
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
        if data["max_points"]:
            print(
                f"- {category}: {data['normalized_score']}/10 "
                f"({data['earned_points']}/{data['max_points']} pts)"
            )

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
Usage: python3 "${CLAUDE_PLUGIN_ROOT}/src/claq/launcher.py" claq.ci.harness_audit [scope] [--scope <repo|hooks|skills|commands|agents>] [--format <text|json>]
       [--root <path>] [--target-kind <repo|consumer>]

Deterministic harness audit based on explicit file/rule checks.
Audits the current working directory by default and auto-detects repo vs consumer-project mode.
--target-kind asserts the expected mode; the audit fails if it disagrees with auto-detection
(catches auditing the wrong root, e.g. a plugin provider repo without --root pointing at the
provider directory).
"""
    )
    raise SystemExit(exit_code)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI のエントリポイント。"""
    try:
        args = parse_args(argv)
        if args["help"]:
            show_help(0)
        report = build_report(args["scope"], root_dir=args["root"], target_mode=args["target_kind"])
        if args["format"] == "json":
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print_text(report)
        return int(any(not check["pass"] for check in report["checks"]))
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
