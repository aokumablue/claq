#!/usr/bin/env python3
"""Background observer runtime for learn."""

from __future__ import annotations

import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from deepblue.lib.core_utils import get_deepblue_dir
from deepblue.skills.learn.cli import detect_project

_CONFIG_DIR = get_deepblue_dir()
_PROMPT_PATTERN = re.compile(
    os.environ.get(
        "CLV2_OBSERVER_PROMPT_PATTERN",
        r"Can you confirm|requires permission|Awaiting (user confirmation|confirmation|approval|permission)|confirm I should proceed|once granted access|grant.*access",
    ),
    re.IGNORECASE,
)


def _resolve_python_cmd() -> str:
    """現在の Python 実行ファイルのパスを返す。"""
    return sys.executable or "python3"


def _project_context() -> dict:
    """環境変数または検出結果からプロジェクトのパス情報をまとめて返す。

    Returns:
        プロジェクト ID・名前・各種ディレクトリ（Path）を含む辞書。
    """
    project_dir_env = os.environ.get("PROJECT_DIR")
    project_root_env = os.environ.get("PROJECT_ROOT")
    if project_dir_env and project_root_env:
        project_dir = Path(project_dir_env)
        project_root = Path(project_root_env)
        project_name = os.environ.get("PROJECT_NAME", project_root.name)
        project_id = os.environ.get("PROJECT_ID", project_dir.name)
        observations_file = Path(os.environ.get("OBSERVATIONS_FILE", str(project_dir / "observations.jsonl")))
        instincts_dir = Path(os.environ.get("INSTINCTS_DIR", str(project_dir / "instincts" / "personal")))
        return {
            "id": project_id,
            "name": project_name,
            "root": project_root,
            "project_dir": project_dir,
            "observations_file": observations_file,
            "instincts_personal": instincts_dir,
            "instincts_inherited": project_dir / "instincts" / "inherited",
            "evolved_dir": project_dir / "evolved",
        }

    project = detect_project()
    project["project_dir"] = Path(project["project_dir"])
    project["observations_file"] = Path(project["observations_file"])
    project["instincts_personal"] = Path(project["instincts_personal"])
    project["instincts_inherited"] = Path(project["instincts_inherited"])
    project["evolved_dir"] = Path(project["evolved_dir"])
    project["root"] = Path(project["root"])
    return project


def _pid_file_candidates(project_dir: Path) -> list[Path]:
    """PID ファイルの探索候補パス一覧を返す。"""
    return [project_dir / ".observer.pid", _CONFIG_DIR / ".observer.pid"]


def _is_running(pid_file: Path) -> bool:
    """PID ファイルが示すプロセスが稼働中か判定する。

    無効・終了済みの場合は PID ファイルを削除して False を返す。
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


def _stop_running_observer(pid_file: Path) -> bool:
    """PID ファイルが示すプロセスへ SIGTERM を送り停止する。

    Returns:
        停止シグナルを送れた場合は True、未起動・無効なら False。
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
    except OSError:
        try:
            pid_file.unlink()
        except OSError:
            pass
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False

    try:
        pid_file.unlink()
    except OSError:
        pass
    return True


def _observer_log_path(project_dir: Path) -> Path:
    """observer ログファイルのパスを返す。"""
    return project_dir / "observer.log"


def _sentinel_path(project_dir: Path, project_root: Path) -> Path:
    """ガード用センチネル（ロック）ファイルのパスを返す。"""
    if project_root.exists():
        return project_root / ".observer.lock"
    return project_dir / ".observer.lock"


def _write_guard_sentinel(project_dir: Path, project_root: Path) -> None:
    """確認・許可プロンプト検出時に observer 一時停止を示すセンチネルを書き出す。"""
    sentinel = _sentinel_path(project_dir, project_root)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(
        "observer paused: confirmation or permission prompt detected; rerun `python3 -m deepblue.skills.learn.observer start --reset` after reviewing observer.log\n",
        encoding="utf-8",
    )


