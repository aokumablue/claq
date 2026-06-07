"""パス検証ユーティリティ — bluecore.mem パッケージ内で使用。

仕様は src/model_build/_paths.py と共有する。
両者は配布物として独立しているため import 関係を持たせず、同一仕様のコピーを維持する。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 4 * 1024 * 1024  # 4 MB 読み取りバッファ


def safe_join(base: Path, name: str) -> Path:
    """name を base に結合し、base 配下に収まることを検証する（パストラバーサル防止）。

    エラーメッセージは repr(name) で制御文字混入を可視化する。
    Python 3.12+ の Path.is_relative_to を使用（install.sh で確保済み）。
    """
    resolved = (base / name).resolve()
    base_resolved = base.resolve()
    if resolved != base_resolved and not resolved.is_relative_to(base_resolved):
        raise ValueError(f"不正なパス: {repr(name)} は許可されたディレクトリ外を指しています")
    return resolved


def validate_sha256_format(value: str, label: str) -> None:
    """SHA256 文字列が 64 文字の16進数であることを検証する。

    大文字・小文字の混在を int(value, 16) で正規化して判定する。
    """
    if len(value) != 64:
        raise ValueError(f"不正な SHA256 値 ({label}): 長さ {len(value)} != 64")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"不正な SHA256 値 ({label}): 16進数でない文字を含んでいます") from exc


def sha256_file(path: Path) -> str:
    """ファイルの SHA256 ハッシュをストリーミングで計算して返す（大ファイル対応）。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()
