"""s-learn の CLI を検証するテスト。

対象:
  - parse_instinct_file() — 内容保持と境界ケース
  - _validate_file_path() — パストラバーサル遮断
  - detect_project() — Git/環境変数をモックしたプロジェクト検出
  - load_all_instincts() — project + global ディレクトリからの読み込みと重複排除
  - _load_instincts_from_dir() — ディレクトリ走査
  - cmd_projects() — レジストリからのプロジェクト一覧表示
  - cmd_status() — ステータス表示
  - _promote_specific() — 単一 instinct の昇格
  - _promote_auto() — 複数プロジェクト横断の自動昇格
"""

import argparse
import builtins
import importlib
import io
import json
import os
import runpy
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from devgear.skills.learn import cli as _mod

parse_instinct_file = _mod.parse_instinct_file
_validate_file_path = _mod._validate_file_path
detect_project = _mod.detect_project
load_all_instincts = _mod.load_all_instincts
load_project_only_instincts = _mod.load_project_only_instincts
_load_instincts_from_dir = _mod._load_instincts_from_dir
cmd_status = _mod.cmd_status
cmd_projects = _mod.cmd_projects
_promote_specific = _mod._promote_specific
_promote_auto = _mod._promote_auto
_find_cross_project_instincts = _mod._find_cross_project_instincts
load_registry = _mod.load_registry
_validate_instinct_id = _mod._validate_instinct_id
_update_registry = _mod._update_registry


# ─────────────────────────────────────────────
# フィクスチャ
# ─────────────────────────────────────────────

SAMPLE_INSTINCT_YAML = """\
---
id: test-instinct
trigger: "when writing tests"
confidence: 0.8
domain: testing
scope: project
---

## Action
Always write tests first.

## Evidence
TDD leads to better design.
"""

SAMPLE_GLOBAL_INSTINCT_YAML = """\
---
id: global-instinct
trigger: "always"
confidence: 0.9
domain: security
scope: global
---

## Action
Validate all user input.
"""


@pytest.fixture
def project_tree(tmp_path):
    """テスト用に実運用に近いプロジェクトディレクトリ構造を作成する。"""
    devgear_dir = tmp_path / ".devgear"
    projects_dir = devgear_dir / "projects"
    global_personal = devgear_dir / "instincts" / "personal"
    global_inherited = devgear_dir / "instincts" / "inherited"
    global_evolved = devgear_dir / "evolved"

    for d in [
        global_personal,
        global_inherited,
        global_evolved / "skills",
        global_evolved / "commands",
        global_evolved / "agents",
        projects_dir,
    ]:
        d.mkdir(parents=True, exist_ok=True)

    return {
        "root": tmp_path,
        "devgear": devgear_dir,
        "projects_dir": projects_dir,
        "global_personal": global_personal,
        "global_inherited": global_inherited,
        "global_evolved": global_evolved,
        "registry_file": devgear_dir / "projects.json",
    }


@pytest.fixture
def global_project(patch_globals):
    """グローバルスコープのプロジェクト辞書フィクスチャ。"""
    tree = patch_globals
    return {
        "id": "global",
        "name": "global",
        "root": "",
        "project_dir": tree["devgear"],
        "instincts_personal": tree["global_personal"],
        "instincts_inherited": tree["global_inherited"],
        "evolved_dir": tree["global_evolved"],
        "observations_file": tree["devgear"] / "observations.jsonl",
    }


@pytest.fixture
def patch_globals(project_tree, monkeypatch):
    """モジュールレベルのグローバル変数を tmp_path 基準のディレクトリに差し替える。"""
    monkeypatch.setattr(_mod, "DEVGEAR_DIR", project_tree["devgear"])
    monkeypatch.setattr(_mod, "PROJECTS_DIR", project_tree["projects_dir"])
    monkeypatch.setattr(_mod, "REGISTRY_FILE", project_tree["registry_file"])
    monkeypatch.setattr(_mod, "GLOBAL_INSTINCTS_DIR", project_tree["devgear"] / "instincts")
    monkeypatch.setattr(_mod, "GLOBAL_PERSONAL_DIR", project_tree["global_personal"])
    monkeypatch.setattr(_mod, "GLOBAL_INHERITED_DIR", project_tree["global_inherited"])
    monkeypatch.setattr(_mod, "GLOBAL_EVOLVED_DIR", project_tree["global_evolved"])
    monkeypatch.setattr(_mod, "GLOBAL_OBSERVATIONS_FILE", project_tree["devgear"] / "observations.jsonl")
    return project_tree


def _make_project(tree, pid="abc123", pname="test-project"):
    """プロジェクトのディレクトリ構造を作成し、project 辞書を返す。"""
    project_dir = tree["projects_dir"] / pid
    personal_dir = project_dir / "instincts" / "personal"
    inherited_dir = project_dir / "instincts" / "inherited"
    for d in [
        personal_dir,
        inherited_dir,
        project_dir / "evolved" / "skills",
        project_dir / "evolved" / "commands",
        project_dir / "evolved" / "agents",
        project_dir / "observations.archive",
    ]:
        d.mkdir(parents=True, exist_ok=True)

    return {
        "id": pid,
        "name": pname,
        "root": str(tree["root"] / "fake-repo"),
        "remote": "https://github.com/test/test-project.git",
        "project_dir": project_dir,
        "instincts_personal": personal_dir,
        "instincts_inherited": inherited_dir,
        "evolved_dir": project_dir / "evolved",
        "observations_file": project_dir / "observations.jsonl",
    }


def _make_instincts(extra: list[dict] | None = None) -> list[dict]:
    """cmd_evolve テスト用の基本 instinct リストを生成する。

    extra に追加 instinct を渡すと末尾に結合して返す。
    """
    base = [
        {"id": "skill-1", "trigger": "when coding", "confidence": 0.9, "domain": "general", "scope": "project", "content": "A", "_scope_label": "project"},
        {"id": "skill-2", "trigger": "when coding", "confidence": 0.85, "domain": "general", "scope": "project", "content": "B", "_scope_label": "project"},
        {"id": "skill-3", "trigger": "when coding", "confidence": 0.8, "domain": "general", "scope": "project", "content": "C", "_scope_label": "project"},
    ]
    return base + (extra or [])


# ─────────────────────────────────────────────
# parse_instinct_file のテスト
# ─────────────────────────────────────────────

MULTI_SECTION = """\
---
id: instinct-a
trigger: "when coding"
confidence: 0.9
domain: general
---

## Action
Do thing A.

## Examples
- Example A1

---
id: instinct-b
trigger: "when testing"
confidence: 0.7
domain: testing
---

## Action
Do thing B.
"""


def test_multiple_instincts_preserve_content():
    result = parse_instinct_file(MULTI_SECTION)
    assert len(result) == 2
    assert "Do thing A." in result[0]["content"]
    assert "Example A1" in result[0]["content"]
    assert "Do thing B." in result[1]["content"]


def test_single_instinct_preserves_content():
    content = """\
---
id: solo
trigger: "when reviewing"
confidence: 0.8
domain: review
---

## Action
Check for security issues.

## Evidence
Prevents vulnerabilities.
"""
    result = parse_instinct_file(content)
    assert len(result) == 1
    assert "Check for security issues." in result[0]["content"]
    assert "Prevents vulnerabilities." in result[0]["content"]


def test_empty_content_no_error():
    content = """\
---
id: empty
trigger: "placeholder"
confidence: 0.5
domain: general
---
"""
    result = parse_instinct_file(content)
    assert len(result) == 1
    assert result[0]["content"] == ""


def test_parse_no_id_skipped():
    """'id' フィールドを持たない instinct は黙って除外されるべき。"""
    content = """\
---
trigger: "when doing nothing"
confidence: 0.5
---

No id here.
"""
    result = parse_instinct_file(content)
    assert len(result) == 0


def test_parse_confidence_is_float():
    content = """\
---
id: float-check
trigger: "when parsing"
confidence: 0.42
domain: general
---

Body.
"""
    result = parse_instinct_file(content)
    assert isinstance(result[0]["confidence"], float)
    assert result[0]["confidence"] == pytest.approx(0.42)


def test_parse_trigger_strips_quotes():
    content = """\
---
id: quote-check
trigger: "when quoting"
confidence: 0.5
domain: general
---

Body.
"""
    result = parse_instinct_file(content)
    assert result[0]["trigger"] == "when quoting"


def test_parse_empty_string():
    result = parse_instinct_file("")
    assert result == []


def test_parse_garbage_input():
    result = parse_instinct_file("this is not yaml at all\nno frontmatter here")
    assert result == []


