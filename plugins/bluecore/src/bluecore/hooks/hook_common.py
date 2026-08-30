"""bluecoreフック実装の共通ユーティリティ。

フック用の入力読み込み、JSON解析、
出力書き込みの共有関数を提供します。
"""

from __future__ import annotations

import functools
import json
import os
import re
import select
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from bluecore.hooks.output_adapter import adapt_context_output, emit_block
from bluecore.lib.core_utils import ensure_private_dir, get_bluecore_dir

MAX_STDIN_BYTES = 1024 * 1024

# コマンド全体をセグメントに割るシェル区切りトークン。`block_no_verify` と
# `pre_bash_commit_quality` が共に shell 区切り文字密着トークン（例:
# ``status;echo``）を誤って 1 トークンとして扱わないよう、この定数と
# `tokenize`/`split_segments` を共有ヘルパとして 1 箇所に持つ（A-01 対応）。
_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")"})


# ``sh -c`` 再帰と heredoc 本文判定で共有する既知シェル実行ファイル（basename 判定）。
# heredoc 側だけ別集合を持つと「本文が実行されるか」の判定軸が 2 つに割れるため、
# block_no_verify から本モジュールへ移して単一情報源にする。
SHELL_WRAPPER_EXECUTABLES = frozenset({"sh", "bash", "zsh", "dash"})

# heredoc 演算子。``<<<``（herestring）を誤って heredoc と読まないよう、
# 前後に ``<`` が無いことを lookbehind / lookahead の両方で要求する
# （lookahead だけだと ``<<<EOF`` が offset 1 で再マッチする）。
_HEREDOC_OPERATOR_RE = re.compile(
    r"(?<!<)<<(?!<)(-?)[ \t]*"
    r"(?:'(?P<squote>[^']*)'|\"(?P<dquote>[^\"]*)\"|\\?(?P<bare>[A-Za-z_][A-Za-z0-9_.-]*))"
)

# 演算子行がこれらで終わる場合、本文の開始位置が次行とは限らない
# （``cat <<'EOF' |`` 改行 ``bash``）。剥がさない側へ倒す。
_HEREDOC_CONTINUATION_SUFFIXES = ("\\", "|", "&")


def is_shell_wrapper_token(token: str) -> bool:
    """トークンが既知シェル実行ファイルかを basename で判定する。

    Args:
        token: 判定対象のトークン。

    Returns:
        basename が `SHELL_WRAPPER_EXECUTABLES` に属するなら True。

    Raises:
        例外は発生しません。
    """
    return token.rsplit("/", 1)[-1] in SHELL_WRAPPER_EXECUTABLES


def _heredoc_delimiters(line: str) -> list[tuple[str, bool]]:
    """行に現れる heredoc の区切り語を出現順に返す。

    Args:
        line: 走査対象の 1 物理行。

    Returns:
        (区切り語, タブ剥がし可（``<<-``）) のリスト。無ければ空リスト。

    Raises:
        例外は発生しません。
    """
    delimiters = []
    for match in _HEREDOC_OPERATOR_RE.finditer(line):
        # ``<<''`` の空文字列区切りを落とさないため or 連結にしない。
        word = next(
            value for value in (match.group("squote"), match.group("dquote"), match.group("bare")) if value is not None
        )
        delimiters.append((word, match.group(1) == "-"))
    return delimiters


def _line_keeps_heredoc_bodies(line: str) -> bool:
    """演算子行を見て、本文を剥がさずに残すべきかを判定する。

    本文が実行されうる形（シェル起動・パイプや継続で次行へ繋がる形）は
    すべて残す側へ倒す。``bash <<'EOF'`` の本文は実際に実行されるため、
    データとして剥がすと ADR-0002 が禁じる誤通過になる（実測で確認済み）。

    Args:
        line: heredoc 演算子を含む物理行。

    Returns:
        本文を残すなら True、剥がしてよいなら False。

    Raises:
        例外は発生しません。
    """
    if line.rstrip().endswith(_HEREDOC_CONTINUATION_SUFFIXES):
        return True
    return any(is_shell_wrapper_token(token) for token in tokenize(line))


