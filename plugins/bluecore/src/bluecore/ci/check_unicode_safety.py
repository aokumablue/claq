"""リポジトリを走査し、危険な不可視 Unicode と絵文字風記号を検出する。"""

from __future__ import annotations

import argparse
import os
import re
import sys
from bisect import bisect_right
from pathlib import Path

from bluecore.ci.ci_common import REPO_ROOT

DEFAULT_ROOT = REPO_ROOT
IGNORED_DIRS = {".git", "node_modules", ".dmux", ".next", "coverage", ".venv", ".pytest_cache"}
TEXT_EXTENSIONS = {
    ".md",
    ".mdx",
    ".txt",
    ".js",
    ".cjs",
    ".mjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".json",
    ".toml",
    ".yml",
    ".yaml",
    ".sh",
    ".bash",
    ".zsh",
    ".ps1",
    ".py",
    ".rs",
}
WRITABLE_EXTENSIONS = {".md", ".mdx", ".txt"}
ALLOWED_EMOJI_CODE_POINTS = {0x00A9, 0x00AE, 0x2122}
KIND_LABELS = {
    "dangerous-invisible": "危険な不可視文字",
    "emoji": "絵文字",
}

DANGEROUS_INVISIBLE_RANGES = [
    (0x200B, 0x200D),
    (0x2060, 0x2060),
    (0xFEFF, 0xFEFF),
    (0x202A, 0x202E),
    (0x2066, 0x2069),
    (0xFE00, 0xFE0F),
    (0xE0100, 0xE01EF),
]

EXTENDED_PICTOGRAPHIC_RANGES = [
    (0x00A9, 0x00A9),
    (0x00AE, 0x00AE),
    (0x203C, 0x203C),
    (0x2049, 0x2049),
    (0x2122, 0x2122),
    (0x2139, 0x2139),
    (0x2194, 0x2199),
    (0x21A9, 0x21AA),
    (0x231A, 0x231B),
    (0x2328, 0x2328),
    (0x2388, 0x2388),
    (0x23CF, 0x23CF),
    (0x23E9, 0x23F3),
    (0x23F8, 0x23FA),
    (0x24C2, 0x24C2),
    (0x25AA, 0x25AB),
    (0x25B6, 0x25B6),
    (0x25C0, 0x25C0),
    (0x25FB, 0x25FE),
    (0x2600, 0x2605),
    (0x2607, 0x2612),
    (0x2614, 0x2685),
    (0x2690, 0x2705),
    (0x2708, 0x2712),
    (0x2714, 0x2714),
    (0x2716, 0x2716),
    (0x271D, 0x271D),
    (0x2721, 0x2721),
    (0x2728, 0x2728),
    (0x2733, 0x2734),
    (0x2744, 0x2744),
    (0x2747, 0x2747),
    (0x274C, 0x274C),
    (0x274E, 0x274E),
    (0x2753, 0x2755),
    (0x2757, 0x2757),
    (0x2763, 0x2767),
    (0x2795, 0x2797),
    (0x27A1, 0x27A1),
    (0x27B0, 0x27B0),
    (0x27BF, 0x27BF),
    (0x2934, 0x2935),
    (0x2B05, 0x2B07),
    (0x2B1B, 0x2B1C),
    (0x2B50, 0x2B50),
    (0x2B55, 0x2B55),
    (0x3030, 0x3030),
    (0x303D, 0x303D),
    (0x3297, 0x3297),
    (0x3299, 0x3299),
    (0x1F000, 0x1F0FF),
    (0x1F10D, 0x1F10F),
    (0x1F12F, 0x1F12F),
    (0x1F16C, 0x1F171),
    (0x1F17E, 0x1F17F),
    (0x1F18E, 0x1F18E),
    (0x1F191, 0x1F19A),
    (0x1F1AD, 0x1F1FF),
    (0x1F201, 0x1F20F),
    (0x1F21A, 0x1F21A),
    (0x1F22F, 0x1F22F),
    (0x1F232, 0x1F23A),
    (0x1F23C, 0x1F23F),
    (0x1F249, 0x1F3FA),
    (0x1F400, 0x1F53D),
    (0x1F546, 0x1F64F),
    (0x1F680, 0x1F6FF),
    (0x1F774, 0x1F77F),
    (0x1F7D5, 0x1F7FF),
    (0x1F80C, 0x1F80F),
    (0x1F848, 0x1F84F),
    (0x1F85A, 0x1F85F),
    (0x1F888, 0x1F88F),
    (0x1F8AE, 0x1F8FF),
    (0x1F90C, 0x1F93A),
    (0x1F93C, 0x1F945),
    (0x1F947, 0x1FAFF),
    (0x1FC00, 0x1FFFD),
]

