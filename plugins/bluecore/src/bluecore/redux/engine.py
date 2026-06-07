"""redux パイプラインエンジン — フィルタ選択と宣言ステージ＋戦略の適用。

宣言ステージ（適用順）:
  1. strip_ansi     — ANSI エスケープ除去
  2. substitute     — 正規表現置換（行単位、ルール連鎖）
  3. short_circuit  — 出力全体がパターン一致なら message を即返す（``unless`` で抑制）
  4. drop/keep      — 正規表現で行を除去 / 保持（相互排他）
  5. clip_width     — 各行を N 文字に切り詰め
  6. head/tail      — 先頭/末尾 N 行を保持し中間を省略
  7. limit_lines    — 絶対行数上限
そのあと bluecore 拡張ステージ:
  8. strategies     — アルゴリズム的圧縮（smart_filter/dedup/group_lint/smart_truncate）
最後に:
  9. empty_message  — 結果が空白なら message に置換
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bluecore.redux.config import ReduxConfig
from bluecore.redux.strategies import STRATEGY_DISPATCH

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    """ANSI エスケープシーケンス（CSI）を除去する。"""
    return _ANSI_RE.sub("", text)


@dataclass
class SubstituteRule:
    """行単位の正規表現置換ルール。複数ルールは順に連鎖適用される。"""

    pattern: re.Pattern[str]
    replacement: str


@dataclass
class ShortCircuitRule:
    """出力全体マッチで短絡するルール。

    ``pattern`` が出力全体に一致したら ``message`` を即返す。
    ``unless`` が設定され、それも一致する場合は短絡をスキップする
    （エラー・警告が含まれるケースで誤った要約を防ぐ）。
    """

    pattern: re.Pattern[str]
    message: str
    unless: re.Pattern[str] | None = None


@dataclass
class ReduxFilterSpec:
    """1 コマンド分の宣言的フィルタ定義。"""

    name: str
    command_pattern: re.Pattern[str]
    description: str = ""
    strip_ansi: bool = False
    substitute: list[SubstituteRule] = field(default_factory=list)
    short_circuit: list[ShortCircuitRule] = field(default_factory=list)
    drop_lines: list[re.Pattern[str]] = field(default_factory=list)
    keep_lines: list[re.Pattern[str]] = field(default_factory=list)
    clip_width: int | None = None
    head_lines: int | None = None
    tail_lines: int | None = None
    limit_lines: int | None = None
    empty_message: str | None = None
    strategies: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 宣言ステージ実装
# ---------------------------------------------------------------------------


def _stage_substitute(lines: list[str], rules: list[SubstituteRule]) -> list[str]:
    """各行に置換ルールを連鎖適用する。"""
    out: list[str] = []
    for line in lines:
        for rule in rules:
            line = rule.pattern.sub(rule.replacement, line)
        out.append(line)
    return out


def _stage_short_circuit(lines: list[str], rules: list[ShortCircuitRule]) -> str | None:
    """出力全体マッチで短絡。一致した最初のルールの message を返す。無ければ None。"""
    blob = "\n".join(lines)
    for rule in rules:
        if rule.pattern.search(blob):
            if rule.unless is not None and rule.unless.search(blob):
                continue
            return rule.message
    return None


def _stage_line_filter(
    lines: list[str],
    drop: list[re.Pattern[str]],
    keep: list[re.Pattern[str]],
) -> list[str]:
    """drop（除去）または keep（保持）で行を絞り込む。drop が優先（相互排他前提）。"""
    if drop:
        return [ln for ln in lines if not any(p.search(ln) for p in drop)]
    if keep:
        return [ln for ln in lines if any(p.search(ln) for p in keep)]
    return lines


def _stage_head_tail(lines: list[str], head: int | None, tail: int | None) -> list[str]:
    """先頭 head 行・末尾 tail 行を保持し、超過分を省略メッセージに置換する。"""
    total = len(lines)
    if head is not None and tail is not None:
        if total > head + tail:
            return [*lines[:head], f"... ({total - head - tail} 行省略)", *lines[total - tail :]]
        return lines
    if head is not None:
        if total > head:
            return [*lines[:head], f"... ({total - head} 行省略)"]
        return lines
    if tail is not None:
        if total > tail:
            return [f"... ({total - tail} 行省略)", *lines[total - tail :]]
        return lines
    return lines


# ---------------------------------------------------------------------------
# パイプライン適用 / フィルタ選択
# ---------------------------------------------------------------------------


def apply_spec(spec: ReduxFilterSpec, output: str, config: ReduxConfig | None = None) -> str:
    """フィルタ定義を出力に適用し、圧縮後テキストを返す。

    Args:
        spec: 適用するフィルタ定義。
        output: 圧縮対象の生出力。
        config: 戦略ステージのパラメータ。None の場合は既定値。

    Returns:
        圧縮後テキスト。short_circuit/empty_message 発火時はその message。
    """
    cfg = config or ReduxConfig()
    lines = output.splitlines()

    if spec.strip_ansi:
        lines = [strip_ansi(ln) for ln in lines]

    if spec.substitute:
        lines = _stage_substitute(lines, spec.substitute)

    if spec.short_circuit:
        message = _stage_short_circuit(lines, spec.short_circuit)
        if message is not None:
            return message

    lines = _stage_line_filter(lines, spec.drop_lines, spec.keep_lines)

    if spec.clip_width is not None:
        lines = [ln[: spec.clip_width] for ln in lines]

    lines = _stage_head_tail(lines, spec.head_lines, spec.tail_lines)

    if spec.limit_lines is not None and len(lines) > spec.limit_lines:
        truncated = len(lines) - spec.limit_lines
        lines = [*lines[: spec.limit_lines], f"... ({truncated} 行切り捨て)"]

    text = "\n".join(lines)

    for name in spec.strategies:
        text = STRATEGY_DISPATCH[name](text, cfg)

    if not text.strip() and spec.empty_message is not None:
        return spec.empty_message

    return text


def select_filter(command: str, specs: list[ReduxFilterSpec]) -> ReduxFilterSpec | None:
    """command_pattern が一致する最初のフィルタを返す。無ければ None。"""
    for spec in specs:
        if spec.command_pattern.search(command):
            return spec
    return None


class ReduxEngine:
    """フィルタ定義群を保持し、コマンド出力を圧縮するエンジン。"""

    def __init__(self, specs: list[ReduxFilterSpec]) -> None:
        """フィルタ定義リスト（評価順）を受け取る。"""
        self._specs = list(specs)

    @property
    def specs(self) -> list[ReduxFilterSpec]:
        """保持しているフィルタ定義リストを返す。"""
        return self._specs

    @classmethod
    def load(cls) -> ReduxEngine:
        """組込・ユーザー・プロジェクトのフィルタを読み込んだエンジンを生成する。"""
        from bluecore.redux.loader import load_filter_specs

        return cls(load_filter_specs())

    def reduce(self, command: str, output: str, config: ReduxConfig | None = None) -> str:
        """コマンドに対応するフィルタで出力を圧縮する。

        Args:
            command: 実行された Bash コマンド文字列。
            output: そのコマンドの出力。
            config: 圧縮設定。None の場合は既定値。

        Returns:
            圧縮後テキスト。無効・空入力・無一致時は元の output。
        """
        cfg = config or ReduxConfig()
        if not cfg.enabled:
            return output
        if not output or not output.strip():
            return output
        spec = select_filter(command, self._specs)
        if spec is None:
            return output
        return apply_spec(spec, output, cfg)