def _consume_heredoc_bodies(
    lines: list[str], start: int, delimiters: list[tuple[str, bool]]
) -> tuple[int, list[str]] | None:
    """区切り語の本文を読み飛ばし、終端行だけを残して返す。

    本文の走査中に ``<<`` を再検出しない。``<<'OUTER'`` の本文に
    ``<<'INNER'`` が現れる形で走査が同期ずれを起こすのを防ぐ。

    Args:
        lines: コマンド全体の物理行リスト。
        start: 本文開始行の index。
        delimiters: 演算子行が宣言した (区切り語, タブ剥がし可) のリスト。

    Returns:
        (再開する index, 出力に残す行のリスト)。1 つでも終端行が見つからな
        ければ None（未終端として呼び出し側が剥がすのをやめる）。

    Raises:
        例外は発生しません。
    """
    index = start
    kept = []
    for word, strip_tabs in delimiters:
        while index < len(lines):
            candidate = lines[index]
            index += 1
            if (candidate.lstrip("\t") if strip_tabs else candidate) == word:
                kept.append(candidate)
                break
        else:
            return None
    return index, kept


def strip_data_heredoc_bodies(command: str) -> str:
    """heredoc の**データ**本文をコマンド文字列から取り除く。

    heredoc 本文はどのシェルでもコマンドの語彙に入らないため、そのまま
    トークン化すると散文が実行命令として読まれる（実測: ``cat > note.md
    <<'EOF'`` の本文に ``git commit --no-verify`` と書いただけで 3 つの保護
    フックが exit 2 になった）。ADR-0002 の「誤検出 > 誤通過」は解析できない
    構文についての規定であり、heredoc の本文範囲は演算子・区切り語・終端行
    だけで決まる環境非依存の構文なので、この規定は本 FP を正当化しない。

    ただし本文がデータだと**静的に確定できる場合だけ**剥がす。次のいずれかに
    当たれば剥がさず現状の挙動（＝検出側）を維持する:

    1. 演算子行にシェル起動トークンがある（``bash <<'EOF'`` / ``cat <<'EOF' | bash``）
    2. 演算子行が継続演算子で終わる（本文開始が次行とは限らない）
    3. 終端行が見つからない（未終端）

    終端行を残すため冪等（``f(f(x)) == f(x)``）。

    Args:
        command: 元のコマンド文字列。

    Returns:
        データ本文を除去した文字列。剥がせないと判断した場合は入力そのまま。

    Raises:
        例外は発生しません。
    """
    if "<<" not in command:
        return command

    lines = command.split("\n")
    output = []
    index = 0
    while index < len(lines):
        line = lines[index]
        output.append(line)
        index += 1
        delimiters = _heredoc_delimiters(line)
        if not delimiters or _line_keeps_heredoc_bodies(line):
            continue
        consumed = _consume_heredoc_bodies(lines, index, delimiters)
        if consumed is None:
            # 未終端。以降は判断材料が無いのでそのまま残す。
            output.extend(lines[index:])
            return "\n".join(output)
        index, kept = consumed
        output.extend(kept)
    return "\n".join(output)


def tokenize(command: str) -> list[str]:
    """シェルコマンドを区切り記号込みのトークン列へ分割する。

    ``shlex`` を ``punctuation_chars=True`` で使い、``git add -A&&git commit``
    のように空白なしで連結された区切り記号も独立トークンにします。クォート
    不整合で ``ValueError`` になる入力は空白分割へフォールバックします
    （クォートが閉じていない入力ではクォート内容もフラグとして走査され、
    ブロック側＝fail-closed に倒れます）。

    Args:
        command: 対象のシェルコマンド文字列。

    Returns:
        トークンのリスト。

    Raises:
        例外は発生しません。
    """
    tokens, _parsed_cleanly = tokenize_with_status(command)
    return tokens