_DANGEROUS_INVISIBLE_RANGES = sorted(DANGEROUS_INVISIBLE_RANGES)
_DANGEROUS_INVISIBLE_STARTS = [start for start, _ in _DANGEROUS_INVISIBLE_RANGES]
_EXTENDED_PICTOGRAPHIC_STARTS = [start for start, _ in EXTENDED_PICTOGRAPHIC_RANGES]

_TARGETED_REPLACEMENTS = [
    (re.compile(r"\u26A0\uFE0F?"), "WARNING:"),
    (re.compile(r"\u23ED\uFE0F?"), "SKIPPED:"),
    (re.compile(r"\u2705"), "PASS:"),
    (re.compile(r"\u274C"), "FAIL:"),
    (re.compile(r"\u2728"), ""),
]


def _in_ranges(code_point: int, ranges: list[tuple[int, int]], starts: list[int]) -> bool:
    """コードポイントが指定された範囲リストに含まれるか判定する。

    Args:
        code_point: チェックする Unicode コードポイント
        ranges: (開始, 終了) のタプルのリスト
        starts: 二分探索用の開始値のソート済みリスト

    Returns:
        範囲内に含まれる場合は True

    Raises:
        例外は発生しません。
    """
    index = bisect_right(starts, code_point) - 1
    return index >= 0 and code_point <= ranges[index][1]


def _is_dangerous_invisible(code_point: int) -> bool:
    """コードポイントが危険な不可視文字かどうか判定する。

    Args:
        code_point: チェックする Unicode コードポイント

    Returns:
        危険な不可視文字の場合は True

    Raises:
        例外は発生しません。
    """
    return _in_ranges(code_point, _DANGEROUS_INVISIBLE_RANGES, _DANGEROUS_INVISIBLE_STARTS)


def _is_emoji_like(code_point: int) -> bool:
    """コードポイントが絵文字相当の文字かどうか判定する。

    Args:
        code_point: チェックする Unicode コードポイント

    Returns:
        絵文字相当の場合は True

    Raises:
        例外は発生しません。
    """
    return _in_ranges(code_point, EXTENDED_PICTOGRAPHIC_RANGES, _EXTENDED_PICTOGRAPHIC_STARTS)


def _code_point_hex(code_point: int) -> str:
    """コードポイントを16進数表記に変換する。

    Args:
        code_point: Unicode コードポイント

    Returns:
        "U+XXXX" 形式の文字列

    Raises:
        例外は発生しません。
    """
    return f"U+{code_point:X}"


def should_skip(entry_path: str | Path) -> bool:
    """パスがスキップ対象のディレクトリに含まれるか判定する。

    Args:
        entry_path: チェックするファイルパス

    Returns:
        スキップすべき場合は True

    Raises:
        例外は発生しません。
    """
    return any(part in IGNORED_DIRS for part in Path(entry_path).parts)


def is_text_file(file_path: str | Path) -> bool:
    """ファイルがテキストファイルかどうか拡張子で判定する。

    Args:
        file_path: チェックするファイルパス

    Returns:
        TEXT_EXTENSIONS に含まれる拡張子の場合は True

    Raises:
        例外は発生しません。
    """
    return Path(file_path).suffix.lower() in TEXT_EXTENSIONS


