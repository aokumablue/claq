"""同梱するファイル（pyproject.toml の only-include）と、直接依存の宣言の検査のテスト。"""

from __future__ import annotations

import ast
import json
import re
import shutil
import sys
import tomllib
import types
import unittest
from pathlib import Path

import pytest

from ._support import _MINIMAL_SETTINGS, _OVERLAY, _SKILL_DIR, _TEMPLATE, _load


def _write(root: Path, files: dict[str, str]) -> Path:
    """相対パス → 中身の対応で、ファイルを書く。

    Args:
        root: 書く先のディレクトリ。
        files: 相対パスと中身。

    Returns:
        root。
    """
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content, encoding="utf-8")
    return root


def _pyproject(only_include: list[str] | None) -> str:
    """only-include だけを持つ pyproject.toml の中身を作る。

    Args:
        only_include: only-include の要素。None なら only-include を書かない。

    Returns:
        pyproject.toml の中身。
    """
    wheel = "" if only_include is None else f"only-include = {json.dumps(only_include)}\n"
    return f'[project]\nname = "x"\n\n[tool.hatch.build.targets.wheel]\n{wheel}sources = ["."]\n'


class TestPackaging:
    """pyproject.toml の only-include が、server.py から届くローカルのモジュールを覆うかの検査。"""

    _TREE = {
        "server.py": "from __future__ import annotations\nimport json\nimport mcp.server\nfrom guard import x\nimport pkg.sub as sub\nimport nspkg\n",
        "guard.py": "def x() -> None:\n    import helper\n",
        "helper.py": "",
        "pkg/__init__.py": "from . import sub\n",
        "pkg/sub.py": "from other import y\n",
        "pkg/__pycache__/stale.py": "import ghost\n",
        "other.py": "",
        "ghost.py": "",
        "unused.py": "",
        "nspkg/mod.py": "",
        "smoke_check.py": "",
        "smoke_static.py": "",
    }

    def test_local_modules_follow_imports(self, smoke_static: types.ModuleType, tmp_path: Path) -> None:
        """server.py から import をたどり、生成先のモジュール・パッケージ（配下も）を集め、外部・相対・__pycache__ は追わない。"""
        root = _write(tmp_path, self._TREE)
        assert smoke_static.local_modules(root) == {"server.py", "guard.py", "helper.py", "pkg", "other.py", "nspkg"}

    @pytest.mark.parametrize(
        ("only_include", "gaps"),
        [
            (["server.py", "guard.py", "helper.py", "pkg", "other.py", "nspkg"], []),
            (["./server.py", "guard.py", "./helper.py", "pkg/", "other.py"], ["nspkg"]),
            (["."], []),
            (["server.py", "guard.py", "helper.py", "pkg/__init__.py", "other.py", "nspkg/mod.py"], ["pkg"]),
            (["server.py", "guard.py", "helper.py", "pkg/__init__.py", "pkg/sub.py", "other.py", "nspkg"], []),
            ([], ["guard.py", "helper.py", "nspkg", "other.py", "pkg", "server.py"]),
        ],
    )
    def test_packaging_gaps(self, smoke_static: types.ModuleType, tmp_path: Path, only_include: list[str], gaps: list[str]) -> None:
        """./ と末尾の / を正規化し、ディレクトリの要素は配下を覆うものとして、足りない要素を名前順に返す。"""
        root = _write(tmp_path, {**self._TREE, "pyproject.toml": _pyproject(only_include)})
        assert smoke_static.packaging_gaps(root) == gaps

    def test_packaging_gaps_need_only_include(self, smoke_static: types.ModuleType, tmp_path: Path) -> None:
        """only-include が無ければ同梱するファイルを確かめられないので、直し方を添えて落とす。"""
        root = _write(tmp_path, {**self._TREE, "pyproject.toml": _pyproject(None)})
        with pytest.raises(AssertionError, match="only-include") as missing:
            smoke_static.packaging_gaps(root)
        assert "guard.py" in str(missing.value)

    def test_check_skips_without_pyproject(self, smoke_check: types.ModuleType) -> None:
        """smoke_check.py の隣に pyproject.toml が無ければ SKIP する（スケルトン自身は .template しか持たない）。"""
        with pytest.raises(unittest.SkipTest, match="pyproject.toml"):
            smoke_check.test_packaging_includes_local_modules()

    def test_check_uses_its_own_directory(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """検査は smoke_check.py のディレクトリで閉包を測り、足りなければ名指しで落とす。"""
        root = _write(tmp_path, {**self._TREE, "pyproject.toml": _pyproject(["server.py", "guard.py", "helper.py", "pkg", "other.py"])})
        (root / "smoke_check.py").write_text((_TEMPLATE / "smoke_check.py").read_text(encoding="utf-8"), encoding="utf-8")
        stub = types.ModuleType("smoke_config")
        stub.__dict__.update(_MINIMAL_SETTINGS)
        monkeypatch.setitem(sys.modules, "smoke_config", stub)
        monkeypatch.setitem(sys.modules, "smoke_static", _load("mcp_gen_smoke_static", _TEMPLATE / "smoke_static.py"))
        module = _load("mcp_gen_smoke_check_copy", root / "smoke_check.py")
        with pytest.raises(AssertionError, match=r"\['nspkg'\]"):
            module.test_packaging_includes_local_modules()
        (root / "pyproject.toml").write_text(_pyproject(["server.py", "guard.py", "helper.py", "pkg", "other.py", "nspkg"]), encoding="utf-8")
        module.test_packaging_includes_local_modules()


def _only_include(pyproject_text: str) -> list[str]:
    """pyproject の中身から [tool.hatch.build.targets.wheel] の only-include を読む。

    Args:
        pyproject_text: pyproject.toml の中身。

    Returns:
        only-include の要素。
    """
    return tomllib.loads(pyproject_text)["tool"]["hatch"]["build"]["targets"]["wheel"]["only-include"]


def _overlay_additions() -> list[str]:
    """SKILL.md の手順 2 から、重ね合わせたときに only-include に足す名前を抜き出す（正本は SKILL.md）。

    Returns:
        足す名前（SKILL.md に書かれた順）。
    """
    text = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    sentence = re.search(r"重ね合わせたなら\s*`only-include`\s*に(.+?)を足す。", text, re.DOTALL)
    assert sentence, "SKILL.md の手順 2 に「重ね合わせたなら `only-include` に ... を足す。」がありません"
    names = re.findall(r'`"([^"`]+)"`', sentence.group(1))
    assert names, sentence.group(0)
    return names


def _merged_skeleton(destination: Path) -> Path:
    """template を複製し、facade-overlay を上書きで重ねた生成先を作る（SKILL.md の手順 2 と同じ重ね方）。

    Args:
        destination: 作る先のディレクトリ。

    Returns:
        destination。
    """
    shutil.copytree(_TEMPLATE, destination, dirs_exist_ok=True)
    shutil.copytree(_OVERLAY, destination, dirs_exist_ok=True)
    return destination


class TestSkeletonPackaging:
    """スケルトンの pyproject.toml.template と SKILL.md の手順が、import の閉包を同梱する。"""

    _TEMPLATE_PYPROJECT = (_TEMPLATE / "pyproject.toml.template").read_text(encoding="utf-8")

    def test_template_closure_is_included(self, smoke_static: types.ModuleType) -> None:
        """共通スケルトンの server.py から届くモジュールは、全て template の only-include にある。"""
        assert smoke_static.local_modules(_TEMPLATE) <= set(_only_include(self._TEMPLATE_PYPROJECT))

    def test_overlay_adds_exactly_skill_names(self, smoke_static: types.ModuleType, tmp_path: Path) -> None:
        """重ね合わせの server.py から届くモジュールのうち template に無いものは、SKILL.md が足させる名前と一致する。"""
        root = _merged_skeleton(tmp_path / "dest")
        added = smoke_static.local_modules(root) - set(_only_include(self._TEMPLATE_PYPROJECT))
        assert added == set(_overlay_additions())

    def test_merged_tree_gaps(self, smoke_static: types.ModuleType, tmp_path: Path) -> None:
        """重ね合わせた生成先で、SKILL.md の名前を足せば不足なし、足さなければ adapters.py と facade が足りない。"""
        root = _merged_skeleton(tmp_path / "dest")
        (root / "pyproject.toml").write_text(self._TEMPLATE_PYPROJECT, encoding="utf-8")
        assert smoke_static.packaging_gaps(root) == ["adapters.py", "facade"]
        line = re.search(r"(?m)^only-include = .*$", self._TEMPLATE_PYPROJECT)
        assert line, "pyproject.toml.template に only-include の行がありません"
        extended = json.dumps([*_only_include(self._TEMPLATE_PYPROJECT), *_overlay_additions()])
        (root / "pyproject.toml").write_text(self._TEMPLATE_PYPROJECT.replace(line.group(0), f"only-include = {extended}"), encoding="utf-8")
        assert smoke_static.packaging_gaps(root) == []

    def test_smoke_modules_are_not_shipped(self, smoke_static: types.ModuleType, tmp_path: Path) -> None:
        """smoke 用のモジュールは、server.py から届かず、only-include にも SKILL.md が足させる名前にも無い（配布物に入れない）。"""
        smoke = {"smoke_check.py", "smoke_static.py", "smoke_config.py"}
        assert smoke <= {path.name for path in _TEMPLATE.iterdir()}
        reached = smoke_static.local_modules(_TEMPLATE) | smoke_static.local_modules(_merged_skeleton(tmp_path / "dest"))
        listed = set(_only_include(self._TEMPLATE_PYPROJECT)) | set(_overlay_additions())
        assert (reached & smoke, listed & smoke) == (set(), set())


def _third_party_imports(smoke_static: types.ModuleType, root: Path) -> set[str]:
    """生成先で同梱するモジュールが直接 import する、標準ライブラリでも生成先のものでもない名前を集める。

    Args:
        smoke_static: 読み込んだ smoke_static モジュール（同梱するモジュールの閉包を測る）。
        root: 生成先のディレクトリ。

    Returns:
        import の先頭の名前（`mcp_types` など）。
    """
    shipped = smoke_static.local_modules(root)
    sources = [path for entry in shipped for path in ((root / entry).rglob("*.py") if (root / entry).is_dir() else [root / entry])]
    names = {
        name
        for source in sources
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
        for name in (
            [alias.name.split(".")[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [node.module.split(".")[0]]
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
            else []
        )
    }
    return names - {Path(entry).stem for entry in shipped} - set(sys.stdlib_module_names) - {"__future__"}


@pytest.mark.parametrize("overlay", [False, True])
def test_dependencies_cover_direct_imports(smoke_static: types.ModuleType, tmp_path: Path, overlay: bool) -> None:
    """同梱するモジュールが直接 import するサードパーティは、全て pyproject.toml.template の dependencies にある。"""
    root = _merged_skeleton(tmp_path / "dest") if overlay else _TEMPLATE
    project = tomllib.loads((_TEMPLATE / "pyproject.toml.template").read_text(encoding="utf-8"))["project"]
    declared = {re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0].lower() for requirement in project["dependencies"]}
    imported = {name.lower().replace("_", "-") for name in _third_party_imports(smoke_static, root)}
    assert imported == {"mcp", "mcp-types", "pydantic"}
    assert imported <= declared
