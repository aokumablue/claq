"""argparse エントリポイント。

``_ensure_global_dirs`` と各 ``cmd_*`` は ``cli`` 名前空間で ``monkeypatch``
差し替えされるため、``_pkg`` 経由で呼び出す。
"""

import argparse

import bluecore.skills.learn.cli as _pkg

from .paths import PENDING_TTL_DAYS


def _build_parser() -> argparse.ArgumentParser:
    """instinct CLI の ArgumentParser を構築して返す。"""
    parser = argparse.ArgumentParser(description="Instinct CLI for Continuous Learning v2.1 (Project-Scoped)")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    import_parser = subparsers.add_parser("import", help="Import instincts")
    import_parser.add_argument("source", help="File path or URL")
    import_parser.add_argument("--dry-run", action="store_true", help="Preview without importing")
    import_parser.add_argument("--force", action="store_true", help="Skip confirmation")
    import_parser.add_argument("--min-confidence", type=float, help="Minimum confidence threshold")
    import_parser.add_argument(
        "--scope", choices=["project", "global"], default="project", help="Import scope (default: project)"
    )

    export_parser = subparsers.add_parser("export", help="Export instincts")
    export_parser.add_argument("--output", "-o", help="Output file")
    export_parser.add_argument("--domain", help="Filter by domain")
    export_parser.add_argument("--min-confidence", type=float, help="Minimum confidence")
    export_parser.add_argument(
        "--scope", choices=["project", "global", "all"], default="all", help="Export scope (default: all)"
    )

    evolve_parser = subparsers.add_parser("evolve", help="Analyze and evolve instincts")
    evolve_parser.add_argument("--generate", action="store_true", help="Generate evolved structures")

    promote_parser = subparsers.add_parser("promote", help="Promote project instincts to global scope")
    promote_parser.add_argument("instinct_id", nargs="?", help="Specific instinct ID to promote")
    promote_parser.add_argument("--force", action="store_true", help="Skip confirmation")
    promote_parser.add_argument("--dry-run", action="store_true", help="Preview without promoting")

    prune_parser = subparsers.add_parser("prune", help="Delete pending instincts older than TTL")
    prune_parser.add_argument(
        "--max-age",
        type=int,
        default=PENDING_TTL_DAYS,
        help=f"Max age in days before pruning (default: {PENDING_TTL_DAYS})",
    )
    prune_parser.add_argument("--dry-run", action="store_true", help="Preview without deleting")
    prune_parser.add_argument("--quiet", action="store_true", help="Suppress output (for automated use)")

    subparsers.add_parser("status", help="Show status of all instincts (project + global)")
    subparsers.add_parser("projects", help="List known projects and their instinct counts")

    return parser


def _dispatch(args, parser: argparse.ArgumentParser) -> int:
    """解析済み引数に基づいてサブコマンドハンドラへディスパッチする。"""
    if args.command == "import":
        return _pkg.cmd_import(args)
    elif args.command == "export":
        return _pkg.cmd_export(args)
    elif args.command == "evolve":
        return _pkg.cmd_evolve(args)
    elif args.command == "promote":
        return _pkg.cmd_promote(args)
    elif args.command == "prune":
        return _pkg.cmd_prune(args)
    elif args.command == "status":
        return _pkg.cmd_status(args)
    elif args.command == "projects":
        return _pkg.cmd_projects(args)
    else:
        parser.print_help()
        return 1


def main() -> int:
    """instinct CLI のエントリポイント。引数を解析してサブコマンドを実行し、終了コードを返す。"""
    _pkg._ensure_global_dirs()
    parser = _build_parser()
    args = parser.parse_args()
    return _dispatch(args, parser)
