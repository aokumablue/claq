import pytest

from bluecore.hooks.insights_security_monitor import extract_content


@pytest.mark.parametrize("tool_name", ["Bash", "Write", "Edit", "MultiEdit"])
def test_extract_content_preserves_raw_string_tool_input(tool_name: str) -> None:
    raw_input = "raw tool payload"

    text, context = extract_content({"tool_name": tool_name, "tool_input": raw_input})

    assert text == raw_input
    assert context == f"{tool_name.lower()}:raw"


@pytest.mark.parametrize(
    ("data", "expected_text", "expected_context"),
    [
        ({"tool_name": "Bash", "tool_input": {"command": "bundle exec rake test"}}, "bundle exec rake test", "bash:bundle exec rake test"),
        ({"tool_name": "Write", "tool_input": {"content": "new file", "file_path": "lib/example.py"}}, "new file", "file:lib/example.py"),
        ({"tool_name": "Edit", "tool_input": {"new_string": "replacement", "file_path": "lib/example.py"}}, "replacement", "file:lib/example.py"),
        ({"tool_name": "MultiEdit", "tool_input": {"edits": [{"new_string": "first"}, {"new_string": "second"}], "file_path": "lib/example.py"}}, "first\nsecond", "file:lib/example.py"),
    ],
)
def test_extract_content_preserves_structured_tool_input(
    data: dict[str, object], expected_text: str, expected_context: str
) -> None:
    text, context = extract_content(data)

    assert text == expected_text
    assert context == expected_context