def tokenize_with_status(command: str) -> tuple[list[str], bool]:
    """`tokenize` に加えて、``shlex`` で解析し切れたかどうかを返す。

    フォールバックしたかどうかを呼び出し側が知る必要があるのは、生文字列への
    正規表現フォールバックを「解析できなかったときだけ」に限定するため。
    解析できた入力にまで生文字列の正規表現を当てると、``echo "git commit"`` の
    ような引用文まで実行命令と誤認する（F-08）。

    Args:
        command: 対象のシェルコマンド文字列。

    Returns:
        (トークンのリスト, ``shlex`` が最後まで解析できたか) のタプル。

    Raises:
        例外は発生しません。
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer), True
    except ValueError:
        return command.split(), False


def split_segments(tokens: list[str]) -> list[list[str]]:
    """トークン列をシェル区切りごとのセグメントへ分割する。

    Args:
        tokens: `tokenize` が返したトークン列。

    Returns:
        区切りトークンを含まないセグメント（トークンリスト）のリスト。
        空セグメントは除外します。

    Raises:
        例外は発生しません。
    """
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            if current:
                segments.append(current)
            current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


@functools.lru_cache(maxsize=1)
def resolve_repo_root() -> Path | None:
    """`git rev-parse --show-toplevel` でリポジトリルートの絶対パスを解決しキャッシュする。

    `bash_config_protection`（A-06: 保護対象パスがリポジトリ配下かの判定）と
    `pre_bash_commit_quality`（`git commit -a` の未ステージ変更を作業ツリーから
    読むため）が共有する。呼び出し側は「保護対象 basename のヒットがあった
    場合にのみ」呼ぶことを前提にしており（`git ls-files` 等が出現しない
    大多数の Bash 呼び出しでは subprocess を起動しない）、`lru_cache` で
    同一プロセス内では最大 1 回だけ実際に `git` を実行する（セグメントや
    呼び出し箇所ごとに再起動しない）。テストでキャッシュを跨がせない場合は
    `resolve_repo_root.cache_clear()` を呼ぶこと。

    タイムアウト・失敗時は None を返し、呼び出し側で非ブロッキングに
    フォールバック（deny を避ける、または INDEX のみ検査に留める）
    できるようにする。

    Args:
        引数はありません。

    Returns:
        リポジトリルートの絶対パス。解決できなければ None を返します。

    Raises:
        例外は発生しません。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            return None
        top = result.stdout.strip()
        return Path(top) if top else None
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


# hooks は Claude Code が spawn 直後に stdin へ JSON を書き込むため、
# 最初のバイト到着まで 2 秒あれば十分な余裕がある。
# stdin リダイレクト漏れ（パイプ未接続のまま open）での無期限ブロックを防ぐ。
# launcher がインプロセス実行になったことで、この guard は各フックが
# 自分で stdin を読む read_raw_stdin* の先頭に置く（旧: launcher._read_stdin）。
STDIN_FIRST_BYTE_TIMEOUT = 2.0

# _read_stdin_bytes のチャンク読み取りループ全体に許す壁時計予算（秒）。
# _stdin_ready の最初のバイト到着待ち（STDIN_FIRST_BYTE_TIMEOUT）とは別予算で、
# _read_stdin_bytes が呼ばれた時点から計測する。hooks の stdin ペイロードは
# Claude Code から渡される KB オーダーの JSON であり、5 秒は正常系では絶対に
# 触れない余裕であって、正常系を制約する値ではない。
STDIN_READ_DEADLINE_SECONDS = 5.0

# 1 回の read 呼び出しで要求する最大バイト数（チャンクサイズ）。
STDIN_CHUNK_BYTES = 65536


def _stdin_ready() -> bool:
    """stdin が TTY でなく、最初のバイトが時間内に届くかを判定します。

    `sys.stdin` が None（detach された子プロセス等）の場合や、
    `isatty()`/`select.select` が OSError/ValueError を投げる場合も
    「入力なし」として扱い、例外を外へ伝播させません
    （A-01: docstring の「例外は発生しません」を実装で保証する）。

    Args:
        なし

    Returns:
        読み取りを続行してよければ True。stdin が None、TTY 接続時、
        STDIN_FIRST_BYTE_TIMEOUT 秒以内に最初のバイトが到着しない場合、
        または syscall が例外化した場合は False（select 非 ready の
        場合のみ stderr に警告を出す）。

    Raises:
        例外は発生しません。
    """
    if sys.stdin is None:
        return False
    try:
        if sys.stdin.isatty():
            return False
        ready, _, _ = select.select([sys.stdin], [], [], STDIN_FIRST_BYTE_TIMEOUT)
    except (OSError, ValueError, AttributeError):
        return False
    if not ready:
        write_stderr(
            "WARNING: stdin から入力が届かないため空入力で続行します（stdin リダイレクト漏れの可能性）\n"
        )
        return False
    return True


