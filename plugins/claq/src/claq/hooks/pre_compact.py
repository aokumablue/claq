"""セッション追跡のためコンテキスト圧縮イベントをログに記録

トリガー: pre:compact
入力: なし（圧縮イベントメタデータ）
出力: 圧縮ログにエントリを追加
終了: 0。失敗は例外のまま launcher へ伝わり exit 1（PreCompact をブロックしない非ブロッキングのエラー）
"""

from __future__ import annotations

from claq.lib.core_utils import (
    append_file,
    get_datetime_string,
    get_sessions_dir,
    log,
)


def main() -> int:
    """コンパクション発生をログに記録する。

    Returns:
        終了コード 0。
    """
    sessions_dir = get_sessions_dir()
    append_file(sessions_dir / "compaction-log.txt", f"[{get_datetime_string()}] Context compaction triggered\n")
    log("[PreCompact] State saved before compaction")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
