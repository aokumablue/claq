"""ハーネス実機検証用の診断フック。

Copilot CLI / Codex で hooks.json のフックが実際にどう起動されるかを確認する。
環境変数（CLAUDE_PLUGIN_ROOT の展開有無、COPILOT_* / CODEX_* / PLUGIN_*）と
stdin ペイロードのスナップショットを ~/.bluecore/logs/harness_probe.log に
JSON Lines で追記する。本番の hooks.json には登録せず、検証時に任意の
イベントへ一時的に差し込んで使う。

使い方（Copilot 実機検証の例）:
  1. プラグインの hooks.json の検証したいイベントにエントリを追加:
     {"type": "command",
      "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/src/bluecore/launcher.py\" bluecore.hooks.harness_probe"}
  2. copilot セッションを起動してツールを 1 回実行
  3. ~/.bluecore/logs/harness_probe.log を確認:
     - レコードが無い → ${CLAUDE_PLUGIN_ROOT} が未展開（フック起動自体が失敗）
     - env.CLAUDE_PLUGIN_ROOT / stdin の形式から対応方針を判断
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from bluecore.lib.harness import detect_harness

_LOG_PATH = Path.home() / ".bluecore" / "logs" / "harness_probe.log"
_MAX_STDIN_SNAPSHOT = 4096
_ENV_PREFIXES = ("CLAUDE", "COPILOT", "CODEX", "PLUGIN", "GITHUB_COPILOT")


def _snapshot_env() -> dict[str, str]:
    """ハーネス関連の環境変数のみ抽出して返す。

    Args:
        引数はありません。

    Returns:
        対象プレフィックスに一致する環境変数の辞書。

    Raises:
        例外は発生しません。
    """
    return {k: v for k, v in os.environ.items() if k.startswith(_ENV_PREFIXES)}


def build_record(raw_stdin: str) -> dict:
    """診断レコードを構築する。

    Args:
        raw_stdin: フックに渡された生の stdin。

    Returns:
        タイムスタンプ・判定結果・環境変数・stdin スナップショットを含む辞書。

    Raises:
        例外は発生しません。
    """
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "detected_harness": detect_harness(),
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "env": _snapshot_env(),
        "stdin": raw_stdin[:_MAX_STDIN_SNAPSHOT],
    }


def main() -> int:
    """診断レコードをログへ追記するエントリポイント。

    Args:
        引数はありません。

    Returns:
        終了コード（常に 0 — 検証フックがセッションを妨げないようにする）。

    Raises:
        例外は発生しません。
    """
    try:
        raw = "" if sys.stdin.isatty() else sys.stdin.read()
        record = build_record(raw)
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"[HarnessProbe] recorded: {_LOG_PATH}", file=sys.stderr)
    except Exception as err:
        print(f"[HarnessProbe] error: {err}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
