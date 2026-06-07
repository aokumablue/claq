"""セッションのトークン使用量とコストメトリクスを追跡します。

トリガー: post:tool_use (セッション追跡を通じて暗黙的)
入力: トークン数とモデルメタデータを含むJSON
出力: コストデータをセッションログに追記
終了: 0 (常に成功)
"""

from __future__ import annotations

import json
import os
from datetime import datetime

from bluecore.hooks.hook_common import parse_json_object, read_raw_stdin, write_stdout
from bluecore.lib.core_utils import append_file, ensure_dir, get_bluecore_dir


def to_number(value: object) -> int | float:
    """値を数値に変換し、無効な値は 0 に置き換える。

    Args:
        value: 変換する値

    Returns:
        変換された数値（NaN や Inf の場合は 0）

    Raises:
        例外は発生しません（TypeError や ValueError は 0 として扱う）。
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return number if number == number and number not in (float("inf"), float("-inf")) else 0


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """モデルとトークン数からコストを推定する（USD）。

    Args:
        model: モデル名
        input_tokens: 入力トークン数
        output_tokens: 出力トークン数

    Returns:
        推定コスト（USD、小数点以下6桁）

    Raises:
        例外は発生しません。
    """
    table = {
        "haiku": {"in": 0.8, "out": 4.0},
        "sonnet": {"in": 3.0, "out": 15.0},
        "opus": {"in": 15.0, "out": 75.0},
    }

    normalized = model.lower()
    rates = table["sonnet"]
    if "haiku" in normalized:
        rates = table["haiku"]
    elif "opus" in normalized:
        rates = table["opus"]

    cost = (input_tokens / 1_000_000) * rates["in"] + (output_tokens / 1_000_000) * rates["out"]
    return round(cost, 6)


def main() -> int:
    """トークン使用量とコストを追跡してログに記録する。

    Args:
        引数はありません（標準入力から読み取る）。

    Returns:
        終了コード（0: 成功）

    Raises:
        例外は発生しません。
    """
    raw = read_raw_stdin()
    input_data = parse_json_object(raw)

    if input_data is not None:
        usage = input_data.get("usage") or input_data.get("token_usage") or {}
        input_tokens = int(to_number(usage.get("input_tokens") or usage.get("prompt_tokens") or 0))
        output_tokens = int(to_number(usage.get("output_tokens") or usage.get("completion_tokens") or 0))
        model = str(
            input_data.get("model")
            or (input_data.get("_cursor") or {}).get("model")
            or os.environ.get("CLAUDE_MODEL")
            or "unknown"
        )
        session_id = str(os.environ.get("CLAUDE_SESSION_ID") or "default")

        metrics_dir = get_bluecore_dir() / "metrics"
        ensure_dir(metrics_dir)

        row = {
            "timestamp": datetime.now().isoformat(),
            "session_id": session_id,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": estimate_cost(model, input_tokens, output_tokens),
        }
        append_file(metrics_dir / "costs.jsonl", f"{json.dumps(row)}\n")

    write_stdout(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