def _read_stdin_bytes(max_bytes: int) -> bytes:
    """stdin から最大 `max_bytes` 分をバイト列として読みます。

    `.buffer` がある場合は STDIN_CHUNK_BYTES 単位のチャンクをループで読み
    継ぎます。各チャンクは `.read()` ではなく `.read1()` で読みます。
    `.read(n)` は `n` バイト届くか EOF まで待ち続けるため、書き手が
    チャンクサイズ未満のデータを送って途中で止まった場合、1 回の
    `.read()` 呼び出し自体がデッドラインの外側で無期限ブロックしえます。
    `.read1()` は下層の 1 回の raw read で得られた分だけを即座に返すため、
    実際に読めたバイト数に関わらず必ずループへ制御が戻り、デッドライン
    判定が機能します（`sys.stdin.buffer` は `io.BufferedReader` であり
    `.read1()` を常に持ちます。`.read1()` を持たないオブジェクトは
    `fileno()` も持たない/使えないことが多く、`select.select` が
    OSError/ValueError を投げうる状態です。本関数はループ内の
    `select.select` と `.read1()`/`.read()` の両方を
    (OSError, ValueError) で捕捉し、その時点までの部分データ（空の
    場合を含む）を返します（A-01: `_stdin_ready` 通過後でも下層の
    fd がその後閉じられる等の競合で例外化しうるため、二重の防御と
    して本関数側でも捕捉します）。ループ全体には
    `_read_stdin_bytes` 呼び出し開始時点
    から STDIN_READ_DEADLINE_SECONDS 秒の壁時計予算があり、各チャンクの前に
    `select` で次データの到着を待ちます。予算切れ・select 非 ready の
    いずれでも、その時点まで集めた部分データを打ち切って返します（stderr
    に警告）。低速/ハングした書き手が `max_bytes` に満たないデータしか
    送らず接続も閉じない場合の無期限ブロックを防ぐためです。書き手が
    正常にパイプを閉じた場合（EOF）は警告なしで打ち切ります。
    `.buffer` が無い場合（io.StringIO 等）は文字数で読んだあと UTF-8 に
    再エンコードします。文字数 read ではバイト上限を最大 4 倍超過しうる
    ため、呼び出し側でバイト換算の切り詰めを行います。

    Args:
        max_bytes: 読み取る最大バイト数（buffer 無し時は最大文字数）です。

    Returns:
        読み取ったバイト列を返します。デッドライン超過・select 非 ready・
        EOF・syscall 例外のいずれで打ち切られた場合も、その時点までの
        部分データを返します。

    Raises:
        例外は発生しません。
    """
    stdin_buffer = getattr(sys.stdin, "buffer", None)
    if stdin_buffer is None:
        try:
            return sys.stdin.read(max_bytes).encode("utf-8", errors="replace")
        except (OSError, ValueError):
            return b""

    collected = b""
    deadline = time.monotonic() + STDIN_READ_DEADLINE_SECONDS
    while len(collected) < max_bytes:
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            write_stderr(
                "WARNING: stdin 読み取りが上限時間に達したため、"
                "受信済みの部分データで打ち切ります（stdin の書き手が"
                "応答しない可能性）\n"
            )
            break
        try:
            ready, _, _ = select.select([sys.stdin], [], [], remaining_time)
        except (OSError, ValueError):
            break
        if not ready:
            write_stderr(
                "WARNING: stdin の続きが届かないため、受信済みの部分データで"
                "打ち切ります（stdin リダイレクト漏れの可能性）\n"
            )
            break
        chunk_size = min(STDIN_CHUNK_BYTES, max_bytes - len(collected))
        try:
            chunk = stdin_buffer.read1(chunk_size)
        except (OSError, ValueError):
            break
        if chunk == b"":
            break
        collected += chunk
    return collected


def read_raw_stdin(max_bytes: int = MAX_STDIN_BYTES) -> str:
    """標準入力から生のテキストをバイト単位の上限つきで読み取ります。

    TTY 接続時、または最初のバイトが STDIN_FIRST_BYTE_TIMEOUT 秒以内に
    届かない場合は空文字列を返します（stdin リダイレクト漏れでの無期限
    ブロックを防ぐ）。

    Args:
        max_bytes: 読み取る最大バイト数です。

    Returns:
        読み取られた文字列（max_bytes バイトで切り捨て済み）を返します。

    Raises:
        例外は発生しません。
    """
    if not _stdin_ready():
        return ""
    return _read_stdin_bytes(max_bytes)[:max_bytes].decode("utf-8", errors="replace")


