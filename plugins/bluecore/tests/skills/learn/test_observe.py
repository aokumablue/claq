"""observe の観測フックランタイムを検証するテスト。

対象: _now_utc / _read_raw_stdin / _resolve_python_cmd / _is_disabled /
_should_skip_automation / _set_project_dir_from_cwd / _restore_project_dir /
_scrub_secret_text / _ensure_project_dirs / _archive_old_observation_files /
_archive_if_too_large / _append_observation / _parse_input / _build_observation /
_start_observer_if_needed / _pid_is_running / _should_signal_now /
_send_sigusr1_to_pid_file / _signal_observers / _write_parse_error /
_handle_parse_error / _record_and_signal / main

subprocess・os.kill・signal・detect_project を全モック、Path I/O は tmp_path、
モジュール定数 _CONFIG_DIR は monkeypatch で差し替える。
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from bluecore.skills.learn import observe


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """_CONFIG_DIR を tmp 配下に差し替えて返す。"""
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setattr(observe, "_CONFIG_DIR", cfg)
    return cfg


@pytest.fixture
def project(tmp_path: Path) -> dict:
    """detect_project 戻り値を模したプロジェクト辞書を返す。"""
    pdir = tmp_path / "proj" / ".claude-bluecore"
    pdir.mkdir(parents=True)
    return {
        "project_dir": pdir,
        "root": tmp_path / "proj",
        "name": "proj",
        "id": "proj-id",
        "observations_file": pdir / "observations.jsonl",
        "instincts_personal": pdir / "instincts" / "personal",
    }


# --- 単純ヘルパー ------------------------------------------------------------


def test_now_utc_z_suffix() -> None:
    """UTC ISO8601 を Z 終端で返す。"""
    assert observe._now_utc().endswith("Z")


def test_read_raw_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """標準入力を UTF-8 復号して返す。"""
    monkeypatch.setattr(observe.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"hi")))
    assert observe._read_raw_stdin() == "hi"


def test_resolve_python_cmd_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    """sys.executable があればそれを返す。"""
    monkeypatch.setattr(observe.sys, "executable", "/usr/bin/python3")
    assert observe._resolve_python_cmd() == "/usr/bin/python3"


def test_resolve_python_cmd_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """sys.executable が空なら python3 を返す。"""
    monkeypatch.setattr(observe.sys, "executable", "")
    assert observe._resolve_python_cmd() == "python3"


# --- _is_disabled ------------------------------------------------------------


def test_is_disabled_config_marker(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """設定ディレクトリに disabled があれば True。"""
    monkeypatch.delenv("CLV2_CONFIG", raising=False)
    (config_dir / "disabled").touch()
    assert observe._is_disabled() is True


def test_is_disabled_clv2_marker(config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLV2_CONFIG の隣に disabled があれば True。"""
    clv2 = tmp_path / "clv2" / "config.json"
    clv2.parent.mkdir()
    (clv2.parent / "disabled").touch()
    monkeypatch.setenv("CLV2_CONFIG", str(clv2))
    assert observe._is_disabled() is True


def test_is_disabled_false(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """マーカーが無ければ False。"""
    monkeypatch.delenv("CLV2_CONFIG", raising=False)
    assert observe._is_disabled() is False


# --- _should_skip_automation -------------------------------------------------


def test_skip_non_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """対象外エントリポイントはスキップ。"""
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "vscode")
    assert observe._should_skip_automation({}) is True


def test_skip_env_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """BLUECORE_SKIP_OBSERVE=1 はスキップ。"""
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("BLUECORE_SKIP_OBSERVE", "1")
    assert observe._should_skip_automation({}) is True


def test_skip_agent_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """agent_id 付きはサブエージェント実行としてスキップ。"""
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("BLUECORE_SKIP_OBSERVE", "0")
    assert observe._should_skip_automation({"agent_id": "a1"}) is True


