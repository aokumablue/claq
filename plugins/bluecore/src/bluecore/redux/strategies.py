"""アルゴリズム的圧縮戦略 — 宣言的フィルタでは表現できない処理を担う。

宣言的フィルタ（TOML の行フィルタ・truncate など）が静的ルールで圧縮するのに対し、
本モジュールの戦略は入力全体を解析して動的に圧縮する:

  - ``smart_filter``    — ボイラープレート行・コメント行を除去
  - ``dedup``           — 同一パターン行をカウント付きで折りたたむ
  - ``group_lint``      — ESLint/ruff/pytest エラーをルール別に集約
  - ``smart_truncate``  — 先頭/末尾を保持しながら中間を省略

``default.toml`` の catch-all フィルタがこれらを ``strategies`` で宣言し、
コマンド非依存の汎用圧縮として適用する。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bluecore.redux.config import ReduxConfig

# ---------------------------------------------------------------------------
# 戦略1: スマートフィルタリング
# ---------------------------------------------------------------------------

# 除去するボイラープレート行パターン
_BORING_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\s*#.*$"),  # シェルコメント行
    re.compile(r"^[-=]{5,}\s*$"),  # 区切り線（----- / =====）
    re.compile(r"^\s*\d+\s+passing\b", re.I),  # mocha/jest "X passing"
    re.compile(r"^\s*\d+\s+pending\b", re.I),  # mocha/jest "X pending"
    re.compile(r"^npm warn ", re.I),  # npm warn
    re.compile(r"^\[notice\]\s", re.I),  # pip notice
    re.compile(r"^hint:\s", re.I),  # git hint
    re.compile(r"^remote:\s+Counting objects", re.I),  # git push verbosity
    re.compile(r"^Requirement already satisfied:", re.I),  # pip noop
    re.compile(r"^\s*\.\s*$"),  # pytest dot-only 行
]

# 重要行パターン（フィルタを免除する）
_IMPORTANT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\berror\b", re.I),
    re.compile(r"\bfailed\b", re.I),
    re.compile(r"\bwarning\b", re.I),
    re.compile(r"\bfatal\b", re.I),
    re.compile(r"Traceback"),
    re.compile(r'^\s+File\s+"'),  # Python スタックトレース
    re.compile(r"^\s+at\s+\w+\s+\("),  # JS スタックトレース
    re.compile(r"AssertionError"),
    re.compile(r"^\s*\d+\s+error", re.I),
]


def _is_boring(line: str) -> bool:
    """重要でないボイラープレート行かどうか判定する。"""
    if any(p.search(line) for p in _IMPORTANT_PATTERNS):
        return False
    return any(p.match(line) for p in _BORING_PATTERNS)


def smart_filter(text: str) -> str:
    """コメント・ボイラープレート行を除去し、連続空行を1行に圧縮する。"""
    result: list[str] = []
    prev_blank = False
    for line in text.splitlines():
        if not line.strip():
            # 空行: 連続する場合は省略
            if not prev_blank:
                result.append(line)
            prev_blank = True
            continue
        prev_blank = False
        if _is_boring(line):
            continue
        result.append(line)
    return "\n".join(result)


# ---------------------------------------------------------------------------
# 戦略2: 重複排除
# ---------------------------------------------------------------------------

_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})?")
_HEXADDR_RE = re.compile(r"0x[0-9a-fA-F]{4,}")
_LONG_DIGITS_RE = re.compile(r"\b\d{3,}\b")


def _normalize_for_dedup(line: str) -> str:
    """タイムスタンプ・アドレス・長い数値を正規化して dedup キーを生成する。"""
    s = _TIMESTAMP_RE.sub("<TS>", line)
    s = _HEXADDR_RE.sub("<ADDR>", s)
    s = _LONG_DIGITS_RE.sub("<N>", s)
    return s.strip()


def dedup_lines(text: str, threshold: int = 3) -> str:
    """同一の正規化行が threshold 回以上出現する行を折りたたむ。

    最初の出現行のみ保持し、その直後に折りたたみ通知を挿入する。
    非連続に出現する重複（``A, B, A, B, A`` 等）も最初の ``A`` の直後に
    まとめて通知し、2 件目以降の同一行は出力しない。このため折りたたみ後の
    行順は元の出現順とは一致しないことがある。
    """
    lines = text.splitlines()
    key_counts: Counter[str] = Counter(_normalize_for_dedup(ln) for ln in lines)

    result: list[str] = []
    emitted: set[str] = set()
    for line in lines:
        key = _normalize_for_dedup(line)
        count = key_counts[key]
        if count >= threshold:
            if key not in emitted:
                emitted.add(key)
                result.append(line)
                result.append(f"[×{count}] [同一パターン {count} 件を折りたたみ]: {key}")
            # 2件目以降は出力しない
        else:
            result.append(line)

    return "\n".join(result)


# ---------------------------------------------------------------------------
# 戦略3: グループ化（lint エラー集約）
# ---------------------------------------------------------------------------

# ESLint/TSLint 出力パターン（ファイル:行:列 severity 以降）。
# メッセージと rule は 2 連続空白区切り。``.+?\s{2,}\S+$`` の貪欲＋末尾アンカー
# は無区切りの長い行で二次のバックトラックを招くため、正規表現では severity
# までを取り、残り（rest）は _split_eslint_rest で線形分割する。
_ESLINT_HEAD = re.compile(
    r"^\s+(?P<file>[^\s:][^:]*):(?P<line>\d+):(?P<col>\d+)\s+"
    r"(?P<severity>error|warning)\s+(?P<rest>.+)$",
    re.I,
)
_DOUBLE_SPACE_RE = re.compile(r"\s{2,}")
# ruff/flake8 出力パターン（ファイル:行:列: エラーコード メッセージ）。
# file は単一量化子 [^:\s]+ で表し、重複量化子による余分なバックトラックを避ける。
_RUFF_LINE = re.compile(r"^(?P<file>[^:\s]+):(?P<line>\d+):(?P<col>\d+):\s+(?P<code>[A-Z]\d+)\s+(?P<msg>.+)$")
# pytest FAILED 出力パターン（FAILED テスト名 - 失敗理由）
_PYTEST_FAIL = re.compile(r"^FAILED\s+(?P<test>[^\s]+)\s+-\s+(?P<reason>.+)$")


@dataclass
class _LintGroup:
    """同一ルールの lint 結果を件数・対象ファイルとともに集約する。"""

    rule: str
    severity: str
    count: int = 0
    files: list[str] = field(default_factory=list)
    first_msg: str = ""


def _fmt_files(files: list[str], max_show: int = 3) -> str:
    """ファイル一覧を短縮して返す。"""
    shown = files[:max_show]
    rest = len(files) - max_show
    result = ", ".join(shown)
    if rest > 0:
        result += f" (+{rest}ファイル)"
    return result


def _split_eslint_rest(rest: str) -> tuple[str, str] | None:
    """ESLint 行の severity 以降（メッセージ + rule）を 2 連続空白で分割する。

    rule は末尾の 2 連続以上の空白で区切られた最終トークン。区切りが無ければ
    ESLint 行とみなさず None を返す。``re.split`` は線形動作のため、貪欲な
    正規表現が長い無区切り行で起こす二次のバックトラックを避けられる。

    Args:
        rest: severity の直後から行末までのテキスト。

    Returns:
        ``(msg, rule)`` のタプル。2 連続空白の区切りが無ければ None。
    """
    parts = _DOUBLE_SPACE_RE.split(rest)
    if len(parts) < 2 or not parts[-1].strip():
        return None
    rule = parts[-1].strip()
    msg = "  ".join(parts[:-1]).strip()
    return msg, rule


def _classify_lint_lines(
    lines: list[str],
) -> tuple[dict[str, _LintGroup], dict[str, _LintGroup], dict[str, list[str]], set[int]]:
    """行リストを ESLint/ruff/pytest グループに分類してインデックスセットとともに返す。"""
    eslint_groups: dict[str, _LintGroup] = {}
    ruff_groups: dict[str, _LintGroup] = {}
    pytest_groups: dict[str, list[str]] = {}
    grouped_indices: set[int] = set()

    for i, line in enumerate(lines):
        head = _ESLINT_HEAD.match(line)
        parsed = _split_eslint_rest(head.group("rest")) if head else None
        if head and parsed is not None:
            msg, rule = parsed
            if rule not in eslint_groups:
                eslint_groups[rule] = _LintGroup(rule=rule, severity=head.group("severity"), first_msg=msg)
            eslint_groups[rule].count += 1
            f = head.group("file")
            if f not in eslint_groups[rule].files:
                eslint_groups[rule].files.append(f)
            grouped_indices.add(i)
            continue
        m = _RUFF_LINE.match(line)
        if m:
            code = m.group("code")
            if code not in ruff_groups:
                ruff_groups[code] = _LintGroup(rule=code, severity="error", first_msg=m.group("msg"))
            ruff_groups[code].count += 1
            f = m.group("file")
            if f not in ruff_groups[code].files:
                ruff_groups[code].files.append(f)
            grouped_indices.add(i)
            continue
        m = _PYTEST_FAIL.match(line)
        if m:
            reason = m.group("reason")[:60]
            pytest_groups.setdefault(reason, []).append(m.group("test"))
            grouped_indices.add(i)

    return eslint_groups, ruff_groups, pytest_groups, grouped_indices


def _render_lint_groups(
    output_parts: list[str],
    eslint_groups: dict[str, _LintGroup],
    ruff_groups: dict[str, _LintGroup],
    pytest_groups: dict[str, list[str]],
) -> None:
    """グループ化済みの lint 結果をサマリ形式で output_parts に追記する。"""
    if eslint_groups:
        output_parts.append("--- ESLint/TSLint (グループ化) ---")
        for rule, g in sorted(eslint_groups.items(), key=lambda x: -x[1].count):
            sev = f" ({g.severity})" if g.severity != "error" else ""
            output_parts.append(f"[{rule}]{sev}: {g.count}件")
            output_parts.append(f"  {_fmt_files(g.files)}")
            if g.first_msg:
                output_parts.append(f"  例: {g.first_msg[:80]}")
    if ruff_groups:
        output_parts.append("--- ruff/flake8 (グループ化) ---")
        for code, g in sorted(ruff_groups.items(), key=lambda x: -x[1].count):
            output_parts.append(f"[{code}]: {g.count}件 — {g.first_msg[:60]}")
            output_parts.append(f"  {_fmt_files(g.files)}")
    if pytest_groups:
        output_parts.append("--- pytest FAILED (グループ化) ---")
        for reason, tests in sorted(pytest_groups.items(), key=lambda x: -len(x[1])):
            output_parts.append(f"{len(tests)}件 — {reason}")
            output_parts.append(f"  {_fmt_files(tests)}")


def group_lint_errors(text: str) -> str:
    """ESLint/ruff/pytest スタイルのエラーをルール別にグループ化して圧縮する。"""
    lines = text.splitlines()
    eslint_groups, ruff_groups, pytest_groups, grouped_indices = _classify_lint_lines(lines)
    output_parts: list[str] = [ln for i, ln in enumerate(lines) if i not in grouped_indices]
    _render_lint_groups(output_parts, eslint_groups, ruff_groups, pytest_groups)
    return "\n".join(output_parts)


# ---------------------------------------------------------------------------
# 戦略4: スマートトランケーション
# ---------------------------------------------------------------------------


def smart_truncate(
    text: str,
    max_len: int = 3000,
    head_lines: int = 30,
    tail_lines: int = 30,
) -> str:
    """先頭・末尾を保持しながら中間を省略する。

    文字数が max_len 以下の場合はそのまま返す。
    """
    if len(text) <= max_len:
        return text

    lines = text.splitlines()
    total = len(lines)

    if total <= head_lines + tail_lines:
        # 行数は少ないが文字数が多い場合は文字数ベースでトランケート
        keep = max_len // 2
        return f"{text[:keep]}\n... ({len(text) - 2 * keep} 文字省略) ...\n{text[-keep:]}"

    omitted = total - head_lines - tail_lines
    head = "\n".join(lines[:head_lines])
    tail = "\n".join(lines[-tail_lines:])
    return f"{head}\n... ({omitted} 行省略 / 計 {total} 行) ...\n{tail}"


# ---------------------------------------------------------------------------
# 戦略ディスパッチ — 宣言的フィルタの ``strategies`` から名前で呼び出す
# ---------------------------------------------------------------------------


def _run_smart_filter(text: str, config: ReduxConfig) -> str:
    """smart_filter 戦略を設定フラグ付きで実行する。"""
    if not config.smart_filter_enabled:
        return text
    return smart_filter(text)


def _run_dedup(text: str, config: ReduxConfig) -> str:
    """dedup 戦略を設定フラグ付きで実行する。"""
    if not config.dedup_enabled:
        return text
    return dedup_lines(text, threshold=config.dedup_threshold)


def _run_group_lint(text: str, config: ReduxConfig) -> str:
    """group_lint 戦略を設定フラグ付きで実行する。"""
    if not config.group_lint_enabled:
        return text
    return group_lint_errors(text)


def _run_smart_truncate(text: str, config: ReduxConfig) -> str:
    """smart_truncate 戦略を設定フラグ付きで実行する。"""
    if not config.smart_truncate_enabled:
        return text
    if len(text) <= config.max_output_len:
        return text
    return smart_truncate(text, config.max_output_len, config.head_lines, config.tail_lines)


# 戦略名 → 実行関数。未知の名前は engine 側で検証・拒否する。
STRATEGY_DISPATCH: dict[str, Callable[[str, ReduxConfig], str]] = {
    "smart_filter": _run_smart_filter,
    "dedup": _run_dedup,
    "group_lint": _run_group_lint,
    "smart_truncate": _run_smart_truncate,
}
