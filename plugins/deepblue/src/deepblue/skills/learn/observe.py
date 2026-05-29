#!/usr/bin/env python3
"""Observation hook runtime for s-learn."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from devgear.lib.core_utils import get_devgear_dir
from devgear.skills.learn.project import detect_project

_CONFIG_DIR = get_devgear_dir()
_DEFAULT_SIGNAL_EVERY_N = 20
_DEFAULT_SKIP_PATHS = ("observer-sessions", ".claude-mem")
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization|credentials?|auth)"
    r"""(["'\s:=]+)"""
    r"([A-Za-z]+\s+)?"
    r"([A-Za-z0-9_\-/.+=]{8,})"
)


def _now_utc() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _read_raw_stdin() -> str:
    return sys.stdin.buffer.read().decode("utf-8", errors="replace")


def _resolve_python_cmd() -> str:
    return sys.executable or "python3"


def _is_disabled() -> bool:
    if (_CONFIG_DIR / "disabled").exists():
        return True

    clv2_config = os.environ.get("CLV2_CONFIG")
    if clv2_config and (Path(clv2_config).resolve().parent / "disabled").exists():
        return True

    return False


def _should_skip_automation(stdin_data: dict) -> bool:
    entrypoint = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli")
    if entrypoint not in {"cli", "sdk-ts"}:
        return True

    if os.environ.get("DEVGEAR_SKIP_OBSERVE", "0") == "1":
        return True

    if stdin_data.get("agent_id"):
        return True

    skip_paths = os.environ.get("DEVGEAR_OBSERVE_SKIP_PATHS", ",".join(_DEFAULT_SKIP_PATHS))
    cwd = str(stdin_data.get("cwd", "") or "")
    if cwd:
        for pattern in (part.strip() for part in skip_paths.split(",")):
            if pattern and pattern in cwd:
                return True

    return False


def _set_project_dir_from_cwd(stdin_data: dict) -> str | None:
    cwd = str(stdin_data.get("cwd", "") or "")
    if not cwd or not Path(cwd).is_dir():
        return None

    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        project_dir = result.stdout.strip() if result.returncode == 0 else cwd
    except (FileNotFoundError, subprocess.TimeoutExpired):
        project_dir = cwd

    previous = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    return previous


def _restore_project_dir(previous: str | None) -> None:
    if previous is None:
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
    else:
        os.environ["CLAUDE_PROJECT_DIR"] = previous


def _scrub_secret_text(value: str | None) -> str | None:
    if value is None:
        return None
    return _SECRET_RE.sub(lambda match: match.group(1) + match.group(2) + (match.group(3) or "") + "[REDACTED]", str(value))


def _ensure_project_dirs(project_dir: Path) -> None:
    (project_dir / "observations.archive").mkdir(parents=True, exist_ok=True)
    (project_dir / "instincts" / "personal").mkdir(parents=True, exist_ok=True)
    (project_dir / "instincts" / "inherited").mkdir(parents=True, exist_ok=True)
    (project_dir / "evolved" / "skills").mkdir(parents=True, exist_ok=True)
    (project_dir / "evolved" / "commands").mkdir(parents=True, exist_ok=True)
    (project_dir / "evolved" / "agents").mkdir(parents=True, exist_ok=True)


def _archive_old_observation_files(project_dir: Path) -> None:
    purge_marker = project_dir / ".last-purge"
    try:
        stale = not purge_marker.exists() or (datetime.now(UTC).timestamp() - purge_marker.stat().st_mtime) > 86400
    except OSError:
        stale = True

    if not stale:
        return

    archive_dir = project_dir / "observations.archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(UTC).timestamp() - (30 * 24 * 60 * 60)
    for path in archive_dir.glob("observations-*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue

    try:
        purge_marker.touch()
    except OSError:
        pass


def _archive_if_too_large(obs_path: Path, project_dir: Path) -> None:
    if not obs_path.exists():
        return

    try:
        if obs_path.stat().st_size < 10 * 1024 * 1024:
            return
    except OSError:
        return

    archive_dir = project_dir / "observations.archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"observations-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.jsonl"
    try:
        obs_path.replace(archive_path)
    except OSError:
        pass


def _append_observation(obs_path: Path, payload: dict) -> None:
    obs_path.parent.mkdir(parents=True, exist_ok=True)
    with obs_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def _parse_input(raw: str) -> dict | None:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {"parsed": False, "error": "hook payload is not a JSON object"}
    except json.JSONDecodeError as error:
        return {"parsed": False, "error": str(error)}


def _build_observation(stdin_data: dict, phase: str, project: dict) -> dict:
    event = "tool_start" if phase == "pre" else "tool_complete"
    tool_name = stdin_data.get("tool_name", stdin_data.get("tool", "unknown"))
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
        "project_id": project["id"],
        "project_name": project["name"],
    }
    if tool_input_str:
        observation["input"] = _scrub_secret_text(tool_input_str)
    if tool_output_str is not None:
        observation["output"] = _scrub_secret_text(tool_output_str)
    return observation


def _start_observer_if_needed(project: dict) -> None:
    pid_files = [
        project["project_dir"] / ".observer.pid",
        _CONFIG_DIR / ".observer.pid",
    ]
    if any(_pid_is_running(path) for path in pid_files):
        return

    env = os.environ.copy()
    env["DEVGEAR_SKIP_OBSERVE"] = "1"
    env.setdefault("CLV2_IS_WINDOWS", "false")
    env["PROJECT_DIR"] = str(project["project_dir"])
    env["PROJECT_ROOT"] = str(project["root"])
    env["PROJECT_NAME"] = str(project["name"])
    env["PROJECT_ID"] = str(project["id"])
    env["OBSERVATIONS_FILE"] = str(project["observations_file"])
    env["INSTINCTS_DIR"] = str(project["instincts_personal"])
    try:
        subprocess.Popen(
            [_resolve_python_cmd(), "-m", "devgear.skills.learn.observer", "start"],
            cwd=str(project["root"]),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
        )
    except OSError:
        return


def _pid_is_running(pid_file: Path) -> bool:
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


def _signal_observers(project: dict) -> None:
    signal_every_n = int(os.environ.get("DEVGEAR_OBSERVER_SIGNAL_EVERY_N", str(_DEFAULT_SIGNAL_EVERY_N)))
    counter_file = project["project_dir"] / ".observer-signal-counter"
    try:
        counter = int(counter_file.read_text(encoding="utf-8").strip()) if counter_file.exists() else 0
    except (OSError, ValueError):
        counter = 0

    counter += 1
    should_signal = False
    if counter >= signal_every_n:
        should_signal = True
        counter = 0

    try:
        counter_file.write_text(str(counter), encoding="utf-8")
    except OSError:
        pass

    if not should_signal or not hasattr(signal, "SIGUSR1"):
        return

    signaled: set[int] = set()
    for pid_file in [project["project_dir"] / ".observer.pid", _CONFIG_DIR / ".observer.pid"]:
        if not pid_file.exists():
            continue
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            try:
                pid_file.unlink()
            except OSError:
                pass
            continue

        if pid in signaled or pid <= 1:
            continue

        try:
            os.kill(pid, 0)
        except OSError:
            try:
                pid_file.unlink()
            except OSError:
                pass
            continue

        try:
            os.kill(pid, signal.SIGUSR1)
            signaled.add(pid)
        except OSError:
            continue


def _write_parse_error(obs_path: Path, raw: str) -> None:
    _append_observation(
        obs_path,
        {
            "timestamp": _now_utc(),
            "event": "parse_error",
            "raw": _scrub_secret_text(raw[:2000]),
        },
    )


def main(argv: list[str] | None = None) -> int:
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
        previous = _set_project_dir_from_cwd(stdin_data)
        try:
            project = detect_project()
            obs_path = project["observations_file"]
            _ensure_project_dirs(Path(project["project_dir"]))
            _write_parse_error(Path(obs_path), raw)
        finally:
            _restore_project_dir(previous)
        return 0

    if _should_skip_automation(stdin_data):
        return 0

    previous = _set_project_dir_from_cwd(stdin_data)
    try:
        project = detect_project()
    finally:
        _restore_project_dir(previous)

    project_dir = Path(project["project_dir"])
    obs_path = Path(project["observations_file"])
    _ensure_project_dirs(project_dir)
    _archive_old_observation_files(project_dir)
    _archive_if_too_large(obs_path, project_dir)

    observation = _build_observation(stdin_data, phase, project)
    _append_observation(obs_path, observation)

    if _is_disabled():
        return 0

    _start_observer_if_needed(project)
    _signal_observers(project)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
