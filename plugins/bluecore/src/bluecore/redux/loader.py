"""TOML フィルタ定義のローダ — 組込・ユーザー・プロジェクトを優先順位付きで統合。

ルックアップ優先順位（先頭ほど優先 = ``select_filter`` が先に評価）:
  1. プロジェクト   ``{cwd}/.bluecore/redux/filters.toml``
  2. ユーザー全体   ``~/.bluecore/redux/filters.toml``
  3. 組込           ``src/bluecore/redux/filters/*.toml``（``default.toml`` は常に末尾）

``default.toml`` は ``command_pattern = ".*"`` の catch-all で、どのコマンドにも
一致しなかった出力に汎用戦略（smart_filter/dedup/group_lint/smart_truncate）を適用する。
"""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bluecore.redux.engine import ReduxFilterSpec, ShortCircuitRule, SubstituteRule
from bluecore.redux.strategies import STRATEGY_DISPATCH

_BUILTIN_DIR = Path(__file__).parent / "filters"
_SCHEMA_VERSION = 1
_DEFAULT_FILTER_FILE = "default.toml"
# ユーザー/プロジェクト定義の正規表現に許す最大長（ReDoS パターン混入の緩和）。
_MAX_USER_PATTERN_LEN = 500


@dataclass
class FilterCase:
    """TOML 内インラインテストケース（``[[cases.<filter>]]``）。"""

    filter_name: str
    name: str
    input: str
    expected: str


def _check_pattern_lengths(name: str, d: dict[str, Any]) -> None:
    """ユーザー定義フィルタの正規表現がすべて長さ上限以内か検証する（ReDoS 緩和）。

    Args:
        name: フィルタ名（エラーメッセージ用）。
        d: フィルタ定義辞書。

    Raises:
        ValueError: いずれかの正規表現が :data:`_MAX_USER_PATTERN_LEN` を超える場合。
    """
    patterns: list[str] = [str(d.get("command_pattern", ""))]
    patterns.extend(str(rule.get("pattern", "")) for rule in d.get("substitute", []))
    for rule in d.get("short_circuit", []):
        patterns.append(str(rule.get("pattern", "")))
        unless = rule.get("unless")
        if unless is not None:
            patterns.append(str(unless))
    patterns.extend(str(p) for p in d.get("drop_lines", []))
    patterns.extend(str(p) for p in d.get("keep_lines", []))
    over = next((p for p in patterns if len(p) > _MAX_USER_PATTERN_LEN), None)
    if over is not None:
        raise ValueError(f"filter '{name}': 正規表現が長すぎます（{len(over)} > {_MAX_USER_PATTERN_LEN} 文字）")


def _build_spec(name: str, d: dict[str, Any], *, trusted: bool = True) -> ReduxFilterSpec:
    """フィルタ定義辞書を :class:`ReduxFilterSpec` に変換・検証する。

    Args:
        name: フィルタ名。
        d: フィルタ定義辞書。
        trusted: 組込フィルタなら True。False（ユーザー/プロジェクト定義）の場合は
            正規表現の長さ上限を検証し、ReDoS パターンの混入を緩和する。

    Raises:
        ValueError: drop_lines と keep_lines の併用、未知の戦略名、または
            trusted=False で正規表現が長さ上限を超える場合。
    """
    if not trusted:
        _check_pattern_lengths(name, d)
    substitute = [SubstituteRule(re.compile(r["pattern"]), r["replacement"]) for r in d.get("substitute", [])]
    short_circuit = [
        ShortCircuitRule(
            re.compile(r["pattern"]),
            r["message"],
            re.compile(r["unless"]) if "unless" in r else None,
        )
        for r in d.get("short_circuit", [])
    ]
    drop_lines = [re.compile(p) for p in d.get("drop_lines", [])]
    keep_lines = [re.compile(p) for p in d.get("keep_lines", [])]
    if drop_lines and keep_lines:
        raise ValueError(f"filter '{name}': drop_lines と keep_lines は併用できない")

    strategies = list(d.get("strategies", []))
    for strategy in strategies:
        if strategy not in STRATEGY_DISPATCH:
            raise ValueError(f"filter '{name}': 未知の戦略 '{strategy}'")

    return ReduxFilterSpec(
        name=name,
        command_pattern=re.compile(d["command_pattern"]),
        description=d.get("description", ""),
        strip_ansi=d.get("strip_ansi", False),
        substitute=substitute,
        short_circuit=short_circuit,
        drop_lines=drop_lines,
        keep_lines=keep_lines,
        clip_width=d.get("clip_width"),
        head_lines=d.get("head_lines"),
        tail_lines=d.get("tail_lines"),
        limit_lines=d.get("limit_lines"),
        empty_message=d.get("empty_message"),
        strategies=strategies,
    )