def read_raw_stdin_with_truncation(max_bytes: int = MAX_STDIN_BYTES) -> tuple[str, bool]:
    """標準入力を読み取り、切り捨ての有無を返します。

    TTY 接続時、または最初のバイトが STDIN_FIRST_BYTE_TIMEOUT 秒以内に
    届かない場合は ("", False) を返します（stdin リダイレクト漏れでの
    無期限ブロックを防ぐ）。

    Args:
        max_bytes: 読み取る最大バイト数です。

    Returns:
        読み取った文字列と、切り捨てが発生したかどうかのタプルを返します。

    Raises:
        例外は発生しません。
    """
    if not _stdin_ready():
        return "", False
    raw_bytes = _read_stdin_bytes(max_bytes + 1)
    truncated = len(raw_bytes) > max_bytes
    if truncated:
        raw_bytes = raw_bytes[:max_bytes]
    return raw_bytes.decode("utf-8", errors="replace"), truncated


def parse_json_object(raw: str) -> dict[str, Any] | None:
    """JSON 文字列を辞書としてパースします。

    Args:
        raw: パース対象の JSON 文字列です。

    Returns:
        パースされた辞書、または失敗時は None を返します。

    Raises:
        例外は発生せず、パースエラー時は None を返します。
    """
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def write_stdout(text: str) -> None:
    """標準出力にテキストを書き出します。

    Args:
        text: 出力するテキストです。

    Returns:
        なし

    Raises:
        例外は発生しません。
    """
    sys.stdout.write(text)


def write_stderr(text: str) -> None:
    """標準エラーにテキストを書き出します。

    Args:
        text: 出力するテキストです。

    Returns:
        なし

    Raises:
        例外は発生しません。
    """
    sys.stderr.write(text)


def is_truthy(value: str | None) -> bool:
    """文字列が真値を表すかどうかを判定します。

    Args:
        value: 判定対象の文字列です。

    Returns:
        '1', 'true', 'yes', 'on' の場合は True を返します。

    Raises:
        例外は発生しません。
    """
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def basename(path: str) -> str:
    """パスからファイル名を取得します。

    Args:
        path: ファイルパスです。

    Returns:
        ファイル名を返します。

    Raises:
        例外は発生しません。
    """
    return Path(path).name


def is_git_executable_token(token: str) -> bool:
    """トークンが git 実行ファイルを指すかを判定します（basename 化・大小無視・.exe 許容）。

    `/usr/bin/git`（絶対パス）・`git.exe`（Windows）・
    `C:\\Program Files\\Git\\bin\\git.exe`（Windows 絶対パス）・`GIT`（大文字。
    macOS 既定の APFS は大小文字を区別しないため `GIT --version` は実 git を
    起動する）をいずれも同一視します。`block_no_verify` と
    `pre_bash_commit_quality` の両方が使う共有実装で、2 箇所へ別々に実装すると
    正規化の齟齬（H-04）が再発するためここへ集約します。

    Args:
        token: `shlex` 等でトークン化された1トークンです。

    Returns:
        git 実行ファイルとみなせるなら True。

    Raises:
        例外は発生しません。
    """
    basename_part = token.replace("\\", "/").rsplit("/", 1)[-1]
    name = basename_part.lower()
    if name.endswith(".exe"):
        name = name[: -len(".exe")]
    return name == "git"


def resolve_effective_target(raw_path: str) -> Path | None:
    """cwd 基準で解決し、symlink を辿った実体 path を返します（H-02 対応）。

    `config_protection` / `bash_config_protection` が basename だけで保護対象
    判定していたため、`alias -> pyproject.toml` のような symlink 経由の
    書き込みが判定をすり抜けていた。両モジュールがこの共有 helper で解決後の
    実体 path を得てから basename 判定することで、判定基準を一本化する。

    Args:
        raw_path: 検査対象の生パス文字列（相対 / 絶対 / symlink いずれも可）。

    Returns:
        解決できた実体 Path。壊れた・循環した symlink 等で解決できない場合は
        None（呼び出し側はこの場合を fail-closed/fail-open どちらに倒すか
        自身の文脈で決める）。

    Raises:
        例外は発生しません。
    """
    try:
        return (Path.cwd() / raw_path).resolve(strict=False)
    except (OSError, RuntimeError):
        return None


