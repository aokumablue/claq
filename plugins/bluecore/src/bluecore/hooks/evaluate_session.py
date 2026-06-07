"""セッション終了時にセッション品質を評価し、学習内容を抽出します。

トリガー: stop (セッション終了)
入力: セッションメタデータと統計情報を含むJSON
出力: セッション評価レポートを生成
終了: 0 (常に成功)
"""

from __future__ import annotations

import json
from pathlib import Path

from bluecore.hooks.hook_common import parse_json_object, read_raw_stdin
from bluecore.lib.core_utils import count_in_file, ensure_dir, get_learned_skills_dir, log, read_file


def _default_config_path() -> Path:
    """セッション学習スキル（learn）の config.json パスを返す。

    Returns:
        デフォルトの設定ファイルパス。

    Raises:
        例外は発生しません。
    """
    script_dir = Path(__file__).resolve().parent
    return script_dir.parents[2] / "skills" / "learn" / "config.json"


def _load_learn_config(config_file: Path) -> tuple[int, Path]:
    """config.json を読み込み、min_session_length と learned_skills_path を返す。

    設定ファイルが存在しない・不正な場合はデフォルト値を返す。

    Args:
        config_file: 設定ファイルのパス。

    Returns:
        (min_session_length, learned_skills_path) のタプル。
    """
    min_session_length = 10
    learned_skills_path = get_learned_skills_dir()
    content = read_file(config_file)
    if not content:
        return min_session_length, learned_skills_path
    try:
        config = json.loads(content)
        value = config.get("min_session_length")
        if value is not None:
            min_session_length = value
        custom_path = config.get("learned_skills_path")
        if isinstance(custom_path, str) and custom_path:
            if custom_path.startswith("~"):
                custom_path = str(Path.home()) + custom_path[1:]
            learned_skills_path = Path(custom_path)
    except json.JSONDecodeError as err:
        log(f"[learn] Failed to parse config: {err}, using defaults")
    return min_session_length, learned_skills_path


def main() -> int:
    """セッション終了時にトランスクリプトを評価してスキルを抽出する。

    Args:
        引数はありません（標準入力から読み取る）。

    Returns:
        終了コード（0: 成功、その他: エラー）

    Raises:
        例外は発生しません（エラーは終了コードで返す）。
    """
    try:
        raw = read_raw_stdin()
        input_data = parse_json_object(raw)
        transcript_path = input_data.get("transcript_path") if input_data else None

        min_session_length, learned_skills_path = _load_learn_config(_default_config_path())
        ensure_dir(learned_skills_path)

        if not transcript_path or not Path(transcript_path).exists():
            return 0

        message_count = count_in_file(transcript_path, r'"type"\s*:\s*"user"')
        if message_count < min_session_length:
            log(f"[learn] Session too short ({message_count} messages), skipping")
            return 0

        log(f"[learn] Session has {message_count} messages - evaluate for extractable patterns")
        log(f"[learn] Save learned skills to: {learned_skills_path}")
    except Exception as err:  # noqa: BLE001 - hook must remain non-blocking
        log(f"[learn] Error: {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
