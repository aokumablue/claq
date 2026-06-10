"""コンテキストが膨らんできたら /compact を提案します。

pre:edit と pre:write で呼び出し回数を数え、閾値に達したら圧縮のタイミングを知らせます。
提案だけを行うノンブロッキングのフックなので、通常の編集処理は止めません。
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from bluecore.hooks.hook_common import read_raw_stdin, write_stderr
from bluecore.lib.core_utils import write_file


def sanitize_session_id(raw: str | None) -> str:
    """セッション ID をサニタイズします。

    Args:
        raw: 生のセッション ID 文字列です。

    Returns:
        ファイルシステム安全なセッション ID を返します。

    Raises:
        例外は発生しません。
    """
    session_id = re.sub(r"[^A-Za-z0-9_-]", "", str(raw or ""))
    return session_id or "default"


def parse_threshold(raw: str | None) -> int:
    """閾値文字列を整数に変換します。

    Args:
        raw: 生の閾値文字列です。

    Returns:
        パースされた閾値、または無効な場合のデフォルト値を返します。

    Raises:
        例外は発生しません。
    """
    text = str(raw or "50").strip()
    match = re.match(r"^[+-]?\d+", text)
    if not match:
        return 50
    value = int(match.group(0), 10)
    return value if 0 < value <= 10000 else 50


def read_and_increment(counter_file: Path) -> int:
    """カウンターファイルを読み取り、インクリメントします。

    Args:
        counter_file: カウンターファイルのパスです。

    Returns:
        インクリメント後のカウント値を返します。

    Raises:
        例外は発生しません。
    """
    count = 1
    try:
        with counter_file.open("a+", encoding="utf-8") as handle:
            handle.seek(0)
            content = handle.read(64).strip()
            if content:
                try:
                    parsed = int(content, 10)
                except ValueError:
                    parsed = 0
                count = parsed + 1 if 0 < parsed <= 1_000_000 else 1

            handle.seek(0)
            handle.truncate(0)
            handle.write(str(count))
    except OSError:
        write_file(counter_file, str(count))

    return count


def get_node_temp_dir() -> Path:
    """OS のテンポラリディレクトリを取得します。

    Returns:
        テンポラリディレクトリのパスを返します。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    return Path(os.environ.get("TMPDIR") or os.environ.get("TMP") or os.environ.get("TEMP") or tempfile.gettempdir())


def main() -> int:
    """呼び出し回数を監視し、圧縮の目安だけを通知します。

    Returns:
        常に 0 を返します。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    try:
        read_raw_stdin()

        raw_session_id = os.environ.get("CLAUDE_SESSION_ID")
        if not raw_session_id:
            # セッション ID なしでは並行セッション間でカウンタが混線するため提案しない
            return 0

        session_id = sanitize_session_id(raw_session_id)
        counter_file = get_node_temp_dir() / f"claude-tool-count-{session_id}"
        threshold = parse_threshold(os.environ.get("COMPACT_THRESHOLD"))

        count = read_and_increment(counter_file)

        # 閾値に達したら最初の警告を出します。
        if count == threshold:
            write_stderr(
                f"[StrategicCompact] {threshold} tool calls reached - consider /compact if transitioning phases\n"
            )
        # その後は 25 回ごとに、ノンブロッキングで再通知します。
        if count > threshold and (count - threshold) % 25 == 0:
            write_stderr(f"[StrategicCompact] {count} tool calls - good checkpoint for /compact if context is stale\n")
    except Exception as err:  # noqa: BLE001 - フックはノンブロッキングのままにする必要がある
        write_stderr(f"[StrategicCompact] Error: {err}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