# ─────────────────────────────────────────────
# _validate_file_path のテスト
# ─────────────────────────────────────────────


def test_validate_normal_path(tmp_path):
    test_file = tmp_path / "test.yaml"
    test_file.write_text("hello")
    result = _validate_file_path(str(test_file), must_exist=True)
    assert result == test_file.resolve()


def test_validate_rejects_etc():
    with pytest.raises(ValueError, match="system directory"):
        _validate_file_path("/etc/passwd")


def test_validate_rejects_var_log():
    with pytest.raises(ValueError, match="system directory"):
        _validate_file_path("/var/log/syslog")


def test_validate_rejects_usr():
    with pytest.raises(ValueError, match="system directory"):
        _validate_file_path("/usr/local/bin/foo")


def test_validate_rejects_proc():
    with pytest.raises(ValueError, match="system directory"):
        _validate_file_path("/proc/self/status")


def test_validate_must_exist_fails(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        _validate_file_path(str(tmp_path / "nonexistent.yaml"), must_exist=True)


def test_validate_home_expansion(tmp_path):
    """チルダ展開が機能すること。"""
    result = _validate_file_path("~/test.yaml")
    assert str(result).startswith(str(Path.home()))


def test_validate_relative_path(tmp_path, monkeypatch):
    """相対パスが解決されること。"""
    monkeypatch.chdir(tmp_path)
    test_file = tmp_path / "rel.yaml"
    test_file.write_text("content")
    result = _validate_file_path("rel.yaml", must_exist=True)
    assert result == test_file.resolve()


# ─────────────────────────────────────────────
# detect_project のテスト
# ─────────────────────────────────────────────


def test_detect_project_global_fallback(patch_globals, monkeypatch, tmp_path):
    """Git も環境変数もない場合は cwd ベースの project を返すこと。"""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    # git が利用できない状況を再現するため subprocess.run をモック
    def mock_run(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr("subprocess.run", mock_run)

    project = detect_project()
    assert project["id"] != "global"
    assert project["root"] == str(tmp_path)
    assert project["name"] == tmp_path.name


def test_detect_project_from_env(patch_globals, monkeypatch, tmp_path):
    """CLAUDE_PROJECT_DIR 環境変数がプロジェクトルートとして使われること。"""
    fake_repo = tmp_path / "my-repo"
    fake_repo.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(fake_repo))

    # URL を返すように git remote をモック
    def mock_run(cmd, **kwargs):
        if "rev-parse" in cmd:
            return SimpleNamespace(returncode=0, stdout=str(fake_repo) + "\n", stderr="")
        if "get-url" in cmd:
            return SimpleNamespace(returncode=0, stdout="https://github.com/test/my-repo.git\n", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", mock_run)

    project = detect_project()
    assert project["id"] != "global"
    assert project["name"] == "my-repo"


def test_detect_project_git_timeout(patch_globals, monkeypatch):
    """Git タイムアウト時は cwd ベースの project へフォールバックすること。"""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    import subprocess as sp

    def mock_run(cmd, **kwargs):
        raise sp.TimeoutExpired(cmd, 5)

    monkeypatch.setattr("subprocess.run", mock_run)

    project = detect_project()
    assert project["id"] != "global"
    assert project["root"] == str(Path.cwd())


def test_detect_project_creates_directories(patch_globals, monkeypatch, tmp_path):
    """detect_project がプロジェクトディレクトリ構造を作成すること。"""
    fake_repo = tmp_path / "structured-repo"
    fake_repo.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(fake_repo))

    def mock_run(cmd, **kwargs):
        if "rev-parse" in cmd:
            return SimpleNamespace(returncode=0, stdout=str(fake_repo) + "\n", stderr="")
        if "get-url" in cmd:
            return SimpleNamespace(returncode=1, stdout="", stderr="no remote")
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", mock_run)

    project = detect_project()
    assert project["instincts_personal"].exists()
    assert project["instincts_inherited"].exists()
    assert (project["evolved_dir"] / "skills").exists()


# ─────────────────────────────────────────────
# _load_instincts_from_dir のテスト
# ─────────────────────────────────────────────


def test_load_from_empty_dir(tmp_path):
    result = _load_instincts_from_dir(tmp_path, "personal", "project")
    assert result == []


def test_load_from_nonexistent_dir(tmp_path):
    result = _load_instincts_from_dir(tmp_path / "does-not-exist", "personal", "project")
    assert result == []


def test_load_annotates_metadata(tmp_path):
    """読み込まれた instinct が _source_file / _source_type / _scope_label を持つこと。"""
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(SAMPLE_INSTINCT_YAML)

    result = _load_instincts_from_dir(tmp_path, "personal", "project")
    assert len(result) == 1
    assert result[0]["_source_file"] == str(yaml_file)
    assert result[0]["_source_type"] == "personal"
    assert result[0]["_scope_label"] == "project"


def test_load_defaults_scope_from_label(tmp_path):
    """フロントマターに 'scope' がない instinct では scope_label が既定値になること。"""
    no_scope_yaml = """\
---
id: no-scope
trigger: "test"
confidence: 0.5
domain: general
---

Body.
"""
    (tmp_path / "no-scope.yaml").write_text(no_scope_yaml)
    result = _load_instincts_from_dir(tmp_path, "inherited", "global")
    assert result[0]["scope"] == "global"


def test_load_preserves_explicit_scope(tmp_path):
    """フロントマターに scope が明示されている場合は保持されること。"""
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(SAMPLE_INSTINCT_YAML)

    result = _load_instincts_from_dir(tmp_path, "personal", "global")
    # フロントマターは scope: project、scope_label は global
    # 明示的な scope は保持されるべき（上書きしない）
    assert result[0]["scope"] == "project"


def test_load_handles_corrupt_file(tmp_path, capsys):
    """壊れた YAML ファイルは警告しつつ、クラッシュしないこと。"""
    # parse_instinct_file が空を返すファイル
    (tmp_path / "good.yaml").write_text(SAMPLE_INSTINCT_YAML)
    (tmp_path / "bad.yaml").write_text("not yaml\nno frontmatter")

    result = _load_instincts_from_dir(tmp_path, "personal", "project")
    # bad.yaml には有効な instinct（id）がないため、good.yaml のみが対象
    assert len(result) == 1
    assert result[0]["id"] == "test-instinct"


def test_load_supports_yml_extension(tmp_path):
    yml_file = tmp_path / "test.yml"
    yml_file.write_text(SAMPLE_INSTINCT_YAML)

    result = _load_instincts_from_dir(tmp_path, "personal", "project")
    ids = {i["id"] for i in result}
    assert "test-instinct" in ids


def test_load_supports_md_extension(tmp_path):
    md_file = tmp_path / "legacy-instinct.md"
    md_file.write_text(SAMPLE_INSTINCT_YAML)

    result = _load_instincts_from_dir(tmp_path, "personal", "project")
    ids = {i["id"] for i in result}
    assert "test-instinct" in ids


def test_load_instincts_from_dir_uses_utf8_encoding(tmp_path, monkeypatch):
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text("placeholder")
    calls = []

    def fake_read_text(self, *args, **kwargs):
        calls.append(kwargs.get("encoding"))
        return SAMPLE_INSTINCT_YAML

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    result = _load_instincts_from_dir(tmp_path, "personal", "project")
    assert result[0]["id"] == "test-instinct"
    assert calls == ["utf-8"]


# ─────────────────────────────────────────────
# load_all_instincts のテスト
# ─────────────────────────────────────────────


def test_load_all_project_and_global(patch_globals):
    """project と global の両ディレクトリから読み込まれること。"""
    tree = patch_globals
    project = _make_project(tree)

    # project instinct を書き込む
    (project["instincts_personal"] / "proj.yaml").write_text(SAMPLE_INSTINCT_YAML)
    # global instinct を書き込む
    (tree["global_personal"] / "glob.yaml").write_text(SAMPLE_GLOBAL_INSTINCT_YAML)

    result = load_all_instincts(project)
    ids = {i["id"] for i in result}
    assert "test-instinct" in ids
    assert "global-instinct" in ids


def test_load_all_project_overrides_global(patch_globals):
    """project と global で同じ ID の場合は project 側が優先されること。"""
    tree = patch_globals
    project = _make_project(tree)

    # 同じ ID だが信頼度が異なる
    proj_yaml = SAMPLE_INSTINCT_YAML.replace("id: test-instinct", "id: shared-id")
    proj_yaml = proj_yaml.replace("confidence: 0.8", "confidence: 0.9")
    glob_yaml = SAMPLE_GLOBAL_INSTINCT_YAML.replace("id: global-instinct", "id: shared-id")
    glob_yaml = glob_yaml.replace("confidence: 0.9", "confidence: 0.3")

    (project["instincts_personal"] / "shared.yaml").write_text(proj_yaml)
    (tree["global_personal"] / "shared.yaml").write_text(glob_yaml)

    result = load_all_instincts(project)
    shared = [i for i in result if i["id"] == "shared-id"]
    assert len(shared) == 1
    assert shared[0]["_scope_label"] == "project"
    assert shared[0]["confidence"] == 0.9


def test_load_all_global_only(patch_globals, global_project):
    """global プロジェクトでは global instinct のみ読み込まれること。"""
    tree = patch_globals
    (tree["global_personal"] / "glob.yaml").write_text(SAMPLE_GLOBAL_INSTINCT_YAML)

    result = load_all_instincts(global_project)
    assert len(result) == 1
    assert result[0]["id"] == "global-instinct"


def test_load_project_only_excludes_global(patch_globals):
    """load_project_only_instincts が global instinct を含まないこと。"""
    tree = patch_globals
    project = _make_project(tree)

    (project["instincts_personal"] / "proj.yaml").write_text(SAMPLE_INSTINCT_YAML)
    (tree["global_personal"] / "glob.yaml").write_text(SAMPLE_GLOBAL_INSTINCT_YAML)

    result = load_project_only_instincts(project)
    ids = {i["id"] for i in result}
    assert "test-instinct" in ids
    assert "global-instinct" not in ids


def test_load_project_only_global_fallback_loads_global(patch_globals, global_project):
    """global フォールバック時、project-only クエリでも global instinct を返すこと。"""
    tree = patch_globals
    (tree["global_personal"] / "glob.yaml").write_text(SAMPLE_GLOBAL_INSTINCT_YAML)

    result = load_project_only_instincts(global_project)
    assert len(result) == 1
    assert result[0]["id"] == "global-instinct"


def test_load_all_empty(patch_globals):
    """instinct がまったくない場合は空リストを返すこと。"""
    tree = patch_globals
    project = _make_project(tree)

    result = load_all_instincts(project)
    assert result == []


# ─────────────────────────────────────────────
# cmd_status のテスト
# ─────────────────────────────────────────────


def test_cmd_status_no_instincts(patch_globals, monkeypatch, capsys):
    """instinct がない状態で status を実行するとフォールバックメッセージが表示されること。"""
    tree = patch_globals
    project = _make_project(tree)
    monkeypatch.setattr(_mod, "detect_project", lambda: project)

    args = SimpleNamespace()
    ret = cmd_status(args)
    assert ret == 0
    out = capsys.readouterr().out
    assert "No instincts found." in out


def test_cmd_status_with_instincts(patch_globals, monkeypatch, capsys):
    """status で project と global の instinct 件数が表示されること。"""
    tree = patch_globals
    project = _make_project(tree)
    monkeypatch.setattr(_mod, "detect_project", lambda: project)

    (project["instincts_personal"] / "proj.yaml").write_text(SAMPLE_INSTINCT_YAML)
    (tree["global_personal"] / "glob.yaml").write_text(SAMPLE_GLOBAL_INSTINCT_YAML)

    args = SimpleNamespace()
    ret = cmd_status(args)
    assert ret == 0
    out = capsys.readouterr().out
    assert "INSTINCT STATUS" in out
    assert "Project instincts: 1" in out
    assert "Global instincts:  1" in out
    assert "PROJECT-SCOPED" in out
    assert "GLOBAL" in out


def test_cmd_status_returns_int(patch_globals, monkeypatch):
    """cmd_status が常に int を返すこと。"""
    tree = patch_globals
    project = _make_project(tree)
    monkeypatch.setattr(_mod, "detect_project", lambda: project)

    args = SimpleNamespace()
    ret = cmd_status(args)
    assert isinstance(ret, int)


# ─────────────────────────────────────────────
# cmd_projects のテスト
# ─────────────────────────────────────────────


def test_cmd_projects_empty_registry(patch_globals, capsys):
    """プロジェクトがない場合は案内メッセージが表示されること。"""
    args = SimpleNamespace()
    ret = cmd_projects(args)
    assert ret == 0
    out = capsys.readouterr().out
    assert "No projects registered yet." in out


def test_cmd_projects_with_registry(patch_globals, capsys):
    """レジストリからプロジェクト一覧を表示すること。"""
    tree = patch_globals

    # instinct を含むプロジェクトディレクトリを作成
    pid = "test123abc"
    project = _make_project(tree, pid=pid, pname="my-app")
    (project["instincts_personal"] / "inst.yaml").write_text(SAMPLE_INSTINCT_YAML)

    # レジストリを書き込む
    registry = {
        pid: {
            "name": "my-app",
            "root": "/home/user/my-app",
            "remote": "https://github.com/user/my-app.git",
            "last_seen": "2025-01-15T12:00:00Z",
        }
    }
    tree["registry_file"].write_text(json.dumps(registry))

    args = SimpleNamespace()
    ret = cmd_projects(args)
    assert ret == 0
    out = capsys.readouterr().out
    assert "my-app" in out
    assert pid in out
    assert "1 personal" in out


# ─────────────────────────────────────────────
# _promote_specific のテスト
# ─────────────────────────────────────────────


def test_promote_specific_not_found(patch_globals, capsys):
    """存在しない instinct の昇格は失敗すること。"""
    tree = patch_globals
    project = _make_project(tree)

    ret = _promote_specific(project, "nonexistent", force=True)
    assert ret == 1
    out = capsys.readouterr().out
    assert "not found" in out


def test_promote_specific_rejects_invalid_id(patch_globals, capsys):
    """パス形式の instinct ID はファイル書き込み前に拒否されること。"""
    tree = patch_globals
    project = _make_project(tree)

    ret = _promote_specific(project, "../escape", force=True)
    assert ret == 1
    err = capsys.readouterr().err
    assert "Invalid instinct ID" in err


def test_promote_specific_already_global(patch_globals, capsys):
    """すでに global に存在する instinct の昇格は失敗すること。"""
    tree = patch_globals
    project = _make_project(tree)

    # project と global の両方に同じ ID の instinct を書き込む
    (project["instincts_personal"] / "shared.yaml").write_text(SAMPLE_INSTINCT_YAML)
    global_yaml = SAMPLE_INSTINCT_YAML  # 同一 ID: test-instinct
    (tree["global_personal"] / "shared.yaml").write_text(global_yaml)

    ret = _promote_specific(project, "test-instinct", force=True)
    assert ret == 1
    out = capsys.readouterr().out
    assert "already exists in global" in out


def test_promote_specific_success(patch_globals, capsys):
    """--force で project instinct を global へ昇格できること。"""
    tree = patch_globals
    project = _make_project(tree)

    (project["instincts_personal"] / "inst.yaml").write_text(SAMPLE_INSTINCT_YAML)

    ret = _promote_specific(project, "test-instinct", force=True)
    assert ret == 0
    out = capsys.readouterr().out
    assert "Promoted" in out

    # global ディレクトリにファイルが作成されたことを確認
    promoted_file = tree["global_personal"] / "test-instinct.yaml"
    assert promoted_file.exists()
    content = promoted_file.read_text()
    assert "scope: global" in content
    assert "promoted_from: abc123" in content


# ─────────────────────────────────────────────
# _promote_auto のテスト
# ─────────────────────────────────────────────


def test_promote_auto_no_candidates(patch_globals, capsys):
    """クロスプロジェクト instinct がない場合の自動昇格で、その旨が表示されること。"""
    tree = patch_globals
    project = _make_project(tree)

    # 空のレジストリ
    tree["registry_file"].write_text("{}")

    ret = _promote_auto(project, force=True, dry_run=False)
    assert ret == 0
    out = capsys.readouterr().out
    assert "No instincts qualify" in out


def test_promote_auto_dry_run(patch_globals, capsys):
    """ドライランでは候補を表示しつつ、ファイルは書き込まないこと。"""
    tree = patch_globals

    # 同じ高信頼度 instinct を持つ 2 つのプロジェクトを作成
    p1 = _make_project(tree, pid="proj1", pname="project-one")
    p2 = _make_project(tree, pid="proj2", pname="project-two")

    high_conf_yaml = """\
---
id: cross-project-instinct
trigger: "when reviewing"
confidence: 0.95
domain: security
scope: project
---

## Action
Always review for injection.
"""
    (p1["instincts_personal"] / "cross.yaml").write_text(high_conf_yaml)
    (p2["instincts_personal"] / "cross.yaml").write_text(high_conf_yaml)

    # レジストリを書き込む
    registry = {
        "proj1": {"name": "project-one", "root": "/a", "remote": "", "last_seen": "2025-01-01T00:00:00Z"},
        "proj2": {"name": "project-two", "root": "/b", "remote": "", "last_seen": "2025-01-01T00:00:00Z"},
    }
    tree["registry_file"].write_text(json.dumps(registry))

    project = p1
    ret = _promote_auto(project, force=True, dry_run=True)
    assert ret == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "cross-project-instinct" in out

    # ファイルが作成されていないことを確認
    assert not (tree["global_personal"] / "cross-project-instinct.yaml").exists()


def test_promote_auto_writes_file(patch_globals, capsys):
    """force 指定の自動昇格で global instinct ファイルが書き込まれること。"""
    tree = patch_globals

    p1 = _make_project(tree, pid="proj1", pname="project-one")
    p2 = _make_project(tree, pid="proj2", pname="project-two")

    high_conf_yaml = """\
---
id: universal-pattern
trigger: "when coding"
confidence: 0.85
domain: general
scope: project
---

## Action
Use descriptive variable names.
"""
    (p1["instincts_personal"] / "uni.yaml").write_text(high_conf_yaml)
    (p2["instincts_personal"] / "uni.yaml").write_text(high_conf_yaml)

    registry = {
        "proj1": {"name": "project-one", "root": "/a", "remote": "", "last_seen": "2025-01-01T00:00:00Z"},
        "proj2": {"name": "project-two", "root": "/b", "remote": "", "last_seen": "2025-01-01T00:00:00Z"},
    }
    tree["registry_file"].write_text(json.dumps(registry))

    ret = _promote_auto(p1, force=True, dry_run=False)
    assert ret == 0

    promoted = tree["global_personal"] / "universal-pattern.yaml"
    assert promoted.exists()
    content = promoted.read_text()
    assert "scope: global" in content
    assert "auto-promoted" in content


def test_promote_auto_skips_invalid_id(patch_globals, capsys):
    tree = patch_globals

    p1 = _make_project(tree, pid="proj1", pname="project-one")
    p2 = _make_project(tree, pid="proj2", pname="project-two")

    bad_id_yaml = """\
---
id: ../escape
trigger: "when coding"
confidence: 0.9
domain: general
scope: project
---

## Action
Invalid id should be skipped.
"""
    (p1["instincts_personal"] / "bad.yaml").write_text(bad_id_yaml)
    (p2["instincts_personal"] / "bad.yaml").write_text(bad_id_yaml)

    registry = {
        "proj1": {"name": "project-one", "root": "/a", "remote": "", "last_seen": "2025-01-01T00:00:00Z"},
        "proj2": {"name": "project-two", "root": "/b", "remote": "", "last_seen": "2025-01-01T00:00:00Z"},
    }
    tree["registry_file"].write_text(json.dumps(registry))

    ret = _promote_auto(p1, force=True, dry_run=False)
    assert ret == 0
    err = capsys.readouterr().err
    assert "Skipping invalid instinct ID" in err
    assert not (tree["global_personal"] / "../escape.yaml").exists()


# ─────────────────────────────────────────────
# _find_cross_project_instincts のテスト
# ─────────────────────────────────────────────


def test_find_cross_project_empty_registry(patch_globals):
    tree = patch_globals
    tree["registry_file"].write_text("{}")
    result = _find_cross_project_instincts()
    assert result == {}


def test_find_cross_project_single_project(patch_globals):
    """単一プロジェクトでは何も返らないこと（2 件以上必要）。"""
    tree = patch_globals
    p1 = _make_project(tree, pid="proj1", pname="project-one")
    (p1["instincts_personal"] / "inst.yaml").write_text(SAMPLE_INSTINCT_YAML)

    registry = {"proj1": {"name": "project-one", "root": "/a", "remote": "", "last_seen": "2025-01-01T00:00:00Z"}}
    tree["registry_file"].write_text(json.dumps(registry))

    result = _find_cross_project_instincts()
    assert result == {}


def test_find_cross_project_shared_instinct(patch_globals):
    """2 つのプロジェクトに同じ instinct ID がある場合に検出されること。"""
    tree = patch_globals
    p1 = _make_project(tree, pid="proj1", pname="project-one")
    p2 = _make_project(tree, pid="proj2", pname="project-two")

    (p1["instincts_personal"] / "shared.yaml").write_text(SAMPLE_INSTINCT_YAML)
    (p2["instincts_personal"] / "shared.yaml").write_text(SAMPLE_INSTINCT_YAML)

    registry = {
        "proj1": {"name": "project-one", "root": "/a", "remote": "", "last_seen": "2025-01-01T00:00:00Z"},
        "proj2": {"name": "project-two", "root": "/b", "remote": "", "last_seen": "2025-01-01T00:00:00Z"},
    }
    tree["registry_file"].write_text(json.dumps(registry))

    result = _find_cross_project_instincts()
    assert "test-instinct" in result
    assert len(result["test-instinct"]) == 2


# ─────────────────────────────────────────────
# load_registry のテスト
# ─────────────────────────────────────────────


def test_load_registry_missing_file(patch_globals):
    result = load_registry()
    assert result == {}


def test_load_registry_corrupt_json(patch_globals):
    tree = patch_globals
    tree["registry_file"].write_text("not json at all {{{")
    result = load_registry()
    assert result == {}


def test_load_registry_valid(patch_globals):
    tree = patch_globals
    data = {"abc": {"name": "test", "root": "/test"}}
    tree["registry_file"].write_text(json.dumps(data))
    result = load_registry()
    assert result == data


def test_load_registry_uses_utf8_encoding(monkeypatch):
    calls = []

    def fake_open(path, mode="r", *args, **kwargs):
        calls.append(kwargs.get("encoding"))
        return io.StringIO("{}")

    monkeypatch.setattr(_mod, "open", fake_open, raising=False)
    assert load_registry() == {}
    assert calls == ["utf-8"]


def test_validate_instinct_id():
    assert _validate_instinct_id("good-id_1.0")
    assert not _validate_instinct_id("../bad")
    assert not _validate_instinct_id("bad/name")
    assert not _validate_instinct_id(".hidden")


def test_project_directory_helpers_cover_current_paths(patch_globals):
    tree = patch_globals
    current_project = tree["projects_dir"] / "current"
    current_dup = tree["projects_dir"] / "dup"

    for path in [current_project, current_dup]:
        path.mkdir(parents=True, exist_ok=True)

    (current_project / "project.json").write_text("{}", encoding="utf-8")
    (current_project / "observations.jsonl").write_text("event\n", encoding="utf-8")
    (current_project / "instincts" / "personal").mkdir(parents=True, exist_ok=True)
    (current_project / "instincts" / "personal" / "item.yaml").write_text("x", encoding="utf-8")

    assert _mod._project_dir_score(tree["projects_dir"] / "missing") == -1
    assert _mod._project_dir_for_id("missing") == tree["projects_dir"] / "missing"
    assert _mod._project_dir_for_id("current") == current_project

    dirs = _mod._all_project_dirs()
    assert current_project in dirs
    assert len([d for d in dirs if d.name == "dup"]) == 1

    tree["registry_file"].write_text("{}", encoding="utf-8")
    assert _mod._preferred_projects_dir() == tree["projects_dir"]
    assert _mod._preferred_registry_file() == tree["registry_file"]


def test_yaml_quote_escapes_special_characters():
    assert _mod._yaml_quote('say "hi"') == '"say \\"hi\\""'
    assert _mod._yaml_quote(r"c:\path") == '"c:\\\\path"'


def test_parse_created_date_uses_frontmatter_and_mtime(tmp_path):
    created_file = tmp_path / "created.yaml"
    created_file.write_text(
        """\
---
created: "2024-01-02T03:04:05Z"
---
Body
""",
        encoding="utf-8",
    )
    created = _mod._parse_created_date(created_file)
    assert created is not None
    assert created.year == 2024

    mtime_file = tmp_path / "mtime.yaml"
    mtime_file.write_text("Body only", encoding="utf-8")
    mtime = datetime(2024, 1, 3, 4, 5, tzinfo=UTC)
    mtime_file.write_text("Body only", encoding="utf-8")
    mtime_file.touch()
    mtime_file.write_text("Body only", encoding="utf-8")
    mtime_timestamp = int(mtime.timestamp())
    mtime_file.touch()
    os.utime(mtime_file, (mtime_timestamp, mtime_timestamp))
    fallback = _mod._parse_created_date(mtime_file)
    assert fallback is not None
    assert fallback.tzinfo == UTC


def test_cmd_export_filters_and_writes_output(patch_globals, monkeypatch, tmp_path, capsys):
    tree = patch_globals
    project = _make_project(tree)
    (project["instincts_personal"] / "inst.yaml").write_text(SAMPLE_INSTINCT_YAML)
    monkeypatch.setattr(_mod, "detect_project", lambda: project)

    args = SimpleNamespace(scope="project", domain="testing", min_confidence=0.8, output=str(tmp_path / "export.md"))
    ret = _mod.cmd_export(args)
    assert ret == 0
    assert (tmp_path / "export.md").exists()
    assert "# Instincts export" in (tmp_path / "export.md").read_text(encoding="utf-8")
    assert "Exported 1 instincts" in capsys.readouterr().out


def test_cmd_export_no_instincts(patch_globals, monkeypatch, capsys):
    tree = patch_globals
    project = _make_project(tree)
    monkeypatch.setattr(_mod, "detect_project", lambda: project)

    args = SimpleNamespace(scope="project", domain=None, min_confidence=None, output=None)
    ret = _mod.cmd_export(args)
    assert ret == 1
    assert "No instincts to export." in capsys.readouterr().out


def test_cmd_export_global_scope_stdout(patch_globals, monkeypatch, capsys):
    tree = patch_globals
    project = _make_project(tree)
    (tree["global_personal"] / "glob.yaml").write_text(SAMPLE_GLOBAL_INSTINCT_YAML)
    monkeypatch.setattr(_mod, "detect_project", lambda: project)

    args = SimpleNamespace(scope="global", domain=None, min_confidence=None, output=None)
    ret = _mod.cmd_export(args)
    assert ret == 0
    out = capsys.readouterr().out
    assert "# Instincts export" in out
    assert "# Scope: global" in out


def test_cmd_export_invalid_directory_and_no_matches(patch_globals, monkeypatch, tmp_path, capsys):
    tree = patch_globals
    project = _make_project(tree)
    (project["instincts_personal"] / "inst.yaml").write_text(SAMPLE_INSTINCT_YAML)
    monkeypatch.setattr(_mod, "detect_project", lambda: project)

    args = SimpleNamespace(scope="project", domain="missing", min_confidence=None, output=str(tmp_path))
    ret = _mod.cmd_export(args)
    assert ret == 1
    assert "is a directory" in capsys.readouterr().err

    args = SimpleNamespace(scope="project", domain="missing", min_confidence=1.0, output=None)
    ret = _mod.cmd_export(args)
    assert ret == 1
    assert "No instincts match the criteria." in capsys.readouterr().out


def test_generate_evolved_writes_skill_command_and_agent(tmp_path):
    evolved_dir = tmp_path / "evolved"
    (evolved_dir / "commands").mkdir(parents=True, exist_ok=True)
    (evolved_dir / "agents").mkdir(parents=True, exist_ok=True)
    generated = _mod._generate_evolved(
        [
            {
                "trigger": "when testing",
                "instincts": [
                    {"id": "inst-1", "content": "## Action\nWrite tests\n"},
                    {"id": "inst-2", "content": "## Action\nRun tests\n"},
                ],
                "avg_confidence": 0.85,
            }
        ],
        [
            {
                "id": "workflow-1",
                "trigger": "when implementing release flow",
                "confidence": 0.9,
                "content": "Ship it",
            }
        ],
        [
            {
                "trigger": "complex review flow",
                "instincts": [{"id": "agent-a"}, {"id": "agent-b"}, {"id": "agent-c"}],
                "avg_confidence": 0.9,
                "domains": ["general", "workflow"],
            }
        ],
        evolved_dir,
    )

    assert len(generated) == 3
    assert (evolved_dir / "skills" / "when-testing" / "SKILL.md").exists()
    assert (evolved_dir / "commands" / "release-flow.md").exists()
    assert (evolved_dir / "agents" / "complex-review-flow.md").exists()


def test_generate_evolved_skips_invalid_names(tmp_path):
    evolved_dir = tmp_path / "evolved"
    (evolved_dir / "commands").mkdir(parents=True, exist_ok=True)
    (evolved_dir / "agents").mkdir(parents=True, exist_ok=True)
    generated = _mod._generate_evolved(
        [{"trigger": "!!!", "instincts": [{"id": "x"}], "avg_confidence": 0.9}],
        [{"id": "workflow-1", "trigger": "", "confidence": 0.9, "content": "body"}],
        [{"trigger": "", "instincts": [{"id": "a"}], "avg_confidence": 0.9, "domains": ["general"]}],
        evolved_dir,
    )
    assert generated == []


def test_cmd_evolve_prints_candidates(patch_globals, monkeypatch, capsys):
    tree = patch_globals
    project = _make_project(tree)
    instincts = _make_instincts(extra=[
        {"id": "workflow-1", "trigger": "when implementing release flow", "confidence": 0.7, "domain": "workflow", "scope": "project", "content": "D", "_scope_label": "project"},
    ])
    monkeypatch.setattr(_mod, "detect_project", lambda: project)
    monkeypatch.setattr(_mod, "load_all_instincts", lambda project: instincts)
    monkeypatch.setattr(_mod, "_show_promotion_candidates", lambda project: None)

    ret = _mod.cmd_evolve(SimpleNamespace(generate=False))
    assert ret == 0
    out = capsys.readouterr().out
    assert "EVOLVE ANALYSIS" in out
    assert "SKILL CANDIDATES" in out
    assert "/release-flow" in out
    assert "AGENT CANDIDATES" in out


def test_cmd_evolve_no_structures_generated(patch_globals, monkeypatch, capsys):
    tree = patch_globals
    project = _make_project(tree)
    instincts = _make_instincts()
    monkeypatch.setattr(_mod, "detect_project", lambda: project)
    monkeypatch.setattr(_mod, "load_all_instincts", lambda project: instincts)
    monkeypatch.setattr(_mod, "_show_promotion_candidates", lambda project: None)
    monkeypatch.setattr(_mod, "_generate_evolved", lambda *args, **kwargs: [])

    ret = _mod.cmd_evolve(SimpleNamespace(generate=True))
    assert ret == 0
    assert "No structures generated" in capsys.readouterr().out


def test_cmd_evolve_generate_branch(patch_globals, monkeypatch, capsys):
    tree = patch_globals
    project = _make_project(tree)
    instincts = _make_instincts()
    monkeypatch.setattr(_mod, "detect_project", lambda: project)
    monkeypatch.setattr(_mod, "load_all_instincts", lambda project: instincts)
    monkeypatch.setattr(_mod, "_show_promotion_candidates", lambda project: None)
    monkeypatch.setattr(_mod, "_generate_evolved", lambda *args, **kwargs: ["generated.md"])

    ret = _mod.cmd_evolve(SimpleNamespace(generate=True))
    assert ret == 0
    out = capsys.readouterr().out
    assert "Generated 1 evolved structures" in out
    assert "generated.md" in out


def test_promote_specific_dry_run_and_cancel(patch_globals, monkeypatch, capsys):
    tree = patch_globals
    project = _make_project(tree)
    (project["instincts_personal"] / "inst.yaml").write_text(SAMPLE_INSTINCT_YAML)

    ret = _mod._promote_specific(project, "test-instinct", force=False, dry_run=True)
    assert ret == 0
    assert "[DRY RUN]" in capsys.readouterr().out

    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    ret = _mod._promote_specific(project, "test-instinct", force=False, dry_run=False)
    assert ret == 0
    assert "Cancelled." in capsys.readouterr().out


def test_cmd_evolve_requires_three_instincts(patch_globals, monkeypatch, capsys):
    tree = patch_globals
    project = _make_project(tree)
    monkeypatch.setattr(_mod, "detect_project", lambda: project)
    monkeypatch.setattr(_mod, "load_all_instincts", lambda project: [{"id": "one"}, {"id": "two"}])

    ret = _mod.cmd_evolve(SimpleNamespace(generate=False))
    assert ret == 1
    out = capsys.readouterr().out
    assert "Need at least 3 instincts" in out


def test_promote_auto_cancel(patch_globals, monkeypatch, capsys):
    tree = patch_globals
    project = _make_project(tree)
    p1 = _make_project(tree, pid="proj1", pname="project-one")
    p2 = _make_project(tree, pid="proj2", pname="project-two")
    high_conf_yaml = """\
---
id: cross-project-instinct
trigger: "when reviewing"
confidence: 0.95
domain: security
scope: project
---

## Action
Always review for injection.
"""
    (p1["instincts_personal"] / "cross.yaml").write_text(high_conf_yaml)
    (p2["instincts_personal"] / "cross.yaml").write_text(high_conf_yaml)
    tree["registry_file"].write_text(json.dumps(
        {
            "proj1": {"name": "project-one", "root": "/a", "remote": "", "last_seen": "2025-01-01T00:00:00Z"},
            "proj2": {"name": "project-two", "root": "/b", "remote": "", "last_seen": "2025-01-01T00:00:00Z"},
        }
    ))

    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    ret = _mod._promote_auto(project, force=False, dry_run=False)
    assert ret == 0
    assert "Cancelled." in capsys.readouterr().out


def test_show_promotion_candidates_prints_candidates(patch_globals, monkeypatch, capsys):
    tree = patch_globals
    project = _make_project(tree)
    inst = {"id": "shared", "confidence": 0.9, "content": "## Action\nDo it"}
    cross = {"shared": [("proj1", "one", inst), ("proj2", "two", {"id": "shared", "confidence": 0.8, "content": "## Action\nDo it"})]}
    monkeypatch.setattr(_mod, "_find_cross_project_instincts", lambda: cross)
    monkeypatch.setattr(_mod, "_load_instincts_from_dir", lambda *args, **kwargs: [])

    _mod._show_promotion_candidates(project)
    assert "PROMOTION CANDIDATES" in capsys.readouterr().out


def test_cmd_projects_with_observations_file(patch_globals, capsys):
    tree = patch_globals
    pid = "test123abc"
    project = _make_project(tree, pid=pid, pname="my-app")
    (project["instincts_personal"] / "inst.yaml").write_text(SAMPLE_INSTINCT_YAML)
    (project["project_dir"] / "observations.jsonl").write_text("{}\n{}\n", encoding="utf-8")
    tree["registry_file"].write_text(json.dumps({pid: {"name": "my-app", "root": "/home/user/my-app", "remote": "", "last_seen": "2025-01-15T12:00:00Z"}}))

    ret = cmd_projects(SimpleNamespace())
    assert ret == 0
    out = capsys.readouterr().out
    assert "Observations: 2 events" in out


def test_cmd_promote_dispatches(monkeypatch, patch_globals):
    tree = patch_globals
    project = _make_project(tree)
    monkeypatch.setattr(_mod, "detect_project", lambda: project)

    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(_mod, "_promote_specific", lambda project, instinct_id, force, dry_run=False: calls.append(("specific", instinct_id)) or 0)
    monkeypatch.setattr(_mod, "_promote_auto", lambda project, force, dry_run: calls.append(("auto", None)) or 0)

    assert _mod.cmd_promote(SimpleNamespace(instinct_id="abc", force=True, dry_run=False)) == 0
    assert _mod.cmd_promote(SimpleNamespace(instinct_id=None, force=True, dry_run=True)) == 0
    assert calls == [("specific", "abc"), ("auto", None)]


def test_parse_created_date_variants(tmp_path):
    missing = tmp_path / "missing.yaml"
    assert _mod._parse_created_date(missing) is None

    naive = tmp_path / "naive.yaml"
    naive.write_text(
        """\
---
created: "2024-01-02T03:04:05"
---
Body
""",
        encoding="utf-8",
    )
    parsed = _mod._parse_created_date(naive)
    assert parsed is not None
    assert parsed.tzinfo == UTC

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text(
        """\
---
created: "not-a-date"
---
Body
""",
        encoding="utf-8",
    )
    mtime = int(datetime(2024, 1, 4, 5, 6, tzinfo=UTC).timestamp())
    os.utime(invalid, (mtime, mtime))
    fallback = _mod._parse_created_date(invalid)
    assert fallback is not None
    assert fallback.year == 2024


def test_cmd_import_url_and_path_errors(patch_globals, monkeypatch, capsys):
    tree = patch_globals
    project = _make_project(tree)
    monkeypatch.setattr(_mod, "detect_project", lambda: project)

    monkeypatch.setattr(_mod.urllib.request, "urlopen", lambda source: (_ for _ in ()).throw(RuntimeError("boom")))
    args = SimpleNamespace(source="https://example.com/instinct.yaml", scope="project", dry_run=False, force=True, min_confidence=None)
    assert _mod.cmd_import(args) == 1
    assert "Error fetching URL" in capsys.readouterr().err

    args = SimpleNamespace(source="/etc/passwd", scope="project", dry_run=False, force=True, min_confidence=None)
    assert _mod.cmd_import(args) == 1
    assert "Invalid path" in capsys.readouterr().err


def test_cmd_import_global_fallback_url_success_and_empty_source(patch_globals, global_project, monkeypatch, capsys):
    monkeypatch.setattr(_mod, "detect_project", lambda: global_project)

    class FakeResponse:
        def __init__(self, body: str) -> None:
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
            return False

        def read(self) -> bytes:
            return self.body.encode("utf-8")

    monkeypatch.setattr(
        _mod.urllib.request,
        "urlopen",
        lambda source: FakeResponse(SAMPLE_GLOBAL_INSTINCT_YAML),
    )
    args = SimpleNamespace(source="https://example.com/instinct.yaml", scope="project", dry_run=True, force=True, min_confidence=None)
    assert _mod.cmd_import(args) == 0
    out = capsys.readouterr().out
    assert "No project detected. Importing as global scope." in out
    assert "Target scope: global" in out

    monkeypatch.setattr(_mod.urllib.request, "urlopen", lambda source: FakeResponse(""))
    args = SimpleNamespace(source="https://example.com/empty.yaml", scope="project", dry_run=False, force=True, min_confidence=None)
    assert _mod.cmd_import(args) == 1
    assert "No valid instincts found in source." in capsys.readouterr().out


def test_cmd_import_directory_path_rejected(patch_globals, monkeypatch, tmp_path, capsys):
    tree = patch_globals
    project = _make_project(tree)
    monkeypatch.setattr(_mod, "detect_project", lambda: project)
    source_dir = tmp_path / "source-dir"
    source_dir.mkdir()

    args = SimpleNamespace(source=str(source_dir), scope="project", dry_run=False, force=True, min_confidence=None)
    assert _mod.cmd_import(args) == 1
    assert "is not a regular file" in capsys.readouterr().err


def test_cmd_import_file_success_and_updates(patch_globals, monkeypatch, tmp_path, capsys):
    tree = patch_globals
    project = _make_project(tree)
    monkeypatch.setattr(_mod, "detect_project", lambda: project)

    existing_file = project["instincts_personal"] / "shared.yaml"
    existing_file.write_text(
        """\
---
id: shared
trigger: "when coding"
confidence: 0.2
domain: general
---

Old body.
""",
        encoding="utf-8",
    )

    source_file = tmp_path / "source.yaml"
    source_file.write_text(
        """\
---
id: shared
trigger: "when coding"
confidence: 0.9
domain: general
---

Updated body.

---
id: new-one
trigger: "when testing"
confidence: 0.8
domain: testing
---

New body.
""",
        encoding="utf-8",
    )

    args = SimpleNamespace(source=str(source_file), scope="project", dry_run=False, force=True, min_confidence=0.0)
    assert _mod.cmd_import(args) == 0
    out = capsys.readouterr().out
    assert "Import complete!" in out
    assert "Added: 1" in out
    assert "Updated: 1" in out
    assert not existing_file.exists()
    assert any(path.suffix == ".yaml" for path in project["instincts_inherited"].iterdir())


def test_cmd_prune_dry_run_and_delete(patch_globals, monkeypatch, capsys):
    tree = patch_globals
    monkeypatch.setattr(_mod, "GLOBAL_INSTINCTS_DIR", tree["devgear"] / "instincts")
    project = _make_project(tree)
    pending_global = _mod.GLOBAL_INSTINCTS_DIR / "pending"
    pending_project = project["project_dir"] / "instincts" / "pending"
    pending_global.mkdir(parents=True, exist_ok=True)
    pending_project.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    old_created = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent_mtime = int((now - timedelta(days=1)).timestamp())

    old_file = pending_global / "old.yaml"
    old_file.write_text(f"---\ncreated: {old_created}\n---\nold\n", encoding="utf-8")
    recent_file = pending_project / "recent.yaml"
    recent_file.write_text("body only\n", encoding="utf-8")
    os.utime(recent_file, (recent_mtime, recent_mtime))

    dry_args = SimpleNamespace(max_age=30, dry_run=True, quiet=False)
    assert _mod.cmd_prune(dry_args) == 0
    dry_out = capsys.readouterr().out
    assert "Would prune 1" in dry_out
    assert old_file.exists()
    assert recent_file.exists()

    delete_args = SimpleNamespace(max_age=30, dry_run=False, quiet=False)
    assert _mod.cmd_prune(delete_args) == 0
    out = capsys.readouterr().out
    assert "Pruned 1 pending instinct" in out
    assert not old_file.exists()
    assert recent_file.exists()


def test_main_dispatches_commands(monkeypatch, patch_globals):
    tree = patch_globals
    project = _make_project(tree)
    monkeypatch.setattr(_mod, "_ensure_global_dirs", lambda: None)
    monkeypatch.setattr(_mod, "detect_project", lambda: project)

    called: list[str] = []
    monkeypatch.setattr(_mod, "cmd_import", lambda args: called.append("import") or 0)
    monkeypatch.setattr(_mod, "cmd_export", lambda args: called.append("export") or 0)
    monkeypatch.setattr(_mod, "cmd_evolve", lambda args: called.append("evolve") or 0)
    monkeypatch.setattr(_mod, "cmd_promote", lambda args: called.append("promote") or 0)
    monkeypatch.setattr(_mod, "cmd_prune", lambda args: called.append("prune") or 0)

    original_parse_args = argparse.ArgumentParser.parse_args

    def fake_parse_args(self):  # noqa: ANN001
        return SimpleNamespace(command=fake_parse_args.command)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", fake_parse_args)

    for command in ["import", "export", "evolve", "promote", "prune"]:
        fake_parse_args.command = command
        assert _mod.main() == 0

    fake_parse_args.command = None
    assert _mod.main() == 1
    assert called == ["import", "export", "evolve", "promote", "prune"]
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", original_parse_args)


def test_update_registry_atomic_replaces_file(patch_globals):
    tree = patch_globals
    _update_registry("abc123", "demo", "/repo", "https://example.com/repo.git")
    data = json.loads(tree["registry_file"].read_text())
    assert "abc123" in data
    leftovers = list(tree["registry_file"].parent.glob(".projects.json.tmp.*"))
    assert leftovers == []


def test_import_handles_missing_fcntl_and_ensure_global_dirs(patch_globals, monkeypatch):
    original_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: ANN001
        if name == "fcntl":
            raise ImportError("missing fcntl")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    reloaded = importlib.reload(_mod)
    assert reloaded._HAS_FCNTL is False

    monkeypatch.setattr(builtins, "__import__", original_import)
    importlib.reload(_mod)
    assert _mod._HAS_FCNTL is True

    _mod._ensure_global_dirs()
    monkeypatch.setattr(_mod, "DEVGEAR_DIR", patch_globals["devgear"])
    monkeypatch.setattr(_mod, "GLOBAL_INSTINCTS_DIR", patch_globals["devgear"] / "instincts")
    monkeypatch.setattr(_mod, "PROJECTS_DIR", patch_globals["projects_dir"])
    monkeypatch.setattr(_mod, "REGISTRY_FILE", patch_globals["registry_file"])
    monkeypatch.setattr(_mod, "GLOBAL_PERSONAL_DIR", patch_globals["global_personal"])
    monkeypatch.setattr(_mod, "GLOBAL_INHERITED_DIR", patch_globals["global_inherited"])
    monkeypatch.setattr(_mod, "GLOBAL_EVOLVED_DIR", patch_globals["global_evolved"])
    monkeypatch.setattr(_mod, "GLOBAL_OBSERVATIONS_FILE", patch_globals["devgear"] / "observations.jsonl")


def test_validate_instinct_id_rejects_additional_invalid_forms() -> None:
    assert not _validate_instinct_id("")
    assert not _validate_instinct_id("x" * 129)
    assert not _validate_instinct_id("bad..name")
    assert not _validate_instinct_id("bad\\name")


def test_detect_project_uses_git_root_and_handles_remote_timeout(
    patch_globals, monkeypatch, tmp_path
):
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

    import subprocess as sp

    def mock_run(cmd, **kwargs):  # noqa: ANN001
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return SimpleNamespace(returncode=0, stdout=f"{fake_repo}\n", stderr="")
        if cmd[:3] == ["git", "-C", str(fake_repo)] and cmd[3:] == ["remote", "get-url", "origin"]:
            raise sp.TimeoutExpired(cmd, 5)
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", mock_run)

    project = detect_project()
    assert project["name"] == "repo"
    assert project["remote"] == ""


def test_update_registry_reads_existing_registry(patch_globals):
    tree = patch_globals
    existing = {
        "existing": {
            "name": "existing-project",
            "root": "/existing",
            "remote": "",
            "last_seen": "2025-01-01T00:00:00Z",
        }
    }
    tree["registry_file"].write_text(json.dumps(existing), encoding="utf-8")

    _update_registry("abc123", "demo", "/repo", "https://example.com/repo.git")
    data = json.loads(tree["registry_file"].read_text(encoding="utf-8"))
    assert data["existing"]["name"] == "existing-project"
    assert data["abc123"]["name"] == "demo"


def test_parse_instinct_file_handles_single_quotes_and_bad_confidence() -> None:
    result = parse_instinct_file(
        """\
---
id: quoted
trigger: 'when quoting'
confidence: not-a-number
domain: general
---

Body.
"""
    )

    assert len(result) == 1
    assert result[0]["trigger"] == "when quoting"
    assert result[0]["confidence"] == 0.5


def test_load_instincts_from_dir_logs_parse_exception(tmp_path, monkeypatch, capsys):
    yaml_file = tmp_path / "broken.yaml"
    yaml_file.write_text(SAMPLE_INSTINCT_YAML, encoding="utf-8")

    original_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs):  # noqa: ANN001
        if self == yaml_file:
            raise OSError("boom")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    assert _load_instincts_from_dir(tmp_path, "personal", "project") == []
    assert "Warning: Failed to parse" in capsys.readouterr().err


