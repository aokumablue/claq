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
- ``roots/latest`` という「無条件で採用される」ポインタは廃止した。

``docs/reports/PLUGIN_ROOT_RESOLVER_2026-08-20_V0.9.36_REVERIFICATION.md``
（H-01/H-02/M-01）を受け、さらに設計を改めた。とくに H-02
（祖先不一致時に「root 値が一致する候補」で推測 fallback していた）は
ユーザーから「リスクがあるとみなせる fallback は望ましくない」との明示
指示を受け、**推測に基づく fallback を全廃**した:

- writer は ``os.getppid()`` だけでなく、その**祖先チェーンを最大 4 段**
  まで辿り、各段の PID にも同じ root を記録する
  （``_resolve_ancestor_chain``）。1 回の ``ps -eo pid=,ppid=,lstart=``
  でチェーンと各 PID の起動時刻（``lstart``）を同時に取得する（実測
  10〜16ms、hook のタイムアウト予算に対して無視できるコスト）。
- 各ポインタは ``root\nlstart\n`` の 2 行になる。``lstart`` は
  resolver 側が「今生きているその PID は、記録時と同一のプロセスか」を
  照合するための識別子（PID は再利用されるが、同一 PID が同一
  ``lstart`` を持つのは同一プロセスの生存中に限られる）。
- 祖先 PID に**既に別の root** が記録されていたら、上書きせず内容を
  空にする（**poison**）。これは、同じ terminal / login shell から
  異なる複数の host が起動され、浅い祖先 PID を共有してしまうケースの
  対処: 後勝ち上書きだと片方が誤って相手の root を掴む可能性が残るが、
  poison にすれば resolver はその祖先をスキップして次へ進むため、
  「どちらか一方が誤って相手を掴む」という事象自体が起こらない。自分の
  より近い祖先（通常は host バイナリ自体の PID）は共有されにくいため、
  共有される深い祖先が poison になっても各 host 自身の解決能力は
  損なわれない。
- resolver（``env-template.sh``）は祖先チェーンの ``lstart`` が一致する
  ポインタしか採用しない。祖先チェーンで解決できなければ、推測せず常に
  ``exit 127`` にする（旧 tier 2「root 値合意 + 鮮度フィルタ」は削除）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from bluecore.lib.constants import BASE_DIR_NAME

_ROOTS_DIRNAME = "roots"
_ENV_FILENAME = "env.sh"
_ENV_TEMPLATE_RELATIVE = Path("runtime") / "env-template.sh"

_MAX_ANCESTOR_DEPTH = 4
"""writer が祖先 PID へポインタを書く最大段数。resolver 側
（``env-template.sh`` の祖先 walk）と同じ深さに揃える。"""

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


def _resolve_ancestor_chain(max_depth: int) -> list[tuple[int, str]]:
    """``os.getppid()`` から始まる祖先チェーンと、各 PID の起動時刻を返す。

    1 回の ``ps -eo pid=,ppid=,lstart=`` でプロセス表全体を取得し、
    Python 側でチェーンを計算する（hook 起動のたびに ``ps`` を複数回
    呼ばずに済む。全 hook 起動でこの関数が走るため、呼び出し回数は
    最小化する）。``lstart`` は末尾の改行だけを取り除いた raw な文字列
    のまま保持する — 内部の空白を正規化（strip 等）すると、shell 側の
    ``$(ps -o lstart= -p <pid>)`` が返す文字列とバイト単位で一致しなく
    なるため（実機で非正規化同士が一致することを確認済み）。

    ``ps`` は ``LC_ALL=C`` を強制した環境で実行する。``lstart`` の出力は
    ロケール依存（例: ``LANG=ja_JP.UTF-8`` では ``木  8/20 ...``、
    ``C`` ロケールでは ``Thu Aug 20 ...``）であることを実機で確認した
    — writer（このプロセスの ambient locale）と resolver（bootstrap を
    実行する shell の ambient locale）が異なる環境で起動されると、
    正規化なしでは同一プロセスなのに文字列が一致せず PID 再利用と誤判定
    してしまう。resolver 側（``env-template.sh``）も同じく ``LC_ALL=C``
    を明示して ``ps`` を呼ぶ。

    Args:
        max_depth: 辿る祖先の最大段数（``os.getppid()`` 自身を含む）。

    Returns:
        ``(pid, lstart)`` の近い祖先から遠い祖先の順のリスト。``ps`` の
        起動・実行に失敗した場合は空リスト（ADR-0001 の fail-open —
        この回はポインタを書かず、次回 hook 起動での自己修復に委ねる）。
        チェーンは PID 0/1 に達するか、プロセス表から親を特定できなく
        なった時点で打ち切る。

    Raises:
        例外は発生しません。
    """
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,lstart="],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_SECONDS,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
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
            pid = int(parts[0])
            ppid = int(parts[1])
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