def test_skip_cwd_pattern(monkeypatch: pytest.MonkeyPatch) -> None:
    """cwd がスキップ対象パスを含めばスキップ。"""
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("BLUECORE_SKIP_OBSERVE", "0")
    assert observe._should_skip_automation({"cwd": "/home/x/observer-sessions/y"}) is True


def test_skip_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """該当しなければスキップしない。"""
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("BLUECORE_SKIP_OBSERVE", "0")
    assert observe._should_skip_automation({"cwd": "/home/x/project"}) is False


def test_skip_empty_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    """cwd が空ならパターン走査をせずスキップしない。"""
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("BLUECORE_SKIP_OBSERVE", "0")
    assert observe._should_skip_automation({"cwd": ""}) is False


# --- _set_project_dir_from_cwd / _restore_project_dir ------------------------


def test_set_project_dir_invalid_cwd() -> None:
    """cwd が無効なら None を返す。"""
    assert observe._set_project_dir_from_cwd({"cwd": ""}) is None


def test_set_project_dir_git_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """git rev-parse 成功でトップレベルを設定する。"""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    completed = SimpleNamespace(stdout=str(tmp_path) + "\n", returncode=0)
    with mock.patch.object(observe.subprocess, "run", return_value=completed):
        prev = observe._set_project_dir_from_cwd({"cwd": str(tmp_path)})
    assert prev is None
    assert observe.os.environ["CLAUDE_PROJECT_DIR"] == str(tmp_path)
    observe.os.environ.pop("CLAUDE_PROJECT_DIR", None)


def test_set_project_dir_git_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """git rc 非0 なら cwd をそのまま使う。"""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/old")
    completed = SimpleNamespace(stdout="", returncode=1)
    with mock.patch.object(observe.subprocess, "run", return_value=completed):
        prev = observe._set_project_dir_from_cwd({"cwd": str(tmp_path)})
    assert prev == "/old"
    assert observe.os.environ["CLAUDE_PROJECT_DIR"] == str(tmp_path)


def test_set_project_dir_git_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """git が見つからない/タイムアウトなら cwd を使う。"""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    with mock.patch.object(observe.subprocess, "run", side_effect=FileNotFoundError):
        observe._set_project_dir_from_cwd({"cwd": str(tmp_path)})
    assert observe.os.environ["CLAUDE_PROJECT_DIR"] == str(tmp_path)
    observe.os.environ.pop("CLAUDE_PROJECT_DIR", None)


def test_restore_project_dir_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """previous が None なら環境変数を削除する。"""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/x")
    observe._restore_project_dir(None)
    assert "CLAUDE_PROJECT_DIR" not in observe.os.environ


def test_restore_project_dir_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """previous があればその値に戻す。"""
    observe._restore_project_dir("/prev")
    assert observe.os.environ["CLAUDE_PROJECT_DIR"] == "/prev"
    observe.os.environ.pop("CLAUDE_PROJECT_DIR", None)


# --- _scrub_secret_text ------------------------------------------------------


def test_scrub_none() -> None:
    """None はそのまま None。"""
    assert observe._scrub_secret_text(None) is None


def test_scrub_redacts_simple() -> None:
    """prefix 無しのシークレットを [REDACTED] に置換する。"""
    assert "[REDACTED]" in observe._scrub_secret_text("token=abcdefgh1234")
    assert "abcdefgh1234" not in observe._scrub_secret_text("token=abcdefgh1234")


def test_scrub_redacts_with_prefix() -> None:
    """Bearer 等の prefix 付きでも置換する。"""
    out = observe._scrub_secret_text("authorization: Bearer abcdefgh1234")
    assert "[REDACTED]" in out
    assert "Bearer" in out


def test_scrub_no_secret() -> None:
    """シークレットが無ければ原文を返す。"""
    assert observe._scrub_secret_text("hello world") == "hello world"


# --- _ensure_project_dirs / _append_observation ------------------------------


def test_ensure_project_dirs(tmp_path: Path) -> None:
    """必要な保存先ディレクトリ群を作成する。"""
    observe._ensure_project_dirs(tmp_path)
    assert (tmp_path / "observations.archive").is_dir()
    assert (tmp_path / "instincts" / "personal").is_dir()
    assert (tmp_path / "evolved" / "agents").is_dir()