def test_cmd_status_reports_observations_and_pending_warning(patch_globals, monkeypatch, capsys):
    tree = patch_globals
    project = _make_project(tree)
    (project["instincts_personal"] / "proj.yaml").write_text(SAMPLE_INSTINCT_YAML, encoding="utf-8")
    (project["project_dir"] / "observations.jsonl").write_text("{}\n{}\n", encoding="utf-8")

    pending_dir = tree["global_personal"].parent / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    old_timestamp = int((datetime.now(UTC) - timedelta(days=29)).timestamp())
    for idx in range(5):
        pending_file = pending_dir / f"pending-{idx}.yaml"
        pending_file.write_text("body only\n", encoding="utf-8")
        os.utime(pending_file, (old_timestamp, old_timestamp))

    monkeypatch.setattr(_mod, "detect_project", lambda: project)

    assert cmd_status(SimpleNamespace()) == 0
    out = capsys.readouterr().out
    assert "Observations: 2 events logged" in out
    assert "Pending instincts: 5 awaiting review" in out
    assert "Expiring within 7 days" in out


def test_cmd_export_rejects_invalid_output_path(patch_globals, monkeypatch, capsys):
    tree = patch_globals
    project = _make_project(tree)
    (project["instincts_personal"] / "proj.yaml").write_text(SAMPLE_INSTINCT_YAML, encoding="utf-8")
    monkeypatch.setattr(_mod, "detect_project", lambda: project)

    args = SimpleNamespace(scope="project", domain=None, min_confidence=None, output="/etc/passwd")
    assert _mod.cmd_export(args) == 1
    assert "Invalid output path" in capsys.readouterr().err


