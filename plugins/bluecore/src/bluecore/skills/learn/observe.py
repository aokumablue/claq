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

from bluecore.lib.harness import normalize_tool_name
from bluecore.mem.settings import Settings
from bluecore.skills.learn.storage import (
    ObservationTarget,
    ensure_storage_dirs,
    resolve_observation_target,
)

_DEFAULT_SIGNAL_EVERY_N = 20
_DEFAULT_SKIP_PATHS = ("observer-sessions", ".claude-mem")
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization|credentials?|auth)"
    r"""(["'\s:=]+)"""
    r"([A-Za-z]+\s+)?"
    r"([A-Za-z0-9_\-/.+=]{8,})"
)


def _now_utc() -> str:
    """現在時刻を Z 終端の UTC ISO8601 文字列で返す。"""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _read_raw_stdin() -> str:
    """標準入力を生のまま読み、UTF-8 として復号した文字列を返す。"""
    return sys.stdin.buffer.read().decode("utf-8", errors="replace")


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


def _scrub_secret_text(value: str | None) -> str | None:
    """テキスト中のシークレット値を [REDACTED] に置換する。"""
    if value is None:
        return None
    return _SECRET_RE.sub(lambda match: match.group(1) + match.group(2) + (match.group(3) or "") + "[REDACTED]", str(value))


def _archive_old_observation_files(target: ObservationTarget) -> None:
    """1 日 1 回、30 日より古いアーカイブ済み観測ファイルを削除する。"""
    purge_marker = target.storage_dir / ".last-purge"
    try:
        stale = not purge_marker.exists() or (datetime.now(UTC).timestamp() - purge_marker.stat().st_mtime) > 86400
    except OSError:
        stale = True

    if not stale:
        return

    archive_dir = target.archive_dir
    archive_dir.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(UTC).timestamp() - (30 * 24 * 60 * 60)
    try:
        archived = list(archive_dir.glob("observations-*.jsonl"))
    except OSError:
        archived = []
    for path in archived:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue

    try:
        purge_marker.touch()
    except OSError:
        pass


def _archive_if_too_large(target: ObservationTarget) -> None:
    """観測ファイルが 10MB を超えたらアーカイブへ退避する。"""
    obs_path = target.observations_file
    try:
        if not obs_path.exists() or obs_path.stat().st_size < 10 * 1024 * 1024:
            return
    except OSError:
        return

    archive_dir = target.archive_dir
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"observations-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.jsonl"
    try:
        obs_path.replace(archive_path)
    except OSError:
        pass


def _append_observation(obs_path: Path, payload: dict) -> None:
    """観測ペイロードを JSONL として 1 行追記する。"""
    obs_path.parent.mkdir(parents=True, exist_ok=True)
    with obs_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


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


def _build_observation(stdin_data: dict, phase: str, target: ObservationTarget) -> dict:
    """フック入力から 1 件分の観測レコードを構築する。

    入力・出力は 5000 文字で切り詰め、シークレットを除去して格納する。
    """
    event = "tool_start" if phase == "pre" else "tool_complete"
    tool_name = normalize_tool_name(str(stdin_data.get("tool_name", stdin_data.get("tool", "unknown"))))
    tool_input = stdin_data.get("tool_input", stdin_data.get("input", ""))
    tool_output = stdin_data.get("tool_response")
    if tool_output is None:
        tool_output = stdin_data.get("tool_output", stdin_data.get("output", ""))

    if isinstance(tool_input, dict):
        tool_input_str = json.dumps(tool_input)[:5000]
    else:
        tool_input_str = str(tool_input)[:5000]

    if isinstance(tool_output, dict):
        tool_output_str = json.dumps(tool_output)[:5000]
    else:
        tool_output_str = str(tool_output)[:5000]

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


def _start_observer_if_needed(target: ObservationTarget) -> None:
    """オブザーバーが未起動なら子プロセスとして起動する。

    PID ファイルで稼働中のプロセスがあれば何もしない。
    """
    pid_files = [
        target.storage_dir / ".observer.pid",
        _data_dir() / ".observer.pid",
    ]
    if any(_pid_is_running(path) for path in pid_files):
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
    if not pid_file.exists():
        return False

    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        try:
            pid_file.unlink()
        except OSError:
            pass
        return False

    if pid <= 1:
        try:
            pid_file.unlink()
        except OSError:
            pass
        return False

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        try:
            pid_file.unlink()
        except OSError:
            pass
        return False


def _should_signal_now(target: ObservationTarget, signal_every_n: int) -> bool:
    """カウンタファイルをインクリメントし、シグナル送出タイミングかを返す。"""
    counter_file = target.storage_dir / ".observer-signal-counter"
    try:
        counter = int(counter_file.read_text(encoding="utf-8").strip()) if counter_file.exists() else 0
    except (OSError, ValueError):
        counter = 0

    counter += 1
    if counter >= signal_every_n:
        counter = 0
        should = True
    else:
        should = False

    try:
        counter_file.write_text(str(counter), encoding="utf-8")
    except OSError:
        pass

    return should


def _send_sigusr1_to_pid_file(pid_file: Path, signaled: set) -> None:
    """PID ファイルのプロセスが有効なら SIGUSR1 を送る。"""
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        try:
            pid_file.unlink()
        except OSError:
            pass
        return

    if pid in signaled or pid <= 1:
        return

    try:
        os.kill(pid, 0)
    except OSError:
        try:
            pid_file.unlink()
        except OSError:
            pass
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
    for pid_file in [target.storage_dir / ".observer.pid", _data_dir() / ".observer.pid"]:
        _send_sigusr1_to_pid_file(pid_file, signaled)


def _write_parse_error(obs_path: Path, raw: str) -> None:
    """解析失敗イベントを観測ファイルに記録する。"""
    _append_observation(
        obs_path,
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
    _write_parse_error(target.observations_file, raw)


def _record_and_signal(stdin_data: dict, phase: str) -> None:
    """リポジトリを解決して観測を記録し、オブザーバーへシグナルを送る。"""
    target = _resolve_target(stdin_data)
    ensure_storage_dirs(target)
    _archive_old_observation_files(target)
    _archive_if_too_large(target)

    _append_observation(target.observations_file, _build_observation(stdin_data, phase, target))

    if not _is_disabled():
        _start_observer_if_needed(target)
        _signal_observers(target)


def main(argv: list[str] | None = None) -> int:
    """観測フックのエントリポイント。

    標準入力からフックペイロードを読み、観測レコードを記録して
    オブザーバーの起動・シグナル送出を行う。

    Returns:
        プロセス終了コード（常に 0）。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    phase = os.environ.get("HOOK_PHASE", "post")
    if args and args[0] in {"pre", "post"}:
        phase = args[0]

    raw = _read_raw_stdin()
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