def _log_tail(path: Path, start_line: int) -> str:
    """ログファイルの指定行以降を結合した文字列を返す。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[start_line:])


def _print_status(project_dir: Path, pid_file: Path, log_file: Path, instincts_dir: Path, observations_file: Path) -> int:
    """observer の稼働状況・観測数・インスティンクト数を表示する。

    Returns:
        稼働中なら 0、未起動なら 1。
    """
    if _is_running(pid_file):
        pid = pid_file.read_text(encoding="utf-8").strip()
        print(f"Observer is running (PID: {pid})")
        print(f"Log: {log_file}")
        try:
            observation_count = len(observations_file.read_text(encoding="utf-8").splitlines())
        except OSError:
            observation_count = 0
        print(f"Observations: {observation_count} lines")
        instinct_count = sum(
            len(list(instincts_dir.glob(pat))) for pat in ("*.md", "*.yaml", "*.yml")
        ) if instincts_dir.exists() else 0
        print(f"Instincts: {instinct_count}")
        return 0

    if pid_file.exists():
        try:
            pid_file.unlink()
        except OSError:
            pass
    print("Observer not running")
    return 1


def _run_prune() -> None:
    """learn CLI の prune サブコマンドを静かに実行する。"""
    try:
        subprocess.run(
            [_resolve_python_cmd(), "-m", "deepblue.skills.learn.cli", "prune", "--quiet"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return


def _get_idle_seconds() -> int:
    """OS ごとの方法でユーザー操作のアイドル秒数を返す。

    取得できない場合や未対応 OS では 0 を返す。
    """
    system = platform.system()
    if system == "Darwin":
        try:
            result = subprocess.run(
                ["/usr/sbin/ioreg", "-c", "IOHIDSystem"],
                capture_output=True,
                text=True,
                check=False,
            )
            for line in result.stdout.splitlines():
                if "HIDIdleTime" in line:
                    value = int(line.split()[-1])
                    return max(0, value // 1_000_000_000)
        except (OSError, ValueError):
            return 0
        return 0

    if system == "Linux":
        if shutil.which("xprintidle") is None:
            return 0
        try:
            result = subprocess.run(["xprintidle"], capture_output=True, text=True, check=False)
            return max(0, int(result.stdout.strip() or "0") // 1000)
        except (OSError, ValueError):
            return 0

    if system.startswith("MINGW") or system.startswith("MSYS") or system.startswith("CYGWIN"):
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "try { "
                    "Add-Type -MemberDefinition '[DllImport(\"user32.dll\")] public static extern bool GetLastInputInfo(ref LASTINPUTINFO p); [StructLayout(LayoutKind.Sequential)] public struct LASTINPUTINFO { public uint cbSize; public int dwTime; }' -Name WinAPI -Namespace PInvoke; "
                    "$l = New-Object PInvoke.WinAPI+LASTINPUTINFO; $l.cbSize = 8; "
                    "[PInvoke.WinAPI]::GetLastInputInfo([ref]$l) | Out-Null; "
                    "[int][Math]::Max(0, [long]([Environment]::TickCount - [long]$l.dwTime) / 1000) "
                    "} catch { 0 }",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            return max(0, int((result.stdout or "0").strip().replace("\r", "")))
        except (OSError, ValueError):
            return 0

    return 0


def _guardian_allows(project_dir: Path, project_root: Path, log_file: Path) -> bool:
    """アクティブ時間帯・クールダウン・アイドル状態を確認し解析実行可否を判定する。

    Returns:
        解析を実行してよい場合は True、抑止すべき場合は False。
    """
    interval = int(os.environ.get("OBSERVER_INTERVAL_SECONDS", "300"))
    last_run_log = Path(os.environ.get("OBSERVER_LAST_RUN_LOG", str(get_deepblue_dir() / "observer-last-run.log")))
    active_start = int(os.environ.get("OBSERVER_ACTIVE_HOURS_START", "800"))
    active_end = int(os.environ.get("OBSERVER_ACTIVE_HOURS_END", "2300"))
    max_idle = int(os.environ.get("OBSERVER_MAX_IDLE_SECONDS", "1800"))

    if active_start != 0 or active_end != 0:
        current_hhmm = int(time.strftime("%H%M"))
        within_active = False
        if active_start < active_end:
            within_active = active_start <= current_hhmm < active_end
        else:
            within_active = current_hhmm >= active_start or current_hhmm < active_end
        if not within_active:
            _append_log(log_file, f"session-guardian: outside active hours ({current_hhmm}, window {active_start}-{active_end})")
            return False

    if not project_root.exists():
        try:
            project_root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False).stdout.strip() or os.getcwd())
        except OSError:
            project_root = Path(os.getcwd())

    project_name = project_root.name
    now = int(time.time())
    last_run_log.parent.mkdir(parents=True, exist_ok=True)

    try:
        entries = {}
        if last_run_log.exists():
            for line in last_run_log.read_text(encoding="utf-8").splitlines():
                if "\t" not in line:
                    continue
                key, value = line.split("\t", 1)
                entries[key] = value
        last_spawn = int(entries.get(str(project_root), "0") or "0")
    except (OSError, ValueError):
        last_spawn = 0

    elapsed = now - last_spawn
    if elapsed < interval:
        _append_log(log_file, f"session-guardian: cooldown active for '{project_name}' (last spawn {elapsed}s ago, interval {interval}s)")
        return False

    try:
        entries[str(project_root)] = str(now)
        with last_run_log.open("w", encoding="utf-8") as handle:
            for key, value in entries.items():
                handle.write(f"{key}\t{value}\n")
    except OSError:
        pass

    if max_idle > 0:
        idle_seconds = _get_idle_seconds()
        if idle_seconds > max_idle:
            _append_log(log_file, f"session-guardian: user idle {idle_seconds}s (threshold {max_idle}s), skipping")
            return False

    return True


def _append_log(path: Path, message: str) -> None:
    """タイムスタンプ付きメッセージをログファイルへ追記する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{time.strftime('%c')}] {message}\n")