def test_cmd_export_scope_all_exports_project_and_global(patch_globals, monkeypatch, tmp_path):
    tree = patch_globals
    project = _make_project(tree)
    (project["instincts_personal"] / "proj.yaml").write_text(SAMPLE_INSTINCT_YAML, encoding="utf-8")
    (tree["global_personal"] / "glob.yaml").write_text(SAMPLE_GLOBAL_INSTINCT_YAML, encoding="utf-8")
    monkeypatch.setattr(_mod, "detect_project", lambda: project)

    output = tmp_path / "export.md"
    args = SimpleNamespace(scope="all", domain=None, min_confidence=None, output=str(output))
    assert _mod.cmd_export(args) == 0
    content = output.read_text(encoding="utf-8")
    assert "# Scope: all" in content
    assert "test-instinct" in content
    assert "global-instinct" in content


def test_cmd_import_duplicate_only_sources_and_no_changes(patch_globals, monkeypatch, tmp_path, capsys):
    tree = patch_globals
    project = _make_project(tree)
    monkeypatch.setattr(_mod, "detect_project", lambda: project)

    source = tmp_path / "duplicates.yaml"
    source.write_text(
        """\
---
id: dup-1
trigger: when coding
confidence: 0.8
domain: general
---

Body 1.

---
id: dup-2
trigger: when coding
confidence: 0.8
domain: general
---

Body 2.

---
id: dup-3
trigger: when coding
confidence: 0.8
domain: general
---

Body 3.

---
id: dup-4
trigger: when coding
confidence: 0.8
domain: general
---

Body 4.

---
id: dup-5
trigger: when coding
confidence: 0.8
domain: general
---

Body 5.

---
id: dup-6
trigger: when coding
confidence: 0.8
domain: general
---

Body 6.
""",
        encoding="utf-8",
    )
    for idx in range(1, 7):
        existing = SAMPLE_INSTINCT_YAML.replace("test-instinct", f"dup-{idx}")
        (project["instincts_personal"] / f"dup-{idx}.yaml").write_text(existing, encoding="utf-8")

    args = SimpleNamespace(source=str(source), scope="project", dry_run=False, force=True, min_confidence=None)
    assert _mod.cmd_import(args) == 0
    out = capsys.readouterr().out
    assert "SKIP (6 - already exists with equal/higher confidence)" in out
    assert "... and 1 more" in out
    assert "Nothing to import." in out


