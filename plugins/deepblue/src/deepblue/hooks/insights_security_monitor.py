#!/usr/bin/env python3
"""
ツール入力をローカルで監視し、危険な異常を検出します。

認証情報の露出やプロンプトインジェクションを確認し、必要ならツール実行をブロックします。
監査イベントはローカルの JSONL に追記し、あとから追跡できるようにします。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from typing import Any

# stdoutプロトコルに干渉しないようstderrにログ設定
logging.basicConfig(
    stream=sys.stderr,
    format="[InsAIts] %(message)s",
    level=logging.DEBUG if os.environ.get("INSAITS_VERBOSE") else logging.WARNING,
)
log = logging.getLogger("insaits-hook")

# InsAIts SDKのインポートを試行
try:
    from insa_its import insAItsMonitor

    INSAITS_AVAILABLE: bool = True
except ImportError:
    INSAITS_AVAILABLE = False

# --- 定数 ---
AUDIT_FILE: str = ".insaits_audit_session.jsonl"
MIN_CONTENT_LENGTH: int = 10
MAX_SCAN_LENGTH: int = 4000
DEFAULT_MODEL: str = "claude-opus"
BLOCKING_SEVERITIES: frozenset = frozenset({"CRITICAL"})


def extract_content(data: dict[str, Any]) -> tuple[str, str]:
    """検査対象のテキストと監査用コンテキストを抽出します。

    Args:
        data: ツール入力データです。

    Returns:
        スキャン対象のテキストと短いコンテキストラベルのタプルを返します。

    Raises:
        例外は発生しません。
    """
    tool_name: str = data.get("tool_name", "")
    tool_input: dict[str, Any] = data.get("tool_input", {})

    text: str = ""
    context: str = ""

    if tool_name in ("Write", "Edit", "MultiEdit"):
        text = tool_input.get("content", "") or tool_input.get("new_string", "")
        context = "file:" + str(tool_input.get("file_path", ""))[:80]
    elif tool_name == "Bash":
        # PreToolUse: ツールはまだ実行されていない、コマンドを検査
        command: str = str(tool_input.get("command", ""))
        text = command
        context = "bash:" + command[:80]
    elif "content" in data:
        content: Any = data["content"]
        if isinstance(content, list):
            text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
        elif isinstance(content, str):
            text = content
        context = str(data.get("task", ""))

    return text, context


def write_audit(event: dict[str, Any]) -> None:
    """監査イベントを JSONL ログへ追記します。

    Args:
        event: 記録する監査イベントです。

    Returns:
        None を返します。

    Raises:
        例外は発生しません。
    """
    try:
        enriched: dict[str, Any] = {
            **event,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        enriched["hash"] = hashlib.sha256(json.dumps(enriched, sort_keys=True).encode()).hexdigest()[:16]
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(enriched) + "\n")
    except OSError as exc:
        log.warning("Failed to write audit log %s: %s", AUDIT_FILE, exc)


def get_anomaly_attr(anomaly: Any, key: str, default: str = "") -> str:
    """異常オブジェクトから指定フィールドを取り出します。

    Args:
        anomaly: dict または属性アクセス可能なオブジェクトです。
        key: 取得したいフィールド名です。
        default: 値がない場合に返す既定値です。

    Returns:
        文字列化したフィールド値を返します。

    Raises:
        例外は発生しません。
    """
    if isinstance(anomaly, dict):
        return str(anomaly.get(key, default))
    return str(getattr(anomaly, key, default))


def format_feedback(anomalies: list[Any]) -> str:
    """検出結果をフィードバック文に整形します。

    Args:
        anomalies: 検出された異常の一覧です。

    Returns:
        人間が読める複数行のフィードバック文字列を返します。

    Raises:
        例外は発生しません。
    """
    lines: list[str] = [
        "== InsAIts Security Monitor -- Issues Detected ==",
        "",
    ]
    for i, a in enumerate(anomalies, 1):
        sev: str = get_anomaly_attr(a, "severity", "MEDIUM")
        atype: str = get_anomaly_attr(a, "type", "UNKNOWN")
        detail: str = get_anomaly_attr(a, "details", "")
        lines.extend(
            [
                f"{i}. [{sev}] {atype}",
                f"   {detail[:120]}",
                "",
            ]
        )
    lines.extend(
        [
            "-" * 56,
            "Fix the issues above before continuing.",
            "Audit log: " + AUDIT_FILE,
        ]
    )
    return "\n".join(lines)


def main() -> None:
    """PreToolUse フックとして入力を検査し、必要ならブロックします。

    Returns:
        None を返します。重大な異常がある場合は sys.exit(2) で終了します。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    raw: str = sys.stdin.read().strip()
    if not raw:
        sys.exit(0)

    try:
        data: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError:
        data = {"content": raw}

    text, context = extract_content(data)

    # 非常に短いコンテンツはスキップ (例: "OK"、空のbash結果)
    if len(text.strip()) < MIN_CONTENT_LENGTH:
        sys.exit(0)

    if not INSAITS_AVAILABLE:
        log.warning("Not installed. Run: pip install insa-its")
        sys.exit(0)

    # 内部エラーでフックがクラッシュしないようSDK呼び出しをラップ
    try:
        monitor: insAItsMonitor = insAItsMonitor(
            session_name="claude-code-hook",
            dev_mode=os.environ.get("INSAITS_DEV_MODE", "false").lower() in ("1", "true", "yes"),
        )
        result: dict[str, Any] = monitor.send_message(
            text=text[:MAX_SCAN_LENGTH],
            sender_id="claude-code",
            llm_id=os.environ.get("INSAITS_MODEL", DEFAULT_MODEL),
        )
    except Exception as exc:  # 広範囲のcatchは意図的: SDK内部は未知
        fail_mode: str = os.environ.get("INSAITS_FAIL_MODE", "open").lower()
        if fail_mode == "closed":
            sys.stdout.write(
                f"InsAIts SDK error ({type(exc).__name__}); blocking execution to avoid unscanned input.\n"
            )
            sys.exit(2)
        log.warning(
            "SDK error (%s), skipping security scan: %s",
            type(exc).__name__,
            exc,
        )
        sys.exit(0)

    anomalies: list[Any] = result.get("anomalies", [])

    # 検出結果に関わらず監査イベントを書き込み
    write_audit(
        {
            "tool": data.get("tool_name", "unknown"),
            "context": context,
            "anomaly_count": len(anomalies),
            "anomaly_types": [get_anomaly_attr(a, "type") for a in anomalies],
            "text_length": len(text),
        }
    )

    if not anomalies:
        log.debug("Clean -- no anomalies detected.")
        sys.exit(0)

    # 最大深刻度を判定
    has_critical: bool = any(get_anomaly_attr(a, "severity").upper() in BLOCKING_SEVERITIES for a in anomalies)

    feedback: str = format_feedback(anomalies)

    if has_critical:
        # stdoutフィードバック -> モデルに表示
        sys.stdout.write(feedback + "\n")
        sys.exit(2)  # PreToolUse終了コード2 = ツール実行をブロック
    else:
        # 非クリティカル: stderr経由で警告 (非ブロッキング)
        log.warning("\n%s", feedback)
        sys.exit(0)


if __name__ == "__main__":
    main()
