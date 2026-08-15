#!/usr/bin/env python3
"""Observation hook runtime for learn.

ツール呼び出しごとに 1 行の観測を ``~/.bluecore/repos/<repo-id>/observations.jsonl``
へ追記し、必要なら observer プロセスを起こす。ここは **生ログの置き場** であり、
観測から抽出した知識は observer が ``knowledge`` テーブルへ書く。
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from bluecore.hooks.hook_common import read_raw_stdin
from bluecore.lib.harness import normalize_tool_name
from bluecore.mem.settings import Settings
from bluecore.skills.learn.storage import (
    JsonlLog,
    ObservationTarget,
    ensure_storage_dirs,
    resolve_observation_target,
)

_DEFAULT_SIGNAL_EVERY_N = 20
_DEFAULT_SKIP_PATHS = ("observer-sessions", ".claude-mem")
_TOOL_PAYLOAD_LIMIT = 5000
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization|credentials?|auth)"
    r"""(["'\s:=]+)"""
    r"([A-Za-z]+\s+)?"
    r"([A-Za-z0-9_\-/.+=]{8,})"
)


def _now_utc() -> str:
    """現在時刻を Z 終端の UTC ISO8601 文字列で返す。"""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _resolve_python_cmd() -> str:
    """子プロセス起動に使う Python 実行コマンドを解決する。"""
    return sys.executable or "python3"


def _data_dir() -> Path:
    """bluecore のデータディレクトリを返す。

    ``Settings`` 経由で解決するため ``BLUECORE_DATA_PATH`` による隔離が効く。

    Returns:
        ``~/.bluecore`` 相当のパス。
    """
    return Settings().data_path


def _is_disabled() -> bool:
    """学習機能が無効化されているかを判定する。

    設定ディレクトリまたは CLV2_CONFIG の隣に ``disabled`` ファイルがあれば
    無効とみなす。
    """
    if (_data_dir() / "disabled").exists():
        return True

    clv2_config = os.environ.get("CLV2_CONFIG")
    if clv2_config and (Path(clv2_config).resolve().parent / "disabled").exists():
        return True

    return False


def _should_skip_automation(stdin_data: dict) -> bool:
    """観測の自動処理をスキップすべきかを判定する。

    対象外エントリポイント・スキップ環境変数・サブエージェント実行・
    スキップ対象パスのいずれかに該当する場合に ``True`` を返す。
    """
    entrypoint = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli")
    if entrypoint not in {"cli", "sdk-ts"}:
        return True

    if os.environ.get("BLUECORE_SKIP_OBSERVE", "0") == "1":
        return True

    if stdin_data.get("agent_id"):
        return True

    skip_paths = os.environ.get("BLUECORE_OBSERVE_SKIP_PATHS", ",".join(_DEFAULT_SKIP_PATHS))
    cwd = str(stdin_data.get("cwd", "") or "")
    if cwd:
        for pattern in (part.strip() for part in skip_paths.split(",")):
            if pattern and pattern in cwd:
                return True

    return False


def _resolve_target(stdin_data: dict) -> ObservationTarget:
    """フック入力の cwd から観測ログの書き込み先を解決する。

    フックが渡す ``cwd`` を最優先し、無ければ ``CLAUDE_PROJECT_DIR``、
    それも無ければプロセスの cwd（``resolve_observation_target`` の既定）を使う。
    git 本体リポジトリへの寄せ込みは ``repos`` の正体キー解決が行う。

    Args:
        stdin_data: フックが渡した JSON。

    Returns:
        解決した ObservationTarget。
    """
    cwd = str(stdin_data.get("cwd", "") or "")
    if not cwd or not Path(cwd).is_dir():
        cwd = os.environ.get("CLAUDE_PROJECT_DIR", "")
    return resolve_observation_target(cwd or None)


def _redact_secret_match(match: re.Match[str]) -> str:
    """シークレット値だけを [REDACTED] にした置換文字列を返す。"""
    prefix = match.group(3) or ""
    return f"{match.group(1)}{match.group(2)}{prefix}[REDACTED]"


def _scrub_secret_text(value: str | None) -> str | None:
    """テキスト中のシークレット値を [REDACTED] に置換する。"""
    if value is None:
        return None
    return _SECRET_RE.sub(_redact_secret_match, str(value))


def _parse_input(raw: str) -> dict | None:
    """生入力を JSON として解析する。

    Returns:
        解析できた dict。オブジェクト以外や解析失敗時は
        ``{"parsed": False, "error": ...}`` を返す。
    """
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {"parsed": False, "error": "hook payload is not a JSON object"}
    except json.JSONDecodeError as error:
        return {"parsed": False, "error": str(error)}


def _tool_payload_text(value: object) -> str:
    """ツール入出力を観測用に JSON/文字列化し、上限文字数で切り詰める。"""
    text = json.dumps(value) if isinstance(value, dict) else str(value)
    return text[:_TOOL_PAYLOAD_LIMIT]


def _build_observation(stdin_data: dict, phase: str, target: ObservationTarget) -> dict:
    """フック入力から 1 件分の観測レコードを構築する。

    入力・出力は ``_TOOL_PAYLOAD_LIMIT`` 文字で切り詰め、シークレットを除去して格納する。
    """
    event = "tool_start" if phase == "pre" else "tool_complete"
    tool_name = normalize_tool_name(str(stdin_data.get("tool_name", stdin_data.get("tool", "unknown"))))
    tool_input = stdin_data.get("tool_input", stdin_data.get("input", ""))
    tool_output = stdin_data.get("tool_response")
    if tool_output is None:
        tool_output = stdin_data.get("tool_output", stdin_data.get("output", ""))

    tool_input_str = _tool_payload_text(tool_input)
    tool_output_str = _tool_payload_text(tool_output)

    observation = {
        "timestamp": _now_utc(),
        "event": event,
        "tool": tool_name,
        "session": stdin_data.get("session_id", stdin_data.get("session", "unknown")),
        "repo": target.repo_id,
    }
    if tool_input_str:
        observation["input"] = _scrub_secret_text(tool_input_str)
    if tool_output_str is not None:  # pragma: no branch
        observation["output"] = _scrub_secret_text(tool_output_str)
    return observation


def _safe_unlink(path: Path) -> None:
    """ファイルを削除する。存在しない・削除失敗時は何もしない。"""
    try:
        path.unlink()
    except OSError:
        pass


def _read_pid_file(pid_file: Path) -> int | None:
    """PID ファイルから PID を読む。無い・不正なら None。不正内容はファイルを消す。"""
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        _safe_unlink(pid_file)
        return None


def _observer_pid_files(target: ObservationTarget) -> list[Path]:
    """リポジトリ側と共通データディレクトリの observer PID ファイル候補を返す。"""
    return [target.storage_dir / ".observer.pid", _data_dir() / ".observer.pid"]


def _start_observer_if_needed(target: ObservationTarget) -> None:
    """オブザーバーが未起動なら子プロセスとして起動する。

    PID ファイルで稼働中のプロセスがあれば何もしない。
    """
    if any(_pid_is_running(path) for path in _observer_pid_files(target)):
        return

    env = os.environ.copy()
    env["BLUECORE_SKIP_OBSERVE"] = "1"
    env.setdefault("CLV2_IS_WINDOWS", "false")
    env["REPO_ID"] = target.repo_id
    env["REPO_ROOT"] = str(target.repo_root)
    try:
        subprocess.Popen(
            [_resolve_python_cmd(), "-m", "bluecore.skills.learn.observer", "start"],
            cwd=str(target.repo_root),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
        )
    except OSError:
        return


def _pid_is_running(pid_file: Path) -> bool:
    """PID ファイルの示すプロセスが稼働中か判定する。

    不正・未稼働の場合は PID ファイルを削除して ``False`` を返す。
    """
    pid = _read_pid_file(pid_file)
    if pid is None:
        return False
    if pid <= 1:
        _safe_unlink(pid_file)
        return False

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        _safe_unlink(pid_file)
        return False


def _should_signal_now(target: ObservationTarget, signal_every_n: int) -> bool:
    """カウンタファイルをインクリメントし、シグナル送出タイミングかを返す。"""
    counter_file = target.storage_dir / ".observer-signal-counter"
    try:
        counter = int(counter_file.read_text(encoding="utf-8").strip()) if counter_file.exists() else 0
    except (OSError, ValueError):
        counter = 0

    counter += 1
    should = counter >= signal_every_n
    if should:
        counter = 0

    try:
        counter_file.write_text(str(counter), encoding="utf-8")
    except OSError:
        pass

    return should


def _send_sigusr1_to_pid_file(pid_file: Path, signaled: set[int]) -> None:
    """PID ファイルのプロセスが有効なら SIGUSR1 を送る。"""
    pid = _read_pid_file(pid_file)
    if pid is None or pid in signaled or pid <= 1:
        return

    try:
        os.kill(pid, 0)
    except OSError:
        _safe_unlink(pid_file)
        return

    try:
        os.kill(pid, signal.SIGUSR1)
        signaled.add(pid)
    except OSError:
        pass


def _signal_observers(target: ObservationTarget) -> None:
    """N 件ごとに稼働中オブザーバーへ SIGUSR1 を送る。

    カウンタファイルで間引き、閾値到達時のみシグナルを送出する。
    """
    signal_every_n = int(os.environ.get("BLUECORE_OBSERVER_SIGNAL_EVERY_N", str(_DEFAULT_SIGNAL_EVERY_N)))
    if not _should_signal_now(target, signal_every_n):
        return

    if not hasattr(signal, "SIGUSR1"):  # pragma: no cover
        return

    signaled: set[int] = set()
    for pid_file in _observer_pid_files(target):
        _send_sigusr1_to_pid_file(pid_file, signaled)


def _write_parse_error(log: JsonlLog, raw: str) -> None:
    """解析失敗イベントを観測ログに記録する。"""
    log.append(
        {
            "timestamp": _now_utc(),
            "event": "parse_error",
            "raw": _scrub_secret_text(raw[:2000]),
        },
    )


def _handle_parse_error(stdin_data: dict, raw: str) -> None:
    """解析エラー時にリポジトリを解決してエラーを記録する。"""
    target = _resolve_target(stdin_data)
    ensure_storage_dirs(target)
    _write_parse_error(target.observations_log, raw)


def _record_and_signal(stdin_data: dict, phase: str) -> None:
    """リポジトリを解決して観測を記録し、オブザーバーへシグナルを送る。"""
    target = _resolve_target(stdin_data)
    ensure_storage_dirs(target)
    log = target.observations_log
    log.maintain()

    log.append(_build_observation(stdin_data, phase, target))

    if not _is_disabled():
        _start_observer_if_needed(target)
        _signal_observers(target)


def _run(argv: list[str] | None) -> int:
    """観測フック本体。標準入力を読み、観測レコードを記録してオブザーバーへ通知する。

    Returns:
        常に 0。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    phase = os.environ.get("HOOK_PHASE", "post")
    if args and args[0] in {"pre", "post"}:
        phase = args[0]

    raw = read_raw_stdin()
    if not raw:
        return 0

    stdin_data = _parse_input(raw)
    if stdin_data is None:
        return 0

    if stdin_data.get("parsed") is False:
        _handle_parse_error(stdin_data, raw)
        return 0

    if _should_skip_automation(stdin_data):
        return 0

    _record_and_signal(stdin_data, phase)
    return 0


def main(argv: list[str] | None = None) -> int:
    """観測フックのエントリポイント。

    標準入力からフックペイロードを読み、観測レコードを記録して
    オブザーバーの起動・シグナル送出を行う。``*`` matcher で全ツール呼び出し毎に
    発火するため、リポジトリ解決・DB アクセス・ファイル I/O のいずれで例外が
    起きても握りつぶす。PreToolUse の非ゼロ終了コードはツール実行のブロックとして
    扱われるため、観測の失敗でユーザーのツール実行を止めてはならない。

    Returns:
        プロセス終了コード（常に 0）。
    """
    try:
        return _run(argv)
    except Exception:  # noqa: BLE001 - 観測失敗でツール実行をブロックしない
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