def _write_or_poison_pointer(path: Path, root_text: str, lstart: str) -> None:
    """1 つの祖先 PID のポインタを書くか、対立する記録を poison する。

    - ポインタが存在しない → ``root_text``/``lstart`` を書く。
    - 既存の root が ``root_text`` と同じ → 同内容で書き直す（mtime と
      ``lstart`` を現在値へ更新する）。
    - 既存の root が ``root_text`` と異なり、まだ poison（空）でない →
      内容を空にして poison する（同じ祖先 PID を共有する別 host の
      root と衝突したため、以後どちらの root にも解決させない — H-02
      対応。「後勝ち上書き」で誤った root を静かに掴む経路を、
      「双方とも使わない」に変える）。
    - 既に poison（空ファイル）→ 何もしない（そのファイル名の PID が
      生存し続ける限り、誰にも上書きさせない）。

    Args:
        path: ``roots/<pid>`` のポインタパス。
        root_text: 記録したい plugin root（改行/NUL を含まないことは
            呼び出し元で検証済み）。
        lstart: この祖先 PID 自身の起動時刻（resolver が照合する識別子）。

    Returns:
        なし。

    Raises:
        OSError: 既存ファイルの読み取り・新規書き込みに失敗した場合
            （``FileNotFoundError`` は正常系として内部で処理する）。
    """
    try:
        existing_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        _atomic_write_text(path, f"{root_text}\n{lstart}\n")
        return

    if existing_text == "":
        return

    existing_root = existing_text.split("\n", 1)[0]
    if existing_root == root_text:
        _atomic_write_text(path, f"{root_text}\n{lstart}\n")
        return

    _atomic_write_text(path, "")


def _write_env_pointer_unsafe(plugin_root: Path) -> None:
    """``write_env_pointer`` の本体。``OSError`` を送出しうる。

    書き込み順は「祖先ポインタ群 → ``env.sh`` → GC」。GC を最後にする
    ことで、途中で GC が失敗しても本来の目的（root の記録）は既に
    達成済みになる。

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

    chain = _resolve_ancestor_chain(_MAX_ANCESTOR_DEPTH)
    for pid, lstart in chain:
        _write_or_poison_pointer(roots_dir / str(pid), root_text, lstart)
    written_pids = frozenset(pid for pid, _lstart in chain)

    template_path = plugin_root / _ENV_TEMPLATE_RELATIVE
    template_text = template_path.read_text(encoding="utf-8")
    _atomic_write_text(bluecore_dir / _ENV_FILENAME, template_text)

    _maybe_run_gc(bluecore_dir, roots_dir, keep_pids=written_pids)


def _maybe_run_gc(bluecore_dir: Path, roots_dir: Path, *, keep_pids: frozenset[int]) -> None:
    """throttle を守りつつ ``roots/`` の GC を実行する。

    ``roots-gc.stamp`` の mtime を見て ``_GC_THROTTLE_SECONDS`` 以内なら何もしない
    （全 hook 起動のたびに ``roots/`` を全走査しないため）。stamp の更新自体も
    GC 実行の合図として扱う。

    Args:
        bluecore_dir: ``$HOME/.bluecore`` の Path。
        roots_dir: ``$HOME/.bluecore/roots`` の Path。
        keep_pids: この呼び出しで書いた祖先チェーン全体の PID 集合。GC の
            対象から無条件で除外する（祖先 PID が launcher 起動のたびに
            使い捨てられる中間 shell を指す場合、書いた直後の PID が既に
            「不在」に見えて GC 対象になりうるため。実行中ホストが自分で
            書いたばかりの記録を、そのホスト自身が壊すことがあってはならない）。

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

    _gc_roots(roots_dir, keep_pids=keep_pids)


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


def _gc_roots(roots_dir: Path, *, keep_pids: frozenset[int] = frozenset()) -> None:
    """``roots/`` を掃除する。実行中ホストのポインタは残す。

    削除条件（いずれか）:

    - ファイル名が数字のみでない（旧形式 ``<pid>.sh``・``latest``・``latest.sh``、
      および他 writer の in-flight 一時ファイルを含む。``_DEAD_PID_GRACE_SECONDS``
      による age-gate は ``_gc_one`` 側で行う）。
    - PID が既に存在せず、かつ mtime が ``_DEAD_PID_GRACE_SECONDS`` より古い
      （書き込み直後の短命プロセスは 1 サイクル猶予する）。
    - PID は存在するが mtime が ``_MAX_ROOT_AGE_SECONDS`` より古い（稼働中の
      ホストは全 hook 起動で mtime を更新し続けるため、これは PID 再利用と判断
      できる）。

    ``keep_pids`` に含まれるファイル名は、上記のどの条件に当てはまっても
    削除しない（このプロセス自身が今まさに書いた祖先チェーン全体のポインタを、
    同じ呼び出しの中で自分自身が消してしまう自己矛盾を避けるため）。

    Args:
        roots_dir: ``$HOME/.bluecore/roots`` の Path。
        keep_pids: 削除対象から無条件で除外する PID の集合。

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

    Args:
        entry: 判定対象のファイル。
        now: 現在時刻（unix time）。

    Returns:
        なし。

    Raises:
        OSError: stat/unlink に失敗した場合（呼び出し元で無視される）。
    """
    if not entry.name.isdigit():
        # 旧形式ポインタ（`<pid>.sh`・`latest`・`latest.sh`）だけでなく、
        # 別 writer が `os.replace()` 直前に作った
        # `<pid>.tmp.<writer_pid>` 一時ファイル（`_atomic_write_text` が
        # `roots/` 内に作る）もこの分岐に落ちる。即削除すると、その
        # writer の rename 前に消してしまう race がある（M-01）。実際の
        # 一時ファイル寿命はミリ秒オーダーなので、`_DEAD_PID_GRACE_SECONDS`
        # を過ぎてから削除しても実害は無く、race window を閉じられる。
        mtime = entry.stat().st_mtime
        if mtime < now - _DEAD_PID_GRACE_SECONDS:
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
