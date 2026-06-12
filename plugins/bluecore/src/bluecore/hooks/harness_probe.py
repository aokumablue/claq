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
_MAX_LOG_BYTES = 5 * 1024 * 1024
_ENV_PREFIXES = ("CLAUDE", "COPILOT", "CODEX", "GITHUB_COPILOT")
_ENV_EXACT_KEYS = frozenset({"PLUGIN_DATA"})
# 値を <redacted> へ置換するキーのマーカー（COPILOT_TOKEN 等の認証情報を
# 平文ログへ残さないため）。PLUGIN_DATA は値に認証情報を含みうるため常に redact。
_SENSITIVE_KEY_MARKERS = ("TOKEN", "KEY", "SECRET", "AUTH", "PASS", "CRED")


def _is_sensitive_key(key: str) -> bool:
    """環境変数キーが機密値を持ちうるかを判定する。

    Returns:
        機密マーカーを含む、または常時 redact 対象のキーなら True。

    Raises:
        例外は発生しません。
    """
    upper = key.upper()
    return key in _ENV_EXACT_KEYS or any(marker in upper for marker in _SENSITIVE_KEY_MARKERS)


def _snapshot_env() -> dict[str, str]:
    """ハーネス関連の環境変数のみ抽出して返す（機密値は redact）。

    Returns:
        対象プレフィックスまたは対象キーに一致する環境変数の辞書。

    Raises:
        例外は発生しません。
    """
    return {
        k: ("<redacted>" if _is_sensitive_key(k) else v)
        for k, v in os.environ.items()
        if k.startswith(_ENV_PREFIXES) or k in _ENV_EXACT_KEYS
    }


def _snapshot_stdin(raw_stdin: str) -> str | dict:
    """stdin スナップショットを構築する。

    機密（ファイル内容等）の永続化を避けるため、既定では JSON のトップレベル
    キー一覧と長さのみ記録する。BLUECORE_PROBE_STDIN が真値のときだけ
    全文（上限つき）を記録する。

    Args:
        raw_stdin: フックに渡された生の stdin。

    Returns:
        全文記録時は文字列、既定では {"length", "keys"} の辞書。

    Raises:
        例外は発生しません。
    """
    if str(os.environ.get("BLUECORE_PROBE_STDIN") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return raw_stdin[:_MAX_STDIN_SNAPSHOT]
    try:
        data = json.loads(raw_stdin)
    except json.JSONDecodeError:
        data = None
    return {
        "length": len(raw_stdin),
        "keys": sorted(data) if isinstance(data, dict) else None,
    }


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
        "stdin": _snapshot_stdin(raw_stdin),
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
        # 上限超過時は truncate して無制限肥大化（ディスク DoS）を防ぐ
        if _LOG_PATH.exists() and _LOG_PATH.stat().st_size > _MAX_LOG_BYTES:
            _LOG_PATH.write_text("", encoding="utf-8")
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"[HarnessProbe] recorded: {_LOG_PATH}", file=sys.stderr)
    except Exception as err:
        print(f"[HarnessProbe] error: {err}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
