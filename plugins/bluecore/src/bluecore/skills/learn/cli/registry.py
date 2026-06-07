"""プロジェクト検出とレジストリ管理。

``detect_project`` は ``cli`` 名前空間側で ``monkeypatch`` 差し替えされる
ため、各サブコマンドはこれを ``_pkg.detect_project`` 経由で呼び出す。
"""

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import bluecore.skills.learn.cli as _pkg

from .paths import _preferred_projects_dir, _preferred_registry_file

# ─────────────────────────────────────────────
# プロジェクト検出（共通 Python 実装）
# ─────────────────────────────────────────────


def _resolve_project_root() -> str:
    """CLAUDE_PROJECT_DIR 環境変数または git から現在のプロジェクトルートを解決する。"""
    env_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_dir and os.path.isdir(env_dir):
        return env_dir.rstrip("/")

    try:
        result = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return result.stdout.strip().rstrip("/")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return str(Path.cwd().resolve())


def _resolve_remote_url(project_root: str) -> str:
    """git リモート URL を取得する。取得できない場合は空文字を返す。"""
    try:
        result = subprocess.run(
            ["git", "-C", project_root, "remote", "get-url", "origin"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return ""


def _ensure_project_dirs(project_dir: Path) -> None:
    """プロジェクトディレクトリ構造を保証する。"""
    for d in [
        project_dir / "instincts" / "personal",
        project_dir / "instincts" / "inherited",
        project_dir / "observations.archive",
        project_dir / "evolved" / "skills",
        project_dir / "evolved" / "commands",
        project_dir / "evolved" / "agents",
    ]:
        d.mkdir(parents=True, exist_ok=True)


def detect_project() -> dict:
    """現在のプロジェクトコンテキストを検出する。id/name/root/project_dir を含む辞書を返す。"""
    project_root = _resolve_project_root()
    project_name = os.path.basename(project_root)

    remote_url = _resolve_remote_url(project_root)
    hash_source = remote_url if remote_url else project_root
    project_id = hashlib.sha256(hash_source.encode()).hexdigest()[:12]

    project_dir = _preferred_projects_dir() / project_id
    _ensure_project_dirs(project_dir)
    _update_registry(project_id, project_name, project_root, remote_url)

    return {
        "id": project_id,
        "name": project_name,
        "root": project_root,
        "remote": remote_url,
        "project_dir": project_dir,
        "instincts_personal": project_dir / "instincts" / "personal",
        "instincts_inherited": project_dir / "instincts" / "inherited",
        "evolved_dir": project_dir / "evolved",
        "observations_file": project_dir / "observations.jsonl",
    }


def _update_registry(pid: str, pname: str, proot: str, premote: str) -> None:
    """projects.json レジストリを更新する。

    利用可能な環境ではファイルロックを使い、同時実行セッション同士の
    更新上書きを防ぐ。
    """
    registry_file = _preferred_registry_file()
    registry_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = registry_file.parent / f".{registry_file.name}.lock"
    lock_fd = None

    try:
        # アドバイザリロックを取得して読み取り・更新・書き込みを直列化
        if _pkg._HAS_FCNTL:
            lock_fd = open(lock_path, "w")
            _pkg.fcntl.flock(lock_fd, _pkg.fcntl.LOCK_EX)

        try:
            with open(registry_file, encoding="utf-8") as f:
                registry = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            registry = {}

        registry[pid] = {
            "name": pname,
            "root": proot,
            "remote": premote,
            "last_seen": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }

        tmp_file = registry_file.parent / f".{registry_file.name}.tmp.{os.getpid()}"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(registry, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, registry_file)
    finally:
        if lock_fd is not None:
            _pkg.fcntl.flock(lock_fd, _pkg.fcntl.LOCK_UN)
            lock_fd.close()


def load_registry() -> dict:
    """プロジェクトレジストリを読み込む。

    ``open`` はパッケージ名前空間（``_pkg.open``）経由で参照する。``cli`` パッケージは
    組込み ``open`` を ``open`` 属性として公開しているため、テストの
    ``monkeypatch.setattr(cli, "open", ...)`` がそのままこの呼び出しに反映される。
    """
    registry: dict = {}
    try:
        with _pkg.open(_pkg.REGISTRY_FILE, encoding="utf-8") as f:
            registry.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return registry
