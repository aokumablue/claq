"""claq 本体がランタイムでサードパーティ依存を持たないことを機械的に保証するテスト。

`src/claq/**/*.py` を `ast` で走査し、全 import 文のトップレベルモジュール名が
明示の許可集合 :data:`ALLOWED_TOP_LEVEL_MODULES` に収まることを表明する。

なぜ ``sys.stdlib_module_names`` を直接使わないか（H-12）:
    以前は ``frozenset(sys.stdlib_module_names)`` を許可集合にしていた。これは
    **テストを実行しているインタプリタ**の標準ライブラリであって、CLAUDE.md と
    ``launcher.py`` が宣言する対応下限 3.12 のそれではない。開発 venv は 3.14 な
    ので、3.14 では標準ライブラリだが 3.12 に存在しない名前（``annotationlib`` /
    ``compression`` / ``_interpreters`` など）を import しても本テストは緑のまま
    通り、3.12 ホストでは ImportError で落ちていた。つまり CLAUDE.md が掲げる
    「依存ゼロは本テストが機械的に保証する」という主張が、保証すべき下限に対して
    成立していなかった。

なぜ「3.12 の標準ライブラリ全一覧」ではなく「実際に import している名前の一覧」か:
    3.12 のインタプリタが手元に無い環境では全一覧を機械生成できず、手写しでは
    誤りが検査されないまま残る。一方この一覧は下の
    :func:`test_allowlist_has_no_unused_entry` が実際の import 集合と完全一致を
    強制するため、内容が機械的に裏付けられる。新しい import を足すときは本集合へ
    1 行加える必要があり、そのレビュー時点で「3.12 に存在するか」を人間が確認する
    関門になる。許可集合としては ``sys.stdlib_module_names`` より狭いので、
    サードパーティ import の検出力は落ちない。

デシジョンテーブル:
  - `import a.b.c` → トップレベル名 `a` を収集
  - `from a.b import c` → トップレベル名 `a` を収集
  - `from . import x` / `from ..y import z`（`node.level > 0`） → 自己参照なので除外
  - 関数内 import・条件付き import → `ast.walk` で収集対象に含める
  - `ALLOWED_TOP_LEVEL_MODULES` に載る名前 → 許可
  - それ以外 → 違反（ファイル名・行番号・モジュール名付きで失敗させる）
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "claq"

# CLAUDE.md / `launcher.py` が宣言する対応 Python の下限。
MIN_PYTHON = (3, 12)

# claq が import してよいトップレベルモジュール名。
#
# `claq` 以外はすべて Python 3.12 の標準ライブラリである（最も新しい `tomllib`
# でも 3.11 追加で、下限を下回らない）。ここへ名前を足すときは、追加した
# モジュールが 3.12 に存在することを確認すること —— 実行中インタプリタに
# あるかどうかは根拠にならない（それが H-12 の欠陥そのもの）。
ALLOWED_TOP_LEVEL_MODULES = frozenset({
    "__future__",
    "argparse",
    "collections",
    "contextlib",
    "dataclasses",
    "datetime",
    "functools",
    "getpass",
    "hashlib",
    "json",
    "logging",
    "os",
    "pathlib",
    "claq",
    "queue",
    "re",
    "runpy",
    "shlex",
    "sqlite3",
    "stat",
    "subprocess",
    "sys",
    "tempfile",
    "threading",
    "time",
    "tomllib",
    "types",
    "typing",
    "warnings",
})

# 3.14 では `sys.stdlib_module_names` に含まれるが 3.12 には存在しない名前。
# 旧実装（実行中インタプリタ基準）ではこれらが素通りしていたため、下限基準へ
# 移ったことを直接表明するための対照サンプルとして持つ。
_STDLIB_ADDED_AFTER_MIN_PYTHON = frozenset({"annotationlib", "compression", "_interpreters"})


def _iter_source_files() -> list[Path]:
    """走査対象となる claq パッケージ配下の Python ソースを列挙する。"""
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _iter_imports(source_file: Path) -> list[tuple[str, int]]:
    """1 ファイルから (トップレベルモジュール名, 行番号) の一覧を抽出する。

    相対 import は自己参照のため除外する。関数内や条件分岐内の import も
    `ast.walk` で漏れなく拾う。
    """
    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    imports: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imports.append((node.module.split(".")[0], node.lineno))
    return imports


def _imported_top_level_modules() -> frozenset[str]:
    """claq パッケージ全体が import しているトップレベル名の集合を返す。"""
    return frozenset(module for path in _iter_source_files() for module, _ in _iter_imports(path))


class TestRuntimeDependencies:
    """ランタイム依存ゼロ（標準ライブラリのみ）の静的ガード"""

    def test_source_files_are_discovered(self) -> None:
        """走査対象が 0 件だと表明が空振りするため、ソースの存在自体を確認する。"""
        assert _iter_source_files(), f"claq のソースが見つかりません: {PACKAGE_ROOT}"

    def test_imports_are_collected(self) -> None:
        """import 収集が機能していること（収集器の不具合による偽陰性を防ぐ）。"""
        collected = _imported_top_level_modules()
        assert "claq" in collected
        assert "pathlib" in collected

    def test_no_third_party_runtime_imports(self) -> None:
        """claq パッケージが許可集合の外を import しないこと。"""
        violations: list[str] = []
        for source_file in _iter_source_files():
            for module, lineno in _iter_imports(source_file):
                if module not in ALLOWED_TOP_LEVEL_MODULES:
                    relative = source_file.relative_to(PACKAGE_ROOT.parents[1])
                    violations.append(f"{relative}:{lineno}: {module}")

        assert not violations, (
            "claq はランタイム依存ゼロ（Python "
            f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]} の標準ライブラリのみ）である必要があります。"
            "許可集合外の import を検出しました:\n  " + "\n  ".join(sorted(violations))
        )

    def test_allowlist_has_no_unused_entry(self) -> None:
        """許可集合が実際の import 集合と完全一致すること。

        許可集合が実態より広いと、そのぶん「何を import してよいか」の宣言が
        緩み、最終的に旧実装（`sys.stdlib_module_names` 丸ごと）へ戻るのと同じ
        状態になる。使わなくなった名前は削除する（後方互換の残置はしない）。
        """
        assert ALLOWED_TOP_LEVEL_MODULES == _imported_top_level_modules()

    def test_allowlist_entries_exist_in_running_stdlib(self) -> None:
        """許可集合の各名前が実行中インタプリタの標準ライブラリにも在ること。

        綴り誤りと、下限より後のバージョンで**削除**された名前（PEP 594 で
        3.13 が落とした `cgi` 等）を検出する。3.12 に在ることの確認は追加時の
        人間の責務で、本テストはその逆方向（上限側での消失）を受け持つ。
        """
        missing = sorted((ALLOWED_TOP_LEVEL_MODULES - {"claq"}) - set(sys.stdlib_module_names))
        assert not missing, f"実行中 Python の標準ライブラリに存在しない許可名: {missing}"

    def test_allowlist_excludes_stdlib_added_after_minimum(self) -> None:
        """下限より後に標準ライブラリ入りした名前を許可しないこと。

        許可集合を実行中インタプリタから作っていた頃はこれらが素通りしていた
        （H-12）。ここが赤くなるのは許可集合の基準が実行中バージョンへ戻った
        ときであり、判定軸が下限 3.12 のままかを直接見張る対照。

        実行中インタプリタの ``sys.stdlib_module_names`` とは照合しない。対照
        サンプルは定義上「3.12 より後に追加された」名前なので、下限の 3.12 で
        走らせれば当然すべて不在になり、対照の実在確認は 3.12 で必ず失敗する
        —— 対応ランタイムで偽 FAIL するテストになる。3.14 の開発機でしか
        気付けないその型の欠陥こそ、本テストが直している当のものである。
        """
        assert not (_STDLIB_ADDED_AFTER_MIN_PYTHON & ALLOWED_TOP_LEVEL_MODULES)
