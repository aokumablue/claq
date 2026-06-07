"""セッション状態追跡でセッション終了をマーク

トリガー: stop:session:end:marker
入力: セッションメタデータを含む JSON
出力: 入力を変更せずにパススルー
終了: 0（常に成功）
"""

from __future__ import annotations

from bluecore.hooks.hook_common import read_raw_stdin


def main() -> int:
    """セッション終了時のマーカーとして標準入力を読み捨てる。

    Args:
        引数はありません（標準入力から読み取る）。

    Returns:
        終了コード（0: 成功）

    Raises:
        例外は発生しません。
    """
    read_raw_stdin()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
