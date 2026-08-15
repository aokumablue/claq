#!/usr/bin/env python3
"""
言語プリセットに基づく品質ゲート runner。

`detect_project().primary_language` に応じた言語プリセット
(`quality_gate_presets.resolve_quality_gate_config`) からルールを解決し、
`post-edit` アクションで設定済みの step を順に実行します。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bluecore.hooks.hook_common import emit_post_tool_use_output, parse_json_object, read_raw_stdin
from bluecore.hooks.quality_gate_presets import resolve_quality_gate_config
from bluecore.lib.core_utils import log
from bluecore.lib.harness import extract_file_paths, extract_raw_tool_name, extract_tool_input, normalize_tool_name

PLUGIN_ROOT = Path(__file__).resolve().parents[3]

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ALLOWED_EXPANSION_VARS = frozenset(
    {
        "CLAUDE_PLUGIN_ROOT",
        "QUALITY_GATE_PROJECT_ROOT",
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
    }
)
# hooks.json の matcher は書込み系に絞ってあるが、matcher の解釈が異なるハーネス
# でも対象外ツールを弾けるよう早期 return の判定に使う。_extract_tool_name() が
# 正規化した Claude Code 表記（Codex の apply_patch は Edit、Copilot CLI の
# lowercase tool_name も正規化済み）を小文字化して比較する。
_WRITE_TOOL_NAMES = frozenset({"edit", "write", "multiedit"})

# step の timeout 既定値。hooks.json の timeout（15秒）より十分小さくして、
# ハーネス側で打ち切られる前にフック自身が結果を返せるようにする。
DEFAULT_STEP_TIMEOUT = 10.0
# 1 step あたりの出力上限（文字）。
STEP_OUTPUT_LIMIT = 1500
# additionalContext 全体の上限（文字）。壊れたビルドがコンテキストを食い潰さないため。
CONTEXT_LIMIT = 4000


def _normalize_name(value: str | None) -> str:
    """文字列を比較用に正規化する。

    Args:
        value: 正規化する値です。

    Returns:
        小文字化して前後の空白を削除した文字列を返します。

    Raises:
        例外は発生しません。
    """
    return str(value or "").strip().lower()


def _normalize_extension(value: str | None) -> str:
    """拡張子を比較用に正規化する。

    Args:
        value: 正規化する拡張子です。

    Returns:
        先頭にドットが付いた小文字の拡張子を返します。

    Raises:
        例外は発生しません。
    """
    normalized = _normalize_name(value)
    if not normalized:
        return ""
    return normalized if normalized.startswith(".") else f".{normalized}"


def _expand_text(value: str, env: dict[str, str], allowed_names: set[str] | None = None) -> str:
    """`${VAR}` 形式の参照を環境変数で展開する。

    Args:
        value: 展開対象の文字列です。
        env: 参照に使う環境変数です。
        allowed_names: 展開を許可する変数名。None なら既定の許可リスト。

    Returns:
        展開後の文字列を返します。

    Raises:
        例外は発生しません。
    """
    if not value:
        return value

    allowed = _ALLOWED_EXPANSION_VARS if allowed_names is None else allowed_names

    def replace(match: re.Match[str]) -> str:
        """マッチした変数参照を許可リストに従って環境変数値へ置換する。"""
        name = match.group(1)
        if name not in allowed:
            return match.group(0)
        return env.get(name, os.environ.get(name, match.group(0)))

    return _ENV_PATTERN.sub(replace, value)


def _project_root() -> Path:
    """quality-gate が基準に使うプロジェクトルートを返す。

    Returns:
        プロジェクトルートの Path を返します。

    Raises:
        例外は発生しません。
    """
    raw = os.environ.get("CLAUDE_PROJECT_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.cwd().resolve()


def _base_env() -> dict[str, str]:
    """子プロセス用の環境変数を構築する。

    Returns:
        実行用に調整した環境変数の辞書を返します。

    Raises:
        例外は発生しません。
    """
    env = os.environ.copy()
    env.setdefault("CLAUDE_PLUGIN_ROOT", str(PLUGIN_ROOT))
    env.setdefault("QUALITY_GATE_PROJECT_ROOT", str(_project_root()))

    pythonpath = env.get("PYTHONPATH")
    paths = [str(PLUGIN_ROOT / "src")]
    if pythonpath:
        paths.append(pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def load_config(file_path: str | None = None) -> dict[str, Any]:
    """quality-gate 設定を取得する。

    `detect_project().primary_language` に応じた言語プリセット
    (`quality_gate_presets.resolve_quality_gate_config`) から生成する。

    Args:
        file_path: 変更されたファイルパス。指定時は単一ファイル対象の lint コマンドを生成する。

    Returns:
        設定オブジェクトを返します。解決できない場合は空設定を返します。

    Raises:
        例外は発生しません。
    """
    return resolve_quality_gate_config(file_path=file_path)


def _extract_target_file_paths(input_data: dict[str, Any]) -> list[str]:
    """フック入力から対象ファイルパスをすべて取り出す。

    構造化パッチ（Codex の apply_patch 等）はパッチテキストから全対象
    ファイルを取り出す（複数ファイルパッチの一部だけ lint が走る取りこぼしを
    防ぐ）。パッチかどうかは tool_name の文字列一致だけに頼らず、
    ``extract_file_paths`` が生入力の内容（マーカー行の有無）でも判定する
    ため、ハーネスごとの tool_name の表記ゆれでも取りこぼさない。

    Args:
        input_data: hook 入力です。

    Returns:
        ファイルパスのリスト。判定不能な場合は空リストを返します。

    Raises:
        例外は発生しません。
    """
    tool_input = extract_tool_input(input_data)
    if isinstance(tool_input, dict):
        file_path = tool_input.get("file_path")
        if isinstance(file_path, str) and file_path:
            return [file_path]

    tool_name = extract_raw_tool_name(input_data)
    structured_paths = extract_file_paths(tool_name, tool_input) or []
    if structured_paths:
        return structured_paths

    file_path = input_data.get("file_path")
    if isinstance(file_path, str) and file_path:
        return [file_path]
    return []


def _extract_tool_name(input_data: dict[str, Any]) -> str:
    """フック入力から tool_name を取り出す。

    Args:
        input_data: hook 入力です。

    Returns:
        tool_name があれば文字列、なければ空文字列を返します。

    Raises:
        例外は発生しません。
    """
    tool_name = extract_raw_tool_name(input_data)
    return normalize_tool_name(tool_name) if tool_name else ""


def _is_write_tool(input_data: dict[str, Any]) -> bool:
    """入力が quality-gate 対象の書込み系ツールか判定する。

    matcher が "*" のため、Edit/Write/MultiEdit（Codex の apply_patch・
    Copilot CLI の lowercase tool_name を含む）以外は対象外として扱う。

    Args:
        input_data: hook 入力です。

    Returns:
        書込み系ツールなら True、それ以外は False を返します。

    Raises:
        例外は発生しません。
    """
    return _normalize_name(_extract_tool_name(input_data)) in _WRITE_TOOL_NAMES


def _rule_matches(rule: dict[str, Any], input_data: dict[str, Any], file_path: str = "") -> bool:
    """設定ルールが入力に一致するか判定する。

    Args:
        rule: ルール定義です。
        input_data: hook 入力です。
        file_path: 拡張子判定の対象ファイルパス（対象なしは空文字列）。

    Returns:
        一致する場合は True、それ以外は False を返します。

    Raises:
        例外は発生しません。
    """
    extensions = rule.get("extensions")
    if extensions is not None:
        if not file_path:
            return False
        allowed = set()
        for ext in extensions:
            normalized = _normalize_extension(ext)
            if normalized:
                allowed.add(normalized)
        if Path(file_path).suffix.lower() not in allowed:
            return False

    tool_names = rule.get("tool_names")
    if tool_names is not None:
        allowed_tools = {_normalize_name(name) for name in tool_names if _normalize_name(name)}
        if allowed_tools and _normalize_name(_extract_tool_name(input_data)) not in allowed_tools:
            return False

    return True


def _build_step_command(
    step: dict[str, Any],
    env: dict[str, str],
    allowed_names: set[str],
) -> list[str] | None:
    """step 定義から実行コマンドを作る。

    Args:
        step: 設定された step です。
        env: 展開に使う環境変数です。
        allowed_names: 展開を許可する変数名。

    Returns:
        subprocess に渡すコマンド列、または不正な場合は None を返します。

    Raises:
        例外は発生しません。
    """
    module = step.get("module")
    if isinstance(module, str) and module.strip():
        args = step.get("args", [])
        extra_args: list[str] = []
        if isinstance(args, list):
            extra_args = [
                _expand_text(str(arg), env, allowed_names=allowed_names)
                for arg in args
                if isinstance(arg, str)
            ]
        return [sys.executable, "-m", module.strip(), *extra_args]

    argv = step.get("argv")
    if isinstance(argv, list) and argv:
        return [_expand_text(str(arg), env, allowed_names=allowed_names) for arg in argv]

    return None


def _build_step_env(
    step: dict[str, Any],
    base_env: dict[str, str],
    allowed_names: set[str],
) -> dict[str, str]:
    """step 用に環境変数を拡張する。

    Args:
        step: 設定された step です。
        base_env: 基本環境です。
        allowed_names: 展開を許可する変数名。

    Returns:
        step 用に拡張した環境変数を返します。

    Raises:
        例外は発生しません。
    """
    env = base_env.copy()
    extra_env = step.get("env")
    if isinstance(extra_env, dict):
        for key, value in extra_env.items():
            if value is None:
                continue
            env[str(key)] = _expand_text(str(value), env, allowed_names=allowed_names)
    return env


def _build_step_cwd(
    step: dict[str, Any],
    default_cwd: Path,
    env: dict[str, str],
    allowed_names: set[str],
) -> Path | None:
    """step 用の作業ディレクトリを解決する。

    Args:
        step: 設定された step です。
        default_cwd: 省略時に使う作業ディレクトリです。
        env: 展開に使う環境変数です。
        allowed_names: 展開を許可する変数名。

    Returns:
        解決済みの作業ディレクトリを返します。

    Raises:
        例外は発生しません。
    """
    raw_cwd = step.get("cwd")
    if not isinstance(raw_cwd, str) or not raw_cwd.strip():
        return default_cwd

    cwd = Path(_expand_text(raw_cwd, env, allowed_names=allowed_names)).expanduser()
    if not cwd.is_absolute():
        cwd = (default_cwd / cwd).resolve()
    else:
        cwd = cwd.resolve()

    try:
        cwd.relative_to(default_cwd)
    except ValueError:
        log(f"[QualityGate] rejected cwd outside project root: {cwd}")
        return None

    return cwd


def _resolve_timeout(step: dict[str, Any]) -> float:
    """step 設定から timeout（秒）を float で返す。不正値は 30.0 にフォールバックする。

    Args:
        step: step 定義辞書。

    Returns:
        正の float 値のタイムアウト秒数。
    """
    timeout_raw = step.get("timeout_seconds", step.get("timeout", DEFAULT_STEP_TIMEOUT))
    try:
        timeout = float(timeout_raw)
        return timeout if timeout > 0 else DEFAULT_STEP_TIMEOUT
    except (TypeError, ValueError):
        return DEFAULT_STEP_TIMEOUT


@dataclass(frozen=True)
class _StepCmd:
    """_execute_step_command のコマンド実行パラメータ。"""

    command: list[str]
    raw_input: str
    cwd: Path
    env: dict[str, str]
    timeout: float
    name: str


@dataclass(frozen=True)
class _StepResult:
    """1 step の実行結果。additionalContext の組み立てに使う。"""

    name: str
    returncode: int
    output: str


def _execute_step_command(step_cmd: _StepCmd) -> _StepResult | None:
    """コマンドをサブプロセスで実行し、結果を返す。

    stdout と stderr を結合して STEP_OUTPUT_LIMIT 文字で切り詰め、呼び出し元が
    additionalContext に載せられる形で返す。stderr の素通し転送は行わない
    （Claude Code では PostToolUse の stderr がモデルに渡らないため）。

    Args:
        step_cmd: 実行パラメータ。

    Returns:
        実行できた場合は _StepResult、OSError / タイムアウトで起動できなかった
        場合は None。
    """
    try:
        result = subprocess.run(
            step_cmd.command,
            input=step_cmd.raw_input,
            text=True,
            capture_output=True,
            cwd=str(step_cmd.cwd),
            env=step_cmd.env,
            timeout=step_cmd.timeout,
            check=False,
        )
    except OSError as err:
        log(f"[QualityGate] step skipped ({step_cmd.name}): {err}")
        return None
    except subprocess.TimeoutExpired:
        log(f"[QualityGate] step timed out ({step_cmd.name}): {step_cmd.timeout:g}s")
        return None
    if result.returncode != 0:
        log(f"[QualityGate] step failed ({step_cmd.name}): exit code {result.returncode}")
    combined = "".join(part for part in (result.stdout, result.stderr) if part)
    return _StepResult(
        name=step_cmd.name,
        returncode=result.returncode,
        output=combined.strip()[:STEP_OUTPUT_LIMIT],
    )


def run_step(
    step: dict[str, Any],
    raw_input: str,
    *,
    base_env: dict[str, str] | None = None,
    default_cwd: Path | None = None,
) -> _StepResult | None:
    """設定された step を 1 回実行する。

    Args:
        step: 実行する step 定義です。
        raw_input: 子プロセスへ渡す stdin です。
        base_env: 子プロセスの基本環境です。
        default_cwd: 省略時の作業ディレクトリです。

    Returns:
        step を起動できた場合は _StepResult、設定不備や起動失敗の場合は None を返します。

    Raises:
        例外は発生しません。
    """
    base_env = base_env or _base_env()
    default_cwd = default_cwd or _project_root()
    extra_env = step.get("env")
    allowed_names = set(_ALLOWED_EXPANSION_VARS)
    if isinstance(extra_env, dict):
        allowed_names.update(str(key) for key in extra_env.keys())

    env = _build_step_env(step, base_env, allowed_names)
    command = _build_step_command(step, env, allowed_names)
    if not command:
        log("[QualityGate] invalid step definition: module or argv is required")
        return None

    cwd = _build_step_cwd(step, default_cwd, env, allowed_names)
    if cwd is None:
        return None

    timeout = _resolve_timeout(step)
    name = str(step.get("name") or step.get("module") or command[0])
    return _execute_step_command(_StepCmd(
        command=command,
        raw_input=raw_input,
        cwd=cwd,
        env=env,
        timeout=timeout,
        name=name,
    ))


def exec_command(command: str, args: list[str], cwd: str | Path | None = None) -> dict[str, Any]:
    """コマンドを同期実行する。

    Args:
        command: 実行する command です。
        args: command に渡す引数です。
        cwd: 作業ディレクトリです。

    Returns:
        実行結果の辞書を返します。

    Raises:
        例外は発生しません。
    """
    try:
        result = subprocess.run(
            [command, *args],
            cwd=str(cwd or Path.cwd()),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return {
            "status": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except (subprocess.TimeoutExpired, OSError) as err:
        return {
            "status": -1,
            "stdout": "",
            "stderr": str(err),
        }


def _run_rule_steps(
    rule: dict[str, Any], raw_input: str, base_env: dict[str, str], default_cwd: Path
) -> list[_StepResult]:
    """1つの rule に属する steps をすべて実行し、起動できた step の結果を返す。

    Args:
        rule: ルール定義辞書。
        raw_input: 子プロセスへ渡す stdin。
        base_env: 子プロセスの基本環境。
        default_cwd: 省略時の作業ディレクトリ。

    Returns:
        起動できた step の _StepResult のリスト。
    """
    steps = rule.get("steps")
    if not isinstance(steps, list):
        log("[QualityGate] rule is missing steps")
        return []
    results: list[_StepResult] = []
    for step in steps:
        if isinstance(step, dict):
            result = run_step(step, raw_input, base_env=base_env, default_cwd=default_cwd)
            if result is not None:
                results.append(result)
        else:
            log("[QualityGate] invalid step entry ignored")
    return results


def _run_configured_rules(
    action: str,
    raw_input: str,
    input_data: dict[str, Any],
    config: dict[str, Any],
    file_path: str = "",
    run_pathless: bool = True,
) -> list[_StepResult]:
    """設定ファイルに従って rule を実行する。

    Args:
        action: 実行アクション名です。
        raw_input: 子プロセスへ渡す stdin です。
        input_data: hook 入力です。
        config: 読み込んだ quality-gate 設定です。
        file_path: 拡張子判定の対象ファイルパス（対象なしは空文字列）。
        run_pathless: extensions 指定のない rule を実行するか。複数ファイル
            パッチで 2 件目以降のファイルを処理する際に False を渡し、
            プロジェクト全体ルールの重複実行を防ぎます。

    Returns:
        起動できた step の _StepResult のリストを返します。

    Raises:
        例外は発生しません。
    """
    actions = config.get("actions")
    if not isinstance(actions, dict):
        return []
    action_config = actions.get(action)
    if not isinstance(action_config, dict):
        return []
    rules = action_config.get("rules")
    if not isinstance(rules, list):
        return []

    base_env = _base_env()
    default_cwd = _project_root()
    results: list[_StepResult] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("extensions") is None and not run_pathless:
            continue
        if _rule_matches(rule, input_data, file_path):
            results.extend(_run_rule_steps(rule, raw_input, base_env, default_cwd))
    return results


def run(raw_input: str, action: str = "post-edit") -> list[_StepResult]:
    """quality-gate を実行する。

    Args:
        raw_input: hook の生入力です。
        action: 実行するアクション名です。

    Returns:
        起動できた step の _StepResult のリストを返します。書き込み系ツール
        以外の入力では空リストを返します。

    Raises:
        例外は発生しません。
    """
    normalized_action = _normalize_name(action) or "post-edit"
    input_data = parse_json_object(raw_input) or {}
    if not _is_write_tool(input_data):
        return []
    results: list[_StepResult] = []
    paths = _extract_target_file_paths(input_data)
    for index, fp in enumerate(paths or [None]):
        config = load_config(file_path=fp)
        results.extend(
            _run_configured_rules(
                normalized_action,
                raw_input,
                input_data,
                config,
                file_path=fp or "",
                run_pathless=index == 0,
            )
        )
    return results


def _build_failure_context(results: list[_StepResult]) -> str:
    """失敗した step の出力から additionalContext 本文を組み立てる。

    Args:
        results: 実行済み step の結果リスト。

    Returns:
        additionalContext に載せる文字列。報告すべき失敗がなければ空文字列。

    Raises:
        例外は発生しません。
    """
    failures = [r for r in results if r.returncode != 0 and r.output]
    if not failures:
        return ""
    body = "\n\n".join(f"## {r.name} (exit {r.returncode})\n{r.output}" for r in failures)
    return f"<quality-gate>\n{body[:CONTEXT_LIMIT]}\n</quality-gate>"


def main(argv: list[str] | None = None) -> int:
    """スクリプト実行時のエントリーポイント。

    lint が失敗した step があれば、その出力を additionalContext として stdout に
    返す。成功時は何も出力しないためトークンコストはゼロ。ブロックは行わず
    常に 0 を返す。

    Args:
        argv: コマンドライン引数です。省略時は sys.argv を使います。

    Returns:
        常に 0 を返します。

    Raises:
        例外は発生しません。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    action = args[0] if args else "post-edit"
    raw = ""

    try:
        raw = read_raw_stdin()
        context = _build_failure_context(run(raw, action=action))
        if context:
            print(emit_post_tool_use_output(context), end="")
        return 0
    except Exception as err:  # noqa: BLE001 - hook must remain non-blocking
        log(f"[QualityGate] unexpected error: {err}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
