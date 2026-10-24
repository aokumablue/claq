"""config_protection / bash_config_protection の symlink 経由バイパス回帰テスト。

`alias -> pyproject.toml` のような symlink 経由の書込みは、raw path の
basename（``alias``）だけを見る判定をすり抜ける。
両モジュールが `hook_common.resolve_effective_target` で実体 path を解決
してから basename 判定することを、Edit/Write（`config_protection`）と
Bash 直接書込み（`bash_config_protection`）の両経路で確認する。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claq.hooks import bash_config_protection, config_protection

from ._support import _repo_scope as _repo_scope
from ._support import _run, _write


def _link(tmp_path: Path, link: str, target: str, *, target_exists: bool = True) -> None:
    """repo ルート（tmp_path）に ``link -> target`` を作る。target_exists が偽なら壊れた symlink にする。"""
    if target_exists:
        (tmp_path / target).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / target).write_text("", encoding="utf-8")
    (tmp_path / link).symlink_to(tmp_path / target)


@pytest.mark.parametrize(
    ("file_path", "target", "target_exists", "expected"),
    [
        pytest.param("alias-file", ".ruff.toml", True, 2, id="symlink-to-protected-denied-by-real-basename"),
        pytest.param("alias-safe", "README.md", True, 0, id="symlink-to-unprotected-allowed"),
        # 壊れた symlink は解決不能として raw basename にフォールバックし、クラッシュしない。
        pytest.param("broken-link", "does-not-exist", False, 0, id="broken-symlink-allowed"),
        # symlink を介さない直接 path 指定は deny する（回帰防止）。
        pytest.param(".ruff.toml", None, False, 2, id="direct-protected-path"),
    ],
)
def test_write_through_symlink(
    file_path: str,
    target: str | None,
    target_exists: bool,
    expected: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edit/Write 経路は symlink の実体 basename で保護判定する。"""
    if target is not None:
        _link(tmp_path, file_path, target, target_exists=target_exists)

    assert _run("config_protection", _write(file_path), monkeypatch) == expected


def test_effective_basename_falls_back_when_resolve_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_effective_target が None を返す場合、raw path の basename にフォールバックする。"""
    monkeypatch.setattr(config_protection, "resolve_effective_target", lambda _path: None)

    assert config_protection._effective_basename(".ruff.toml") == ".ruff.toml"


@pytest.mark.parametrize(
    ("command", "link", "target", "target_exists", "expected"),
    [
        pytest.param("printf x > alias-file", "alias-file", ".ruff.toml", True, ".ruff.toml", id="redirect-to-protected"),
        pytest.param("tee alias-tee <<< 'x'", "alias-tee", ".ruff.toml", True, ".ruff.toml", id="tee-to-protected"),
        pytest.param("printf x > alias-safe", "alias-safe", "notes.txt", True, None, id="unprotected-target"),
        # repo スコープの契約を維持: 解決後 target がリポジトリ外なら allow。
        pytest.param(
            "printf x > alias-outside",
            "alias-outside",
            "../outside-repo-root-fixture/.ruff.toml",
            True,
            None,
            id="target-outside-repo-root",
        ),
        pytest.param("printf x > broken-link", "broken-link", "does-not-exist", False, None, id="broken-symlink"),
    ],
)
def test_bash_write_through_symlink(
    command: str, link: str, target: str, target_exists: bool, expected: str | None, tmp_path: Path
) -> None:
    """Bash 直接書込み経路も symlink の実体で保護判定し、実体の basename を返す。"""
    _link(tmp_path, link, target, target_exists=target_exists)

    assert bash_config_protection.find_protected_write(command) == expected