def _analyze_observations(
    project_dir: Path,
    project_root: Path,
    project_name: str,
    project_id: str,
    observations_file: Path,
    instincts_dir: Path,
    log_file: Path,
    min_observations: int,
    analysis_cooldown: int,
) -> None:
    """観測ログを claude CLI に渡してインスティンクト候補を抽出・書き出す。

    閾値・ガード・プラットフォーム条件を満たす場合のみ解析を実行し、
    完了後は観測ファイルをアーカイブする。
    """
    if not observations_file.exists():
        return

    try:
        obs_count = len(observations_file.read_text(encoding="utf-8").splitlines())
    except OSError:
        return

    if obs_count < min_observations:
        return

    _append_log(log_file, f"Analyzing {obs_count} observations for project {project_name}...")

    if os.environ.get("CLV2_IS_WINDOWS", "false") == "true" and os.environ.get("DEEPBLUE_OBSERVER_ALLOW_WINDOWS", "false") != "true":
        _append_log(log_file, "Skipping claude analysis on Windows due to known non-interactive hang issue (#295). Set DEEPBLUE_OBSERVER_ALLOW_WINDOWS=true to override.")
        return

    if shutil.which("claude") is None:
        _append_log(log_file, "claude CLI not found, skipping analysis")
        return

    if not _guardian_allows(project_dir, project_root, log_file):
        _append_log(log_file, "Observer cycle skipped by session-guardian")
        return

    observer_tmp_dir = project_dir / ".observer-tmp"
    observer_tmp_dir.mkdir(parents=True, exist_ok=True)
    analysis_file = observer_tmp_dir / f"deepblue-observer-analysis-{os.getpid()}-{int(time.time())}.jsonl"

    try:
        lines = observations_file.read_text(encoding="utf-8").splitlines()
        recent_lines = lines[-int(os.environ.get("DEEPBLUE_OBSERVER_MAX_ANALYSIS_LINES", "500")) :]
        analysis_file.write_text("\n".join(recent_lines) + ("\n" if recent_lines else ""), encoding="utf-8")
    except OSError:
        return

    analysis_relpath = f".observer-tmp/{analysis_file.name}"
    prompt = (
        "IMPORTANT: You are running in non-interactive --print mode. You MUST use the Write tool directly to create files. "
        "Do NOT ask for permission, do NOT ask for confirmation, do NOT output summaries instead of writing. Just read, analyze, and write.\n\n"
        f"Read {analysis_relpath} and identify patterns for the project {project_name} (user corrections, error resolutions, repeated workflows, tool preferences).\n"
        f"If you find 3+ occurrences of the same pattern, you MUST write an instinct file directly to {instincts_dir}/<id>.md using the Write tool.\n"
        "Do NOT ask for permission to write files, do NOT describe what you would write, and do NOT stop at analysis when a qualifying pattern exists.\n\n"
        "CRITICAL: Every instinct file MUST use this exact format:\n\n"
        "---\n"
        "id: kebab-case-name\n"
        "trigger: when <specific condition>\n"
        "confidence: <0.3-0.85 based on frequency: 3-5 times=0.5, 6-10=0.7, 11+=0.85>\n"
        "domain: <one of: code-style, testing, git, debugging, workflow, file-patterns>\n"
        "source: session-observation\n"
        "scope: project\n"
        f"project_id: {project_id}\n"
        f"project_name: {project_name}\n"
        "---\n\n"
        "# Title\n\n"
        "## Action\n"
        "<what to do, one clear sentence>\n\n"
        "## Evidence\n"
        "- Observed N times in session <id>\n"
        "- Pattern: <description>\n"
        "- Last observed: <date>\n\n"
        "Rules:\n"
        "- Be conservative, only clear patterns with 3+ observations\n"
        "- Use narrow, specific triggers\n"
        "- Never include actual code snippets, only describe patterns\n"
        "- When a qualifying pattern exists, write or update the instinct file in this run instead of asking for confirmation\n"
        f"- If a similar instinct already exists in {instincts_dir}/, update it instead of creating a duplicate\n"
        "- The YAML frontmatter (between --- markers) with id field is MANDATORY\n"
        "- If a pattern seems universal (not project-specific), set scope to global instead of project\n"
        "- Examples of global patterns: always validate user input, prefer explicit error handling\n"
        "- Examples of project patterns: use React functional components, follow Django REST framework conventions\n"
    )

    timeout_seconds = int(os.environ.get("DEEPBLUE_OBSERVER_TIMEOUT_SECONDS", "120"))
    max_turns = int(os.environ.get("DEEPBLUE_OBSERVER_MAX_TURNS", "10"))
    if max_turns < 4:
        max_turns = 10

    env = os.environ.copy()
    env["DEEPBLUE_SKIP_OBSERVE"] = "1"
    try:
        result = subprocess.run(
            [
                "claude",
                "--model",
                "haiku",
                "--max-turns",
                str(max_turns),
                "--print",
                "--allowedTools",
                "Read,Write",
                "-p",
                prompt,
            ],
            text=True,
            capture_output=True,
            env=env,
            cwd=str(project_dir),
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _append_log(log_file, f"Claude analysis timed out after {timeout_seconds}s; terminating process")
        return
    except OSError as error:
        _append_log(log_file, f"Claude analysis failed to start: {error}")
        return

    if result.stdout:
        _append_log(log_file, result.stdout.strip())
    if result.stderr:
        _append_log(log_file, result.stderr.strip())
    if result.returncode != 0:
        _append_log(log_file, f"Claude analysis failed (exit {result.returncode})")

    try:
        analysis_file.unlink()
    except OSError:
        pass

    if observations_file.exists():
        archive_dir = project_dir / "observations.archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"processed-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.jsonl"
        try:
            observations_file.replace(archive_path)
        except OSError:
            pass


def _loop_once(
    project_dir: Path,
    project_root: Path,
    project_name: str,
    project_id: str,
    observations_file: Path,
    instincts_dir: Path,
    log_file: Path,
    min_observations: int,
    analysis_cooldown: int,
    wake_event: threading.Event,
    state: dict,
) -> None:
    """1 サイクル分の解析を実行する。

    解析中フラグやクールダウンを確認し、条件を満たす場合のみ
    _analyze_observations を呼び出して状態を更新する。
    """
    if state.get("analyzing"):
        _append_log(log_file, "Analysis already in progress, skipping signal")
        return

    now_epoch = int(time.time())
    elapsed = now_epoch - int(state.get("last_analysis_epoch", 0))
    if elapsed < analysis_cooldown:
        _append_log(log_file, f"Analysis cooldown active ({elapsed}s < {analysis_cooldown}s), skipping")
        return

    state["analyzing"] = True
    try:
        _analyze_observations(
            project_dir,
            project_root,
            project_name,
            project_id,
            observations_file,
            instincts_dir,
            log_file,
            min_observations,
            analysis_cooldown,
        )
        state["last_analysis_epoch"] = int(time.time())
    finally:
        state["analyzing"] = False


def _run_loop(project_dir: Path, project_root: Path, log_file: Path, pid_file: Path, observations_file: Path, instincts_dir: Path, project_name: str, project_id: str, min_observations: int, interval_seconds: int) -> int:
    """observer のメインループ。一定間隔または SIGUSR1 受信で解析を回す。"""
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    _append_log(log_file, f"Observer started for {project_name} (PID: {os.getpid()})")
    _run_prune()

    wake_event = threading.Event()
    state: dict[str, int | bool] = {"analyzing": False, "last_analysis_epoch": 0}

    def _on_usr1(signum, frame):  # noqa: ANN001, ARG001
        """SIGUSR1 ハンドラ。ループの早期起床を要求する。"""
        wake_event.set()
        state["usr1_fired"] = True

    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, _on_usr1)

    while True:
        wake_event.wait(interval_seconds)
        usr1_fired = bool(state.pop("usr1_fired", False))
        wake_event.clear()
        if usr1_fired:
            continue
        _loop_once(
            project_dir,
            project_root,
            project_name,
            project_id,
            observations_file,
            instincts_dir,
            log_file,
            min_observations,
            interval_seconds,
            wake_event,
            state,
        )


