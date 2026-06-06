"""observer のバックグラウンド解析ランタイムを検証するテスト。

対象: _resolve_python_cmd / _project_context / _pid_file_candidates /
_is_running / _stop_running_observer / _observer_log_path / _sentinel_path /
_write_guard_sentinel / _log_tail / _print_status / _run_prune /
_resolve_project_root / _check_active_hours / _check_cooldown / _guardian_allows /
_append_log / _build_analysis_prompt / _prepare_analysis_file /
_run_claude_analysis / _archive_observations / _analyze_observations /
_loop_once / _run_loop / _build_observer_env / _spawn_observer_process /
_check_prompt_abort / _start_observer / _stop_observer / _parse_main_args /
_run_loop_action / main

subprocess・os.kill・signal・threading・time.sleep・shutil.which・
detect_project・_get_idle_seconds を全モックし、実プロセス/スレッドを起動しない。
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from deepblue.skills.learn import observer


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """_CONFIG_DIR を tmp 配下に差し替えて返す。"""
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setattr(observer, "_CONFIG_DIR", cfg)
    return cfg


@pytest.fixture
def project_dict(tmp_path: Path) -> dict:
    """detect_project 戻り値を模したプロジェクト辞書を返す。"""
    pdir = tmp_path / "proj" / "store"
    root = tmp_path / "proj"
    return {
        "id": "pid",
        "name": "proj",
        "root": root,
        "project_dir": pdir,
        "observations_file": pdir / "observations.jsonl",
        "instincts_personal": pdir / "instincts" / "personal",
        "instincts_inherited": pdir / "instincts" / "inherited",
        "evolved_dir": pdir / "evolved",
    }


def _obs_project(project_dict: dict) -> observer.ObserverProject:
    """ObserverProject を辞書から構築する。"""
    return observer.ObserverProject(
        project_dir=project_dict["project_dir"],
        project_root=project_dict["root"],
        project_name=project_dict["name"],
        project_id=project_dict["id"],
        observations_file=project_dict["observations_file"],
        instincts_dir=project_dict["instincts_personal"],
    )


# --- _resolve_python_cmd -----------------------------------------------------


def test_resolve_python_cmd(monkeypatch: pytest.MonkeyPatch) -> None:
    """sys.executable があればそれを返す。"""
    monkeypatch.setattr(observer.sys, "executable", "/py")
    assert observer._resolve_python_cmd() == "/py"


def test_resolve_python_cmd_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """sys.executable が空なら python3 を返す。"""
    monkeypatch.setattr(observer.sys, "executable", "")
    assert observer._resolve_python_cmd() == "python3"


# --- _project_context --------------------------------------------------------


def test_project_context_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """PROJECT_DIR/ROOT が揃えば環境変数からパス情報を構築する。"""
    monkeypatch.setenv("PROJECT_DIR", str(tmp_path / "pd"))
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / "pr"))
    monkeypatch.delenv("PROJECT_NAME", raising=False)
    monkeypatch.delenv("PROJECT_ID", raising=False)
    ctx = observer._project_context()
    assert ctx["root"] == tmp_path / "pr"
    assert ctx["project_dir"] == tmp_path / "pd"
    assert ctx["observations_file"] == tmp_path / "pd" / "observations.jsonl"


def test_project_context_from_detect(monkeypatch: pytest.MonkeyPatch, project_dict: dict) -> None:
    """環境変数が無ければ detect_project の結果を Path 化して返す。"""
    monkeypatch.delenv("PROJECT_DIR", raising=False)
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    detect_ret = {k: str(v) if isinstance(v, Path) else v for k, v in project_dict.items()}
    monkeypatch.setattr(observer, "detect_project", lambda: dict(detect_ret))
    ctx = observer._project_context()
    assert isinstance(ctx["project_dir"], Path)
    assert ctx["name"] == "proj"


# --- _pid_file_candidates ----------------------------------------------------


def test_pid_file_candidates(config_dir: Path, tmp_path: Path) -> None:
    """プロジェクトと設定ディレクトリの 2 候補を返す。"""
    cands = observer._pid_file_candidates(tmp_path)
    assert cands == [tmp_path / ".observer.pid", config_dir / ".observer.pid"]


# --- _is_running -------------------------------------------------------------


def test_is_running_not_exists(tmp_path: Path) -> None:
    """PID ファイルが無ければ False。"""
    assert observer._is_running(tmp_path / "x.pid") is False


def test_is_running_bad_content(tmp_path: Path) -> None:
    """不正内容なら unlink して False。"""
    pid = tmp_path / "p.pid"
    pid.write_text("bad", encoding="utf-8")
    assert observer._is_running(pid) is False
    assert not pid.exists()


def test_is_running_bad_content_unlink_oserror(tmp_path: Path) -> None:
    """不正内容かつ unlink 失敗でも False。"""
    pid = tmp_path / "p.pid"
    pid.write_text("bad", encoding="utf-8")
    with mock.patch.object(Path, "unlink", side_effect=OSError):
        assert observer._is_running(pid) is False


def test_is_running_too_small(tmp_path: Path) -> None:
    """pid<=1 なら unlink して False。"""
    pid = tmp_path / "p.pid"
    pid.write_text("1", encoding="utf-8")
    assert observer._is_running(pid) is False


def test_is_running_too_small_unlink_oserror(tmp_path: Path) -> None:
    """pid<=1 かつ unlink 失敗でも False。"""
    pid = tmp_path / "p.pid"
    pid.write_text("1", encoding="utf-8")
    with mock.patch.object(Path, "unlink", side_effect=OSError):
        assert observer._is_running(pid) is False


def test_is_running_alive(tmp_path: Path) -> None:
    """os.kill 成功なら True。"""
    pid = tmp_path / "p.pid"
    pid.write_text("4242", encoding="utf-8")
    with mock.patch.object(observer.os, "kill", return_value=None):
        assert observer._is_running(pid) is True


def test_is_running_dead(tmp_path: Path) -> None:
    """os.kill が OSError なら unlink して False。"""
    pid = tmp_path / "p.pid"
    pid.write_text("4242", encoding="utf-8")
    with mock.patch.object(observer.os, "kill", side_effect=OSError):
        assert observer._is_running(pid) is False
    assert not pid.exists()


def test_is_running_dead_unlink_oserror(tmp_path: Path) -> None:
    """死活確認失敗かつ unlink 失敗でも False。"""
    pid = tmp_path / "p.pid"
    pid.write_text("4242", encoding="utf-8")
    with (
        mock.patch.object(observer.os, "kill", side_effect=OSError),
        mock.patch.object(Path, "unlink", side_effect=OSError),
    ):
        assert observer._is_running(pid) is False


# --- _stop_running_observer --------------------------------------------------


def test_stop_not_exists(tmp_path: Path) -> None:
    """PID ファイルが無ければ False。"""
    assert observer._stop_running_observer(tmp_path / "x.pid") is False


def test_stop_bad_content(tmp_path: Path) -> None:
    """不正内容なら unlink して False。"""
    pid = tmp_path / "p.pid"
    pid.write_text("bad", encoding="utf-8")
    assert observer._stop_running_observer(pid) is False
    assert not pid.exists()


def test_stop_bad_content_unlink_oserror(tmp_path: Path) -> None:
    """不正内容かつ unlink 失敗でも False。"""
    pid = tmp_path / "p.pid"
    pid.write_text("bad", encoding="utf-8")
    with mock.patch.object(Path, "unlink", side_effect=OSError):
        assert observer._stop_running_observer(pid) is False


def test_stop_too_small(tmp_path: Path) -> None:
    """pid<=1 なら unlink して False。"""
    pid = tmp_path / "p.pid"
    pid.write_text("0", encoding="utf-8")
    assert observer._stop_running_observer(pid) is False


def test_stop_too_small_unlink_oserror(tmp_path: Path) -> None:
    """pid<=1 かつ unlink 失敗でも False。"""
    pid = tmp_path / "p.pid"
    pid.write_text("0", encoding="utf-8")
    with mock.patch.object(Path, "unlink", side_effect=OSError):
        assert observer._stop_running_observer(pid) is False


def test_stop_dead(tmp_path: Path) -> None:
    """死活確認 OSError なら unlink して False。"""
    pid = tmp_path / "p.pid"
    pid.write_text("4242", encoding="utf-8")
    with mock.patch.object(observer.os, "kill", side_effect=OSError):
        assert observer._stop_running_observer(pid) is False
    assert not pid.exists()


def test_stop_dead_unlink_oserror(tmp_path: Path) -> None:
    """死活確認失敗かつ unlink 失敗でも False。"""
    pid = tmp_path / "p.pid"
    pid.write_text("4242", encoding="utf-8")
    with (
        mock.patch.object(observer.os, "kill", side_effect=OSError),
        mock.patch.object(Path, "unlink", side_effect=OSError),
    ):
        assert observer._stop_running_observer(pid) is False


def test_stop_sigterm_oserror(tmp_path: Path) -> None:
    """SIGTERM 送出が OSError なら False。"""
    pid = tmp_path / "p.pid"
    pid.write_text("4242", encoding="utf-8")
    # kill(pid,0) 成功 → kill(pid,SIGTERM) で OSError
    calls = {"n": 0}

    def fake_kill(_pid: int, sig: int) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError

    with mock.patch.object(observer.os, "kill", side_effect=fake_kill):
        assert observer._stop_running_observer(pid) is False


def test_stop_success(tmp_path: Path) -> None:
    """SIGTERM を送れたら unlink して True。"""
    pid = tmp_path / "p.pid"
    pid.write_text("4242", encoding="utf-8")
    with mock.patch.object(observer.os, "kill", return_value=None):
        assert observer._stop_running_observer(pid) is True
    assert not pid.exists()


def test_stop_success_unlink_oserror(tmp_path: Path) -> None:
    """SIGTERM 成功後の unlink が OSError でも True。"""
    pid = tmp_path / "p.pid"
    pid.write_text("4242", encoding="utf-8")
    with (
        mock.patch.object(observer.os, "kill", return_value=None),
        mock.patch.object(Path, "unlink", side_effect=OSError),
    ):
        assert observer._stop_running_observer(pid) is True


# --- _observer_log_path / _sentinel_path / _write_guard_sentinel -------------


def test_observer_log_path(tmp_path: Path) -> None:
    """observer.log のパスを返す。"""
    assert observer._observer_log_path(tmp_path) == tmp_path / "observer.log"


def test_sentinel_path_root_exists(tmp_path: Path) -> None:
    """root が存在すれば root 配下にセンチネルを置く。"""
    root = tmp_path / "root"
    root.mkdir()
    assert observer._sentinel_path(tmp_path / "pd", root) == root / ".observer.lock"


def test_sentinel_path_root_missing(tmp_path: Path) -> None:
    """root が無ければ project_dir 配下にセンチネルを置く。"""
    pd = tmp_path / "pd"
    assert observer._sentinel_path(pd, tmp_path / "missing") == pd / ".observer.lock"


def test_write_guard_sentinel(tmp_path: Path) -> None:
    """センチネルファイルを書き出す。"""
    root = tmp_path / "root"
    root.mkdir()
    observer._write_guard_sentinel(tmp_path / "pd", root)
    assert (root / ".observer.lock").exists()


# --- _log_tail ---------------------------------------------------------------


def test_log_tail_reads(tmp_path: Path) -> None:
    """指定行以降を結合して返す。"""
    log = tmp_path / "l.log"
    log.write_text("a\nb\nc\n", encoding="utf-8")
    assert observer._log_tail(log, 1) == "b\nc"


def test_log_tail_oserror(tmp_path: Path) -> None:
    """読み込み失敗時は空文字を返す。"""
    assert observer._log_tail(tmp_path / "missing.log", 0) == ""


# --- _print_status -----------------------------------------------------------


def test_print_status_running(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """稼働中は観測数・インスティンクト数を表示して 0 を返す。"""
    pid = tmp_path / "p.pid"
    pid.write_text("4242", encoding="utf-8")
    obs = tmp_path / "observations.jsonl"
    obs.write_text("l1\nl2\n", encoding="utf-8")
    inst = tmp_path / "instincts"
    inst.mkdir()
    (inst / "a.md").touch()
    monkeypatch.setattr(observer, "_is_running", lambda p: True)
    assert observer._print_status(tmp_path, pid, tmp_path / "log", inst, obs) == 0
    out = capsys.readouterr().out
    assert "Observations: 2 lines" in out
    assert "Instincts: 1" in out


def test_print_status_running_obs_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """観測ファイル読込失敗時は 0 件として続行する。"""
    pid = tmp_path / "p.pid"
    pid.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(observer, "_is_running", lambda p: True)
    # 観測ファイルは存在しない → OSError → 0 件、instincts_dir も無し → 0
    assert observer._print_status(tmp_path, pid, tmp_path / "log", tmp_path / "noinst", tmp_path / "noobs") == 0


def test_print_status_not_running_removes_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未起動で残存 PID ファイルがあれば削除し 1 を返す。"""
    pid = tmp_path / "p.pid"
    pid.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(observer, "_is_running", lambda p: False)
    assert observer._print_status(tmp_path, pid, tmp_path / "log", tmp_path / "i", tmp_path / "o") == 1
    assert not pid.exists()


