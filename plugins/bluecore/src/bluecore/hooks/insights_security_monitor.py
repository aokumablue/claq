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
from pathlib import Path
from typing import Any

from bluecore.lib.core_utils import get_bluecore_dir

# stdoutプロトコルに干渉しないよう、ルートロガーを汚染せず専用ロガーへ
# stderr ハンドラを直接付与する。basicConfig はルート全体に影響するため使わない。
log = logging.getLogger("insaits-hook")
log.setLevel(logging.DEBUG if os.environ.get("INSAITS_VERBOSE") else logging.WARNING)
# 多重起動でハンドラが二重登録されないようガードする。
if not log.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("[InsAIts] %(message)s"))
    log.addHandler(_handler)
# ルートロガーへ伝播させない（他ハンドラによる重複出力を防ぐ）。
log.propagate = False

# InsAIts SDKのインポートを試行
try:
    from insa_its import insAItsMonitor

    INSAITS_AVAILABLE: bool = True
except ImportError:
    INSAITS_AVAILABLE = False

# --- 定数 ---
# CWD 相対だと監査ログ（コマンド全文＝機密混入リスク）が作業ディレクトリに散在し
# パーミッションも不定になるため、~/.bluecore/logs 配下の絶対パスへ書き込む。
# None の場合は _resolve_audit_path() が実行時に既定パスを解決する（import 時評価を避ける）。
AUDIT_FILE: str | None = None
MIN_CONTENT_LENGTH: int = 10
MAX_SCAN_LENGTH: int = 4000
DEFAULT_MODEL: str = "claude-opus"
BLOCKING_SEVERITIES: frozenset = frozenset({"CRITICAL"})


def _resolve_audit_path() -> Path:
    """監査ログの書き込み先を解決する。

    AUDIT_FILE が設定されていればそのパスを、未設定（None）なら
    ~/.bluecore/logs 配下の既定パスを実行時に解決して返す。import 時に
    ホームディレクトリを評価しないことでテストの環境隔離を妨げない。
    """
    if AUDIT_FILE is not None:
        return Path(AUDIT_FILE)
    return get_bluecore_dir() / "logs" / "insaits_audit.jsonl"


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
        edits = tool_input.get("edits")
        if isinstance(edits, list):
            edit_texts = [str(edit.get("new_string", "")) for edit in edits if isinstance(edit, dict)]
            text = "\n".join(part for part in [text, *edit_texts] if part)
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
        path = _resolve_audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # 監査ログは所有者のみ読み書き可（機密混入を想定し 0o600）
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(enriched) + "\n")
    except OSError as exc:
        log.warning("Failed to write audit log %s: %s", path, exc)


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
            "Audit log: " + str(_resolve_audit_path()),
        ]
    )
    return "\n".join(lines)


def _parse_input() -> dict[str, Any]:
    """stdin を読み取り JSON として返す。デコード失敗時は content キーでラップする。"""
    raw: str = sys.stdin.read().strip()
    if not raw:
        sys.exit(0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"content": raw}


def _run_insaits_scan(text: str) -> dict[str, Any]:
    """InsAIts SDK でテキストをスキャンし、結果辞書を返す。エラー時は fail_mode に従い終了する。

    Args:
        text: スキャン対象のテキスト。

    Returns:
        SDK の send_message() 戻り値。
    """
    try:
        monitor: insAItsMonitor = insAItsMonitor(
            session_name="claude-code-hook",
            dev_mode=os.environ.get("INSAITS_DEV_MODE", "false").lower() in ("1", "true", "yes"),
        )
        return monitor.send_message(
            text=text[:MAX_SCAN_LENGTH],
            sender_id="claude-code",
            llm_id=os.environ.get("INSAITS_MODEL", DEFAULT_MODEL),
        )
    except Exception as exc:  # 広範囲のcatchは意図的: SDK内部は未知
        fail_mode: str = os.environ.get("INSAITS_FAIL_MODE", "open").lower()
        if fail_mode == "closed":
            sys.stdout.write(f"InsAIts SDK error ({type(exc).__name__}); blocking execution to avoid unscanned input.\n")
            sys.exit(2)
        log.warning("SDK error (%s), skipping security scan: %s", type(exc).__name__, exc)
        sys.exit(0)


def _handle_anomalies(anomalies: list[Any], data: dict[str, Any], text: str, context: str) -> None:
    """監査ログを書き込み、異常があればフィードバックを出力して必要ならブロックする。

    Args:
        anomalies: SDK が返した異常リスト。
        data: 元のフック入力辞書。
        text: スキャン対象テキスト（文字数計上用）。
        context: 監査ログ用コンテキストラベル。
    """
    write_audit({
        "tool": data.get("tool_name", "unknown"),
        "context": context,
        "anomaly_count": len(anomalies),
        "anomaly_types": [get_anomaly_attr(a, "type") for a in anomalies],
        "text_length": len(text),
    })
    if not anomalies:
        log.debug("Clean -- no anomalies detected.")
        sys.exit(0)
    has_critical: bool = any(get_anomaly_attr(a, "severity").upper() in BLOCKING_SEVERITIES for a in anomalies)
    feedback: str = format_feedback(anomalies)
    if has_critical:
        sys.stdout.write(feedback + "\n")
        sys.exit(2)
    else:
        log.warning("\n%s", feedback)
        sys.exit(0)


def main() -> None:
    """PreToolUse フックとして入力を検査し、必要ならブロックします。

    Returns:
        None を返します。重大な異常がある場合は sys.exit(2) で終了します。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    data = _parse_input()
    text, context = extract_content(data)

    if len(text.strip()) < MIN_CONTENT_LENGTH:
        sys.exit(0)

    if not INSAITS_AVAILABLE:
        log.warning("Not installed. Run: pip install insa-its")
        sys.exit(0)

    result = _run_insaits_scan(text)
    anomalies: list[Any] = result.get("anomalies", [])
    _handle_anomalies(anomalies, data, text, context)


if __name__ == "__main__":
    main()