def _start_observer(project: dict, reset: bool) -> int:
    """observer プロセスをバックグラウンド起動する。

    既存稼働の確認、起動直後のプロンプト検出によるフェイルクローズを行う。

    Returns:
        終了コード（0=起動/既存稼働、1=起動失敗、2=プロンプト検出による中止）。
    """
    project_dir = Path(project["project_dir"])
    pid_file = project_dir / ".observer.pid"
    log_file = _observer_log_path(project_dir)
    instincts_dir = Path(project["instincts_personal"])
    min_observations = int(os.environ.get("MIN_OBSERVATIONS", "20"))
    interval_seconds = os.environ.get("OBSERVER_INTERVAL_SECONDS", "300")
    project_dir.mkdir(parents=True, exist_ok=True)
    log_file.touch(exist_ok=True)

    if reset:
        sentinel = _sentinel_path(project_dir, Path(project["root"]))
        try:
            sentinel.unlink()
        except OSError:
            pass

    for candidate in _pid_file_candidates(project_dir):
        if _is_running(candidate):
            pid = candidate.read_text(encoding="utf-8").strip()
            print(f"Observer already running for {project['name']} (PID: {pid})")
            return 0

    print(f"Starting observer agent for {project['name']}...")
    start_line = len(log_file.read_text(encoding="utf-8").splitlines()) if log_file.exists() else 0

    env = os.environ.copy()
    env.update(
        {
            "CONFIG_DIR": str(_CONFIG_DIR),
            "PID_FILE": str(pid_file),
            "LOG_FILE": str(log_file),
            "OBSERVATIONS_FILE": str(project["observations_file"]),
            "INSTINCTS_DIR": str(instincts_dir),
            "PROJECT_DIR": str(project_dir),
            "PROJECT_ROOT": str(project["root"]),
            "PROJECT_NAME": project["name"],
            "PROJECT_ID": project["id"],
            "MIN_OBSERVATIONS": str(min_observations),
            "OBSERVER_INTERVAL_SECONDS": interval_seconds,
            "CLV2_IS_WINDOWS": str(platform.system().startswith(("MINGW", "MSYS", "CYGWIN"))).lower(),
        }
    )

    try:
        with log_file.open("a", encoding="utf-8") as log_handle:
            proc_kwargs: dict[str, object] = {"cwd": str(project_dir), "env": env, "stdout": log_handle, "stderr": subprocess.STDOUT}
            if os.name == "nt":
                proc_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                proc_kwargs["start_new_session"] = True
            subprocess.Popen([_resolve_python_cmd(), "-m", "deepblue.skills.learn.observer", "loop"], **proc_kwargs)
    except OSError as error:
        print(f"Failed to start observer: {error}")
        return 1

    time.sleep(2)
    if _PROMPT_PATTERN.search(_log_tail(log_file, start_line)):
        print("OBSERVER_ABORT: Confirmation or permission prompt detected in observer output. Failing closed.")
        for path in _pid_file_candidates(project_dir):
            _stop_running_observer(path)
        _write_guard_sentinel(project_dir, Path(project["root"]))
        return 2

    if _is_running(pid_file):
        pid = pid_file.read_text(encoding="utf-8").strip()
        print(f"Observer started (PID: {pid})")
        print(f"Log: {log_file}")
        return 0

    print(f"Failed to start observer (process died immediately, check {log_file})")
    return 1