def can_auto_write(relative_path: str | Path) -> bool:
    """ファイルが自動書き込み可能かどうか拡張子で判定する。

    Args:
        relative_path: チェックする相対パス

    Returns:
        WRITABLE_EXTENSIONS に含まれる拡張子の場合は True

    Raises:
        例外は発生しません。
    """
    return Path(relative_path).suffix.lower() in WRITABLE_EXTENSIONS


def list_files(dir_path: str | Path) -> list[str]:
    """ディレクトリ配下のテキストファイルを再帰的にリストアップする。

    Args:
        dir_path: スキャンするディレクトリパス

    Returns:
        テキストファイルのパスリスト

    Raises:
        例外は発生しません（OSError はスキップ）。
    """
    results: list[str] = []
    for entry in os.scandir(dir_path):
        entry_path = Path(entry.path)
        if should_skip(entry_path):
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                results.extend(list_files(entry.path))
            elif entry.is_file(follow_symlinks=False) and is_text_file(entry.path):
                results.append(entry.path)
        except OSError:
            continue
    return results


def strip_dangerous_invisible_chars(text: str) -> str:
    """テキストから危険な不可視文字を除去する。

    Args:
        text: 処理対象のテキスト

    Returns:
        危険な不可視文字を除去した後のテキスト

    Raises:
        例外は発生しません。
    """
    return "".join(ch for ch in text if not _is_dangerous_invisible(ord(ch)))


def sanitize_text(text: str) -> str:
    """テキストをサニタイズして危険な文字や不要な絵文字を除去する。

    Args:
        text: サニタイズ対象のテキスト

    Returns:
        サニタイズ済みのテキスト

    Raises:
        例外は発生しません。
    """
    next_text = strip_dangerous_invisible_chars(text)

    for pattern, replacement in _TARGETED_REPLACEMENTS:
        next_text = pattern.sub(replacement, next_text)

    next_text = "".join(ch for ch in next_text if ord(ch) in ALLOWED_EMOJI_CODE_POINTS or not _is_emoji_like(ord(ch)))
    next_text = re.sub(r"^ +(?=\*\*)", "", next_text, flags=re.M)
    next_text = re.sub(r"^(\*\*)\s+", r"\1", next_text, flags=re.M)
    next_text = re.sub(r"^(#+)\s{2,}", r"\1 ", next_text, flags=re.M)
    next_text = re.sub(r"^>\s{2,}", "> ", next_text, flags=re.M)
    next_text = re.sub(r"^-\s{2,}", "- ", next_text, flags=re.M)
    next_text = re.sub(r"^(\d+\.)\s{2,}", r"\1 ", next_text, flags=re.M)
    next_text = re.sub(r"[ \t]+$", "", next_text, flags=re.M)
    return next_text


def collect_dangerous_invisible_matches(text: str) -> list[dict[str, object]]:
    """テキスト中の危険な不可視文字を収集して位置情報とともに返す。

    Args:
        text: 検査するテキスト

    Returns:
        危険な不可視文字の位置情報リスト

    Raises:
        例外は発生しません。
    """
    matches: list[dict[str, object]] = []
    line = 1
    column = 1

    for ch in text:
        code_point = ord(ch)
        if _is_dangerous_invisible(code_point):
            matches.append(
                {
                    "kind": "dangerous-invisible",
                    "char": ch,
                    "codePoint": _code_point_hex(code_point),
                    "line": line,
                    "column": column,
                }
            )

        if ch == "\n":
            line += 1
            column = 1
        else:
            column += 2 if code_point > 0xFFFF else 1

    return matches


