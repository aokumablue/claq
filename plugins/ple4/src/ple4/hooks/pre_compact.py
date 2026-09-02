"""セッション追跡のためコンテキスト圧縮イベントをログに記録

トリガー: pre:compact
入力: なし（圧縮イベントメタデータ）
出力: 圧縮ログにエントリを追加
終了: 0（常に成功）
"""

from __future__ import annotations

from ple4.lib.core_utils import (
    append_file,
    ensure_dir,
    get_datetime_string,
    get_sessions_dir,
    log,
)


def main() -> int:
    """コンパクション発生をログに記録する。

    Args:
        引数はありません。

    Returns:
        終了コード（0: 成功、その他: エラー）

    Raises:
        例外は発生しません（エラーは終了コードで返す）。
    """
    try:
        sessions_dir = get_sessions_dir()
        compaction_log = sessions_dir / "compaction-log.txt"

        ensure_dir(sessions_dir)
        append_file(compaction_log, f"[{get_datetime_string()}] Context compaction triggered\n")

        log("[PreCompact] State saved before compaction")
    except Exception as err:  # noqa: BLE001 - フックはノンブロッキングのままにする必要がある
        log(f"[PreCompact] Error: {err}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