def test_print_status_not_running_pid_unlink_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PID 削除が OSError でも 1 を返す。"""
    pid = tmp_path / "p.pid"
    pid.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(observer, "_is_running", lambda p: False)
    with mock.patch.object(Path, "unlink", side_effect=OSError):
        assert observer._print_status(tmp_path, pid, tmp_path / "log", tmp_path / "i", tmp_path / "o") == 1


def test_print_status_not_running_no_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未起動で PID ファイルも無ければ 1 を返す。"""
    monkeypatch.setattr(observer, "_is_running", lambda p: False)
    assert observer._print_status(tmp_path, tmp_path / "nopid", tmp_path / "log", tmp_path / "i", tmp_path / "o") == 1


# --- _run_prune --------------------------------------------------------------


def test_run_prune_ok() -> None:
    """prune サブコマンドを呼ぶ。"""
    with mock.patch.object(observer.subprocess, "run") as run:
        observer._run_prune()
        run.assert_called_once()


def test_run_prune_oserror() -> None:
    """subprocess 失敗でも握りつぶす。"""
    with mock.patch.object(observer.subprocess, "run", side_effect=OSError):
        observer._run_prune()


# --- _resolve_project_root ---------------------------------------------------


def test_resolve_project_root_exists(tmp_path: Path) -> None:
    """存在すればそのまま返す。"""
    assert observer._resolve_project_root(tmp_path) == tmp_path


