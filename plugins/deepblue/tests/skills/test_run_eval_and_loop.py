"""run_eval / run_loop モジュールのテスト。"""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepblue.skills import run_eval, run_loop
from deepblue.skills._eval_config import EvalConfig, SingleQueryConfig
from deepblue.skills.run_loop import LoopConfig


class _FakeStdout:
    def fileno(self) -> int:
        return 1


class _FakeProcess:
    def __init__(self) -> None:
        self.stdout = _FakeStdout()
        self.killed = False
        self.waited = False

    def poll(self) -> None:
        return None

    def kill(self) -> None:
        self.killed = True

    def wait(self) -> None:
        self.waited = True


class _PollingStdout:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def fileno(self) -> int:
        return 1

    def read(self) -> bytes:
        return self._data


class _PollingProcess:
    def __init__(self, data: bytes) -> None:
        self.stdout = _PollingStdout(data)
        self.killed = False
        self.waited = False

    def poll(self) -> int:
        return 0

    def kill(self) -> None:
        self.killed = True

    def wait(self) -> None:
        self.waited = True


class _FakeFuture:
    def __init__(self, value: object | None = None, exc: Exception | None = None) -> None:
        self.value = value
        self.exc = exc

    def result(self) -> object:
        if self.exc:
            raise self.exc
        return self.value


class _FakeExecutor:
    def __init__(self, max_workers: int, futures: list[_FakeFuture]) -> None:
        self.max_workers = max_workers
        self._futures = futures
        self.submissions: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    def __enter__(self) -> _FakeExecutor:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def submit(self, fn, *args, **kwargs):  # noqa: ANN001
        future = self._futures[len(self.submissions)]
        self.submissions.append((fn, args, kwargs))
        return future


def _make_eval_cfg(tmp_path: Path) -> EvalConfig:
    """テスト用 EvalConfig を生成する。"""
    return EvalConfig(
        num_workers=1,
        timeout=5,
        project_root=tmp_path,
        runs_per_query=1,
        trigger_threshold=0.5,
        model="sonnet",
    )


def _make_loop_cfg(tmp_path: Path, *, max_iterations: int = 3, holdout: float = 0.0, verbose: bool = False, log_dir: Path | None = None) -> LoopConfig:
    """テスト用 LoopConfig を生成する。"""
    return LoopConfig(
        max_iterations=max_iterations,
        holdout=holdout,
        verbose=verbose,
        log_dir=log_dir,
        eval_config=_make_eval_cfg(tmp_path),
    )


def test_find_project_root_prefers_claude_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    current = tmp_path / "work" / "nested"
    current.mkdir(parents=True)
    monkeypatch.chdir(current)

    assert run_eval.find_project_root() == current

    claude_root = tmp_path / "work"
    (claude_root / ".claude").mkdir(parents=True)

    assert run_eval.find_project_root() == claude_root