def test_cmd_import_cancels_on_confirmation(patch_globals, monkeypatch, tmp_path, capsys):
    tree = patch_globals
    project = _make_project(tree)
    monkeypatch.setattr(_mod, "detect_project", lambda: project)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    source = tmp_path / "source.yaml"
    source.write_text(
        """\
---
id: fresh-instinct
trigger: when testing
confidence: 0.9
domain: testing
---

Body.
""",
        encoding="utf-8",
    )

    args = SimpleNamespace(source=str(source), scope="project", dry_run=False, force=False, min_confidence=None)
    assert _mod.cmd_import(args) == 0
    assert "Cancelled." in capsys.readouterr().out


def test_cmd_import_global_scope_writes_source_repo_and_handles_stale_delete_failure(
    patch_globals, global_project, monkeypatch, tmp_path
):
    tree = patch_globals
    monkeypatch.setattr(_mod, "detect_project", lambda: global_project)

    existing = tree["global_personal"] / "shared.yaml"
    existing.write_text(
        """\
---
id: shared-instinct
trigger: when coding
confidence: 0.2
domain: general
scope: global
---

Old body.
""",
        encoding="utf-8",
    )

    source = tmp_path / "source.yaml"
    source.write_text(
        """\
---
id: shared-instinct
trigger: when coding
confidence: 0.9
domain: general
source_repo: github.com/test/repo
---

Updated body.
""",
        encoding="utf-8",
    )

    original_unlink = Path.unlink

    def fake_unlink(self: Path):  # noqa: ANN001
        if self == existing:
            raise OSError("boom")
        return original_unlink(self)

    monkeypatch.setattr(Path, "unlink", fake_unlink)

    args = SimpleNamespace(source=str(source), scope="global", dry_run=False, force=True, min_confidence=None)
    assert _mod.cmd_import(args) == 0
    created_files = list(tree["global_inherited"].glob("source-*.yaml"))
    assert len(created_files) == 1
    content = created_files[0].read_text(encoding="utf-8")
    assert "source_repo: github.com/test/repo" in content
    assert existing.exists()


