"""smoke_check.py が使う、サーバを起動しない検査の部品（旧仕様の名前の走査と、同梱するファイルの確認）。

標準ライブラリのみで動く（`mcp` を import すらしない）。生成先のファイルを読むだけで、
サーバは起動しない。旧仕様の API 名・機構のパターン `FORBIDDEN_PATTERN` の正本はここに置く。

smoke 用のモジュールなので配布物には入れない（server.py から import しないので、
pyproject.toml の only-include にも足さない）。このファイルは編集しない。
"""

from __future__ import annotations

import ast
import json
import os
import re
import stat
import tomllib
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

# 旧仕様の API 名・機構（正本）。生成先のファイルに 1 件でも当たれば旧仕様が混入している。
# 各パターンの意味は、生成に使った mcp-gen スキルの references/spec-2026-07-28.md にある。
FORBIDDEN_PATTERN = re.compile(
    r"FastMCP|Mcp-Session-Id|mcp_session_id|resources/(un)?subscribe|Last-Event-ID|logging/setLevel"
    r"|elicitationId|-32002|notifications/(initialized|elicitation/complete)|ctx\.elicit(_url)?\("
    r"|ctx\.(log|info|debug|warning|error)\(|send_log_message\(|create_message\(|list_roots\("
)
# 旧仕様の名前を探すファイルの大きさの上限。超えるファイルは読まずに WARN で知らせる。
MAX_SCANNED_BYTES = 1024 * 1024
# 生成先の外から来たファイル（仮想環境・依存・キャッシュ）は見ない。SDK 自身が旧名を含むため。
SKIPPED_DIRS = frozenset(
    {"__pycache__", ".venv", "venv", "site-packages", ".git", "node_modules", ".pytest_cache", ".ruff_cache",
     ".mypy_cache", "build", "dist"}
)


def _read_lines(path: Path) -> list[str]:
    """通常のファイルだけを、大きさの上限の内で読んで行に分ける。

    シンボリックリンク・FIFO などは開かない（FIFO を開くと書き手が来るまで止まる）。

    Args:
        path: 読むファイル。

    Returns:
        ファイルの行。

    Raises:
        OSError: 通常のファイルでない・上限を超える・読めないとき（文面に理由を書く）。
        UnicodeDecodeError: UTF-8 として読めない（バイナリの）とき。
    """
    status = os.lstat(path)
    if not stat.S_ISREG(status.st_mode):
        raise OSError("通常のファイルではない")
    if status.st_size > MAX_SCANNED_BYTES:
        raise OSError(f"{MAX_SCANNED_BYTES} バイトを超える")
    return path.read_text(encoding="utf-8").splitlines()


def legacy_api_hits(root: Path, *, skip: Iterable[Path]) -> tuple[list[str], list[str]]:
    """root 以下のテキストファイルから、旧仕様の API 名・機構に当たる行を集める。

    バイナリ（.pyc など）は黙って飛ばす。通常のファイルでない・大きすぎる・読めない
    ファイルは、走査しなかったものとして返す（黙って飛ばすと、見ていないのに合格になる）。

    Args:
        root: 走査の起点。
        skip: 走査しないファイル（パターンの定義そのものを含む smoke_static.py と、それを使う smoke_check.py）。

    Returns:
        （当たり `パス:行番号: 行` の一覧、走査しなかったファイル `パス (理由)` の一覧）。
    """
    hits: list[str] = []
    unscanned: list[str] = []
    skipped = {path.resolve() for path in skip}
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = sorted(name for name in subdirs if name not in SKIPPED_DIRS and not name.endswith(".egg-info"))
        for name in sorted(files):
            path = Path(directory, name)
            if path.resolve() in skipped:
                continue
            try:
                lines = _read_lines(path)
            except UnicodeDecodeError:
                continue  # バイナリ（.pyc など）は見ない
            except OSError as error:
                unscanned.append(f"{path.relative_to(root)} ({error.strerror or error})")
                continue
            hits.extend(
                f"{path.relative_to(root)}:{number}: {line.strip()}"
                for number, line in enumerate(lines, 1)
                if FORBIDDEN_PATTERN.search(line)
            )
    return hits, unscanned


