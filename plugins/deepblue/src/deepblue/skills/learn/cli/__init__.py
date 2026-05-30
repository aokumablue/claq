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
``from deepblue.skills.learn.cli import X`` および ``cli.X`` 属性アクセス、
``monkeypatch.setattr(cli, "X", ...)`` を成立させる。各サブモジュールは
``monkeypatch`` 差し替え対象を実行時にこのパッケージ名前空間経由で参照する。
"""

# テストが ``cli.socket.getaddrinfo`` / ``cli.urllib.request.build_opener`` を
# 参照・差し替えできるよう、これらの標準ライブラリをパッケージ名前空間へ公開する。
import socket  # noqa: F401
import urllib.parse  # noqa: F401
import urllib.request  # noqa: F401

from .paths import (
    ALLOWED_INSTINCT_EXTENSIONS,
    DEEPBLUE_DIR,
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
from .registry import (
    _HAS_FCNTL,
    _update_registry,
    detect_project,
    fcntl,  # noqa: F401  -- テストが cli.fcntl を参照する可能性に備えて公開
    load_registry,
)
from .instincts import (
    _load_instincts_from_dir,
    _print_instincts_by_domain,
    load_all_instincts,
    load_project_only_instincts,
    parse_instinct_file,
)
from .pending import (
    _collect_pending_dirs,
    _collect_pending_instincts,
    _parse_created_date,
)
from .evolve import (
    _find_cross_project_instincts,
    _generate_evolved,
    _show_promotion_candidates,
    cmd_evolve,
)
from .promote import (
    _promote_auto,
    _promote_specific,
    cmd_promote,
)
from .commands import (
    cmd_export,
    cmd_import,
    cmd_projects,
    cmd_prune,
    cmd_status,
)
from .entry import main

__all__ = [
    "ALLOWED_INSTINCT_EXTENSIONS",
    "DEEPBLUE_DIR",
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
    "load_all_instincts",
    "load_project_only_instincts",
    "load_registry",
    "main",
    "parse_instinct_file",
]


if __name__ == "__main__":
    import sys

    sys.exit(main())
