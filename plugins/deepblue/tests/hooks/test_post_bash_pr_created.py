"""post:bash PR 作成通知フックのテスト。"""

from __future__ import annotations

import json
import runpy

import pytest

from devgear.hooks import post_bash_pr_created as pr_created


@pytest.mark.parametrize(
    ("service", "command", "output", "expected_label", "expected_review_command"),
    [
        (
            "github",
            "gh pr create",
            "https://github.com/owner/repo/pull/12",
            "Pull Request",
            "gh pr review 12 --repo owner/repo",
        ),
        (
            "gitlab",
            "glab mr create",
            "https://gitlab.com/group/subgroup/repo/-/merge_requests/34",
            "Merge Request",
            "glab mr view 34 --repo group/subgroup/repo",
        ),
    ],
)
def test_evaluate_reports_host_specific_commands(
    monkeypatch: pytest.MonkeyPatch,
    service: str,
    command: str,
    output: str,
    expected_label: str,
    expected_review_command: str,
) -> None:
    messages: list[str] = []
    monkeypatch.setattr(pr_created, "write_stderr", messages.append)

    payload = json.dumps(
        {
            "tool_input": {"command": command},
            "tool_output": {"output": output},
        }
    )

    assert pr_created.evaluate(payload, service=service) == payload
    assert any(expected_label in message for message in messages)
    assert any(expected_review_command in message for message in messages)


def test_main_entrypoint_exits_zero_and_writes_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("devgear.hooks.hook_common.read_raw_stdin", lambda: "{}")
    outputs: list[str] = []
    monkeypatch.setattr("devgear.hooks.hook_common.write_stdout", outputs.append)
    monkeypatch.setattr("devgear.hooks.hook_common.write_stderr", lambda message: outputs.append(message))

    with pytest.raises(SystemExit) as exc:
        runpy.run_module("devgear.hooks.post_bash_pr_created", run_name="__main__")

    assert exc.value.code == 0
    assert outputs == ["{}"]
