"""実行中の plugin root の ``runtime/`` と ``src/claq/`` を、Edit/Write と Bash の両方で守る検査。

全保護フックはこのコードから起動するので、書き換えれば次のツール呼び出しから全部が外れる。ただし
plugin root が作業中リポジトリ（git の toplevel）の内側にある開発用 checkout は対象外にする。

スイート自体は開発用 checkout の中で走るため、そのままでは除外が効いて再現が赤にならず、本物の git が
lru_cache 付きで走る。各テストは `config_protection.get_plugin_root` を tmp_path の外に作った
キャッシュ風の root へ、`config_protection.resolve_repo_root` を「toplevel が決まらない」へ差し替える。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from claq.hooks import bash_config_protection, config_protection

from ._support import _EXPECTED_VOCABULARIES, _bash, _run, _write
from ._support import _repo_scope as _repo_scope


@pytest.fixture(autouse=True)
def _running_root(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """実行中の plugin root を、cwd（tmp_path）の外にあるキャッシュ風のディレクトリへ差し替える。

    Args:
        tmp_path_factory: pytest の一時ディレクトリ生成フィクスチャ。
        monkeypatch: pytest の monkeypatch フィクスチャ。

    Returns:
        差し替えた plugin root（実体パス）。
    """
    root = tmp_path_factory.mktemp("cache").resolve() / "m" / "claq" / "0"
    (root / "runtime").mkdir(parents=True)
    (root / "runtime" / "claq-hook").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "runtime" / "claq-hook.cmd").write_text("@echo off\n", encoding="utf-8")
    (root / "src" / "claq" / "hooks").mkdir(parents=True)
    (root / "src" / "claq" / "launcher.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(config_protection, "get_plugin_root", lambda: root)
    monkeypatch.setattr(config_protection, "resolve_repo_root", lambda: None)
    return root


def _edit(file_path: str) -> dict[str, Any]:
    """file_path への Edit ツール呼び出しの payload を組み立てる。"""
    return {"tool_name": "Edit", "tool_input": {"file_path": file_path, "old_string": "a", "new_string": "b"}}


@pytest.mark.parametrize(
    "relative", ["runtime/claq-hook", "runtime/claq-hook.cmd", "src/claq/launcher.py", "src/claq/hooks/x.py"]
)
def test_write_and_edit_into_running_plugin_are_denied(
    relative: str,
    _running_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """実行中の plugin root の ``runtime/`` と ``src/claq/`` 配下への Write と Edit を deny する。"""
    target = str(_running_root / relative)

    codes = (_run("config_protection", _write(target), monkeypatch), _run("config_protection", _edit(target), monkeypatch))
    err = capsys.readouterr().err

    assert (codes, err.count("running claq plugin")) == ((2, 2), 2)


@pytest.mark.parametrize("relative", ["RUNTIME/claq-hook", "Src/Claq/x.py"])
def test_case_variant_of_running_plugin_path_is_denied(
    relative: str, _running_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """APFS / NTFS で同じ場所を指す大小違いの綴りも deny する。"""
    assert _run("config_protection", _write(str(_running_root / relative)), monkeypatch) == 2


def test_symlink_into_running_runtime_is_denied(
    _running_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """symlink 経由で実行中のランタイムへ書く Write も、実体パスで deny する。"""
    alias = tmp_path / "alias"
    alias.symlink_to(_running_root / "runtime" / "claq-hook")

    assert _run("config_protection", _write(str(alias)), monkeypatch) == 2


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("printf x > {root}/runtime/claq-hook", "runtime/"),
        ("sed -i s/a/b/ {root}/src/claq/launcher.py", "src/claq/"),
        ("cp evil {root}/runtime/claq-hook", "runtime/"),
        ("chmod -x {root}/runtime/claq-hook", "runtime/"),
        ("rm -rf {root}/src", "src/claq/"),
        ("rm -rf {root}/src/claq", "src/claq/"),
        ("mv {root}/runtime x", "runtime/"),
    ],
)
def test_bash_write_into_running_plugin_is_denied(template: str, expected: str, _running_root: Path) -> None:
    """Bash の書込み・削除・退避が、実行中の plugin root の保護ディレクトリ（配下・自身・祖先）に当たれば塞ぐ。"""
    assert bash_config_protection.find_protected_write(template.format(root=_running_root)) == expected


def test_bash_write_into_running_plugin_is_blocked_with_message(
    _running_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bash 経路も main() で exit 2 になり、実行中のプラグインを守る理由を出す。"""
    code = _run("bash_config_protection", _bash(f"printf x > {_running_root}/runtime/claq-hook"), monkeypatch)

    assert (code, "running claq plugin" in capsys.readouterr().err) == (2, True)


