"""Bash ツール出力のトークン削減（RTK スタイル） — LLM 不要の純 Python 実装。

4 戦略を順次適用するパイプライン:
  1. smart_filter       — ボイラープレート行・コメント行を除去
  2. dedup_lines        — 同一行をカウント付きで折りたたむ
  3. group_lint_errors  — ESLint/ruff/pytest エラーをルール別に集約
  4. smart_truncate     — 先頭/末尾を保持しながら中間を省略
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

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

_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
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

# ESLint/TSLint 出力パターン（ファイル:行:列 severity メッセージ rule）
_ESLINT_LINE = re.compile(
    r"^\s+(?P<file>[^\s:][^:]+):(?P<line>\d+):(?P<col>\d+)\s+"
    r"(?P<severity>error|warning)\s+(?P<msg>.+?)\s{2,}(?P<rule>\S+)\s*$",
    re.I,
)
# ruff/flake8 出力パターン（ファイル:行:列: エラーコード メッセージ）
_RUFF_LINE = re.compile(r"^(?P<file>[^:\s][^:]*):(?P<line>\d+):(?P<col>\d+):\s+(?P<code>[A-Z]\d+)\s+(?P<msg>.+)$")
# pytest FAILED 出力パターン（FAILED テスト名 - 失敗理由）
_PYTEST_FAIL = re.compile(r"^FAILED\s+(?P<test>[^\s]+)\s+-\s+(?P<reason>.+)$")


@dataclass
class _LintGroup:
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


def group_lint_errors(text: str) -> str:
    """ESLint/ruff/pytest スタイルのエラーをルール別にグループ化して圧縮する。"""
    lines = text.splitlines()
    eslint_groups: dict[str, _LintGroup] = {}
    ruff_groups: dict[str, _LintGroup] = {}
    pytest_groups: dict[str, list[str]] = {}
    grouped_indices: set[int] = set()

    for i, line in enumerate(lines):
        m = _ESLINT_LINE.match(line)
        if m:
            rule = m.group("rule")
            if rule not in eslint_groups:
                eslint_groups[rule] = _LintGroup(rule=rule, severity=m.group("severity"), first_msg=m.group("msg"))
            eslint_groups[rule].count += 1
            f = m.group("file")
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
            # 理由の先頭60文字をグループキーにする
            reason = m.group("reason")[:60]
            pytest_groups.setdefault(reason, []).append(m.group("test"))
            grouped_indices.add(i)

    # グループ化されなかった行をそのまま保持
    output_parts: list[str] = [line for i, line in enumerate(lines) if i not in grouped_indices]

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
# パイプライン統合
# ---------------------------------------------------------------------------


@dataclass
class ReduceConfig:
    """トークン削減パイプラインの設定。"""

    enabled: bool = True
    smart_filter_enabled: bool = True
    group_lint_enabled: bool = True
    dedup_enabled: bool = True
    smart_truncate_enabled: bool = True
    max_output_len: int = 3000
    head_lines: int = 30
    tail_lines: int = 30
    dedup_threshold: int = 3


def reduce_bash_output(text: str, config: ReduceConfig | None = None) -> str:
    """RTK スタイルの4戦略を順次適用して Bash 出力を削減する。

    Args:
        text: 削減対象のテキスト。
        config: 削減設定。None の場合はデフォルト設定を使用。

    Returns:
        削減後のテキスト。enabled=False または空入力の場合は元テキストをそのまま返す。
    """
    if not text or not text.strip():
        return text

    cfg = config or ReduceConfig()
    if not cfg.enabled:
        return text

    result = text

    if cfg.smart_filter_enabled:
        result = smart_filter(result)

    if cfg.dedup_enabled:
        result = dedup_lines(result, threshold=cfg.dedup_threshold)

    if cfg.group_lint_enabled:
        result = group_lint_errors(result)

    if cfg.smart_truncate_enabled and len(result) > cfg.max_output_len:
        result = smart_truncate(
            result,
            max_len=cfg.max_output_len,
            head_lines=cfg.head_lines,
            tail_lines=cfg.tail_lines,
        )

    return result
