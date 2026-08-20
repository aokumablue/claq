"""plugin root をベンダ非依存の固定住所（``~/.bluecore/env.sh``）へ書き出す。

M-02（ホスト固有パスの探索ループが agents/commands/skills の md に重複していた
問題）と、ユーザー要件「``.claude``/``.copilot``/``.grok`` 等のベンダ固有パスを
md へ書かない」を同時に解決する。``CLAUDE_PLUGIN_ROOT`` は Bash tool の環境変数に
乗らない（実測）ため、md 側から plugin root を知る手段が無かった。本モジュールは
`launcher.py` の全 hook 起動時に、その時点で分かっている plugin root
（``launcher.REPO_ROOT``）を ``~/.bluecore`` 配下へ書き出す。md 側は
``. "$HOME/.bluecore/env.sh"`` の 1 行だけを書けばよくなる。

ポインタはホストプロセス単位で分離する（``os.getppid()`` をキーにする）。単一
ファイルにすると、別ターミナルで異なるホスト（Claude Code / Copilot 等）を同時に
開いたとき後勝ちで上書きされ、先に開いていたホストのセッションが別ホストの
install を source してしまうため。Bash tool の親プロセスは常にホストのバイナリ
そのものであり、hook（launcher）も Bash tool も同じホストプロセスの子なので、
Python 側の ``os.getppid()`` と shell 側の ``$PPID`` は一致する（実測）。

環境変数によるホスト判定（``COPILOT_*`` 等）は採らない。Copilot CLI / Grok CLI が
bash tool の環境へ自身の識別変数を注入することは文書化されておらず、確認できた
のは Claude Code の変数（``CLAUDECODE`` 等）のみだったため（詳細は
``docs/adr/0008-*.md``）。

``docs/reports/PLUGIN_ROOT_RESOLVER_2026-08-20_VERIFICATION.md``（R-01〜R-06）を
受け、v0.9.35 の設計を改めた:

- ポインタは shell script ではなく **データファイル**（絶対パス 1 行のみ）にする。
  resolver（``runtime/env-template.sh``）が ``.``/``source`` で読み込むのをやめ、
  ``read`` で 1 行だけ取り出す形に変えたため、ポインタの内容がそのまま shell
  として実行されることはない（R-01/R-03）。
  ``BLUECORE_ROOT="..."`` という shell 代入形式は廃止した。
- ポインタの置き場所は ``get_bluecore_dir()``（``BLUECORE_HOME`` を見る、
  テスト隔離用のノブ）ではなく **``$HOME/.bluecore`` 固定**にする（R-04）。
  md の bootstrap 行 ``. "$HOME/.bluecore/env.sh"`` は環境依存の分岐を持たない
  固定住所である必要があり、writer 側もそれに合わせる。データ永続化
  （``mem.db``・``logs``）側の ``get_bluecore_dir()`` 契約とは別の契約であり、
  意図的に切り離している。
- ``roots/latest`` という「無条件で採用される」ポインタは廃止した。resolver 側
  （``env-template.sh``）が「``roots/`` の有効な候補が全て同じ root 値に
  一致するときだけ採用する」形に変わったため、writer は ``roots/<pid>`` を
  書くだけでよい（同一ホストが launcher 起動のたびに異なる短命 PID を
  記録しても、root 値さえ一致していれば解決できる）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from bluecore.lib.constants import BASE_DIR_NAME

_ROOTS_DIRNAME = "roots"
_ENV_FILENAME = "env.sh"
_ENV_TEMPLATE_RELATIVE = Path("runtime") / "env-template.sh"

_MAX_ROOT_AGE_SECONDS = 7 * 24 * 60 * 60
"""生存 PID のポインタでもこれより mtime が古ければ PID 再利用とみなし削除する。"""

_DEAD_PID_GRACE_SECONDS = 60 * 60
"""PID が既に居ないポインタでも、この秒数より新しければ削除を 1 サイクル待つ
（書き込み直後の短命プロセスを誤って消さないための猶予）。"""

_GC_THROTTLE_SECONDS = 60 * 60
"""GC を実行する最小間隔。全 hook 起動のたびに ``roots/`` を全走査しないための throttle。"""

_GC_STAMP_FILENAME = "roots-gc.stamp"

_warned_this_process = False


def write_env_pointer(plugin_root: Path) -> None:
    """plugin root を ``~/.bluecore`` 配下のポインタへ書き出す。

    書き込み失敗（``OSError``）は握り潰す。本関数は hook のあらゆる起動経路から
    呼ばれるため、失敗しても呼び出し元の hook 本来の処理を妨げてはならない。
    ただし失敗はプロセスごと 1 回だけ stderr へ記録し、無音の機能低下を防ぐ。

    Args:
        plugin_root: このプラグインのソースルート（``launcher.REPO_ROOT``）。

    Returns:
        なし。

    Raises:
        例外は発生しません（``OSError`` は内部で捕捉します）。
    """
    try:
        _write_env_pointer_unsafe(plugin_root)
    except OSError as exc:
        _warn_once(str(exc))


def _warn_once(reason: str) -> None:
    """pointer 書き込み失敗をプロセスごと 1 回だけ stderr へ記録する。

    Args:
        reason: 失敗理由（例外メッセージ）。

    Returns:
        なし。

    Raises:
        例外は発生しません。
    """
    global _warned_this_process
    if _warned_this_process:
        return
    _warned_this_process = True
    try:
        sys.stderr.write(
            json.dumps({"bluecoreEnvPointerWriteFailed": True, "reason": reason}) + "\n"
        )
    except OSError:
        pass


def _state_home() -> Path:
    """env pointer の置き場所となる HOME を解決する。

    ``BLUECORE_HOME`` は見ない（テスト隔離専用のノブであり、md の bootstrap 行が
    ``$HOME`` 固定であるため writer 側もそれに合わせる契約。データ永続化側の
    ``get_bluecore_dir()``/``get_home_dir()`` とは別契約）。

    Args:
        なし。

    Returns:
        HOME ディレクトリの Path。``HOME`` 未設定時は ``Path.home()``、それも
        失敗する場合は ``Path.cwd()``。

    Raises:
        例外は発生しません。
    """
    raw = os.environ.get("HOME")
    if raw:
        return Path(raw).expanduser()
    try:
        return Path.home()
    except RuntimeError:
        return Path.cwd()


def _state_dir() -> Path:
    """env pointer 一式（``env.sh``・``roots/``）を置くディレクトリを返す。

    Args:
        なし。

    Returns:
        ``$HOME/.bluecore`` の Path。

    Raises:
        例外は発生しません。
    """
    return _state_home() / BASE_DIR_NAME


def _ensure_private_dir(path: Path) -> Path:
    """ディレクトリを作成し 0700 に締める（``core_utils.ensure_private_dir`` と同等）。

    ``core_utils`` 側は ``get_bluecore_dir()``（``BLUECORE_HOME`` 対応）基準で
    「bluecore_dir 配下かどうか」を判定するため、``$HOME`` 固定の本モジュールでは
    そのまま流用できない。ロジックを複製せず、必要な最小限だけをここに持つ。

    Args:
        path: 作成・権限設定するディレクトリ。

    Returns:
        対象ディレクトリの Path。

    Raises:
        OSError: 作成・chmod に失敗した場合。
    """
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _atomic_write_text(path: Path, text: str) -> None:
    """テキストを一時ファイル経由で原子的に書き込む（partial read を防ぐ）。

    同一ディレクトリに ``<name>.tmp.<pid>.<counter>`` を mode 0600 で作成し、
    ``fsync`` 後に ``os.replace()`` で公開する。reader は常に「書き込み前の
    内容」か「書き込み後の内容」のどちらかしか見えない。

    Args:
        path: 最終的な公開先ファイルパス。
        text: 書き込む内容。

    Returns:
        なし。

    Raises:
        OSError: 一時ファイルの作成・書き込み・rename に失敗した場合。
    """
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except OSError:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _write_env_pointer_unsafe(plugin_root: Path) -> None:
    """``write_env_pointer`` の本体。``OSError`` を送出しうる。

    書き込み順は「ポインタ → ``env.sh`` → GC」。GC を最後にすることで、
    途中で GC が失敗しても本来の目的（root の記録）は既に達成済みになる。

    Args:
        plugin_root: このプラグインのソースルート。

    Returns:
        なし。

    Raises:
        OSError: root 文字列が不正、またはディレクトリ作成・ファイル書き込みに
            失敗した場合。
    """
    root_text = str(plugin_root)
    if "\n" in root_text or "\r" in root_text or "\x00" in root_text:
        raise OSError(f"plugin root contains a newline or NUL byte: {root_text!r}")

    bluecore_dir = _ensure_private_dir(_state_dir())
    roots_dir = _ensure_private_dir(bluecore_dir / _ROOTS_DIRNAME)

    own_pid = os.getppid()
    _atomic_write_text(roots_dir / str(own_pid), root_text + "\n")

    template_path = plugin_root / _ENV_TEMPLATE_RELATIVE
    template_text = template_path.read_text(encoding="utf-8")
    _atomic_write_text(bluecore_dir / _ENV_FILENAME, template_text)

    _maybe_run_gc(bluecore_dir, roots_dir, keep_pid=own_pid)


def _maybe_run_gc(bluecore_dir: Path, roots_dir: Path, *, keep_pid: int | None) -> None:
    """throttle を守りつつ ``roots/`` の GC を実行する。

    ``roots-gc.stamp`` の mtime を見て ``_GC_THROTTLE_SECONDS`` 以内なら何もしない
    （全 hook 起動のたびに ``roots/`` を全走査しないため）。stamp の更新自体も
    GC 実行の合図として扱う。

    Args:
        bluecore_dir: ``$HOME/.bluecore`` の Path。
        roots_dir: ``$HOME/.bluecore/roots`` の Path。
        keep_pid: この呼び出しで書いたばかりのポインタの PID。GC の対象から
            無条件で除外する（``os.getppid()`` が launcher 起動のたびに
            使い捨てられる中間 shell を指す場合、書いた直後の PID が既に
            「不在」に見えて GC 対象になりうるため。実行中ホストが自分で
            書いたばかりの記録を、そのホスト自身が壊すことがあってはならない）。
            ``None`` なら除外なし。

    Returns:
        なし。

    Raises:
        例外は発生しません（内部の GC 処理は失敗を個別に無視します）。
    """
    stamp = bluecore_dir / _GC_STAMP_FILENAME
    try:
        if stamp.stat().st_mtime > _now() - _GC_THROTTLE_SECONDS:
            return
    except OSError:
        pass

    try:
        stamp.touch()
    except OSError:
        pass

    _gc_roots(roots_dir, keep_pid=keep_pid)


def _now() -> float:
    """現在時刻の unix time を返す（テストで monkeypatch しやすいよう分離）。"""
    import time

    return time.time()


def _pid_is_alive(pid: int) -> bool:
    """PID が現在生存しているか調べる。

    Args:
        pid: 検査対象の PID。

    Returns:
        生存していれば True。自プロセスグループ全体へシグナルが飛ぶ ``pid=0``
        は常に False（無効な入力として扱う）。他ユーザー所有で ``kill`` の権限が
        無い場合（``PermissionError``）は「生存している」とみなす（可視性が無い
        だけで、存在自体は確からしいため）。

    Raises:
        例外は発生しません。
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _gc_roots(roots_dir: Path, *, keep_pid: int | None = None) -> None:
    """``roots/`` を掃除する。実行中ホストのポインタは残す。

    削除条件（いずれか）:

    - ファイル名が数字のみでない（旧形式 ``<pid>.sh``・``latest``・``latest.sh``
      を含む）。
    - PID が既に存在せず、かつ mtime が ``_DEAD_PID_GRACE_SECONDS`` より古い
      （書き込み直後の短命プロセスは 1 サイクル猶予する）。
    - PID は存在するが mtime が ``_MAX_ROOT_AGE_SECONDS`` より古い（稼働中の
      ホストは全 hook 起動で mtime を更新し続けるため、これは PID 再利用と判断
      できる）。

    ``keep_pid`` と一致するファイル名は、上記のどの条件に当てはまっても削除
    しない（このプロセス自身が今まさに書いたポインタを、同じ呼び出しの中で
    自分自身が消してしまう自己矛盾を避けるため）。

    Args:
        roots_dir: ``$HOME/.bluecore/roots`` の Path。
        keep_pid: 削除対象から無条件で除外する PID。``None`` なら除外なし。

    Returns:
        なし。

    Raises:
        例外は発生しません（個々のエントリの stat/unlink 失敗は無視します）。
    """
    now = _now()
    try:
        entries = list(roots_dir.iterdir())
    except OSError:
        return

    keep_name = str(keep_pid) if keep_pid is not None else None
    for entry in entries:
        if entry.name == _GC_STAMP_FILENAME or entry.name == keep_name:
            continue
        try:
            _gc_one(entry, now)
        except OSError:
            continue


def _gc_one(entry: Path, now: float) -> None:
    """``roots/`` 配下の 1 エントリを判定し、対象なら削除する。

    Args:
        entry: 判定対象のファイル。
        now: 現在時刻（unix time）。

    Returns:
        なし。

    Raises:
        OSError: stat/unlink に失敗した場合（呼び出し元で無視される）。
    """
    if not entry.name.isdigit():
        entry.unlink()
        return

    pid = int(entry.name)
    mtime = entry.stat().st_mtime

    if _pid_is_alive(pid):
        if mtime < now - _MAX_ROOT_AGE_SECONDS:
            entry.unlink()
        return

    if mtime < now - _DEAD_PID_GRACE_SECONDS:
        entry.unlink()