def test_resolve_project_root_git(tmp_path: Path) -> None:
    """非存在なら git トップレベルを使う。"""
    completed = SimpleNamespace(stdout=str(tmp_path) + "\n")
    with mock.patch.object(observer.subprocess, "run", return_value=completed):
        assert observer._resolve_project_root(tmp_path / "missing") == tmp_path


def test_resolve_project_root_git_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """git 出力が空なら cwd を使う。"""
    completed = SimpleNamespace(stdout="")
    monkeypatch.setattr(observer.os, "getcwd", lambda: str(tmp_path))
    with mock.patch.object(observer.subprocess, "run", return_value=completed):
        assert observer._resolve_project_root(tmp_path / "missing") == tmp_path


def test_resolve_project_root_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """git が OSError なら cwd を使う。"""
    monkeypatch.setattr(observer.os, "getcwd", lambda: str(tmp_path))
    with mock.patch.object(observer.subprocess, "run", side_effect=OSError):
        assert observer._resolve_project_root(tmp_path / "missing") == tmp_path


# --- _check_active_hours -----------------------------------------------------


def test_active_hours_unlimited(tmp_path: Path) -> None:
    """0-0 は無制限で常に True。"""
    assert observer._check_active_hours(0, 0, tmp_path / "l") is True


