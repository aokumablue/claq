#!/usr/bin/env python3
"""loop-dev の反復テレメトリを JSONL 生ログとして記録・読み出しする CLI。

反復テレメトリ（Task / Result / Iter / Blockers / Flake / Commits）は
**知識ではなく生ログ** なので ``knowledge`` テーブルには入れない。人間可読の
学びでもなく、SessionStart への低トークン注入にも使えず、入れれば DB を
膨らませたうえ ``status='pending'``（observer が抽出した昇格待ちの知識候補）の
枠を埋めて昇格作業を選り分け作業に変えてしまう。

そのため置き場は learn の観測ログと同じ ``~/.bluecore/repos/<repo-id>/`` 配下の
``loop-dev.jsonl``。追記・ローテーション・古いアーカイブの削除は
``bluecore.skills.learn.storage.JsonlLog`` の機構をそのまま共有する。

保存先の解決は ``repos`` 台帳（SQLite）を引くため、``record`` / ``list`` /
``path`` のいずれもハードタイムアウト下で実行する。時間内に終わらなければ
テレメトリを捨てて終了し、呼び出し側の loop-dev を止めない。
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
from datetime import UTC, datetime

from bluecore.mem.redaction import redact
from bluecore.skills.learn.storage import ObservationTarget, resolve_observation_target

RESULTS = ("converged", "not-converged", "circuit-break", "stopped")
"""Result の語彙。loop-dev の反復履歴 4 値と同一に保つ。"""

DEFAULT_TIMEOUT_SECONDS = 5.0
"""``--timeout`` の既定値（秒）。"""

MAX_TEXT_CHARS = 500
"""``--task`` / ``--note`` を切り詰める文字数。生ログを肥大させないための上限。"""

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TelemetryError(Exception):
    """利用者へ 1 行で提示する想定内のエラー。

    ``main()`` がこれを捕捉し stderr へメッセージだけを出して終了コード 1 を返す。
    """


class _Timeout(Exception):
    """ハードタイムアウトの発火を表す内部例外。"""


def _now_utc() -> str:
    """現在時刻を Z 終端の UTC ISO8601 文字列で返す。

    Returns:
        ``2026-08-09T12:34:56.789012Z`` 形式の文字列。
    """
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _clean_text(value: str) -> str:
    """自由文をシークレット除去・空白畳み込み・長さ切り詰めして返す。

    Args:
        value: 元の文字列。

    Returns:
        整形後の文字列。
    """
    return redact(" ".join(value.split()))[:MAX_TEXT_CHARS]


def _resolve_target() -> ObservationTarget:
    """現在のリポジトリの生ログ保存先を ``repos`` 台帳で解決する。

    Returns:
        ``~/.bluecore/repos/<repo-id>/`` を指す ObservationTarget。
    """
    return resolve_observation_target()


def _build_record(args: argparse.Namespace, repo_id: str) -> dict:
    """コマンドライン引数から 1 件分のテレメトリレコードを構築する。

    Args:
        args: ``record`` サブコマンドの解析済み引数。
        repo_id: ``repos.id``。

    Returns:
        JSONL へ書き込むレコード。
    """
    record = {
        "timestamp": _now_utc(),
        "repo": repo_id,
        "task": _clean_text(args.task),
        "result": args.result,
        "iterations": args.iterations,
        "max_iterations": args.max_iterations,
        "blockers": args.blockers,
        "flakes": args.flakes,
        "commits": [_clean_text(commit) for commit in args.commit],
    }
    if args.note:
        record["note"] = _clean_text(args.note)
    return record


def _handle_record(args: argparse.Namespace) -> int:
    """``record`` — テレメトリ 1 件を JSONL へ追記する。

    Args:
        args: 解析済み引数。

    Returns:
        終了コード 0。

    Raises:
        TelemetryError: 反復数・件数に負値が渡された場合。
    """
    for name in ("iterations", "max_iterations", "blockers", "flakes"):
        if getattr(args, name) < 0:
            raise TelemetryError(f"--{name.replace('_', '-')} には 0 以上を指定してください")

    target = _resolve_target()
    log = target.loop_telemetry_log
    log.ensure_dirs()
    log.maintain()
    log.append(_build_record(args, target.repo_id))
    print(log.path)
    return 0


def _parse_date(value: str | None, option: str) -> str | None:
    """``--since`` / ``--until`` の日付を検証して返す。

    Args:
        value: 入力値。None なら未指定。
        option: エラーメッセージに出すオプション名。

    Returns:
        検証済みの ``YYYY-MM-DD`` 文字列。未指定なら None。

    Raises:
        TelemetryError: ``YYYY-MM-DD`` 形式でない場合。
    """
    if value is None:
        return None
    if not _DATE_RE.match(value):
        raise TelemetryError(f"{option} は YYYY-MM-DD 形式で指定してください: {value}")
    return value


def _in_period(record: dict, since: str | None, until: str | None) -> bool:
    """レコードが指定期間に含まれるかを判定する。

    ``timestamp`` の先頭 10 文字（``YYYY-MM-DD``）を文字列比較する。``until``
    はその日を含む。``timestamp`` を持たないレコードは期間指定時に除外する。

    Args:
        record: 判定対象のレコード。
        since: 開始日（含む）。None なら下限なし。
        until: 終了日（含む）。None なら上限なし。

    Returns:
        期間に含まれるなら True。
    """
    if since is None and until is None:
        return True
    day = str(record.get("timestamp", ""))[:10]
    if not _DATE_RE.match(day):
        return False
    if since is not None and day < since:
        return False
    if until is not None and day > until:
        return False
    return True


def _handle_list(args: argparse.Namespace) -> int:
    """``list`` — アーカイブ込みの全テレメトリを古い順に JSONL で出力する。

    ``search`` と違い関連度上位の打ち切りが無いため、loop-audit は全件を
    決定論的に集計できる。

    Args:
        args: 解析済み引数。

    Returns:
        終了コード 0。
    """
    since = _parse_date(args.since, "--since")
    until = _parse_date(args.until, "--until")

    log = _resolve_target().loop_telemetry_log
    records = [record for record in log.read_records() if _in_period(record, since, until)]
    if args.limit > 0:
        records = records[-args.limit :]
    for record in records:
        print(json.dumps(record, ensure_ascii=False))
    return 0


def _handle_path(_args: argparse.Namespace) -> int:
    """``path`` — テレメトリ JSONL の絶対パスを出力する。

    Args:
        _args: 解析済み引数（未使用）。

    Returns:
        終了コード 0。
    """
    print(_resolve_target().loop_telemetry_log.path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """テレメトリ CLI の引数パーサを構築する。

    Returns:
        サブコマンド ``record`` / ``list`` / ``path`` を持つパーサ。
    """
    parser = argparse.ArgumentParser(
        prog="bluecore.skills.loop_dev.telemetry",
        description="loop-dev の反復テレメトリを JSONL 生ログへ記録・読み出しする",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"ハードタイムアウト秒（既定 {DEFAULT_TIMEOUT_SECONDS}）。0 以下で無効",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record", help="テレメトリ 1 件を追記する")
    record.add_argument("--task", required=True, help="タスクの 1 行要約")
    record.add_argument("--result", required=True, choices=RESULTS, help="収束結果")
    record.add_argument("--iterations", required=True, type=int, help="到達した反復番号")
    record.add_argument("--max-iterations", type=int, default=2, help="反復上限（既定 2）")
    record.add_argument("--blockers", type=int, default=0, help="blocker 件数")
    record.add_argument("--flakes", type=int, default=0, help="flake 件数")
    record.add_argument("--commit", action="append", default=[], help="コミットハッシュ（複数指定可）")
    record.add_argument("--note", help="補足（任意）")
    record.set_defaults(handler=_handle_record)

    listing = subparsers.add_parser("list", help="テレメトリを古い順に JSONL で出力する")
    listing.add_argument("--since", help="開始日 YYYY-MM-DD（含む）")
    listing.add_argument("--until", help="終了日 YYYY-MM-DD（含む）")
    listing.add_argument("--limit", type=int, default=0, help="末尾 N 件に絞る（既定 0 = 全件）")
    listing.set_defaults(handler=_handle_list)

    path = subparsers.add_parser("path", help="テレメトリ JSONL のパスを出力する")
    path.set_defaults(handler=_handle_path)

    return parser


def _arm_timeout(seconds: float) -> None:
    """ハードタイムアウトのアラームを設定する。

    Args:
        seconds: 発火までの秒数。0 以下なら何もしない。
    """
    if seconds <= 0:
        return

    def _on_alarm(_signum: int, _frame: object) -> None:
        """SIGALRM を内部例外へ変換する。"""
        raise _Timeout

    signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)


def _disarm_timeout() -> None:
    """設定済みのハードタイムアウトを解除する。"""
    signal.setitimer(signal.ITIMER_REAL, 0)


def main(argv: list[str] | None = None) -> int:
    """テレメトリ CLI のエントリポイント。

    Args:
        argv: 引数リスト。None なら ``sys.argv[1:]``。

    Returns:
        プロセス終了コード。成功で 0、想定内エラーとタイムアウトで 1。
    """
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)

    _arm_timeout(args.timeout)
    try:
        return args.handler(args)
    except _Timeout:
        print(f"loop-dev telemetry: {args.timeout}s のハードタイムアウトで打ち切りました", file=sys.stderr)
        return 1
    except TelemetryError as error:
        print(f"loop-dev telemetry: {error}", file=sys.stderr)
        return 1
    finally:
        _disarm_timeout()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
