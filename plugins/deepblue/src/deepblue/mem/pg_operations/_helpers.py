"""PgDatabase 操作ミックスイン共通のヘルパー関数。

``pg_database.py`` と各ミックスインの双方から参照される純粋関数を集約する。
``pg_database`` から本モジュールへ依存を一方向に保つことで循環 import を避ける。
"""

from __future__ import annotations


def _to_json(val: list | dict | None) -> str | None:
    """リストや辞書を JSON 文字列に変換。"""
    if val is None:
        return None
    import json

    return json.dumps(val, ensure_ascii=False)