def test_append_observation(tmp_path: Path) -> None:
    """観測を JSONL 1 行として追記する。"""
    obs = tmp_path / "sub" / "observations.jsonl"
    observe._append_observation(obs, {"a": 1})
    assert json.loads(obs.read_text(encoding="utf-8").strip()) == {"a": 1}


# --- _archive_old_observation_files ------------------------------------------


def test_archive_old_no_marker(tmp_path: Path) -> None:
    """purge marker 無し→stale 判定で古いファイルを削除し marker を作る。"""
    archive = tmp_path / "observations.archive"
    archive.mkdir()
    old = archive / "observations-old.jsonl"
    old.touch()
    import os as _os

    _os.utime(old, (0, 0))  # 1970 年→cutoff より古い
    new = archive / "observations-new.jsonl"
    new.touch()
    observe._archive_old_observation_files(tmp_path)
    assert not old.exists()
    assert new.exists()
    assert (tmp_path / ".last-purge").exists()


def test_archive_old_not_stale(tmp_path: Path) -> None:
    """新しい purge marker があれば何もしない。"""
    marker = tmp_path / ".last-purge"
    marker.touch()
    archive = tmp_path / "observations.archive"
    archive.mkdir()
    old = archive / "observations-old.jsonl"
    old.touch()
    import os as _os

    _os.utime(old, (0, 0))
    observe._archive_old_observation_files(tmp_path)
    assert old.exists()  # not stale なので削除されない


def test_archive_old_stat_oserror(tmp_path: Path) -> None:
    """marker の stat が OSError なら stale 扱いで処理を進める。"""
    marker = tmp_path / ".last-purge"
    marker.touch()
    with mock.patch.object(Path, "stat", side_effect=OSError):
        # stale=True 経路。glob 対象なしでも touch 失敗を握りつぶし完走する
        observe._archive_old_observation_files(tmp_path)


def test_archive_old_unlink_oserror(tmp_path: Path) -> None:
    """古いファイルの unlink が OSError でも continue する。"""
    archive = tmp_path / "observations.archive"
    archive.mkdir()
    old = archive / "observations-old.jsonl"
    old.touch()
    import os as _os

    _os.utime(old, (0, 0))
    with mock.patch.object(Path, "unlink", side_effect=OSError):
        observe._archive_old_observation_files(tmp_path)


def test_archive_old_touch_oserror(tmp_path: Path) -> None:
    """marker の touch が OSError でも握りつぶす。"""
    archive = tmp_path / "observations.archive"
    archive.mkdir()
    with mock.patch.object(Path, "touch", side_effect=OSError):
        observe._archive_old_observation_files(tmp_path)


# --- _archive_if_too_large ---------------------------------------------------


def test_archive_large_not_exists(tmp_path: Path) -> None:
    """観測ファイルが無ければ何もしない。"""
    observe._archive_if_too_large(tmp_path / "nope.jsonl", tmp_path)


def test_archive_large_small(tmp_path: Path) -> None:
    """10MB 未満なら退避しない。"""
    obs = tmp_path / "observations.jsonl"
    obs.write_text("small", encoding="utf-8")
    observe._archive_if_too_large(obs, tmp_path)
    assert obs.exists()


def test_archive_large_stat_oserror(tmp_path: Path) -> None:
    """stat が OSError なら return する。"""
    obs = tmp_path / "observations.jsonl"
    obs.touch()
    with mock.patch.object(Path, "stat", side_effect=OSError):
        observe._archive_if_too_large(obs, tmp_path)
    assert obs.exists()


def test_archive_large_replaces(tmp_path: Path) -> None:
    """10MB 超ならアーカイブへ退避する。"""
    obs = tmp_path / "observations.jsonl"
    obs.touch()
    big = SimpleNamespace(st_size=11 * 1024 * 1024)
    with mock.patch.object(Path, "stat", return_value=big):
        observe._archive_if_too_large(obs, tmp_path)
    assert not obs.exists()
    assert list((tmp_path / "observations.archive").glob("observations-*.jsonl"))