def test_active_hours_within(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """通常窓内なら True。"""
    monkeypatch.setattr(observer.time, "strftime", lambda fmt: "1000")
    assert observer._check_active_hours(800, 2300, tmp_path / "l") is True


def test_active_hours_outside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """通常窓外なら False。"""
    monkeypatch.setattr(observer.time, "strftime", lambda fmt: "0500")
    assert observer._check_active_hours(800, 2300, tmp_path / "l") is False


def test_active_hours_wrap_within(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """日跨ぎ窓内なら True。"""
    monkeypatch.setattr(observer.time, "strftime", lambda fmt: "2350")
    assert observer._check_active_hours(2300, 600, tmp_path / "l") is True


def test_active_hours_wrap_outside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """日跨ぎ窓外なら False。"""
    monkeypatch.setattr(observer.time, "strftime", lambda fmt: "1200")
    assert observer._check_active_hours(2300, 600, tmp_path / "l") is False


# --- _check_cooldown ---------------------------------------------------------


def test_cooldown_no_log_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """履歴が無ければ通過し last_run_log を書く。"""
    monkeypatch.setattr(observer.time, "time", lambda: 100000)
    log = tmp_path / "last.log"
    assert observer._check_cooldown(tmp_path / "proj", 300, log, tmp_path / "l") is True
    assert log.exists()


def test_cooldown_active_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """直近実行から interval 未満なら False。"""
    monkeypatch.setattr(observer.time, "time", lambda: 1000)
    root = tmp_path / "proj"
    log = tmp_path / "last.log"
    log.write_text(f"{root}\t900\n", encoding="utf-8")
    assert observer._check_cooldown(root, 300, log, tmp_path / "l") is False


def test_cooldown_passes_after_interval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """interval 経過後は通過する。"""
    monkeypatch.setattr(observer.time, "time", lambda: 100000)
    root = tmp_path / "proj"
    log = tmp_path / "last.log"
    log.write_text(f"{root}\t900\nother\tx\n", encoding="utf-8")  # "other\tx" は無効値→entries に残る
    assert observer._check_cooldown(root, 300, log, tmp_path / "l") is True


def test_cooldown_skips_lines_without_tab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """タブ無し行はスキップする。"""
    monkeypatch.setattr(observer.time, "time", lambda: 100000)
    root = tmp_path / "proj"
    log = tmp_path / "last.log"
    log.write_text("no-tab-line\n", encoding="utf-8")
    assert observer._check_cooldown(root, 300, log, tmp_path / "l") is True


def test_cooldown_parse_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """エントリ値が非数値なら last_spawn=0 として通過する。"""
    monkeypatch.setattr(observer.time, "time", lambda: 100000)
    root = tmp_path / "proj"
    log = tmp_path / "last.log"
    log.write_text(f"{root}\tnotanumber\n", encoding="utf-8")
    assert observer._check_cooldown(root, 300, log, tmp_path / "l") is True


def test_cooldown_write_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """last_run_log の書き込みが OSError でも True を返す。"""
    monkeypatch.setattr(observer.time, "time", lambda: 100000)
    with mock.patch.object(Path, "open", side_effect=OSError):
        assert observer._check_cooldown(tmp_path / "proj", 300, tmp_path / "last.log", tmp_path / "l") is True


# --- _guardian_allows --------------------------------------------------------


def test_guardian_active_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """アクティブ時間外なら False。"""
    monkeypatch.setattr(observer, "_check_active_hours", lambda *a: False)
    assert observer._guardian_allows(tmp_path, tmp_path, tmp_path / "l") is False


def test_guardian_cooldown_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """クールダウン中なら False。"""
    monkeypatch.setattr(observer, "_check_active_hours", lambda *a: True)
    monkeypatch.setattr(observer, "_check_cooldown", lambda *a: False)
    assert observer._guardian_allows(tmp_path, tmp_path, tmp_path / "l") is False


def test_guardian_idle_exceeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """アイドル超過なら False。"""
    monkeypatch.setattr(observer, "_check_active_hours", lambda *a: True)
    monkeypatch.setattr(observer, "_check_cooldown", lambda *a: True)
    monkeypatch.setenv("OBSERVER_MAX_IDLE_SECONDS", "1800")
    monkeypatch.setattr(observer, "_get_idle_seconds", lambda: 9999)
    assert observer._guardian_allows(tmp_path, tmp_path, tmp_path / "l") is False


def test_guardian_allows_true(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """全条件通過なら True。"""
    monkeypatch.setattr(observer, "_check_active_hours", lambda *a: True)
    monkeypatch.setattr(observer, "_check_cooldown", lambda *a: True)
    monkeypatch.setenv("OBSERVER_MAX_IDLE_SECONDS", "1800")
    monkeypatch.setattr(observer, "_get_idle_seconds", lambda: 1)
    assert observer._guardian_allows(tmp_path, tmp_path, tmp_path / "l") is True


def test_guardian_no_idle_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """max_idle<=0 ならアイドル判定を飛ばして True。"""
    monkeypatch.setattr(observer, "_check_active_hours", lambda *a: True)
    monkeypatch.setattr(observer, "_check_cooldown", lambda *a: True)
    monkeypatch.setenv("OBSERVER_MAX_IDLE_SECONDS", "0")
    assert observer._guardian_allows(tmp_path, tmp_path, tmp_path / "l") is True


# --- _append_log / _build_analysis_prompt ------------------------------------


def test_append_log(tmp_path: Path) -> None:
    """タイムスタンプ付きで追記する。"""
    log = tmp_path / "sub" / "l.log"
    observer._append_log(log, "hello")
    assert "hello" in log.read_text(encoding="utf-8")


def test_build_analysis_prompt(tmp_path: Path) -> None:
    """プロンプトに必須要素を含む。"""
    prompt = observer._build_analysis_prompt("rel.jsonl", "proj", "pid", tmp_path / "inst")
    assert "rel.jsonl" in prompt
    assert "project_id: pid" in prompt


# --- _prepare_analysis_file --------------------------------------------------


def test_prepare_analysis_file_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """直近行を一時ファイルに書き出す。"""
    obs = tmp_path / "observations.jsonl"
    obs.write_text("a\nb\nc\n", encoding="utf-8")
    monkeypatch.setattr(observer.time, "time", lambda: 1)
    result = observer._prepare_analysis_file(obs, tmp_path / "tmp")
    assert result is not None
    assert result.read_text(encoding="utf-8").strip().splitlines() == ["a", "b", "c"]


def test_prepare_analysis_file_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """観測ファイル読込失敗時は None を返す。"""
    monkeypatch.setattr(observer.time, "time", lambda: 1)
    result = observer._prepare_analysis_file(tmp_path / "missing.jsonl", tmp_path / "tmp")
    assert result is None


# --- _run_claude_analysis ----------------------------------------------------


def test_run_claude_analysis_success(tmp_path: Path) -> None:
    """正常終了時は stdout/stderr をログに書き、解析ファイルを削除する。"""
    analysis = tmp_path / "a.jsonl"
    analysis.touch()
    completed = SimpleNamespace(stdout="out", stderr="err", returncode=0)
    with mock.patch.object(observer.subprocess, "run", return_value=completed):
        observer._run_claude_analysis("p", tmp_path, tmp_path / "log", analysis)
    assert not analysis.exists()


def test_run_claude_analysis_nonzero(tmp_path: Path) -> None:
    """非0終了時は失敗ログを追記する。"""
    analysis = tmp_path / "a.jsonl"
    analysis.touch()
    completed = SimpleNamespace(stdout="", stderr="", returncode=3)
    log = tmp_path / "log"
    with mock.patch.object(observer.subprocess, "run", return_value=completed):
        observer._run_claude_analysis("p", tmp_path, log, analysis)
    assert "exit 3" in log.read_text(encoding="utf-8")


def test_run_claude_analysis_timeout(tmp_path: Path) -> None:
    """タイムアウト時はログを残して return。"""
    log = tmp_path / "log"
    with mock.patch.object(observer.subprocess, "run", side_effect=observer.subprocess.TimeoutExpired("claude", 120)):
        observer._run_claude_analysis("p", tmp_path, log, tmp_path / "a.jsonl")
    assert "timed out" in log.read_text(encoding="utf-8")


def test_run_claude_analysis_oserror(tmp_path: Path) -> None:
    """起動失敗時はログを残して return。"""
    log = tmp_path / "log"
    with mock.patch.object(observer.subprocess, "run", side_effect=OSError("nope")):
        observer._run_claude_analysis("p", tmp_path, log, tmp_path / "a.jsonl")
    assert "failed to start" in log.read_text(encoding="utf-8")


def test_run_claude_analysis_low_max_turns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """max_turns<4 は 10 に補正される。"""
    monkeypatch.setenv("DEEPBLUE_OBSERVER_MAX_TURNS", "2")
    analysis = tmp_path / "a.jsonl"
    analysis.touch()
    completed = SimpleNamespace(stdout="", stderr="", returncode=0)
    with mock.patch.object(observer.subprocess, "run", return_value=completed) as run:
        observer._run_claude_analysis("p", tmp_path, tmp_path / "log", analysis)
    assert "10" in run.call_args.args[0]


def test_run_claude_analysis_unlink_oserror(tmp_path: Path) -> None:
    """解析ファイル削除が OSError でも握りつぶす。"""
    completed = SimpleNamespace(stdout="", stderr="", returncode=0)
    with (
        mock.patch.object(observer.subprocess, "run", return_value=completed),
        mock.patch.object(Path, "unlink", side_effect=OSError),
    ):
        observer._run_claude_analysis("p", tmp_path, tmp_path / "log", tmp_path / "a.jsonl")


# --- _archive_observations ---------------------------------------------------


def test_archive_observations_not_exists(tmp_path: Path) -> None:
    """観測ファイルが無ければ何もしない。"""
    observer._archive_observations(tmp_path / "missing.jsonl", tmp_path)


def test_archive_observations_moves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """観測ファイルをアーカイブへ退避する。"""
    monkeypatch.setattr(observer.time, "strftime", lambda fmt: "20260101-000000")
    obs = tmp_path / "observations.jsonl"
    obs.write_text("x", encoding="utf-8")
    observer._archive_observations(obs, tmp_path)
    assert not obs.exists()
    assert list((tmp_path / "observations.archive").glob("processed-*.jsonl"))


def test_archive_observations_replace_oserror(tmp_path: Path) -> None:
    """replace が OSError でも握りつぶす。"""
    obs = tmp_path / "observations.jsonl"
    obs.write_text("x", encoding="utf-8")
    with mock.patch.object(Path, "replace", side_effect=OSError):
        observer._archive_observations(obs, tmp_path)


# --- _analyze_observations ---------------------------------------------------


def test_analyze_no_observations_file(project_dict: dict) -> None:
    """観測ファイルが無ければ何もしない。"""
    observer._analyze_observations(_obs_project(project_dict), observer.ObserverConfig(project_dict["project_dir"] / "log", 20, 300))


def test_analyze_read_oserror(project_dict: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """観測ファイル読込が OSError なら return。"""
    obs = project_dict["observations_file"]
    obs.parent.mkdir(parents=True, exist_ok=True)
    obs.touch()
    with mock.patch.object(Path, "read_text", side_effect=OSError):
        observer._analyze_observations(_obs_project(project_dict), observer.ObserverConfig(project_dict["project_dir"] / "log", 20, 300))


def test_analyze_below_min(project_dict: dict) -> None:
    """観測数が最小値未満なら return。"""
    obs = project_dict["observations_file"]
    obs.parent.mkdir(parents=True, exist_ok=True)
    obs.write_text("a\n", encoding="utf-8")
    observer._analyze_observations(_obs_project(project_dict), observer.ObserverConfig(project_dict["project_dir"] / "log", 20, 300))


def test_analyze_windows_skip(project_dict: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 環境では解析をスキップする。"""
    obs = project_dict["observations_file"]
    obs.parent.mkdir(parents=True, exist_ok=True)
    obs.write_text("a\nb\n", encoding="utf-8")
    monkeypatch.setenv("CLV2_IS_WINDOWS", "true")
    monkeypatch.setenv("DEEPBLUE_OBSERVER_ALLOW_WINDOWS", "false")
    log = project_dict["project_dir"] / "log"
    observer._analyze_observations(_obs_project(project_dict), observer.ObserverConfig(log, 1, 300))
    assert "Windows" in log.read_text(encoding="utf-8")


def test_analyze_no_claude(project_dict: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """claude CLI が無ければスキップする。"""
    obs = project_dict["observations_file"]
    obs.parent.mkdir(parents=True, exist_ok=True)
    obs.write_text("a\nb\n", encoding="utf-8")
    monkeypatch.setenv("CLV2_IS_WINDOWS", "false")
    monkeypatch.setattr(observer.shutil, "which", lambda c: None)
    log = project_dict["project_dir"] / "log"
    observer._analyze_observations(_obs_project(project_dict), observer.ObserverConfig(log, 1, 300))
    assert "claude CLI not found" in log.read_text(encoding="utf-8")


def test_analyze_guardian_blocks(project_dict: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """session-guardian に抑止されればスキップする。"""
    obs = project_dict["observations_file"]
    obs.parent.mkdir(parents=True, exist_ok=True)
    obs.write_text("a\nb\n", encoding="utf-8")
    monkeypatch.setenv("CLV2_IS_WINDOWS", "false")
    monkeypatch.setattr(observer.shutil, "which", lambda c: "/usr/bin/claude")
    monkeypatch.setattr(observer, "_guardian_allows", lambda *a: False)
    log = project_dict["project_dir"] / "log"
    observer._analyze_observations(_obs_project(project_dict), observer.ObserverConfig(log, 1, 300))
    assert "skipped by session-guardian" in log.read_text(encoding="utf-8")


def test_analyze_prepare_fails(project_dict: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """解析ファイル作成に失敗すれば return。"""
    obs = project_dict["observations_file"]
    obs.parent.mkdir(parents=True, exist_ok=True)
    obs.write_text("a\nb\n", encoding="utf-8")
    monkeypatch.setenv("CLV2_IS_WINDOWS", "false")
    monkeypatch.setattr(observer.shutil, "which", lambda c: "/usr/bin/claude")
    monkeypatch.setattr(observer, "_guardian_allows", lambda *a: True)
    monkeypatch.setattr(observer, "_prepare_analysis_file", lambda *a: None)
    log = project_dict["project_dir"] / "log"
    observer._analyze_observations(_obs_project(project_dict), observer.ObserverConfig(log, 1, 300))


def test_analyze_full_flow(project_dict: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """全条件通過時は解析実行とアーカイブを呼ぶ。"""
    obs = project_dict["observations_file"]
    obs.parent.mkdir(parents=True, exist_ok=True)
    obs.write_text("a\nb\n", encoding="utf-8")
    monkeypatch.setenv("CLV2_IS_WINDOWS", "false")
    monkeypatch.setattr(observer.shutil, "which", lambda c: "/usr/bin/claude")
    monkeypatch.setattr(observer, "_guardian_allows", lambda *a: True)
    monkeypatch.setattr(observer, "_prepare_analysis_file", lambda *a: project_dict["project_dir"] / "a.jsonl")
    ran, archived = [], []
    monkeypatch.setattr(observer, "_run_claude_analysis", lambda *a: ran.append(a))
    monkeypatch.setattr(observer, "_archive_observations", lambda *a: archived.append(a))
    observer._analyze_observations(_obs_project(project_dict), observer.ObserverConfig(project_dict["project_dir"] / "log", 1, 300))
    assert ran and archived


# --- _loop_once --------------------------------------------------------------


def test_loop_once_already_analyzing(project_dict: dict) -> None:
    """解析中なら skip する。"""
    state = {"analyzing": True}
    observer._loop_once(_obs_project(project_dict), observer.ObserverConfig(project_dict["project_dir"] / "log", 1, 300), threading.Event(), state)


def test_loop_once_cooldown(project_dict: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """クールダウン中なら skip する。"""
    monkeypatch.setattr(observer.time, "time", lambda: 100)
    state = {"analyzing": False, "last_analysis_epoch": 99}
    observer._loop_once(_obs_project(project_dict), observer.ObserverConfig(project_dict["project_dir"] / "log", 1, 300), threading.Event(), state)
    assert state["analyzing"] is False


def test_loop_once_runs(project_dict: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """条件を満たせば解析を実行して状態を更新する。"""
    monkeypatch.setattr(observer.time, "time", lambda: 100000)
    called = []
    monkeypatch.setattr(observer, "_analyze_observations", lambda p, c: called.append(1))
    state = {"analyzing": False, "last_analysis_epoch": 0}
    observer._loop_once(_obs_project(project_dict), observer.ObserverConfig(project_dict["project_dir"] / "log", 1, 300), threading.Event(), state)
    assert called
    assert state["analyzing"] is False


# --- _run_loop ---------------------------------------------------------------


def test_run_loop_requires_pid_file(project_dict: dict) -> None:
    """pid_file 未設定なら AssertionError。"""
    config = observer.ObserverConfig(project_dict["project_dir"] / "log", 1, 300, pid_file=None)
    project_dict["project_dir"].mkdir(parents=True, exist_ok=True)
    with pytest.raises(AssertionError):
        observer._run_loop(_obs_project(project_dict), config)


def test_run_loop_signal_and_iteration(project_dict: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """SIGUSR1 早期起床(continue)と通常サイクルを経てループを抜ける。"""
    project_dict["project_dir"].mkdir(parents=True, exist_ok=True)
    pid_file = project_dict["project_dir"] / ".observer.pid"
    config = observer.ObserverConfig(project_dict["project_dir"] / "log", 1, 0, pid_file=pid_file)
    monkeypatch.setattr(observer, "_run_prune", lambda: None)

    captured: list = []
    monkeypatch.setattr(observer.signal, "signal", lambda sig, handler: captured.append(handler))

    fake_event = mock.MagicMock()
    monkeypatch.setattr(observer.threading, "Event", lambda: fake_event)

    state_calls = {"n": 0}

    def wait_side(timeout: float) -> None:
        state_calls["n"] += 1
        if state_calls["n"] == 1:
            captured[0](10, None)  # _on_usr1 → usr1_fired=True

    fake_event.wait.side_effect = wait_side

    loop_calls = {"n": 0}

    def fake_loop_once(*_a: object) -> None:
        loop_calls["n"] += 1
        raise RuntimeError("stop loop")

    monkeypatch.setattr(observer, "_loop_once", fake_loop_once)

    with pytest.raises(RuntimeError, match="stop loop"):
        observer._run_loop(_obs_project(project_dict), config)
    assert loop_calls["n"] == 1
    assert pid_file.read_text(encoding="utf-8")


# --- _build_observer_env -----------------------------------------------------


def test_build_observer_env(project_dict: dict, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """子プロセス用の環境変数を構築する。"""
    monkeypatch.setenv("MIN_OBSERVATIONS", "5")
    env = observer._build_observer_env(
        project_dict, project_dict["project_dir"], project_dict["project_dir"] / ".observer.pid",
        project_dict["project_dir"] / "log", project_dict["instincts_personal"],
    )
    assert env["PROJECT_NAME"] == "proj"
    assert env["MIN_OBSERVATIONS"] == "5"
    assert env["CONFIG_DIR"] == str(config_dir)


# --- _spawn_observer_process -------------------------------------------------


def test_spawn_posix(project_dict: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """posix では start_new_session で起動し 0 を返す。"""
    project_dict["project_dir"].mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(observer.os, "name", "posix")
    with mock.patch.object(observer.subprocess, "Popen") as popen:
        assert observer._spawn_observer_process(project_dict["project_dir"], project_dict["project_dir"] / "log", {}) == 0
        assert popen.call_args.kwargs.get("start_new_session") is True


def test_spawn_windows(project_dict: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """nt では creationflags を使い 0 を返す。"""
    project_dict["project_dir"].mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(observer.os, "name", "nt")
    with mock.patch.object(observer.subprocess, "Popen") as popen:
        assert observer._spawn_observer_process(project_dict["project_dir"], project_dict["project_dir"] / "log", {}) == 0
        assert "creationflags" in popen.call_args.kwargs


def test_spawn_oserror(project_dict: dict, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Popen が OSError なら 1 を返す。"""
    project_dict["project_dir"].mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(observer.os, "name", "posix")
    with mock.patch.object(observer.subprocess, "Popen", side_effect=OSError("boom")):
        assert observer._spawn_observer_process(project_dict["project_dir"], project_dict["project_dir"] / "log", {}) == 1
    assert "Failed to start observer" in capsys.readouterr().out


# --- _check_prompt_abort -----------------------------------------------------


def test_check_prompt_abort_no_match(tmp_path: Path) -> None:
    """パターン非一致なら False。"""
    log = tmp_path / "log"
    log.write_text("normal output\n", encoding="utf-8")
    assert observer._check_prompt_abort(log, 0, tmp_path, tmp_path) is False


def test_check_prompt_abort_match(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """パターン一致なら停止処理を行い True。"""
    log = tmp_path / "log"
    log.write_text("Can you confirm this action?\n", encoding="utf-8")
    stopped = []
    monkeypatch.setattr(observer, "_stop_running_observer", lambda p: stopped.append(p))
    monkeypatch.setattr(observer, "_write_guard_sentinel", lambda pd, pr: None)
    assert observer._check_prompt_abort(log, 0, tmp_path, tmp_path) is True
    assert "OBSERVER_ABORT" in capsys.readouterr().out
    assert stopped


# --- _start_observer ---------------------------------------------------------


def test_start_observer_already_running(project_dict: dict, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """既存稼働があれば 0 を返す。"""
    pdir = project_dict["project_dir"]
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / ".observer.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(observer, "_is_running", lambda p: True)
    assert observer._start_observer(project_dict, reset=False) == 0
    assert "already running" in capsys.readouterr().out


def test_start_observer_reset_unlink_oserror(project_dict: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """reset 時のセンチネル削除失敗を握りつぶしつつ既存稼働で 0。"""
    pdir = project_dict["project_dir"]
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / ".observer.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(observer, "_is_running", lambda p: True)
    with mock.patch.object(Path, "unlink", side_effect=OSError):
        assert observer._start_observer(project_dict, reset=True) == 0


def test_start_observer_spawn_fails(project_dict: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """spawn 失敗時は 1 を返す。"""
    monkeypatch.setattr(observer, "_is_running", lambda p: False)
    monkeypatch.setattr(observer, "_spawn_observer_process", lambda *a: 1)
    assert observer._start_observer(project_dict, reset=False) == 1


def test_start_observer_prompt_abort(project_dict: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """プロンプト検出時は 2 を返す。"""
    monkeypatch.setattr(observer, "_is_running", lambda p: False)
    monkeypatch.setattr(observer, "_spawn_observer_process", lambda *a: 0)
    monkeypatch.setattr(observer.time, "sleep", lambda s: None)
    monkeypatch.setattr(observer, "_check_prompt_abort", lambda *a: True)
    assert observer._start_observer(project_dict, reset=False) == 2


def test_start_observer_success(project_dict: dict, config_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """起動成功で 0 を返す。"""
    monkeypatch.setattr(observer, "_spawn_observer_process", lambda *a: 0)
    monkeypatch.setattr(observer.time, "sleep", lambda s: None)
    monkeypatch.setattr(observer, "_check_prompt_abort", lambda *a: False)
    # 候補チェック 2 回は未起動、起動後の確認で稼働中
    seq = iter([False, False, True])
    monkeypatch.setattr(observer, "_is_running", lambda p: next(seq))
    pdir = project_dict["project_dir"]
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / ".observer.pid").write_text("4242", encoding="utf-8")
    assert observer._start_observer(project_dict, reset=False) == 0
    assert "Observer started" in capsys.readouterr().out


def test_start_observer_died(project_dict: dict, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """起動後すぐ死んだ場合は 1 を返す。"""
    monkeypatch.setattr(observer, "_is_running", lambda p: False)
    monkeypatch.setattr(observer, "_spawn_observer_process", lambda *a: 0)
    monkeypatch.setattr(observer.time, "sleep", lambda s: None)
    monkeypatch.setattr(observer, "_check_prompt_abort", lambda *a: False)
    assert observer._start_observer(project_dict, reset=False) == 1
    assert "process died immediately" in capsys.readouterr().out


# --- _stop_observer ----------------------------------------------------------


def test_stop_observer_success(project_dict: dict, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """停止できれば 0 を返す。"""
    monkeypatch.setattr(observer, "_stop_running_observer", lambda p: True)
    assert observer._stop_observer(project_dict) == 0
    assert "Observer stopped" in capsys.readouterr().out


def test_stop_observer_not_running(project_dict: dict, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """未起動なら 1 を返す。"""
    monkeypatch.setattr(observer, "_stop_running_observer", lambda p: False)
    assert observer._stop_observer(project_dict) == 1
    assert "not running" in capsys.readouterr().out


# --- _parse_main_args --------------------------------------------------------


def test_parse_args_default() -> None:
    """引数なしは start/False。"""
    assert observer._parse_main_args([]) == ("start", False)


@pytest.mark.parametrize("action", ["start", "stop", "status", "loop"])
def test_parse_args_actions(action: str) -> None:
    """既知アクションをそのまま返す。"""
    assert observer._parse_main_args([action]) == (action, False)


def test_parse_args_reset() -> None:
    """--reset フラグを認識する。"""
    assert observer._parse_main_args(["start", "--reset"]) == ("start", True)


def test_parse_args_invalid(capsys: pytest.CaptureFixture[str]) -> None:
    """不正引数は usage を出して ('', False)。"""
    assert observer._parse_main_args(["bogus"]) == ("", False)
    assert "Usage:" in capsys.readouterr().out


# --- _run_loop_action --------------------------------------------------------


def test_run_loop_action(project_dict: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """ObserverProject/Config を構築して _run_loop を呼ぶ。"""
    captured = {}

    def fake_run_loop(p: observer.ObserverProject, c: observer.ObserverConfig) -> int:
        captured["called"] = (p, c)
        return 0

    monkeypatch.setattr(observer, "_run_loop", fake_run_loop)
    pdir = project_dict["project_dir"]
    rc = observer._run_loop_action(project_dict, pdir, pdir / "log", pdir / ".observer.pid", project_dict["instincts_personal"])
    assert rc == 0
    assert isinstance(captured["called"][0], observer.ObserverProject)


# --- main --------------------------------------------------------------------


def test_main_invalid_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """不正引数なら 1 を返す。"""
    monkeypatch.setattr(observer, "_parse_main_args", lambda a: ("", False))
    assert observer.main(["bogus"]) == 1


def test_main_stop(project_dict: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """stop アクションは _stop_observer を呼ぶ。"""
    monkeypatch.setattr(observer, "_project_context", lambda: project_dict)
    monkeypatch.setattr(observer, "_stop_observer", lambda p: 0)
    assert observer.main(["stop"]) == 0


def test_main_status(project_dict: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """status アクションは _print_status を呼ぶ。"""
    monkeypatch.setattr(observer, "_project_context", lambda: project_dict)
    monkeypatch.setattr(observer, "_print_status", lambda *a: 0)
    assert observer.main(["status"]) == 0


def test_main_loop(project_dict: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """loop アクションは _run_loop_action を呼ぶ。"""
    monkeypatch.setattr(observer, "_project_context", lambda: project_dict)
    monkeypatch.setattr(observer, "_run_loop_action", lambda *a: 0)
    assert observer.main(["loop"]) == 0


def test_main_start_with_reset(project_dict: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """start + --reset はセンチネル削除後に _start_observer を呼ぶ。"""
    monkeypatch.setattr(observer, "_project_context", lambda: project_dict)
    monkeypatch.setattr(observer, "_start_observer", lambda p, r: 0)
    with mock.patch.object(Path, "unlink", side_effect=OSError):
        assert observer.main(["start", "--reset"]) == 0