def collect_emoji_matches(text: str) -> list[dict[str, object]]:
    """テキスト中の許可されていない絵文字を収集して位置情報とともに返す。

    Args:
        text: 検査するテキスト

    Returns:
        許可されていない絵文字の位置情報リスト

    Raises:
        例外は発生しません。
    """
    matches: list[dict[str, object]] = []
    line = 1
    column = 1

    for ch in text:
        code_point = ord(ch)
        if _is_emoji_like(code_point) and code_point not in ALLOWED_EMOJI_CODE_POINTS:
            matches.append(
                {
                    "kind": "emoji",
                    "char": ch,
                    "codePoint": _code_point_hex(code_point),
                    "line": line,
                    "column": column,
                }
            )

        if ch == "\n":
            line += 1
            column = 1
        else:
            column += 2 if code_point > 0xFFFF else 1

    return matches


def _normalize_relative_path(relative_path: str | Path) -> str:
    """相対パスを正規化する。

    Args:
        relative_path: 正規化する相対パス

    Returns:
        正規化されたパス文字列

    Raises:
        例外は発生しません。
    """
    return os.path.normpath(str(relative_path))


def _scan_unicode_safety(
    root_path: Path, write_mode: bool
) -> tuple[list[str], list[dict[str, object]]]:
    """ルート配下のテキストファイルを走査し、変更ファイルと違反を収集する。

    Args:
        root_path: 走査するルートディレクトリ
        write_mode: True の場合は書き込み可能ファイルをサニタイズして上書きする

    Returns:
        (サニタイズしたファイルの相対名リスト, 検出した違反のリスト) のタプル

    Raises:
        例外は発生しません（読み取り失敗ファイルはスキップ）。
    """
    changed_files: list[str] = []
    violations: list[dict[str, object]] = []

    for file_path in list_files(root_path):
        relative_path = Path(file_path).relative_to(root_path)
        relative_name = relative_path.as_posix()
        try:
            text = Path(file_path).read_text(encoding="utf-8")
        except OSError:
            continue

        if write_mode and can_auto_write(relative_name):
            sanitized = sanitize_text(text)
            if sanitized != text:
                Path(file_path).write_text(sanitized, encoding="utf-8")
                changed_files.append(relative_name)
                text = sanitized

        for violation in collect_dangerous_invisible_matches(text):
            violations.append({"file": relative_name, **violation})
        for violation in collect_emoji_matches(text):
            violations.append({"file": relative_name, **violation})

    return changed_files, violations


def _report_unicode_violations(violations: list[dict[str, object]]) -> None:
    """検出した Unicode 違反を標準エラー出力に書き出す。

    Args:
        violations: collect 系関数が返した違反情報のリスト

    Returns:
        戻り値はありません。

    Raises:
        例外は発生しません。
    """
    print("Unicode 安全性の違反が検出されました:", file=sys.stderr)
    for violation in violations:
        kind = KIND_LABELS.get(str(violation["kind"]), str(violation["kind"]))
        print(
            f"{violation['file']}:{violation['line']}:{violation['column']} {kind} {violation['codePoint']}",
            file=sys.stderr,
        )


def validate_unicode_safety(root: str | Path = DEFAULT_ROOT, write_mode: bool = False) -> int:
    """危険な Unicode をスキャンし、必要に応じて書き込み可能なテキストファイルをサニタイズする。

    Args:
        root: 処理に渡す root の値です。
        write_mode: 処理に渡す write_mode の値です。

    Returns:
        処理結果を返します。

    Raises:
        例外は発生しません。
    """
    changed_files, violations = _scan_unicode_safety(Path(root), write_mode)

    if changed_files:
        print(f"{len(changed_files)} 個のファイルをサニタイズしました:")
        for file_name in changed_files:
            print(f"- {file_name}")

    if violations:
        _report_unicode_violations(violations)
        return 1

    print("Unicode 安全性チェックに合格しました。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """CLI パーサーを構築する。

    Returns:
        --root / --write を受け付ける argparse.ArgumentParser インスタンス。
    """
    parser = argparse.ArgumentParser(description="Scan for dangerous unicode", add_help=False)
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--write", action="store_true")
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
    args, _unknown = build_parser().parse_known_args(argv)
    try:
        return validate_unicode_safety(args.root, args.write)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"エラー: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
