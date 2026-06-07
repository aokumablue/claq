"""公開ドキュメントにユーザー固有の絶対パスが含まれるのを防ぐ。"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from bluecore.ci.ci_common import REPO_ROOT

TARGETS = [
    "README.md",
    "skills",
    "commands",
    "agents",
    "docs",
    ".opencode/commands",
]

BLOCK_PATTERNS = [
    re.compile(r"/Users/([^/\s]+)"),  # macOS
    re.compile(r"/home/([^/\s]+)"),  # Linux
    re.compile(r"C:\\Users\\([^\\/\s]+)", re.I),  # Windows
]
FILE_EXTENSIONS = re.compile(r"\.(md|json|js|ts|sh|toml|yml|yaml)$", re.I)

# ドキュメント例示で使われる汎用プレースホルダ。個人を特定しないため検出対象から除外する。
# 注: "name"/"me" は実ユーザー名と衝突し検出漏れを招くため意図的に含めない（セキュリティ後退防止）。
PLACEHOLDER_USERS = frozenset(
    {
        "user",
        "users",
        "username",
        "usr",
        "youruser",
        "yourusername",
        "your-username",
        "your_username",
        "yourname",
    }
)
_PLACEHOLDER_CHARS = frozenset("<>${}")


def _collect_files(target_path: Path, out: list[Path]) -> None:
    """指定されたパス配下のファイルを再帰的に収集する。

    Args:
        target_path: スキャンするパス
        out: ファイルパスを追加するリスト

    Returns:
        戻り値はありません（out を直接変更）。

    Raises:
        例外は発生しません。
    """
    if not target_path.exists():
        return
    if target_path.is_file():
        out.append(target_path)
        return

    for entry in target_path.iterdir():
        if entry.name in {"node_modules", ".git"}:
            continue
        _collect_files(entry, out)


def _is_placeholder_user(username: str) -> bool:
    """ユーザー名セグメントが個人を特定しない汎用プレースホルダかを判定する。

    Args:
        username: 検出されたパスのユーザー名セグメント。

    Returns:
        プレースホルダなら True、実ユーザー名らしきなら False。

    Raises:
        例外は発生しません。
    """
    # テンプレート記法（<user>, $USER, ${USER}, {username} 等）は実在パスには現れない。
    if any(char in _PLACEHOLDER_CHARS for char in username):
        return True
    # 文中では `/home/user.` のように句読点が後続しうるため、末尾を除去して比較する。
    cleaned = username.rstrip(".,;:!?)]'\"`")
    return cleaned.lower() in PLACEHOLDER_USERS


def validate_no_personal_paths(root: str | Path = REPO_ROOT) -> int:
    """公開済みドキュメントに個人用のハードコードされたパスが含まれていないことを検証する。

    Args:
        root: 処理に渡す root の値です。

    Returns:
        処理結果を返します。

    Raises:
        例外は発生しません。
    """
    root_path = Path(root)
    files: list[Path] = []
    for target in TARGETS:
        _collect_files(root_path / target, files)

    failures = 0
    for file_path in files:
        if not FILE_EXTENSIONS.search(str(file_path)):
            continue
        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError:
            continue

        for pattern in BLOCK_PATTERNS:
            real_users = [user for user in pattern.findall(content) if not _is_placeholder_user(user)]
            if real_users:
                print(f"エラー: {file_path.relative_to(root_path)} に個人用パスが検出されました")
                failures += len(real_users)
                break

    if failures > 0:
        return 1

    print("検証済み: 配布対象の docs/skills/commands に個人用の絶対パスはありません")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """CLI パーサーを構築する。

    Args:
        引数はありません。

    Returns:
        処理結果を返します。

    Raises:
        例外は発生しません。
    """
    parser = argparse.ArgumentParser(description="Validate docs for personal paths")
    parser.add_argument("--root", default=str(REPO_ROOT))
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI のエントリポイント。

    Args:
        argv: 処理に渡す argv の値です。

    Returns:
        処理結果を返します。

    Raises:
        例外は発生しません。
    """
    args = build_parser().parse_args(argv)
    return validate_no_personal_paths(args.root)


if __name__ == "__main__":
    raise SystemExit(main())
