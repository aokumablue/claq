from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path

from bluecore.mem import settings as settings_mod
from bluecore.mem.cli_record_handlers import RecordDeps, handle_record_project_profile
from bluecore.mem.database import Database
from bluecore.mem.models import ProjectProfile
from bluecore.mem.settings import Settings


class _Logger:
    def info(self, _message: str, *_args: object) -> None:
        return

    def warning(self, _message: str, *_args: object) -> None:
        return


@contextmanager
def _open_db(settings: Settings):
    db = Database(settings.db_path)
    try:
        yield db
    finally:
        db.close()


def _get_project(stdin_data: dict[str, object]) -> str:
    return Path(str(stdin_data.get("cwd", "/tmp/bluecore"))).name


def _build_deps() -> RecordDeps:
    return RecordDeps(
        open_db=_open_db,
        get_project=_get_project,
        log=_Logger(),
        get_git_user_name=lambda: "tester",
    )


def test_record_project_profile_preserves_existing_fields_when_input_is_sparse(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings_mod, "_DEFAULT_DATA_DIR", tmp_path)
    monkeypatch.setattr("bluecore.mem.cli_record_handlers.detect_harness", lambda: "copilot")
    settings = Settings()
    deps = _build_deps()
    now = int(time.time())

    with _open_db(settings) as db:
        db.upsert_project_profile(
            ProjectProfile(
                project="bluecore",
                detected_at_epoch=now,
                last_updated_epoch=now,
                origin_user="tester",
                project_path="/repo/bluecore",
                languages=["python"],
                frameworks=["pytest"],
                primary_language="python",
                test_command="pytest -q",
                build_command="python -m build",
                scope_hint="global",
            )
        )

    handle_record_project_profile(settings, {"cwd": "/repo/bluecore"}, deps)

    with _open_db(settings) as db:
        profile = db.get_project_profile("bluecore", origin_user="tester")

    assert profile is not None
    assert profile.project_path == "/repo/bluecore"
    assert profile.languages == ["python"]
    assert profile.frameworks == ["pytest"]
    assert profile.primary_language == "python"
    assert profile.test_command == "pytest -q"
    assert profile.build_command == "python -m build"
    assert profile.scope_hint == "global"
    assert profile.detected_at_epoch == now


def test_record_project_profile_non_list_field_becomes_empty_list(monkeypatch, tmp_path) -> None:
    """languages が list 型でない場合は空リストとして保存される（型不正の fail-safe）。"""
    monkeypatch.setattr(settings_mod, "_DEFAULT_DATA_DIR", tmp_path)
    settings = Settings()
    deps = _build_deps()

    handle_record_project_profile(
        settings,
        {"cwd": "/repo/bluecore", "languages": "python-not-a-list"},
        deps,
    )

    with _open_db(settings) as db:
        profile = db.get_project_profile("bluecore", origin_user="tester")

    assert profile is not None
    assert profile.languages == []


def test_record_project_profile_updates_fields_when_values_are_provided(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings_mod, "_DEFAULT_DATA_DIR", tmp_path)
    monkeypatch.setattr("bluecore.mem.cli_record_handlers.detect_harness", lambda: "copilot")
    settings = Settings()
    deps = _build_deps()

    handle_record_project_profile(
        settings,
        {
            "cwd": "/repo/bluecore",
            "project_path": "/repo/bluecore",
            "languages": ["ruby", "python"],
            "frameworks": ["rails"],
            "primary_language": "ruby",
            "test_command": "bundle exec rake test",
            "build_command": "bundle exec rake assets:precompile",
            "scope_hint": "project",
        },
        deps,
    )

    with _open_db(settings) as db:
        profile = db.get_project_profile("bluecore", origin_user="tester")

    assert profile is not None
    assert profile.project_path == "/repo/bluecore"
    assert profile.languages == ["ruby", "python"]
    assert profile.frameworks == ["rails"]
    assert profile.primary_language == "ruby"
    assert profile.test_command == "bundle exec rake test"
    assert profile.build_command == "bundle exec rake assets:precompile"
    assert profile.scope_hint == "project"