def test_archive_large_replace_oserror(tmp_path: Path) -> None:
    """replace が OSError でも握りつぶす。"""
    obs = tmp_path / "observations.jsonl"
    obs.touch()
    big = SimpleNamespace(st_size=11 * 1024 * 1024)
    with (
        mock.patch.object(Path, "stat", return_value=big),
        mock.patch.object(Path, "replace", side_effect=OSError),
    ):
        observe._archive_if_too_large(obs, tmp_path)


# --- _parse_input ------------------------------------------------------------


def test_parse_input_dict() -> None:
    """JSON オブジェクトは dict を返す。"""
    assert observe._parse_input('{"a": 1}') == {"a": 1}


def test_parse_input_non_object() -> None:
    """JSON だが非オブジェクトはエラー dict を返す。"""
    assert observe._parse_input("[1, 2]")["parsed"] is False


def test_parse_input_invalid() -> None:
    """解析失敗はエラー dict を返す。"""
    assert observe._parse_input("{ broken")["parsed"] is False


# --- _build_observation ------------------------------------------------------


def test_build_observation_dict_io(project: dict) -> None:
    """dict 入出力は JSON 化して格納する。"""
    stdin_data = {"tool_name": "Bash", "tool_input": {"cmd": "ls"}, "tool_response": {"out": "x"}}
    obs = observe._build_observation(stdin_data, "pre", project)
    assert obs["event"] == "tool_start"
    assert obs["tool"] == "Bash"
    assert '"cmd"' in obs["input"]
    assert '"out"' in obs["output"]


def test_build_observation_str_io_and_none_response(project: dict) -> None:
    """tool_response 無し→tool_output へフォールバック、str 入出力経路。"""
    stdin_data = {"tool": "Edit", "input": "abc", "output": "result"}
    obs = observe._build_observation(stdin_data, "post", project)
    assert obs["event"] == "tool_complete"
    assert obs["input"] == "abc"
    assert obs["output"] == "result"


def test_build_observation_empty_input_omitted(project: dict) -> None:
    """tool_input が空文字なら input キーを付けない。"""
    stdin_data = {"tool_input": "", "tool_response": "x"}
    obs = observe._build_observation(stdin_data, "pre", project)
    assert "input" not in obs


# --- _pid_is_running ---------------------------------------------------------


def test_pid_running_not_exists(tmp_path: Path) -> None:
    """PID ファイルが無ければ False。"""
    assert observe._pid_is_running(tmp_path / "nope.pid") is False


def test_pid_running_bad_content(tmp_path: Path) -> None:
    """不正な内容なら unlink して False。"""
    pid = tmp_path / "p.pid"
    pid.write_text("notapid", encoding="utf-8")
    assert observe._pid_is_running(pid) is False
    assert not pid.exists()


def test_pid_running_too_small(tmp_path: Path) -> None:
    """pid<=1 なら unlink して False。"""
    pid = tmp_path / "p.pid"
    pid.write_text("1", encoding="utf-8")
    assert observe._pid_is_running(pid) is False
    assert not pid.exists()


def test_pid_running_alive(tmp_path: Path) -> None:
    """os.kill 成功なら True。"""
    pid = tmp_path / "p.pid"
    pid.write_text("4242", encoding="utf-8")
    with mock.patch.object(observe.os, "kill", return_value=None):
        assert observe._pid_is_running(pid) is True


def test_pid_running_dead(tmp_path: Path) -> None:
    """os.kill が OSError なら unlink して False。"""
    pid = tmp_path / "p.pid"
    pid.write_text("4242", encoding="utf-8")
    with mock.patch.object(observe.os, "kill", side_effect=OSError):
        assert observe._pid_is_running(pid) is False
    assert not pid.exists()