@pytest.mark.parametrize(
    "item", sorted(_EXPECTED_VOCABULARIES["config_protection._RUNNING_PLUGIN_GUARDED_DIRS"], key=repr)
)
def test_every_running_plugin_guarded_dir_is_denied_on_write_and_bash(
    item: tuple[str, ...], _running_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_RUNNING_PLUGIN_GUARDED_DIRS` の全要素が Write と Bash で deny を生み、同じ名前の別の場所は通すこと（層1）。"""
    inside = _running_root.joinpath(*item, "x")
    elsewhere = _running_root.joinpath("docs", *item, "x")

    verdicts = (
        _run("config_protection", _write(str(inside)), monkeypatch),
        bash_config_protection.find_protected_write(f"printf x > {inside}") is not None,
        bash_config_protection.find_protected_write(f"rm -rf {_running_root / item[0]}") is not None,
        _run("config_protection", _write(str(elsewhere)), monkeypatch),
        bash_config_protection.find_protected_write(f"printf x > {elsewhere}") is not None,
    )

    assert verdicts == (2, True, True, 0, False)


@pytest.mark.parametrize("item", sorted(_EXPECTED_VOCABULARIES["host_guard_targets._RUNTIME_ENTRYPOINT_NAMES"]))
def test_every_runtime_entrypoint_name_is_denied_after_cd(item: str, _running_root: Path) -> None:
    """`cd` を含むコマンドでは、起動ファイル名の書込み先を塞ぎ、`cd` が無ければ通すこと（層1）。"""
    verdicts = (
        bash_config_protection.find_protected_write(f"cd {_running_root}/runtime && printf x > {item}"),
        bash_config_protection.find_protected_write(f"printf x > {item}"),
    )

    assert verdicts == ("runtime/", None)


@pytest.mark.parametrize(
    ("toplevel", "tracked", "expected"),
    [
        # plugin root が作業中リポジトリの内側にある開発用 checkout は、runtime も src も編集できる。
        pytest.param("parent", True, (0, 0, None, None), id="dev-checkout-inside-working-repo"),
        # toplevel の綴りが大小違いでも、開発用 checkout として除外する。
        pytest.param("parent-upper", True, (0, 0, None, None), id="dev-checkout-match-ignores-case"),
        # ホームを git で管理していて plugin root が toplevel の内側に入っても、追跡外なら守る。
        pytest.param("parent", False, (2, 2, "runtime/", "runtime/"), id="untracked-plugin-inside-working-repo"),
        # 作業中リポジトリが plugin root を含まなければ、保護は外れない（追跡判定は差し替えない）。
        pytest.param("unrelated", None, (2, 2, "runtime/", "runtime/"), id="unrelated-working-repo"),
    ],
)
def test_dev_checkout_exclusion(
    toplevel: str,
    tracked: bool | None,
    expected: tuple[Any, ...],
    _running_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """toplevel と追跡状態ごとの、runtime / src への Write と直接・cd 経由の Bash 書込みの判定。"""
    repo = {
        "parent": _running_root.parents[1],
        "parent-upper": Path(str(_running_root.parents[1]).upper()),
        "unrelated": tmp_path,
    }[toplevel]
    monkeypatch.setattr(config_protection, "resolve_repo_root", lambda: repo)
    if tracked is not None:
        monkeypatch.setattr(config_protection, "_plugin_runtime_is_tracked", lambda _repo, _root: tracked)

    verdicts = (
        _run("config_protection", _write(str(_running_root / "runtime" / "claq-hook")), monkeypatch),
        _run("config_protection", _write(str(_running_root / "src" / "claq" / "launcher.py")), monkeypatch),
        bash_config_protection.find_protected_write(f"printf x > {_running_root}/runtime/claq-hook"),
        bash_config_protection.find_protected_write(f"cd {_running_root}/runtime && printf x > claq-hook"),
    )

    assert verdicts == expected


@pytest.mark.parametrize(("tracked", "expected"), [(True, True), (False, False)], ids=["tracked", "untracked"])
def test_plugin_runtime_is_tracked_asks_git_index(
    _running_root: Path, tracked: bool, expected: bool
) -> None:
    """ランタイム入口が index に載っているかを実際の git で判定する。"""
    repo = _running_root.parents[1]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    if tracked:
        subprocess.run(["git", "-C", str(repo), "add", "--", str(_running_root / "runtime" / "claq-hook")], check=True)

    assert config_protection._plugin_runtime_is_tracked(repo, _running_root) is expected


@pytest.mark.parametrize("error", [FileNotFoundError("git"), subprocess.TimeoutExpired("git", 5)], ids=["no-git", "timeout"])
def test_plugin_runtime_is_tracked_is_false_when_git_fails(
    _running_root: Path, error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git を起動できない・時間切れなら追跡外として扱い、保護を外さない。"""

    def _raise(*_args: object, **_kwargs: object) -> None:
        """git の起動失敗を模す。"""
        raise error

    monkeypatch.setattr(config_protection.subprocess, "run", _raise)

    assert config_protection._plugin_runtime_is_tracked(_running_root.parents[1], _running_root) is False


def test_lookalikes_are_allowed(_running_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """保護ディレクトリに似ているが当たらない書込み先は通す。"""
    writes = tuple(
        _run("config_protection", _write(str(path)), monkeypatch)
        for path in (
            _running_root / "runtimes" / "x",
            _running_root / "src" / "claqx" / "x.py",
            _running_root / "docs" / "x.md",
            tmp_path / "runtime" / "claq-hook",
        )
    )
    commands = tuple(
        bash_config_protection.find_protected_write(command)
        for command in (
            "printf x > runtime/claq-hook",
            "cd sub && printf x > runtime.txt",
            f"rm -rf {_running_root}/docs",
        )
    )

    assert (writes, commands) == ((0, 0, 0, 0), (None, None, None))


def test_git_is_consulted_only_after_a_positional_hit(
    _running_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git（toplevel の解決）は、書込み先が保護ディレクトリに当たったときだけ起動する。"""
    calls: list[None] = []

    def _record() -> None:
        """呼び出しを記録し、toplevel が決まらない扱いにする。"""
        calls.append(None)

    monkeypatch.setattr(config_protection, "resolve_repo_root", _record)
    _run("config_protection", _write("notes.txt"), monkeypatch)
    bash_config_protection.find_protected_write("printf x > notes.txt")
    bash_config_protection.find_protected_write("cd sub && printf x > notes.txt")
    before_hit = len(calls)
    _run("config_protection", _write(str(_running_root / "runtime" / "claq-hook")), monkeypatch)

    assert (before_hit, len(calls) > 0) == (0, True)


@pytest.mark.parametrize("display", ["runtime/", "src/claq/"])
def test_bash_message_for_running_plugin_is_selected(display: str) -> None:
    """Bash 側は実行中のプラグインの表示名に、その保護理由の文言を選ぶ。"""
    assert "running claq plugin" in bash_config_protection._blocked_message(display)


def test_unresolvable_path_skips_running_plugin_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """実体パスを解決できない file_path は位置照合をせず、他の判定に任せる。"""

    def _forbidden() -> None:
        """位置照合に進んだら失敗させる。"""
        pytest.fail("git must not be consulted")

    monkeypatch.setattr(config_protection, "resolve_repo_root", _forbidden)

    assert _run("config_protection", _write("a\x00b"), monkeypatch) == 0