def _parse_toml(path: Path, *, trusted: bool = True) -> tuple[list[ReduxFilterSpec], list[FilterCase]]:
    """1 つの TOML ファイルからフィルタ定義とインラインケースを読み込む。

    Args:
        path: 読み込む TOML ファイル。
        trusted: 組込フィルタなら True。False の場合は正規表現の長さ上限を検証する。

    Raises:
        ValueError: schema_version が非対応、または trusted=False で正規表現が長すぎる場合。
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    version = data.get("schema_version")
    if version != _SCHEMA_VERSION:
        raise ValueError(f"{path.name}: schema_version {version!r} は非対応（{_SCHEMA_VERSION} が必須）")

    specs = [_build_spec(name, spec_dict, trusted=trusted) for name, spec_dict in data.get("filters", {}).items()]
    cases: list[FilterCase] = []
    for filter_name, case_list in data.get("cases", {}).items():
        for case in case_list:
            cases.append(FilterCase(filter_name, case["name"], case["input"], case["expected"]))
    return specs, cases


def _user_filter_paths() -> list[Path]:
    """ユーザー定義フィルタファイルのパス（優先順位順: プロジェクト → ユーザー全体）。"""
    return [
        Path.cwd() / ".bluecore" / "redux" / "filters.toml",
        Path.home() / ".bluecore" / "redux" / "filters.toml",
    ]


def builtin_filter_paths() -> list[Path]:
    """組込フィルタ TOML のパス一覧（``default.toml`` を末尾に固定）。"""
    paths = sorted(p for p in _BUILTIN_DIR.glob("*.toml") if p.name != _DEFAULT_FILTER_FILE)
    default_path = _BUILTIN_DIR / _DEFAULT_FILTER_FILE
    if default_path.is_file():
        paths.append(default_path)
    return paths


def load_filter_specs() -> list[ReduxFilterSpec]:
    """ユーザー定義 → 組込の順でフィルタ定義を統合して返す。

    ユーザー/プロジェクトの TOML はパースエラー・不正な正規表現・長さ超過を
    stderr 警告にとどめてスキップし、設定ミスが組込フィルタ全体を無効化しない
    ようにする。組込フィルタのエラーは開発時に検出するためそのまま送出する。
    """
    specs: list[ReduxFilterSpec] = []
    for path in _user_filter_paths():
        if not path.is_file():
            continue
        try:
            file_specs, _ = _parse_toml(path, trusted=False)
        except (ValueError, tomllib.TOMLDecodeError, OSError, re.error) as e:
            sys.stderr.write(f"[redux] ユーザーフィルタを無視しました（{path}）: {e}\n")
            continue
        specs.extend(file_specs)
    for path in builtin_filter_paths():
        file_specs, _ = _parse_toml(path)
        specs.extend(file_specs)
    return specs


def load_builtin_cases() -> list[FilterCase]:
    """組込フィルタの全インラインテストケースを返す。"""
    cases: list[FilterCase] = []
    for path in builtin_filter_paths():
        _, file_cases = _parse_toml(path)
        cases.extend(file_cases)
    return cases