def _stop_observer(project: dict) -> int:
    """対象プロジェクトの observer を停止する。

    Returns:
        停止できた場合は 0、未起動なら 1。
    """
    project_dir = Path(project["project_dir"])
    pid_file = project_dir / ".observer.pid"
    if _stop_running_observer(pid_file):
        print(f"Stopping observer for {project['name']} (PID file: {pid_file})...")
        print("Observer stopped.")
        return 0

    print("Observer not running.")
    return 1


def main(argv: list[str] | None = None) -> int:
    """CLI エントリポイント。start/stop/status/loop を解釈して実行する。

    Returns:
        各サブコマンドの終了コード。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    action = "start"
    reset = False

    for arg in args:
        if arg in {"start", "stop", "status", "loop"}:
            action = arg
        elif arg == "--reset":
            reset = True
        else:
            print(f"Usage: {Path(sys.argv[0]).name} [start|stop|status] [--reset]")
            return 1

    project = _project_context()
    project_dir = Path(project["project_dir"])
    pid_file = project_dir / ".observer.pid"
    log_file = _observer_log_path(project_dir)
    instincts_dir = Path(project["instincts_personal"])
    interval_seconds = int(os.environ.get("OBSERVER_INTERVAL_SECONDS", "300"))
    min_observations = int(os.environ.get("MIN_OBSERVATIONS", "20"))

    print(f"Project: {project['name']} ({project['id']})")
    print(f"Storage: {project_dir}")

    if reset:
        try:
            _sentinel_path(project_dir, Path(project["root"])).unlink()
        except OSError:
            pass

    if action == "stop":
        return _stop_observer(project)

    if action == "status":
        return _print_status(project_dir, pid_file, log_file, instincts_dir, Path(project["observations_file"]))

    if action == "loop":
        project_dir.mkdir(parents=True, exist_ok=True)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        observations_file = Path(project["observations_file"])
        return _run_loop(
            project_dir,
            Path(project["root"]),
            log_file,
            pid_file,
            observations_file,
            instincts_dir,
            str(project["name"]),
            str(project["id"]),
            min_observations,
            interval_seconds,
        )

    return _start_observer(project, reset)


if __name__ == "__main__":
    raise SystemExit(main())