# detach 起動した子の実行時間上限（秒）。start_new_session=True の子はハーネスの
# timeout で kill されないため、自前の watchdog で自決させる。
#
# この値は hooks.json の timeout とは無関係に決める。detach 後の子はハーネスの
# 管轄外であり、hooks.json の値（最大は mem.cli context の 60 秒だが、これは
# detach しない同期エントリ）と紐付ける論拠がないため。
#
# detach 対象（launcher --bg: mem.cli handoff）は正常系ではローカル I/O 数秒で
# 終わる。したがって
# 上限は「正常系を絶対に切らない」ことを優先した安全網の閾値であり、
# 10 分走り続けていれば確実に異常（ハング・暴走）と断定できる 600 秒を採る。
DETACH_TIMEOUT_SECONDS = 600
# SIGTERM を無視して詰まったプロセスを SIGKILL で確実に回収するまでの猶予（秒）。
_DETACH_KILL_AFTER_SECONDS = 30

# detach した子を DETACH_TIMEOUT_SECONDS で SIGTERM、応答なければ
# _DETACH_KILL_AFTER_SECONDS 後に SIGKILL する watchdog。coreutils の
# `timeout`/`gtimeout` は BSD/macOS に標準で存在せず（--kill-after は GNU 固有）、
# ランタイム依存ゼロの方針にも反するため、既に起動に使っている sys.executable
# 自身で実装し外部コマンドへの依存をなくす。
#
# シグナルは GNU timeout と同様に「プロセスグループ」へ送る。子を
# start_new_session=True で新しいセッション（= 新しいプロセスグループ）の
# リーダーにし、os.killpg で子と孫をまとめて回収する。Popen.terminate()/kill()
# は直接の子 1 プロセスにしか届かず、子が孫プロセスを起動する構成になった
# 場合でも無期限残留を防げるよう、汎用的にプロセスグループ全体を回収する。
#
# watchdog 自身が SIGTERM を受けた場合も、そのまま終了すると孫が残るため、
# ハンドラで子グループへ SIGTERM を cascade し、猶予後に SIGKILL してから
# 抜ける（ハンドラ内で proc.wait() を再入させないよう time.sleep で待つ）。
#
# コスト: detach 1 回につき watchdog + 対象の 2 プロセスが起動する。現在の --bg
# 対象（SessionEnd の mem.cli handoff）はセッション終了イベントでのみ発火する
# ため、ツールコールごとの頻度ではない。それでも意図的なコストであり、
# 削減目的で watchdog を外してはならない:
#   - watchdog を消すと、detach 済みの子と孫を kill する主体が消滅する。子は
#     ハーネス timeout の管轄外なので、ハングした子と孫が無制限に残留する。
#   - 子プロセス内の `signal.alarm` では代替できない。alarm は自プロセスにしか
#     届かず、子が孫プロセスを起動する構成になった場合に回収できないため
#     等価ではない。
#   - watchdog は sys.executable の `-c` 実行で、対象モジュールを import せず
#     待つだけなので、追加コストは Python インタプリタ起動 1 回分に留まる。
# すなわち「毎回 1 プロセス分の起動コスト」と「孫プロセスの無制限残留を防ぐ
# kill 保証」のトレードオフであり、後者を採る。
_WATCHDOG_SCRIPT = """
import os, signal, subprocess, sys, time

timeout, kill_after = float(sys.argv[1]), float(sys.argv[2])
log_path = os.environ.get("BLUECORE_BG_LOG_PATH")
log_file = open(log_path, "ab") if log_path else subprocess.DEVNULL
proc = subprocess.Popen(
    sys.argv[3:],
    stdin=sys.stdin,
    stdout=log_file,
    stderr=log_file,
    start_new_session=True,
)

def signal_group(sig):
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, OSError):
        pass

def cascade(signum, frame):
    signal_group(signal.SIGTERM)
    time.sleep(kill_after)
    signal_group(signal.SIGKILL)
    os._exit(128 + signum)

signal.signal(signal.SIGTERM, cascade)
try:
    proc.wait(timeout=timeout)
except subprocess.TimeoutExpired:
    signal_group(signal.SIGTERM)
    try:
        proc.wait(timeout=kill_after)
    except subprocess.TimeoutExpired:
        signal_group(signal.SIGKILL)
        proc.wait()
"""