def test_pid_running_unlink_oserror(tmp_path: Path) -> None:
    """不正内容かつ unlink が OSError でも握りつぶして False。"""
    pid = tmp_path / "p.pid"
    pid.write_text("notapid", encoding="utf-8")
    with mock.patch.object(Path, "unlink", side_effect=OSError):
        assert observe._pid_is_running(pid) is False


def test_pid_running_too_small_unlink_oserror(tmp_path: Path) -> None:
    """pid<=1 かつ unlink が OSError でも握りつぶして False。"""
    pid = tmp_path / "p.pid"
    pid.write_text("1", encoding="utf-8")
    with mock.patch.object(Path, "unlink", side_effect=OSError):
        assert observe._pid_is_running(pid) is False


def test_pid_running_dead_unlink_oserror(tmp_path: Path) -> None:
    """死活確認失敗かつ unlink が OSError でも握りつぶして False。"""
    pid = tmp_path / "p.pid"
    pid.write_text("4242", encoding="utf-8")
    with (
        mock.patch.object(observe.os, "kill", side_effect=OSError),
        mock.patch.object(Path, "unlink", side_effect=OSError),
    ):
        assert observe._pid_is_running(pid) is False


# --- _should_signal_now ------------------------------------------------------


def test_should_signal_increment(project: dict) -> None:
    """カウンタが閾値未満なら False で書き戻す。"""
    assert observe._should_signal_now(project, 20) is False
    counter = (project["project_dir"] / ".observer-signal-counter").read_text(encoding="utf-8")
    assert counter == "1"


def test_should_signal_threshold(project: dict) -> None:
    """カウンタが閾値到達で True、カウンタはリセットされる。"""
    (project["project_dir"] / ".observer-signal-counter").write_text("19", encoding="utf-8")
    assert observe._should_signal_now(project, 20) is True
    assert (project["project_dir"] / ".observer-signal-counter").read_text(encoding="utf-8") == "0"


def test_should_signal_bad_counter(project: dict) -> None:
    """カウンタが不正なら 0 から数え直す。"""
    (project["project_dir"] / ".observer-signal-counter").write_text("xx", encoding="utf-8")
    assert observe._should_signal_now(project, 20) is False


def test_should_signal_write_oserror(project: dict) -> None:
    """書き戻しが OSError でも結果は返る。"""
    with mock.patch.object(Path, "write_text", side_effect=OSError):
        assert observe._should_signal_now(project, 20) is False


# --- _send_sigusr1_to_pid_file -----------------------------------------------


def test_send_sigusr1_not_exists(tmp_path: Path) -> None:
    """PID ファイルが無ければ何もしない。"""
    observe._send_sigusr1_to_pid_file(tmp_path / "nope.pid", set())


def test_send_sigusr1_bad_content(tmp_path: Path) -> None:
    """不正内容なら unlink して return。"""
    pid = tmp_path / "p.pid"
    pid.write_text("bad", encoding="utf-8")
    observe._send_sigusr1_to_pid_file(pid, set())
    assert not pid.exists()


def test_send_sigusr1_bad_unlink_oserror(tmp_path: Path) -> None:
    """不正内容かつ unlink が OSError でも握りつぶして return。"""
    pid = tmp_path / "p.pid"
    pid.write_text("bad", encoding="utf-8")
    with mock.patch.object(Path, "unlink", side_effect=OSError):
        observe._send_sigusr1_to_pid_file(pid, set())


def test_send_sigusr1_dead_unlink_oserror(tmp_path: Path) -> None:
    """死活確認失敗かつ unlink が OSError でも握りつぶして return。"""
    pid = tmp_path / "p.pid"
    pid.write_text("4242", encoding="utf-8")
    with (
        mock.patch.object(observe.os, "kill", side_effect=OSError),
        mock.patch.object(Path, "unlink", side_effect=OSError),
    ):
        observe._send_sigusr1_to_pid_file(pid, set())


