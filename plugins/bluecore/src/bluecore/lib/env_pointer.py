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
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from bluecore.lib.core_utils import ensure_private_dir, get_bluecore_dir

_ROOTS_DIRNAME = "roots"
_ENV_FILENAME = "env.sh"
_LATEST_FILENAME = "latest.sh"
_ENV_TEMPLATE_RELATIVE = Path("runtime") / "env-template.sh"

_MAX_ROOT_AGE_SECONDS = 7 * 24 * 60 * 60
"""``roots/<pid>.sh`` の最大保持期間（秒）。PID は OS に再利用されるため、
古いエントリを刈らないと別プロセスの root を誤って読む可能性が残る。"""


def write_env_pointer(plugin_root: Path) -> None:
    """plugin root を ``~/.bluecore`` 配下のポインタへ書き出す。

    書き込み失敗（``OSError``）は握り潰す。本関数は hook のあらゆる起動経路から
    呼ばれるため、失敗しても呼び出し元の hook 本来の処理を妨げてはならない。

    Args:
        plugin_root: このプラグインのソースルート（``launcher.REPO_ROOT``）。

    Returns:
        なし。

    Raises:
        例外は発生しません（``OSError`` は内部で捕捉します）。
    """
    try:
        _write_env_pointer_unsafe(plugin_root)
    except OSError:
        pass


def _write_env_pointer_unsafe(plugin_root: Path) -> None:
    """``write_env_pointer`` の本体。``OSError`` を送出しうる。

    Args:
        plugin_root: このプラグインのソースルート。

    Returns:
        なし。

    Raises:
        OSError: ディレクトリ作成・ファイル書き込みに失敗した場合。
    """
    bluecore_dir = ensure_private_dir(get_bluecore_dir())
    roots_dir = ensure_private_dir(bluecore_dir / _ROOTS_DIRNAME)

    _prune_stale_roots(roots_dir)

    root_line = f'BLUECORE_ROOT="{plugin_root}"\n'
    (roots_dir / f"{os.getppid()}.sh").write_text(root_line, encoding="utf-8")
    (roots_dir / _LATEST_FILENAME).write_text(root_line, encoding="utf-8")

    template_path = plugin_root / _ENV_TEMPLATE_RELATIVE
    template_text = template_path.read_text(encoding="utf-8")
    (bluecore_dir / _ENV_FILENAME).write_text(template_text, encoding="utf-8")


def _prune_stale_roots(roots_dir: Path) -> None:
    """``_MAX_ROOT_AGE_SECONDS`` より古い ``roots/<pid>.sh`` を削除する。

    ``latest.sh`` は経過時間に関わらず残す（常に最新の 1 本を上書きするだけの
    ファイルであり、古さそのものが無効の印にならないため）。

    Args:
        roots_dir: ``~/.bluecore/roots`` の Path。

    Returns:
        なし。

    Raises:
        例外は発生しません（個々のエントリの stat/unlink 失敗は無視します）。
    """
    cutoff = time.time() - _MAX_ROOT_AGE_SECONDS
    try:
        entries = list(roots_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name == _LATEST_FILENAME:
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue
