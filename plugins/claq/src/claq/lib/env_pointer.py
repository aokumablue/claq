"""plugin root をベンダ非依存の固定住所（``~/.claq/env.sh``）へ書き出す。

``CLAUDE_PLUGIN_ROOT`` は Bash tool の環境変数に乗らないため、md 側には plugin root を
知る手段が無い。`launcher.py` が全 hook 起動のたびに本モジュールで plugin root を
書き出し、md 側は ``. "$HOME/.claq/env.sh"`` の 1 行だけを書く（ベンダ固有パスを
md に書かない）。

- ポインタはホストプロセス単位で分ける。単一ファイルにすると、別ターミナルで異なる
  ホストを同時に開いたとき後勝ちで上書きされ、先のセッションが別ホストの install を
  source してしまう。環境変数によるホスト判定は、Copilot CLI / Grok CLI が識別変数を
  bash tool へ注入する保証が無いため採らない。
- writer は ``os.getppid()`` とその親（最大 2 段）にだけ ``root\\nlstart\\n`` の
  2 行を書く。この 2 段（bash tool の shell と host バイナリ）は host インスタンス
  専有の PID で、他 host と共有されうる上位の祖先には書かない。したがって同一 PID に
  別の root が観測されるのはプラグインのアップグレードだけで、単純に上書きする。
- ``lstart`` は resolver（``runtime/env-template.sh``）が「その PID は記録時と同一の
  プロセスか」を照合する識別子。resolver は祖先で解決できなければ推測せず
  ``exit 127`` にする。``roots/latest`` のような無条件に採用されるポインタは置かない。
- ポインタは shell script ではなくデータで、resolver は ``read`` で取り出す。
- 置き場所は ``get_claq_dir()``（``CLAQ_HOME`` を見る）ではなく ``$HOME/.claq`` 固定。
  md の bootstrap 行が ``$HOME`` 固定なので writer も合わせる。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from claq.lib.constants import BASE_DIR_NAME
from claq.lib.subprocess_utils import run_text

_ROOTS_DIRNAME = "roots"
_ENV_FILENAME = "env.sh"
_ENV_TMP_PREFIX = f"{_ENV_FILENAME}.tmp."
_ENV_TEMPLATE_RELATIVE = Path("runtime") / "env-template.sh"

_MAX_ANCESTOR_DEPTH = 2
"""writer が祖先 PID へポインタを書く最大段数。resolver の祖先 walk と同じ深さに揃える。"""

_PS_TIMEOUT_SECONDS = 5
"""祖先チェーン取得用 ``ps`` 呼び出しのハードタイムアウト。"""

_MAX_ROOT_AGE_SECONDS = 7 * 24 * 60 * 60
"""生存 PID のポインタでもこれより mtime が古ければ PID 再利用とみなし削除する。"""

_DEAD_PID_GRACE_SECONDS = 60 * 60
"""PID が既に居ないポインタでも、この秒数より新しければ削除を 1 サイクル待つ
（書き込み直後の短命プロセスを誤って消さないための猶予）。"""

_GC_THROTTLE_SECONDS = 60 * 60
"""GC を実行する最小間隔。全 hook 起動のたびに ``roots/`` を全走査しないための throttle。"""

_GC_STAMP_FILENAME = "roots-gc.stamp"

_warned_this_process = False


def ancestor_pointers_supported() -> bool:
    """祖先 PID ポインタ方式が成立する OS かを返す。

    この方式は writer（``ps -eo pid=,ppid=,lstart=``）と resolver（POSIX sh +
    ``ps -o lstart=``）の両方に依存する。Windows には ``ps`` が無く、Git Bash の
    ``ps`` は ``-o`` 書式を持たないうえ MSYS の別 PID 空間を返すため照合できない。
    Windows では md の bootstrap 行の代わりに `runtime/claq-hook` を直接呼ぶ必要があり、
    SessionStart の ``mem context`` はこの関数でその読み替え方を提示する。

    プラットフォーム名ではなく ``os.setsid``（POSIX プロセスセッション API）の有無で判定する。

    Returns:
        POSIX（祖先ポインタ方式が使える）なら True。
    """
    return hasattr(os, "setsid")


def write_env_pointer(plugin_root: Path) -> None:
    """plugin root を ``~/.claq`` 配下のポインタへ書き出す。

    hook のあらゆる起動経路から呼ばれるため、失敗（``OSError`` と HOME を決められない
    ``RuntimeError``）で hook 本来の処理を妨げない。ただし無音の機能低下を防ぐため、
    失敗はプロセスごと 1 回だけ stderr へ記録する。

    Args:
        plugin_root: このプラグインのソースルート（``launcher.REPO_ROOT``）。
    """
    try:
        _write_env_pointer_unsafe(plugin_root)
    except (OSError, RuntimeError) as exc:
        _warn_once(str(exc))


def _warn_once(reason: str) -> None:
    """pointer 書き込み失敗をプロセスごと 1 回だけ stderr へ JSON で記録する。"""
    global _warned_this_process
    if _warned_this_process:
        return
    _warned_this_process = True
    try:
        sys.stderr.write(json.dumps({"claqEnvPointerWriteFailed": True, "reason": reason}) + "\n")
    except OSError:
        pass


def _state_home() -> Path:
    """env pointer の置き場所となる HOME を返す。

    ``CLAQ_HOME`` は見ない（モジュール docstring 参照）。``HOME`` 未設定時は ``Path.home()``。

    Raises:
        RuntimeError: HOME ディレクトリを決められない場合（読む側のシェルは ``$HOME``
            しか見ないため、別の場所へは書かない）。
    """
    raw = os.environ.get("HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home()


def _ensure_private_dir(path: Path) -> Path:
    """ディレクトリを根から順に 0700 で作成し、0700 に締めて返す。

    ``core_utils.ensure_private_dir`` は ``get_claq_dir()``（``CLAQ_HOME`` 対応）基準で
    判定するため、``$HOME`` 固定の本モジュールでは流用できない。

    作成と ``chmod`` を分けると、その窓の間に他 OS ユーザーが ``opendir`` で得た fd は
    後続の ``chmod`` で失効しない。``mkdir(mode=..., parents=True)`` は親に mode を
    適用しないため、祖先を 1 段ずつ作る。``chmod`` は既に 0755 で存在する場合の是正用。

    Raises:
        OSError: 作成・chmod に失敗した場合。
    """
    for target in (*reversed(path.parents), path):
        target.mkdir(mode=0o700, exist_ok=True)
    path.chmod(0o700)
    return path


def _atomic_write_text(path: Path, text: str) -> None:
    """テキストを同一ディレクトリの一時ファイル経由で原子的に書き込む。

    ``<name>.tmp.<random>`` を mode 0600 で作り、``fsync`` 後に ``os.replace()`` で公開する
    ので、reader は書き込み前後どちらかの内容しか見ない。名前は数字のみにならない
    （``roots/`` の GC が非数字名として age-gate する契約）。``fdopen`` が失敗した場合は
    mkstemp の fd を閉じてから tmp を消す。

    Raises:
        OSError: 一時ファイルの作成・書き込み・rename に失敗した場合。
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.tmp.")
    tmp_path = Path(tmp_name)
    owned_fd: int | None = fd
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            owned_fd = None
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except OSError:
        if owned_fd is not None:
            try:
                os.close(owned_fd)
            except OSError:
                pass
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _write_text_if_changed(path: Path, text: str) -> bool:
    """内容が既に一致していれば書き込みを丸ごと省く。

    ``env.sh`` は同じプラグインバージョンの間は内容が変わらないが、PreToolUse が全ツール
    呼び出しで書くため、毎回の fsync がネットワーク HOME や暗号化 FS で遅延になる。
    ``roots/<pid>`` は GC が mtime で鮮度を判定するため、この省略を使わない。

    比較はバイト列で行う（``read_text`` は非 UTF-8 の既存ファイルで ``ValueError`` 系を
    送出し、`write_env_pointer` の ``OSError`` ハンドラを素通りしてフックを落とす）。
    symlink は内容が一致していても書き直す。``os.replace`` が実体ファイルへ戻す自己修復を
    失わないため。

    Returns:
        実際に書き込んだなら True、内容一致で省いたなら False。

    Raises:
        OSError: 書き込みに失敗した場合（読み取り失敗は書き込みへ倒すので送出しない）。
    """
    try:
        if not path.is_symlink() and path.read_bytes() == text.encode("utf-8"):
            return False
    except OSError:
        # 未作成・読み取り不能。いずれも「一致していない」として書きに行く。
        pass
    _atomic_write_text(path, text)
    return True