def test_generate_evolved_skips_empty_trigger(tmp_path):
    evolved_dir = tmp_path / "evolved"
    (evolved_dir / "skills").mkdir(parents=True, exist_ok=True)
    assert _mod._generate_evolved([{"trigger": "", "instincts": [{"id": "x"}], "avg_confidence": 0.9}], [], [], evolved_dir) == []


def test_show_promotion_candidates_and_promote_auto_skip_global_ids(patch_globals, monkeypatch, capsys):
    tree = patch_globals
    project = _make_project(tree)
    (tree["global_personal"] / "shared.yaml").write_text(SAMPLE_GLOBAL_INSTINCT_YAML.replace("global-instinct", "shared"), encoding="utf-8")

    shared = {"id": "shared", "confidence": 0.9, "content": "## Action\nDo it"}
    other = {"id": "other", "confidence": 0.9, "content": "## Action\nDo it"}
    cross = {"shared": [("proj1", "one", shared), ("proj2", "two", shared)], "other": [("proj1", "one", other), ("proj2", "two", other)]}

    original_load_instincts_from_dir = _mod._load_instincts_from_dir
    monkeypatch.setattr(_mod, "_find_cross_project_instincts", lambda: cross)
    monkeypatch.setattr(_mod, "_load_instincts_from_dir", lambda *args, **kwargs: [])
    _mod._show_promotion_candidates(project)
    assert "other" in capsys.readouterr().out

    monkeypatch.setattr(_mod, "_find_cross_project_instincts", lambda: cross)
    monkeypatch.setattr(_mod, "_load_instincts_from_dir", original_load_instincts_from_dir)
    assert _mod._promote_auto(project, force=True, dry_run=True) == 0
    assert "shared" not in capsys.readouterr().out