def _watchdog_argv(cmd: list[str]) -> list[str]:
    """detach 対象を watchdog 付きで起動する argv を組み立てます。

    Args:
        cmd: 監視対象のコマンドリストです。

    Returns:
        `sys.executable -c _WATCHDOG_SCRIPT` で対象を包んだ argv を返します。

    Raises:
        例外は発生しません。
    """
    return [
        sys.executable,
        "-c",
        _WATCHDOG_SCRIPT,
        str(DETACH_TIMEOUT_SECONDS),
        str(_DETACH_KILL_AFTER_SECONDS),
        *cmd,
    ]


def _detach_log_path() -> Path | None:
    """detach した子プロセスの stdout/stderr を追記するログファイルのパスを返す。

    以前は DEVNULL に捨てており、detach 対象（--bg 起動）の import 失敗
    等の出力が完全に消えていた（起動受付成功と処理完了・成功の区別が
    付かない一因。F-18）。日付ごとに 1 ファイルへ追記する
    （mem/logger.py の `mem-YYYY-MM-DD.log` と同じ命名規則）。
    best-effort の diagnostics であり、ログディレクトリを作れない場合は
    None を返して detach 自体は継続させる（ログが取れないことを理由に
    起動を諦めない）。

    Args:
        なし

    Returns:
        `~/.bluecore/logs/bg-YYYY-MM-DD.log` の Path。作成失敗時は None。

    Raises:
        例外は発生しません。
    """
    try:
        log_dir = ensure_private_dir(get_bluecore_dir() / "logs")
    except OSError:
        return None
    return log_dir / f"bg-{datetime.now():%Y-%m-%d}.log"


def detach_process(cmd: list[str], raw_stdin: str, *, env: dict[str, str] | None = None) -> bool:
    """コマンドを detached（新セッション）で起動し stdin を一時ファイル経由で渡す。

    親プロセスの終了に影響されず子を走らせ続けるために使う。一時ファイルは
    world-writable な /tmp を避けて ~/.bluecore 配下に作成し、close→reopen の
    TOCTOU 窓を作らないよう同一 fd を seek(0) して子へ継承する。起動直後に
    unlink する（継承済み fd は有効なまま）。

    detach 後の子はハーネスの timeout の管轄外になるため、_WATCHDOG_SCRIPT で
    ラップして DETACH_TIMEOUT_SECONDS で SIGTERM、さらに猶予後 SIGKILL を送り、
    暴走プロセスの無期限残留を防ぐ。シグナルは子のプロセスグループへ送るため、
    子が起動した孫プロセスもまとめて回収される。

    対象の stdout/stderr は `BLUECORE_BG_LOG_PATH` 環境変数で
    _WATCHDOG_SCRIPT へ log ファイルパスを渡し、そこへ追記させる
    （`_detach_log_path` が None を返した場合のみ DEVNULL にフォールバック
    する）。起動受付の成否（この関数の戻り値）と、対象プロセスの実行結果
    は依然として別概念であり、後者を呼び出し元へ同期的に返す契約は無い
    （best-effort の非同期処理という host 契約は変えない）。診断が必要な
    場合は log ファイルを参照する。

    Args:
        cmd: subprocess に渡すコマンドリスト。
        raw_stdin: 子プロセスへ渡す stdin の内容。
        env: 子プロセスの環境変数。None なら親の環境を継承する。

    Returns:
        起動に成功した場合 True、OSError 時は False。

    Raises:
        例外は発生しません。
    """
    try:
        private_dir = ensure_private_dir(get_bluecore_dir())
        tmp = tempfile.NamedTemporaryFile(
            mode="w+", encoding="utf-8", suffix=".stdin", dir=private_dir, delete=False
        )
    except OSError:
        return False
    try:
        tmp.write(raw_stdin)
        tmp.flush()
        tmp.seek(0)
        child_env = dict(env) if env is not None else dict(os.environ)
        log_path = _detach_log_path()
        if log_path is not None:
            child_env["BLUECORE_BG_LOG_PATH"] = str(log_path)
        subprocess.Popen(
            _watchdog_argv(cmd),
            stdin=tmp,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            start_new_session=True,
        )
        return True
    except OSError:
        return False
    finally:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# 前回セッション detach 失敗通知（recent_bg_failure_notice）の 1 ファイル
# あたり読み取り上限バイト数。1 行提示できれば十分なため小さく取る。
_BG_FAILURE_TAIL_MAX_BYTES = 4096