def _resolve_ancestor_chain(max_depth: int) -> list[tuple[int, str]]:
    """``os.getppid()`` から始まる祖先チェーンと、各 PID の起動時刻を返す。

    全 hook 起動で走るため、1 回の ``ps -eo pid=,ppid=,lstart=`` でプロセス表全体を取り、
    チェーンは Python 側で組む。``lstart`` は内部の空白を正規化しない raw な文字列のまま
    保持する（shell 側の ``$(ps -o lstart= -p <pid>)`` とバイト単位で一致させるため）。

    ``lstart`` はロケール依存なので、resolver 側と同じく ``LC_ALL=C`` を強制する。
    subprocess は encoding 指定を集約した ``run_text`` 経由で呼ぶ（locale 依存のデコードで
    ``UnicodeDecodeError`` が下の except を貫通しないため）。

    Args:
        max_depth: 辿る祖先の最大段数（``os.getppid()`` 自身を含む）。

    Returns:
        ``(pid, lstart)`` の近い祖先から遠い祖先の順のリスト。``ps`` の起動・実行に
        失敗した場合は空リスト（docs/adr/hook-failure-direction.md の fail-open —
        この回はポインタを書かず、次回 hook 起動での自己修復に委ねる）。チェーンは
        PID 0/1 に達するか、プロセス表から親を特定できなくなった時点で打ち切る。

    Raises:
        例外は発生しません。
    """
    try:
        proc = run_text(["ps", "-eo", "pid=,ppid=,lstart="], timeout=_PS_TIMEOUT_SECONDS, extra_env={"LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []

    ppid_of: dict[int, int] = {}
    lstart_of: dict[int, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        ppid_of[pid] = ppid
        lstart_of[pid] = parts[2]

    chain: list[tuple[int, str]] = []
    current = os.getppid()
    for _ in range(max_depth):
        lstart = lstart_of.get(current)
        if lstart is None:
            break
        chain.append((current, lstart))
        parent = ppid_of.get(current)
        if parent is None or parent in (0, 1):
            break
        current = parent
    return chain


def _write_ancestor_pointer(path: Path, root_text: str, lstart: str) -> None:
    """1 つの祖先 PID のポインタへ ``root_text`` と ``lstart`` の 2 行を無条件に書く。

    書き込み対象の PID は host インスタンス専有なので、既存の記録と食い違っても
    同一 host のアップグレードでしかなく、衝突検知は要らない。

    Raises:
        OSError: 書き込みに失敗した場合。
    """
    _atomic_write_text(path, f"{root_text}\n{lstart}\n")


def _write_env_pointer_unsafe(plugin_root: Path) -> None:
    """``write_env_pointer`` の本体。

    書き込み順は「祖先ポインタ群 → ``env.sh`` → GC」。GC を最後にして、GC が失敗しても
    root の記録は済んでいるようにする。

    Raises:
        OSError: root 文字列が不正、またはディレクトリ作成・ファイル書き込みに失敗した場合。
    """
    root_text = str(plugin_root)
    if "\n" in root_text or "\r" in root_text or "\x00" in root_text:
        raise OSError(f"plugin root contains a newline or NUL byte: {root_text!r}")

    claq_dir = _ensure_private_dir(_state_home() / BASE_DIR_NAME)
    roots_dir = _ensure_private_dir(claq_dir / _ROOTS_DIRNAME)

    # 祖先ポインタ方式が成立しない OS（Windows）では `ps` を呼ばない。全 hook 起動ごとの
    # 無駄な spawn になるうえ、MSYS/Cygwin 由来の `ps.exe` が PATH にあると別 PID 空間の
    # 値を拾って無意味なポインタを書きうる。
    chain = _resolve_ancestor_chain(_MAX_ANCESTOR_DEPTH) if ancestor_pointers_supported() else []
    for pid, lstart in chain:
        _write_ancestor_pointer(roots_dir / str(pid), root_text, lstart)

    template_text = (plugin_root / _ENV_TEMPLATE_RELATIVE).read_text(encoding="utf-8")
    _write_text_if_changed(claq_dir / _ENV_FILENAME, template_text)

    _maybe_run_gc(claq_dir, roots_dir, keep_pids=frozenset(pid for pid, _lstart in chain))


def _maybe_run_gc(claq_dir: Path, roots_dir: Path, *, keep_pids: frozenset[int]) -> None:
    """throttle を守りつつ ``roots/`` と ``claq_dir`` 直下の一時ファイルの GC を実行する。

    ``roots-gc.stamp`` の mtime が ``_GC_THROTTLE_SECONDS`` 以内なら何もしない。

    Args:
        claq_dir: ``$HOME/.claq`` の Path。
        roots_dir: ``$HOME/.claq/roots`` の Path。
        keep_pids: この呼び出しで書いた祖先チェーンの PID 集合。GC 対象から無条件に
            除外する（祖先が使い捨ての中間 shell だと、書いた直後の PID が既に不在に
            見えるため。実行中ホストが書いたばかりの記録を自分で壊さない）。
    """
    stamp = claq_dir / _GC_STAMP_FILENAME
    try:
        if stamp.stat().st_mtime > _now() - _GC_THROTTLE_SECONDS:
            return
    except OSError:
        pass

    try:
        stamp.touch()
    except OSError:
        pass

    _gc_roots(roots_dir, keep_pids=keep_pids)
    _gc_stale_temp_files(claq_dir, _now())


def _now() -> float:
    """現在時刻の unix time を返す（テストで monkeypatch しやすいよう分離）。"""
    return time.time()


def _pid_is_alive(pid: int) -> bool:
    """PID が現在生存しているか調べる。

    自プロセスグループ全体へシグナルが飛ぶ ``pid=0`` 以下は常に False。他ユーザー所有で
    ``kill`` の権限が無い場合（``PermissionError``）は、存在自体は確からしいので True。
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):  # OverflowError は C の int に収まらない PID（実在しえない）
        return False
    return True


def _gc_roots(roots_dir: Path, *, keep_pids: frozenset[int] = frozenset()) -> None:
    """``roots/`` を掃除する。実行中ホストのポインタは残す。

    ``keep_pids`` に含まれるファイル名は削除しない（同じ呼び出しで書いたばかりの
    ポインタを自分で消さないため）。個々のエントリの判定は ``_gc_one`` が行い、
    stat/unlink の失敗は無視する。
    """
    now = _now()
    try:
        entries = list(roots_dir.iterdir())
    except OSError:
        return

    keep_names = {str(pid) for pid in keep_pids}
    for entry in entries:
        if entry.name == _GC_STAMP_FILENAME or entry.name in keep_names:
            continue
        try:
            _gc_one(entry, now)
        except OSError:
            continue


def _gc_one(entry: Path, now: float) -> None:
    """``roots/`` 配下の 1 エントリを判定し、対象なら削除する。

    - 生存 PID のポインタは mtime が ``_MAX_ROOT_AGE_SECONDS`` より古ければ削除する
      （稼働中のホストは全 hook 起動で mtime を更新し続けるため、PID 再利用と判断できる）。
    - それ以外（PID 不在・数字のみでない名前）は mtime が ``_DEAD_PID_GRACE_SECONDS``
      より古ければ削除する。数字のみでない名前には別 writer が ``os.replace()`` 直前に
      作った一時ファイルも含まれ、即削除するとその rename 前に消す race になる。

    Raises:
        OSError: stat/unlink に失敗した場合（呼び出し元で無視される）。
    """
    # isdigit は ² のような Unicode の数字も真にするので、int() で落ちないよう ASCII に限る
    alive = entry.name.isascii() and entry.name.isdigit() and _pid_is_alive(int(entry.name))
    mtime = entry.stat().st_mtime
    if mtime < now - (_MAX_ROOT_AGE_SECONDS if alive else _DEAD_PID_GRACE_SECONDS):
        entry.unlink()


def _gc_stale_temp_files(claq_dir: Path, now: float) -> None:
    """``claq_dir`` 直下の ``env.sh.tmp.*`` 孤児を age-gate で回収する。

    ``env.sh`` の中間ファイルはクラッシュすると ``roots/`` ではなく ``claq_dir`` 直下に
    残るため、``_gc_roots`` とは別にここで拾う。``_DEAD_PID_GRACE_SECONDS`` を過ぎた
    ものだけを消すので、書き込み中のファイルは取り上げない。iterdir/stat/unlink の失敗は
    無視する。

    detach の stdin 一時ファイルは生成側が ``get_claq_dir()`` 配下に作る別契約なので、
    回収も生成側（`hook_common.gc_detach_stdin_orphans`）に置く。
    """
    try:
        entries = list(claq_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.name.startswith(_ENV_TMP_PREFIX):
            continue
        try:
            if entry.stat().st_mtime < now - _DEAD_PID_GRACE_SECONDS:
                entry.unlink()
        except OSError:
            continue