def test_collect_pending_and_parse_created_date_error_paths(patch_globals, monkeypatch, capsys):
    tree = patch_globals
    pending_dir = tree["global_personal"].parent / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    pending_file = pending_dir / "old.yaml"
    pending_file.write_text("body only\n", encoding="utf-8")

    original_stat = Path.stat

    def fake_stat(self: Path, *args, **kwargs):  # noqa: ANN001
        if self == pending_file:
            raise OSError("boom")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)
    assert _mod._parse_created_date(pending_file) is None

    monkeypatch.setattr(Path, "stat", original_stat)
    monkeypatch.setattr(_mod, "_collect_pending_dirs", lambda: [pending_dir])
    monkeypatch.setattr(_mod, "_parse_created_date", lambda file_path: None)
    assert _mod._collect_pending_instincts() == []
    assert "could not parse age" in capsys.readouterr().err


def test_cmd_prune_reports_no_expired_pending(patch_globals, capsys):
    tree = patch_globals
    pending_dir = tree["global_personal"].parent / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    recent_file = pending_dir / "recent.yaml"
    recent_file.write_text("body only\n", encoding="utf-8")
    recent_timestamp = int((datetime.now(UTC) - timedelta(days=1)).timestamp())
    os.utime(recent_file, (recent_timestamp, recent_timestamp))

    assert _mod.cmd_prune(SimpleNamespace(max_age=30, dry_run=True, quiet=False)) == 0
    assert "No pending instincts older than 30 days." in capsys.readouterr().out