def test_send_sigusr1_already_signaled(tmp_path: Path) -> None:
    """既にシグナル済み/pid<=1 なら return。"""
    pid = tmp_path / "p.pid"
    pid.write_text("4242", encoding="utf-8")
    observe._send_sigusr1_to_pid_file(pid, {4242})
    assert pid.exists()  # unlink されない


def test_send_sigusr1_dead(tmp_path: Path) -> None:
    """生存確認 os.kill(pid,0) が OSError なら unlink して return。"""
    pid = tmp_path / "p.pid"
    pid.write_text("4242", encoding="utf-8")
    with mock.patch.object(observe.os, "kill", side_effect=OSError):
        observe._send_sigusr1_to_pid_file(pid, set())
    assert not pid.exists()


def test_send_sigusr1_success(tmp_path: Path) -> None:
    """生存していれば SIGUSR1 を送り signaled に追加する。"""
    pid = tmp_path / "p.pid"
    pid.write_text("4242", encoding="utf-8")
    signaled: set[int] = set()
    with mock.patch.object(observe.os, "kill", return_value=None):
        observe._send_sigusr1_to_pid_file(pid, signaled)
    assert 4242 in signaled


def test_send_sigusr1_send_oserror(tmp_path: Path) -> None:
    """SIGUSR1 送出が OSError でも握りつぶす。"""
    pid = tmp_path / "p.pid"
    pid.write_text("4242", encoding="utf-8")
    # kill(pid,0) は成功、kill(pid,SIGUSR1) は失敗
    calls = {"n": 0}

    def fake_kill(_pid: int, sig: int) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError

    with mock.patch.object(observe.os, "kill", side_effect=fake_kill):
        observe._send_sigusr1_to_pid_file(pid, set())


# --- _signal_observers -------------------------------------------------------