def _read_tail_bytes(path: Path, max_bytes: int) -> str:
    """ファイル末尾を上限バイトまで読む（seek-from-end）。

    `mem.handoff._read_tail` と同じ末尾限定パターンです。読めない場合は
    空文字列を返します（呼び出し元を落とさない）。

    Args:
        path: 読み取り対象のファイル。
        max_bytes: 末尾から読む最大バイト数。

    Returns:
        デコード済みの末尾テキスト。読めなければ空文字列。

    Raises:
        例外は発生しません。
    """
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - max_bytes))
            return stream.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def recent_bg_failure_notice() -> str:
    """前回セッションの detach 起動（``--bg``）に失敗の痕跡があれば 1 行の通知を返す。

    `detach_process` の戻り値は「起動受付」であり「処理成功」ではないため
    （docstring 参照）、detach した子の失敗は呼び出し元へ同期的に伝わらない
    （§6.2 対応）。そこで次回 SessionStart の ``mem context`` で、前回の
    `_detach_log_path` が書いたログファイル（``bg-YYYY-MM-DD.log``。
    正常系では対象プロセスが何も出力しないため中身は空のはず）に内容が
    あれば「起動受付後に何かが起きた」痕跡とみなし、末尾 1 行だけ提示する。
    失敗時のみ出力するため「出力トークン最小化」原則と両立する。

    読み取り範囲は当日＋前日の 2 ファイルまでに限定し、各ファイルは末尾
    `_BG_FAILURE_TAIL_MAX_BYTES` バイトまでしか読まない（SessionStart の
    hooks.json timeout 60 秒に対する境界を持たせるため）。

    Args:
        なし

    Returns:
        失敗の痕跡があれば人間可読の 1 行。無ければ空文字列。

    Raises:
        例外は発生しません。
    """
    log_dir = get_bluecore_dir() / "logs"
    today = datetime.now()
    for offset in (0, 1):
        day = today - timedelta(days=offset)
        path = log_dir / f"bg-{day:%Y-%m-%d}.log"
        if not path.is_file():
            continue
        tail = _read_tail_bytes(path, _BG_FAILURE_TAIL_MAX_BYTES).strip()
        if not tail:
            continue
        last_line = tail.splitlines()[-1]
        return f"前回のバックグラウンド起動でエラーの痕跡があります（{path.name}）: {last_line}"
    return ""


def emit_block_output(reason: str) -> int:
    """ツール実行ブロックを host 非依存の合併出力で stdout/stderr に書き出す。

    ツール実行をブロックするフックは終了コードを直接返さず、このヘルパの
    戻り値を返すこと（stderr への理由書き出し + stdout への deny JSON を
    同時に出し、exit code は常に 2 を返す）。

    Args:
        reason: ブロック理由（ユーザー / エージェントに提示される）。

    Returns:
        フックが返すべき終了コード。

    Raises:
        例外は発生しません。
    """
    exit_code, deny_out, reason_err = emit_block(reason)
    write_stdout(deny_out)
    write_stderr(reason_err + "\n")
    return exit_code


def _emit_hook_specific_output(event_name: str, additional_context: str) -> str:
    """コンテキスト注入出力を host 非依存の合併 JSON で返す。

    Args:
        event_name: hookEventName に設定するイベント名。
        additional_context: コンテキストに注入する追加文字列。

    Returns:
        ハーネスのプロトコルに適合した JSON 文字列。

    Raises:
        例外は発生しません。
    """
    return adapt_context_output(event_name, additional_context)


def emit_session_start_output(additional_context: str = "") -> str:
    """SessionStart 用のフック出力 JSON 文字列を返す。

    host 非依存の合併出力を output_adapter 経由で生成する。
    stdout への書き込みは行わない純粋関数として使う。

    Args:
        additional_context: コンテキストに注入する追加文字列。

    Returns:
        additionalContext（トップレベル）と hookSpecificOutput を同時に
        含む合併 JSON 文字列。

    Raises:
        例外は発生しません。
    """
    return _emit_hook_specific_output("SessionStart", additional_context)


def print_session_start_output(additional_context: str = "") -> None:
    """SessionStart 用のフック出力を stdout に書き出す。

    Args:
        additional_context: コンテキストに注入する追加文字列。

    Returns:
        None

    Raises:
        例外は発生しません。
    """
    print(emit_session_start_output(additional_context))