def test_cmd_prune_logs_delete_failure(patch_globals, monkeypatch, capsys):
    tree = patch_globals
    pending_dir = tree["global_personal"].parent / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    expired_file = pending_dir / "expired.yaml"
    expired_file.write_text("body only\n", encoding="utf-8")
    expired_timestamp = int((datetime.now(UTC) - timedelta(days=60)).timestamp())
    os.utime(expired_file, (expired_timestamp, expired_timestamp))

    original_unlink = Path.unlink

    def fake_unlink(self: Path):  # noqa: ANN001
        if self == expired_file:
            raise OSError("boom")
        return original_unlink(self)

    monkeypatch.setattr(Path, "unlink", fake_unlink)
    assert _mod.cmd_prune(SimpleNamespace(max_age=30, dry_run=False, quiet=False)) == 0
    captured = capsys.readouterr()
    assert "No pending instincts older than 30 days." in captured.out
    assert "Warning: Failed to delete" in captured.err


def test_show_promotion_candidates_handles_empty_cross(capsys, monkeypatch):
    monkeypatch.setattr(_mod, "_find_cross_project_instincts", lambda: {})

    _mod._show_promotion_candidates({})

    assert capsys.readouterr().out == ""


def test_show_promotion_candidates_skips_global_ids(capsys, monkeypatch):
    cross = {
        "shared": [("proj1", "one", {"id": "shared", "confidence": 0.9, "content": "## Action\nDo it"})],
        "other": [("proj1", "one", {"id": "other", "confidence": 0.9, "content": "## Action\nDo it"})],
    }

    monkeypatch.setattr(_mod, "_find_cross_project_instincts", lambda: cross)
    monkeypatch.setattr(
        _mod,
        "_load_instincts_from_dir",
        lambda directory, source_type, scope_label: [{"id": "shared"}] if scope_label == "global" else [],
    )

    _mod._show_promotion_candidates({})

    out = capsys.readouterr().out
    assert "other" in out
    assert "shared" not in out


def test_main_module_entrypoint_uses_sys_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["learn-cli.py"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("devgear.skills.learn.cli", run_name="__main__")

    assert excinfo.value.code == 1
