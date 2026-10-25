"""tests/skills/test_html_gen_*.py が使う補助（html-gen のテンプレート同梱スクリプトの読み込みと、サイト複製・編集の道具）。

`check_site.py` と `fetch_fonts.py` の読み込みは import 時にこのモジュールで 1 回だけ行い、各テストファイルはその結果を import する。
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import types
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_SKILL_DIR = _PLUGIN_ROOT / "skills" / "html-gen"
_TEMPLATE = _SKILL_DIR / "assets" / "template"
_TOKENS = _SKILL_DIR / "references" / "tokens.json"
_CHECK_SITE = _TEMPLATE / "check_site.py"
_FETCH_FONTS = _TEMPLATE / "fetch_fonts.py"
_SITE_FILES = ("index.html", "styles.css", "tokens.css", "app.js", "check_site.py")


def _load(name: str, path: Path) -> types.ModuleType:
    """ファイル path をモジュール name として読み込む（`assets/template/` は配布物なので、読み込みの副作用で `__pycache__` を書かせない）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    original = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = original
    return module


check_site = _load("html_gen_check_site", _CHECK_SITE)
fetch_fonts = _load("html_gen_fetch_fonts", _FETCH_FONTS)
TOKENS = check_site.load_tokens(_TOKENS)
BASES = check_site.base_ids(TOKENS)
HTML = (_TEMPLATE / "index.html").read_text(encoding="utf-8")
CSS = (_TEMPLATE / "styles.css").read_text(encoding="utf-8")
JS = (_TEMPLATE / "app.js").read_text(encoding="utf-8")
TOKENS_CSS = (_TEMPLATE / "tokens.css").read_text(encoding="utf-8")
E2E_JS = (_TEMPLATE / "e2e_contrast.js").read_text(encoding="utf-8")


def _copy_site(tmp_path: Path, name: str = "site") -> Path:
    """検査可能なサイト一式を一時ディレクトリへ複製する。"""
    dest = tmp_path / name
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir()
    for filename in _SITE_FILES:
        shutil.copy(_TEMPLATE / filename, dest / filename)
    shutil.copy(_TOKENS, dest / "tokens.json")
    return dest


def _edit(dest: Path, filename: str, *replacements: tuple[str, str]) -> str:
    """サイト内のファイルを置換して書き戻し、違反を 1 本の文字列で返す（置換元が無ければ空振りなので落とす）。"""
    path = dest / filename
    text = path.read_text(encoding="utf-8")
    for old, new in replacements:
        assert old in text, f"{filename} に置換元が無い: {old!r}"
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    return "\n".join(check_site.validate_site(dest))


def _append(dest: Path, filename: str, snippet: str) -> str:
    """サイト内のファイルへ追記し、違反を 1 本の文字列で返す。"""
    path = dest / filename
    path.write_text(path.read_text(encoding="utf-8") + snippet, encoding="utf-8")
    return "\n".join(check_site.validate_site(dest))