def test_run_single_query_triggers_on_stream_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    clean_name = "alpha-skill-12345678"
    chunks = [
        (
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_start",
                        "content_block": {"type": "tool_use", "name": "Skill"},
                    },
                }
            )
            + "\n"
        ).encode("utf-8"),
        (
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": f'{{"skill": "{clean_name}"}}',
                        },
                    },
                }
            )
            + "\n"
        ).encode("utf-8"),
    ]
    process = _FakeProcess()
    read_calls = iter([b"".join(chunks), b""])

    monkeypatch.setattr(run_eval.uuid, "uuid4", lambda: SimpleNamespace(hex="12345678abcdef"))
    monkeypatch.setattr(run_eval.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(run_eval.select, "select", lambda r, w, x, timeout=0: (r, [], []))
    monkeypatch.setattr(run_eval.os, "read", lambda fd, size: next(read_calls))

    result = run_eval.run_single_query("query", "alpha", "skill description", SingleQueryConfig(timeout=5, project_root=str(project_root), model=None))

    assert result is True
    assert process.killed is True
    assert process.waited is True
    assert not list((project_root / ".claude" / "commands").glob("*.md"))


def test_run_single_query_fallback_assistant_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    clean_name = "alpha-skill-12345678"
    chunk = (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": f"/tmp/{clean_name}.md"},
                        }
                    ]
                },
            }
        )
        + "\n"
    ).encode("utf-8")
    process = _FakeProcess()
    read_calls = iter([chunk, b""])

    monkeypatch.setattr(run_eval.uuid, "uuid4", lambda: SimpleNamespace(hex="12345678abcdef"))
    monkeypatch.setattr(run_eval.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(run_eval.select, "select", lambda r, w, x, timeout=0: (r, [], []))
    monkeypatch.setattr(run_eval.os, "read", lambda fd, size: next(read_calls))

    result = run_eval.run_single_query("query", "alpha", "skill description", SingleQueryConfig(timeout=5, project_root=str(project_root), model=None))

    assert result is True


def test_run_single_query_result_event_returns_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    chunk = (json.dumps({"type": "result"}) + "\n").encode("utf-8")
    process = _FakeProcess()
    read_calls = iter([chunk, b""])

    monkeypatch.setattr(run_eval.uuid, "uuid4", lambda: SimpleNamespace(hex="12345678abcdef"))
    monkeypatch.setattr(run_eval.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(run_eval.select, "select", lambda r, w, x, timeout=0: (r, [], []))
    monkeypatch.setattr(run_eval.os, "read", lambda fd, size: next(read_calls))

    result = run_eval.run_single_query("query", "alpha", "skill description", SingleQueryConfig(timeout=5, project_root=str(project_root), model=None))

    assert result is False


def test_run_single_query_uses_model_and_rejects_unknown_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    process = _FakeProcess()
    captured_cmds: list[list[str]] = []
    read_calls = iter(
        [
            (
                json.dumps(
                    {
                        "type": "stream_event",
                        "event": {
                            "type": "content_block_start",
                            "content_block": {"type": "tool_use", "name": "Other"},
                        },
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "stream_event",
                        "event": {"type": "message_stop"},
                    }
                )
                + "\n"
            ).encode("utf-8")
        ]
    )

    monkeypatch.setattr(run_eval.uuid, "uuid4", lambda: SimpleNamespace(hex="12345678abcdef"))
    monkeypatch.setattr(run_eval.subprocess, "Popen", lambda cmd, **kwargs: captured_cmds.append(cmd) or process)
    monkeypatch.setattr(run_eval.select, "select", lambda r, w, x, timeout=0: (r, [], []))
    monkeypatch.setattr(run_eval.os, "read", lambda fd, size: next(read_calls))

    result = run_eval.run_single_query("query", "alpha", "skill description", SingleQueryConfig(timeout=5, project_root=str(project_root), model="sonnet"))

    assert result is False
    assert "--model" in captured_cmds[0]
    assert "sonnet" in captured_cmds[0]


def test_run_single_query_skips_invalid_json_and_message_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    process = _FakeProcess()
    read_calls = iter(
        [
            (
                b"{bad json}\n"
                + json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}).encode("utf-8")
                + b"\n"
            )
        ]
    )

    monkeypatch.setattr(run_eval.uuid, "uuid4", lambda: SimpleNamespace(hex="12345678abcdef"))
    monkeypatch.setattr(run_eval.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(run_eval.select, "select", lambda r, w, x, timeout=0: (r, [], []))
    monkeypatch.setattr(run_eval.os, "read", lambda fd, size: next(read_calls))

    result = run_eval.run_single_query("query", "alpha", "skill description", SingleQueryConfig(timeout=5, project_root=str(project_root), model=None))

    assert result is False


def test_run_single_query_returns_remaining_output_when_process_already_exited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    process = _PollingProcess(b'{"type": "result"}\n')

    monkeypatch.setattr(run_eval.uuid, "uuid4", lambda: SimpleNamespace(hex="12345678abcdef"))
    monkeypatch.setattr(run_eval.subprocess, "Popen", lambda *args, **kwargs: process)

    result = run_eval.run_single_query("query", "alpha", "skill description", SingleQueryConfig(timeout=5, project_root=str(project_root), model=None))

    assert result is False
    assert process.killed is False
    assert process.waited is False


def test_run_eval_aggregates_results_and_handles_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    futures = [
        _FakeFuture(True),
        _FakeFuture(False),
        _FakeFuture(exc=RuntimeError("boom")),
        _FakeFuture(False),
    ]

    monkeypatch.setattr(run_eval, "ProcessPoolExecutor", lambda max_workers: _FakeExecutor(max_workers, futures))
    monkeypatch.setattr(run_eval, "as_completed", lambda mapping: list(mapping.keys()))

    result = run_eval.run_eval(
        eval_set=[
            {"query": "q1", "should_trigger": True},
            {"query": "q2", "should_trigger": False},
        ],
        skill_name="alpha",
        description="desc",
        eval_cfg=EvalConfig(
            num_workers=2,
            timeout=5,
            project_root=tmp_path,
            runs_per_query=2,
            trigger_threshold=0.5,
            model="sonnet",
        ),
    )

    assert result["summary"] == {"total": 2, "passed": 2, "failed": 0}
    assert "警告: クエリに失敗しました" in capsys.readouterr().err


def test_run_eval_main_exits_when_skill_md_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    eval_file = tmp_path / "eval.json"
    eval_file.write_text("[]", encoding="utf-8")
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_eval.py",
            "--eval-set",
            str(eval_file),
            "--skill-path",
            str(skill_dir),
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        run_eval.main()

    assert excinfo.value.code == 1
    assert "SKILL.md" in capsys.readouterr().err


def test_run_eval_main_prints_json_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    eval_file = tmp_path / "eval.json"
    eval_file.write_text(json.dumps([{"query": "q1", "should_trigger": True}]), encoding="utf-8")
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    monkeypatch.setattr(run_eval, "parse_skill_md", lambda path: ("alpha", "desc", "content"))
    monkeypatch.setattr(run_eval, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        run_eval,
        "run_eval",
        lambda **kwargs: {
            "skill_name": "alpha",
            "description": "desc",
            "results": [
                {
                    "query": "q1",
                    "should_trigger": True,
                    "trigger_rate": 1.0,
                    "triggers": 1,
                    "runs": 1,
                    "pass": True,
                }
            ],
            "summary": {"total": 1, "passed": 1, "failed": 0},
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_eval.py",
            "--eval-set",
            str(eval_file),
            "--skill-path",
            str(skill_dir),
            "--verbose",
        ],
    )

    run_eval.main()

    captured = capsys.readouterr()
    assert '"skill_name": "alpha"' in captured.out
    assert "評価中: desc" in captured.err


def test_run_eval_main_block_invokes_main(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_eval.py", "--help"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("deepblue.skills.run_eval", run_name="__main__")

    assert excinfo.value.code == 0


def test_split_eval_set_preserves_both_classes() -> None:
    eval_set = [
        {"query": "t1", "should_trigger": True},
        {"query": "t2", "should_trigger": True},
        {"query": "f1", "should_trigger": False},
        {"query": "f2", "should_trigger": False},
    ]

    train_set, test_set = run_loop.split_eval_set(eval_set, holdout=0.5, seed=1)

    assert len(train_set) == 2
    assert len(test_set) == 2
    assert {item["should_trigger"] for item in test_set} == {True, False}


def test_run_loop_all_passed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skill_path = tmp_path / "skill"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    run_calls: list[str] = []

    monkeypatch.setattr(run_loop, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr(run_loop, "parse_skill_md", lambda path: ("alpha", "orig desc", "content"))
    monkeypatch.setattr(run_loop, "split_eval_set", lambda eval_set, holdout, seed=42: ([{"query": "train", "should_trigger": True}], [{"query": "test", "should_trigger": False}]))
    monkeypatch.setattr(
        run_loop,
        "run_eval",
        lambda **kwargs: {
            "results": [
                {
                    "query": "q1",
                    "should_trigger": True,
                    "trigger_rate": 1.0,
                    "triggers": 1,
                    "runs": 1,
                    "pass": True,
                }
            ],
            "summary": {"passed": 1, "failed": 0, "total": 1},
        },
    )
    monkeypatch.setattr(run_loop, "improve_description", lambda *args, **kwargs: run_calls.append("called") or "new desc")

    result = run_loop.run_loop(
        eval_set=[{"query": "q1", "should_trigger": True}],
        skill_path=skill_path,
        description_override=None,
        loop_cfg=_make_loop_cfg(tmp_path, max_iterations=3, holdout=0.0),
    )

    assert result["exit_reason"] == "all_passed (iteration 1)"
    assert run_calls == []


def test_run_loop_hits_max_iterations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skill_path = tmp_path / "skill"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    run_count = {"value": 0}
    train_set = [{"query": "train", "should_trigger": True}]
    test_set = [{"query": "test", "should_trigger": False}]

    monkeypatch.setattr(run_loop, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr(run_loop, "parse_skill_md", lambda path: ("alpha", "orig desc", "content"))
    monkeypatch.setattr(run_loop, "split_eval_set", lambda eval_set, holdout, seed=42: (train_set, test_set))

    def fake_run_eval(**kwargs):  # noqa: ANN001
        run_count["value"] += 1
        if run_count["value"] == 1:
            return {
                "results": [
                    {
                        "query": "train",
                        "should_trigger": True,
                        "trigger_rate": 0.0,
                        "triggers": 0,
                        "runs": 1,
                        "pass": False,
                    },
                    {
                        "query": "test",
                        "should_trigger": False,
                        "trigger_rate": 1.0,
                        "triggers": 1,
                        "runs": 1,
                        "pass": False,
                    },
                ],
                "summary": {"passed": 0, "failed": 2, "total": 2},
            }
        return {
            "results": [
                {
                    "query": "train",
                    "should_trigger": True,
                    "trigger_rate": 1.0,
                    "triggers": 1,
                    "runs": 1,
                    "pass": True,
                },
                {
                    "query": "test",
                    "should_trigger": False,
                    "trigger_rate": 0.0,
                    "triggers": 0,
                    "runs": 1,
                    "pass": True,
                },
            ],
            "summary": {"passed": 2, "failed": 0, "total": 2},
        }

    monkeypatch.setattr(run_loop, "run_eval", fake_run_eval)
    monkeypatch.setattr(run_loop, "improve_description", lambda *args, **kwargs: "improved")

    result = run_loop.run_loop(
        eval_set=[
            {"query": "train", "should_trigger": True},
            {"query": "test", "should_trigger": False},
        ],
        skill_path=skill_path,
        description_override=None,
        loop_cfg=_make_loop_cfg(tmp_path, max_iterations=1, holdout=0.5, log_dir=tmp_path / "logs"),
    )

    assert result["exit_reason"] == "max_iterations (1)"


def test_run_loop_improves_description_and_chooses_best_test_score(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skill_path = tmp_path / "skill"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    train_set = [{"query": "train", "should_trigger": True}]
    test_set = [{"query": "test", "should_trigger": False}]
    run_count = {"value": 0}
    improved: list[str] = []

    monkeypatch.setattr(run_loop, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr(run_loop, "parse_skill_md", lambda path: ("alpha", "orig desc", "content"))
    monkeypatch.setattr(run_loop, "split_eval_set", lambda eval_set, holdout, seed=42: (train_set, test_set))

    def fake_run_eval(**kwargs):  # noqa: ANN001
        run_count["value"] += 1
        if run_count["value"] == 1:
            return {
                "results": [
                    {
                        "query": "train",
                        "should_trigger": True,
                        "trigger_rate": 0.0,
                        "triggers": 0,
                        "runs": 1,
                        "pass": False,
                    },
                    {
                        "query": "test",
                        "should_trigger": False,
                        "trigger_rate": 1.0,
                        "triggers": 1,
                        "runs": 1,
                        "pass": False,
                    },
                ],
                "summary": {"passed": 0, "failed": 2, "total": 2},
            }
        return {
            "results": [
                {
                    "query": "train",
                    "should_trigger": True,
                    "trigger_rate": 1.0,
                    "triggers": 1,
                    "runs": 1,
                    "pass": True,
                },
                {
                    "query": "test",
                    "should_trigger": False,
                    "trigger_rate": 0.0,
                    "triggers": 0,
                    "runs": 1,
                    "pass": True,
                },
            ],
            "summary": {"passed": 2, "failed": 0, "total": 2},
        }

    monkeypatch.setattr(run_loop, "run_eval", fake_run_eval)
    monkeypatch.setattr(
        run_loop,
        "improve_description",
        lambda ctx, **kwargs: improved.append(ctx.current_description) or "improved desc",
    )

    result = run_loop.run_loop(
        eval_set=[
            {"query": "train", "should_trigger": True},
            {"query": "test", "should_trigger": False},
        ],
        skill_path=skill_path,
        description_override=None,
        loop_cfg=_make_loop_cfg(tmp_path, max_iterations=2, holdout=0.5, log_dir=tmp_path / "logs"),
    )

    assert result["exit_reason"] == "all_passed (iteration 2)"
    assert result["best_description"] == "improved desc"
    assert improved == ["orig desc"]


def test_run_loop_main_exits_when_skill_md_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    eval_file = tmp_path / "eval.json"
    eval_file.write_text("[]", encoding="utf-8")
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_loop.py",
            "--eval-set",
            str(eval_file),
            "--skill-path",
            str(skill_dir),
            "--model",
            "sonnet",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        run_loop.main()

    assert excinfo.value.code == 1
    assert "SKILL.md" in capsys.readouterr().err


def test_run_loop_main_writes_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    eval_file = tmp_path / "eval.json"
    eval_file.write_text(json.dumps([{"query": "q1", "should_trigger": True}]), encoding="utf-8")
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    results_dir = tmp_path / "results"
    monkeypatch.setattr(
        run_loop,
        "run_loop",
        lambda **kwargs: {
            "exit_reason": "all_passed (iteration 1)",
            "original_description": "orig desc",
            "best_description": "desc",
            "best_score": "1/1",
            "best_train_score": "1/1",
            "best_test_score": None,
            "final_description": "desc",
            "iterations_run": 1,
            "holdout": 0.0,
            "train_size": 1,
            "test_size": 0,
            "history": [],
        },
    )
    monkeypatch.setattr(run_loop.time, "strftime", lambda fmt: "2026-01-01_000000")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_loop.py",
            "--eval-set",
            str(eval_file),
            "--skill-path",
            str(skill_dir),
            "--model",
            "sonnet",
            "--results-dir",
            str(results_dir),
        ],
    )

    run_loop.main()

    captured = capsys.readouterr()
    assert '"exit_reason": "all_passed (iteration 1)"' in captured.out
    assert "結果を保存しました" in captured.err
    assert (results_dir / "2026-01-01_000000" / "results.json").exists()


def test_run_loop_verbose_prints_train_and_test_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    skill_path = tmp_path / "skill"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text("# Skill\n", encoding="utf-8")

    monkeypatch.setattr(run_loop, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr(run_loop, "parse_skill_md", lambda path: ("alpha", "orig desc", "content"))
    monkeypatch.setattr(
        run_loop,
        "split_eval_set",
        lambda eval_set, holdout, seed=42: ([{"query": "train", "should_trigger": True}], [{"query": "test", "should_trigger": False}]),
    )
    monkeypatch.setattr(
        run_loop,
        "run_eval",
        lambda **kwargs: {
            "results": [
                {
                    "query": "train",
                    "should_trigger": True,
                    "trigger_rate": 0.0,
                    "triggers": 0,
                    "runs": 1,
                    "pass": False,
                },
                {
                    "query": "test",
                    "should_trigger": False,
                    "trigger_rate": 1.0,
                    "triggers": 1,
                    "runs": 1,
                    "pass": False,
                },
            ],
            "summary": {"passed": 0, "failed": 2, "total": 2},
        },
    )

    result = run_loop.run_loop(
        eval_set=[
            {"query": "train", "should_trigger": True},
            {"query": "test", "should_trigger": False},
        ],
        skill_path=skill_path,
        description_override=None,
        loop_cfg=_make_loop_cfg(tmp_path, max_iterations=1, holdout=0.5, verbose=True),
    )

    err = capsys.readouterr().err
    assert result["exit_reason"] == "max_iterations (1)"
    assert "分割:" in err
    assert "学習用:" in err
    assert "検証用:" in err
    assert "Max iterations reached" in err



def test_run_single_query_covers_blank_lines_stop_and_skill_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_single_query の残り stream 分岐を通す。"""
    project_root = tmp_path / "project"
    project_root.mkdir()
    process = _FakeProcess()
    read_calls = iter([b"\n", b""])
    select_calls = {"count": 0}

    def fake_select(r, w, x, timeout=0):  # noqa: ANN001
        select_calls["count"] += 1
        if select_calls["count"] == 1:
            return ([], [], [])
        return (r, [], [])

    monkeypatch.setattr(run_eval.uuid, "uuid4", lambda: SimpleNamespace(hex="12345678abcdef"))
    monkeypatch.setattr(run_eval.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(run_eval.select, "select", fake_select)
    monkeypatch.setattr(run_eval.os, "read", lambda fd, size: next(read_calls))

    result = run_eval.run_single_query("query", "alpha", "skill description", SingleQueryConfig(timeout=5, project_root=str(project_root), model=None))
    assert result is False

    skill_event = (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "ignore"},
                        {
                            "type": "tool_use",
                            "name": "Skill",
                            "input": {"skill": "/tmp/alpha-skill-12345678.md"},
                        },
                    ]
                },
            }
        )
        + "\n"
    ).encode("utf-8")
    read_calls = iter([skill_event])
    process = _FakeProcess()
    monkeypatch.setattr(run_eval.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(run_eval.select, "select", lambda r, w, x, timeout=0: (r, [], []))
    monkeypatch.setattr(run_eval.os, "read", lambda fd, size: next(read_calls))

    result = run_eval.run_single_query("query", "alpha", "skill description", SingleQueryConfig(timeout=5, project_root=str(project_root), model=None))
    assert result is True


def test_run_single_query_content_block_stop_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """content_block_stop の早期終了分岐を通す。"""
    project_root = tmp_path / "project"
    project_root.mkdir()
    process = _FakeProcess()
    read_calls = iter(
        [
            (
                json.dumps(
                    {
                        "type": "stream_event",
                        "event": {
                            "type": "content_block_start",
                            "content_block": {"type": "tool_use", "name": "Skill"},
                        },
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "stream_event",
                        "event": {"type": "content_block_stop"},
                    }
                )
                + "\n"
            ).encode("utf-8")
        ]
    )

    monkeypatch.setattr(run_eval.uuid, "uuid4", lambda: SimpleNamespace(hex="12345678abcdef"))
    monkeypatch.setattr(run_eval.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(run_eval.select, "select", lambda r, w, x, timeout=0: (r, [], []))
    monkeypatch.setattr(run_eval.os, "read", lambda fd, size: next(read_calls))

    result = run_eval.run_single_query("query", "alpha", "skill description", SingleQueryConfig(timeout=5, project_root=str(project_root), model=None))
    assert result is False


def test_run_loop_verbose_improvement_and_all_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """verbose で改善パスと all_passed パスを通す。"""
    skill_path = tmp_path / "skill"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text("# Skill\n", encoding="utf-8")

    run_count = {"value": 0}
    train_set = [{"query": "train", "should_trigger": True}]
    test_set = [{"query": "test", "should_trigger": False}]

    monkeypatch.setattr(run_loop, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr(run_loop, "parse_skill_md", lambda path: ("alpha", "orig desc", "content"))
    monkeypatch.setattr(run_loop, "split_eval_set", lambda eval_set, holdout, seed=42: (train_set, test_set))

    def fake_run_eval(**kwargs):  # noqa: ANN001
        run_count["value"] += 1
        if run_count["value"] == 1:
            return {
                "results": [
                    {
                        "query": "train",
                        "should_trigger": True,
                        "trigger_rate": 0.0,
                        "triggers": 0,
                        "runs": 1,
                        "pass": False,
                    },
                    {
                        "query": "test",
                        "should_trigger": False,
                        "trigger_rate": 1.0,
                        "triggers": 1,
                        "runs": 1,
                        "pass": False,
                    },
                ],
                "summary": {"passed": 0, "failed": 2, "total": 2},
            }
        return {
            "results": [
                {
                    "query": "train",
                    "should_trigger": True,
                    "trigger_rate": 1.0,
                    "triggers": 1,
                    "runs": 1,
                    "pass": True,
                },
                {
                    "query": "test",
                    "should_trigger": False,
                    "trigger_rate": 0.0,
                    "triggers": 0,
                    "runs": 1,
                    "pass": True,
                },
            ],
            "summary": {"passed": 2, "failed": 0, "total": 2},
        }

    monkeypatch.setattr(run_loop, "run_eval", fake_run_eval)
    monkeypatch.setattr(run_loop, "improve_description", lambda ctx, **kwargs: "improved desc")

    result = run_loop.run_loop(
        eval_set=[{"query": "train", "should_trigger": True}, {"query": "test", "should_trigger": False}],
        skill_path=skill_path,
        description_override=None,
        loop_cfg=_make_loop_cfg(tmp_path, max_iterations=2, holdout=0.5, verbose=True),
    )

    err = capsys.readouterr().err
    assert result["exit_reason"] == "all_passed (iteration 2)"
    assert "説明を改善しています" in err
    assert "提案結果" in err
    assert "All train queries passed on iteration 2!" in err


def test_run_loop_main_entrypoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """__main__ entrypoint を通す。"""
    monkeypatch.setattr(sys, "argv", ["run_loop.py", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("deepblue.skills.run_loop", run_name="__main__")

    assert excinfo.value.code == 0


def test_build_query_cmd_non_claude() -> None:
    """claude 以外のバイナリは json 出力形式で組み立てる。"""
    from deepblue.skills.run_eval import _build_query_cmd

    cmd = _build_query_cmd("codex", "q", None)
    assert "--verbose" not in cmd
    assert "json" in cmd


def test_process_stream_event_non_tool_use_block() -> None:
    """tool_use 以外の content_block は None を返す。"""
    from deepblue.skills.run_eval import _process_stream_event

    pending: list = [None]
    acc: list = [""]
    result = _process_stream_event(
        {"type": "content_block_start", "content_block": {"type": "text"}}, "name", pending, acc
    )
    assert result is None


def test_process_stream_event_delta_without_match() -> None:
    """delta に対象名が含まれなければ None を返す。"""
    from deepblue.skills.run_eval import _process_stream_event

    pending: list = ["Skill"]
    acc: list = [""]
    result = _process_stream_event(
        {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "x"}},
        "target",
        pending,
        acc,
    )
    assert result is None


def test_process_event_line_unknown_type() -> None:
    """assistant/result 以外のイベントは None を返す。"""
    from deepblue.skills.run_eval import _process_event_line

    result = _process_event_line('{"type": "other"}', "name", [None], [""], [False])
    assert result is None


def test_process_stream_event_delta_non_json_delta() -> None:
    """delta が input_json_delta 以外なら None。"""
    from deepblue.skills.run_eval import _process_stream_event

    assert _process_stream_event(
        {"type": "content_block_delta", "delta": {"type": "other"}}, "n", ["Skill"], [""]
    ) is None


def test_process_stream_event_block_stop_without_pending() -> None:
    """pending 無しの content_block_stop は None。"""
    from deepblue.skills.run_eval import _process_stream_event

    assert _process_stream_event({"type": "content_block_stop"}, "n", [None], [""]) is None


def test_process_stream_event_unknown_type() -> None:
    """未知の stream event タイプは None。"""
    from deepblue.skills.run_eval import _process_stream_event

    assert _process_stream_event({"type": "unknown"}, "n", [None], [""]) is None


def test_process_event_line_assistant_read_no_match() -> None:
    """assistant の Read が対象に一致しなければトリガーしない。"""
    from deepblue.skills.run_eval import _process_event_line

    line = '{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "other"}}]}}'
    assert _process_event_line(line, "target", [None], [""], [False]) is False


def test_run_eval_main_without_verbose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--verbose 無しでは進捗を stderr に出さず JSON のみ出力する。"""
    eval_file = tmp_path / "eval.json"
    eval_file.write_text(json.dumps([{"query": "q1", "should_trigger": True}]), encoding="utf-8")
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    monkeypatch.setattr(run_eval, "parse_skill_md", lambda path: ("alpha", "desc", "content"))
    monkeypatch.setattr(run_eval, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        run_eval,
        "run_eval",
        lambda **kwargs: {
            "skill_name": "alpha",
            "description": "desc",
            "results": [{"query": "q1", "should_trigger": True, "trigger_rate": 1.0, "triggers": 1, "runs": 1, "pass": True}],
            "summary": {"total": 1, "passed": 1, "failed": 0},
        },
    )
    monkeypatch.setattr(sys, "argv", ["run_eval.py", "--eval-set", str(eval_file), "--skill-path", str(skill_dir)])
    run_eval.main()
    captured = capsys.readouterr()
    assert '"skill_name": "alpha"' in captured.out
    assert "評価中" not in captured.err


def test_run_single_query_no_remaining_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """プロセス終了済みで残り出力が空でも処理できる。"""
    process = _PollingProcess(b"")
    monkeypatch.setattr(run_eval.uuid, "uuid4", lambda: SimpleNamespace(hex="abc"))
    monkeypatch.setattr(run_eval.subprocess, "Popen", lambda *a, **k: process)
    result = run_eval.run_single_query(
        "q", "alpha", "desc", SingleQueryConfig(timeout=5, project_root=str(tmp_path), model=None)
    )
    assert result is False


def test_run_single_query_timeout_immediately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """timeout=0 ではループに入らず終了する。"""

    class _NeverExits:
        def __init__(self):
            self.stdout = __import__("io").BytesIO(b"")
            self.killed = False
            self.waited = False

        def poll(self):
            return None

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            self.waited = True

    monkeypatch.setattr(run_eval.uuid, "uuid4", lambda: SimpleNamespace(hex="abc"))
    monkeypatch.setattr(run_eval.subprocess, "Popen", lambda *a, **k: _NeverExits())
    result = run_eval.run_single_query(
        "q", "alpha", "desc", SingleQueryConfig(timeout=0, project_root=str(tmp_path), model=None)
    )
    assert result is False


def test_run_loop_main_without_results_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--results-dir 無しではファイルを書かず JSON のみ出力する。"""
    eval_file = tmp_path / "eval.json"
    eval_file.write_text(json.dumps([{"query": "q1", "should_trigger": True}]), encoding="utf-8")
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    monkeypatch.setattr(
        run_loop,
        "run_loop",
        lambda **kwargs: {
            "exit_reason": "done", "original_description": "o", "best_description": "d",
            "best_score": "1/1", "best_train_score": "1/1", "best_test_score": None,
            "final_description": "d", "iterations_run": 1, "holdout": 0.0,
            "train_size": 1, "test_size": 0, "history": [],
        },
    )
    monkeypatch.setattr(sys, "argv", ["run_loop.py", "--eval-set", str(eval_file), "--skill-path", str(skill_dir), "--model", "sonnet"])
    run_loop.main()
    assert '"exit_reason": "done"' in capsys.readouterr().out


def test_run_loop_verbose_no_holdout_reaches_max(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """verbose かつ holdout 無しで失敗が続けば最大反復まで回る。"""
    skill_path = tmp_path / "skill"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    monkeypatch.setattr(run_loop, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr(run_loop, "parse_skill_md", lambda path: ("alpha", "orig desc", "content"))
    monkeypatch.setattr(
        run_loop,
        "run_eval",
        lambda **kwargs: {
            "results": [{"query": "q", "should_trigger": True, "trigger_rate": 0.0, "triggers": 0, "runs": 1, "pass": False}],
            "summary": {"passed": 0, "failed": 1, "total": 1},
        },
    )
    monkeypatch.setattr(run_loop, "improve_description", lambda *a, **k: "new desc")
    result = run_loop.run_loop(
        eval_set=[{"query": "q", "should_trigger": True}],
        skill_path=skill_path,
        description_override=None,
        loop_cfg=_make_loop_cfg(tmp_path, max_iterations=2, holdout=0.0, verbose=True),
    )
    assert result["iterations_run"] == 2


def test_run_loop_single_iteration_no_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """max_iterations=1 で失敗のまま反復が尽きてループ終了する。"""
    skill_path = tmp_path / "skill"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    monkeypatch.setattr(run_loop, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr(run_loop, "parse_skill_md", lambda path: ("alpha", "orig desc", "content"))
    monkeypatch.setattr(
        run_loop,
        "run_eval",
        lambda **kwargs: {
            "results": [{"query": "q", "should_trigger": True, "trigger_rate": 0.0, "triggers": 0, "runs": 1, "pass": False}],
            "summary": {"passed": 0, "failed": 1, "total": 1},
        },
    )
    monkeypatch.setattr(run_loop, "improve_description", lambda *a, **k: "new desc")
    result = run_loop.run_loop(
        eval_set=[{"query": "q", "should_trigger": True}],
        skill_path=skill_path,
        description_override=None,
        loop_cfg=_make_loop_cfg(tmp_path, max_iterations=1, holdout=0.0),
    )
    assert result["iterations_run"] == 1
