#!/usr/bin/env python3
"""
継続学習 v2 のための instinct 管理 CLI ツール。

v2.1: プロジェクトスコープに対応。プロジェクトごとに異なる instinct を持ち、
      グローバル instinct は共通で適用される。

コマンド:
  import   - ファイルまたは URL から instinct を取り込む
  export   - instinct をファイルに書き出す
  evolve   - instinct を skill/command/agent にクラスタリングする
  promote  - プロジェクト instinct をグローバルスコープへ昇格する
  prune    - 30 日より古い保留中 instinct（TTL）を削除する

実装はサブモジュール（paths/registry/instincts/pending/evolve/promote/
commands/entry）へ分割されている。このパッケージは全公開シンボルと
モジュール定数をここへ再エクスポートして集約し、従来どおり
``from bluecore.skills.learn.cli import X`` および ``cli.X`` 属性アクセス、
``monkeypatch.setattr(cli, "X", ...)`` を成立させる。各サブモジュールは
``monkeypatch`` 差し替え対象を実行時にこのパッケージ名前空間経由で参照する。
"""

# テストが ``cli.socket.getaddrinfo`` / ``cli.urllib.request.build_opener`` を
# 参照・差し替えできるよう、これらの標準ライブラリをパッケージ名前空間へ公開する。
import builtins as _builtins
import socket  # noqa: F401
import urllib.parse  # noqa: F401
import urllib.request  # noqa: F401

# テストが ``load_registry`` のファイルオープンを差し替えられるよう、組込み ``open`` を
# パッケージ名前空間へ公開する。``registry.py`` はこれを ``_pkg.open`` 経由で参照し、
# ``monkeypatch.setattr(cli, "open", ...)`` がそのままその呼び出しに反映される。
open = _builtins.open  # noqa: A001  -- 組込み open をパッケージ属性として公開

# fcntl の有無検出はこのパッケージ ``__init__`` で行う。テストは ``cli`` パッケージ
# （= この ``__init__``）を ``importlib.reload`` して ``_HAS_FCNTL`` の再評価を期待する
# ため、検出をここに置くことで reload が正しく反映される。``registry.py`` はこの結果を
# ``_pkg._HAS_FCNTL`` / ``_pkg.fcntl`` 経由で実行時に参照する。
try:
    import fcntl  # noqa: F401

    _HAS_FCNTL = True
except ImportError:
    fcntl = None  # type: ignore[assignment]  -- ウィンドウズ環境ではファイルロックをスキップ
    _HAS_FCNTL = False

# サブモジュールの再エクスポートは fcntl/open をパッケージへ公開した後に行う。これらの
# サブモジュールは import 時に ``bluecore.skills.learn.cli`` を参照するため、公開を先に
# 済ませておく必要がある。そのため以下の import は意図的にファイル先頭ではない（E402 抑止）。
from .commands import (  # noqa: E402
    cmd_export,
    cmd_import,
    cmd_projects,
    cmd_prune,
    cmd_status,
)
from .entry import main  # noqa: E402
from .evolve import (  # noqa: E402
    _find_cross_project_instincts,
    _generate_evolved,
    _show_promotion_candidates,
    cmd_evolve,
)
from .instincts import (  # noqa: E402
    _load_instincts_from_dir,
    _print_instincts_by_domain,
    load_all_instincts,
    load_project_only_instincts,
    parse_instinct_file,
)
from .paths import (  # noqa: E402
    ALLOWED_INSTINCT_EXTENSIONS,
    BLUECORE_DIR,
    GLOBAL_EVOLVED_DIR,
    GLOBAL_INHERITED_DIR,
    GLOBAL_INSTINCTS_DIR,
    GLOBAL_OBSERVATIONS_FILE,
    GLOBAL_PERSONAL_DIR,
    PENDING_EXPIRY_WARNING_DAYS,
    PENDING_TTL_DAYS,
    PROJECTS_DIR,
    PROMOTE_CONFIDENCE_THRESHOLD,
    PROMOTE_MIN_PROJECTS,
    REGISTRY_FILE,
    _all_project_dirs,
    _assert_safe_url,
    _ensure_global_dirs,
    _fetch_url,
    _preferred_projects_dir,
    _preferred_registry_file,
    _project_dir_for_id,
    _project_dir_score,
    _SsrfSafeRedirectHandler,
    _validate_file_path,
    _validate_instinct_id,
    _yaml_quote,
)
from .pending import (  # noqa: E402
    _collect_pending_dirs,
    _collect_pending_instincts,
    _parse_created_date,
)
from .promote import (  # noqa: E402
    _promote_auto,
    _promote_specific,
    cmd_promote,
)
from .registry import (  # noqa: E402
    _update_registry,
    detect_project,
    load_registry,
)

__all__ = [
    "ALLOWED_INSTINCT_EXTENSIONS",
    "BLUECORE_DIR",
    "GLOBAL_EVOLVED_DIR",
    "GLOBAL_INHERITED_DIR",
    "GLOBAL_INSTINCTS_DIR",
    "GLOBAL_OBSERVATIONS_FILE",
    "GLOBAL_PERSONAL_DIR",
    "PENDING_EXPIRY_WARNING_DAYS",
    "PENDING_TTL_DAYS",
    "PROJECTS_DIR",
    "PROMOTE_CONFIDENCE_THRESHOLD",
    "PROMOTE_MIN_PROJECTS",
    "REGISTRY_FILE",
    "_HAS_FCNTL",
    "_SsrfSafeRedirectHandler",
    "_all_project_dirs",
    "_assert_safe_url",
    "_collect_pending_dirs",
    "_collect_pending_instincts",
    "_ensure_global_dirs",
    "_fetch_url",
    "_find_cross_project_instincts",
    "_generate_evolved",
    "_load_instincts_from_dir",
    "_parse_created_date",
    "_preferred_projects_dir",
    "_preferred_registry_file",
    "_print_instincts_by_domain",
    "_project_dir_for_id",
    "_project_dir_score",
    "_promote_auto",
    "_promote_specific",
    "_show_promotion_candidates",
    "_update_registry",
    "_validate_file_path",
    "_validate_instinct_id",
    "_yaml_quote",
    "cmd_evolve",
    "cmd_export",
    "cmd_import",
    "cmd_projects",
    "cmd_prune",
    "cmd_promote",
    "cmd_status",
    "detect_project",
    "fcntl",
    "load_all_instincts",
    "load_project_only_instincts",
    "load_registry",
    "main",
    "open",
    "parse_instinct_file",
]


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