def _imported_names(source: Path) -> set[str]:
    """ファイルが絶対 import するモジュールの、先頭の名前を集める（相対 import は除く）。

    関数の中や `if TYPE_CHECKING:` の中の import も数える。

    Args:
        source: Python のファイル。

    Returns:
        `import a.b` なら `a` のような先頭の名前の集合。
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"), filename=str(source))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def _local_entry(root: Path, name: str) -> str | None:
    """import の先頭の名前を、生成先にあるモジュールかパッケージの only-include の要素にする。

    Python の探索と同じく、`__init__.py` のあるパッケージ → `名前.py` → 名前空間パッケージの順に見る。

    Args:
        root: 生成先のディレクトリ。
        name: import の先頭の名前。

    Returns:
        `名前.py` か `名前`。生成先に無い名前（標準ライブラリ・依存）は None。
    """
    package = root / name
    if (package / "__init__.py").is_file():
        return name
    if (root / f"{name}.py").is_file():
        return f"{name}.py"
    return name if package.is_dir() else None


def _sources(root: Path, entry: str) -> list[Path]:
    """only-include の要素が指す Python のファイル（パッケージなら配下の全て。__pycache__ は除く）。

    Args:
        root: 生成先のディレクトリ。
        entry: `名前.py` か `名前`（パッケージ）。

    Returns:
        ファイルの一覧。
    """
    path = root / entry
    if not path.is_dir():
        return [path]
    return sorted(source for source in path.rglob("*.py") if "__pycache__" not in source.relative_to(path).parts)


def local_modules(root: Path) -> set[str]:
    """server.py から import をたどって届く、生成先のモジュールとパッケージを集める。

    パッケージは配下の全てのファイルの import もたどる（adapters.py → facade、facade → 他の
    モジュール）。生成先に無い名前（標準ライブラリ・依存）は無視する。

    Args:
        root: 生成先のディレクトリ（server.py がある）。

    Returns:
        only-include の要素の形（`server.py`・`guard.py`・`facade` など）の集合。server.py 自身を含む。
    """
    found = {"server.py"}
    pending = [root / "server.py"]
    while pending:
        for name in _imported_names(pending.pop()):
            entry = _local_entry(root, name)
            if entry is not None and entry not in found:
                found.add(entry)
                pending.extend(_sources(root, entry))
    return found


def packaging_gaps(root: Path) -> list[str]:
    """生成先の pyproject.toml の only-include が覆わない、server.py から届くモジュールを返す。

    要素の `./` と末尾の `/` は無視し、ディレクトリの要素はその下を覆うものとする。
    パッケージは、配下の全ての .py が覆われていれば足りる。

    Args:
        root: 生成先のディレクトリ（server.py と pyproject.toml がある）。

    Returns:
        足りない only-include の要素（名前順）。

    Raises:
        AssertionError: `[tool.hatch.build.targets.wheel]` に only-include（文字列の配列）が無いとき。
    """
    with (root / "pyproject.toml").open("rb") as file:
        settings = tomllib.load(file)
    wheel = settings.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {})
    included = wheel.get("only-include")
    modules = local_modules(root)
    assert isinstance(included, list) and all(isinstance(item, str) for item in included), (
        "pyproject.toml の [tool.hatch.build.targets.wheel] に only-include（文字列の配列）が無いため、"
        "wheel に同梱するファイルを確かめられません。pyproject.toml.template と同じく "
        f"only-include = {json.dumps(sorted(modules))} のように、server.py から import するモジュールを並べてください"
    )
    covering = [PurePosixPath(item).parts for item in included]

    def covered(source: Path) -> bool:
        """ファイルが only-include のどれか（同じパスか、祖先のディレクトリ）に覆われるか。"""
        parts = PurePosixPath(source.relative_to(root).as_posix()).parts
        return any(parts[: len(prefix)] == prefix for prefix in covering)

    return sorted(entry for entry in modules if not all(covered(source) for source in _sources(root, entry)))