def test_signal_observers_not_now(project: dict, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """シグナルタイミングでなければ何もしない。"""
    monkeypatch.setattr(observe, "_should_signal_now", lambda *a: False)
    observe._signal_observers(project)


def test_signal_observers_dispatches(project: dict, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """タイミング到達時に各 PID ファイルへ送出処理を呼ぶ。"""
    monkeypatch.setattr(observe, "_should_signal_now", lambda *a: True)
    sent = []
    monkeypatch.setattr(observe, "_send_sigusr1_to_pid_file", lambda p, s: sent.append(p))
    observe._signal_observers(project)
    assert len(sent) == 2


# --- _start_observer_if_needed -----------------------------------------------


def test_start_observer_already_running(project: dict, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """稼働中なら起動しない。"""
    monkeypatch.setattr(observe, "_pid_is_running", lambda p: True)
    with mock.patch.object(observe.subprocess, "Popen") as popen:
        observe._start_observer_if_needed(project)
        popen.assert_not_called()


def test_start_observer_spawns_posix(project: dict, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未稼働なら子プロセスを起動する（posix 経路）。"""
    monkeypatch.setattr(observe, "_pid_is_running", lambda p: False)
    monkeypatch.setattr(observe.os, "name", "posix")
    with mock.patch.object(observe.subprocess, "Popen") as popen:
        observe._start_observer_if_needed(project)
        popen.assert_called_once()


def test_start_observer_spawns_windows(project: dict, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """nt 経路では creationflags を使う。"""
    monkeypatch.setattr(observe, "_pid_is_running", lambda p: False)
    monkeypatch.setattr(observe.os, "name", "nt")
    with mock.patch.object(observe.subprocess, "Popen") as popen:
        observe._start_observer_if_needed(project)
        popen.assert_called_once()


def test_start_observer_oserror(project: dict, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Popen が OSError でも握りつぶす。"""
    monkeypatch.setattr(observe, "_pid_is_running", lambda p: False)
    monkeypatch.setattr(observe.os, "name", "posix")
    with mock.patch.object(observe.subprocess, "Popen", side_effect=OSError):
        observe._start_observer_if_needed(project)


# --- _write_parse_error / _handle_parse_error --------------------------------


def test_write_parse_error(tmp_path: Path) -> None:
    """parse_error イベントを記録する。"""
    obs = tmp_path / "observations.jsonl"
    observe._write_parse_error(obs, "raw text")
    record = json.loads(obs.read_text(encoding="utf-8").strip())
    assert record["event"] == "parse_error"


def test_handle_parse_error(project: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """プロジェクト検出してエラーを記録し、CLAUDE_PROJECT_DIR を復元する。"""
    monkeypatch.setattr(observe, "_set_project_dir_from_cwd", lambda d: None)
    monkeypatch.setattr(observe, "detect_project", lambda: project)
    observe._handle_parse_error({"cwd": "/x"}, "bad raw")
    assert json.loads(project["observations_file"].read_text(encoding="utf-8").strip())["event"] == "parse_error"


# --- _record_and_signal ------------------------------------------------------


def test_record_and_signal_enabled(project: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """無効化されていなければ観測記録後にオブザーバー起動とシグナルを行う。"""
    monkeypatch.setattr(observe, "_set_project_dir_from_cwd", lambda d: None)
    monkeypatch.setattr(observe, "detect_project", lambda: project)
    monkeypatch.setattr(observe, "_is_disabled", lambda: False)
    started, signaled = [], []
    monkeypatch.setattr(observe, "_start_observer_if_needed", lambda p: started.append(p))
    monkeypatch.setattr(observe, "_signal_observers", lambda p: signaled.append(p))
    observe._record_and_signal({"tool_name": "Bash", "tool_input": "ls"}, "post")
    assert project["observations_file"].exists()
    assert started and signaled


def test_record_and_signal_disabled(project: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """無効化時は記録のみで起動・シグナルしない。"""
    monkeypatch.setattr(observe, "_set_project_dir_from_cwd", lambda d: None)
    monkeypatch.setattr(observe, "detect_project", lambda: project)
    monkeypatch.setattr(observe, "_is_disabled", lambda: True)
    started = []
    monkeypatch.setattr(observe, "_start_observer_if_needed", lambda p: started.append(p))
    observe._record_and_signal({"tool_name": "Bash", "tool_input": "ls"}, "post")
    assert not started


# --- main --------------------------------------------------------------------


def test_main_empty_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """空入力なら 0 を返す。"""
    monkeypatch.setattr(observe, "_read_raw_stdin", lambda: "")
    assert observe.main([]) == 0


def test_main_parse_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """_parse_input が None を返すケースで 0 を返す。"""
    monkeypatch.setattr(observe, "_read_raw_stdin", lambda: "x")
    monkeypatch.setattr(observe, "_parse_input", lambda raw: None)
    assert observe.main([]) == 0


def test_main_parse_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """解析エラー dict なら parse_error 処理へ。"""
    monkeypatch.setattr(observe, "_read_raw_stdin", lambda: "{ broken")
    handled = []
    monkeypatch.setattr(observe, "_handle_parse_error", lambda d, r: handled.append(r))
    assert observe.main(["pre"]) == 0
    assert handled


def test_main_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    """スキップ判定なら記録せず 0 を返す。"""
    monkeypatch.setattr(observe, "_read_raw_stdin", lambda: '{"tool_name": "Bash"}')
    monkeypatch.setattr(observe, "_should_skip_automation", lambda d: True)
    recorded = []
    monkeypatch.setattr(observe, "_record_and_signal", lambda d, p: recorded.append(d))
    assert observe.main([]) == 0
    assert not recorded


def test_main_records(monkeypatch: pytest.MonkeyPatch) -> None:
    """通常入力は HOOK_PHASE を尊重して記録する。"""
    monkeypatch.setenv("HOOK_PHASE", "post")
    monkeypatch.setattr(observe, "_read_raw_stdin", lambda: '{"tool_name": "Bash"}')
    monkeypatch.setattr(observe, "_should_skip_automation", lambda d: False)
    recorded = []
    monkeypatch.setattr(observe, "_record_and_signal", lambda d, p: recorded.append((d, p)))
    assert observe.main([]) == 0
    assert recorded and recorded[0][1] == "post"
