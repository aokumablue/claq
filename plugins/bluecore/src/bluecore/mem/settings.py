"""設定管理 — ~/.bluecore/settings.json を読み書きする（全プラグイン共通）

設定のうち ``sync.enabled`` と ``sync.postgres_url`` のみ settings.json に永続化する。
他の閾値類（log_level, chunk_max_length, embedding_model 等）はハードコードされた
デフォルト値を使用し、ユーザは設定できない。

ランタイム状態（``last_synced_at`` / ``last_sync_attempt_at`` / ``last_sync_success``
および ``last_compacted_at``）は別ファイル ``~/.bluecore/sync_state.json`` で管理する。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urlparse, urlunparse

from bluecore.lib.constants import BASE_DIR_NAME

_DEFAULT_DATA_DIR = Path(os.environ["BLUECORE_DATA_PATH"]) if "BLUECORE_DATA_PATH" in os.environ else Path.home() / BASE_DIR_NAME
_DEFAULT_EMBEDDING_MODEL = "cl-nagoya/ruri-v3-310m"
# HF Hub commit SHA をピン留めし、サプライチェーン攻撃（名前空間再利用・改竄プッシュ）を防ぐ
_DEFAULT_EMBEDDING_REVISION = "18b60fb8c2b9df296fb4212bb7d23ef94e579cd3"
_SYNC_STATE_FILENAME = "sync_state.json"


def pgpass_path() -> Path:
    """pgpass ファイルの絶対パス（<data_dir>/.pgpass）を返す。

    データディレクトリは settings.json と同じ ``_DEFAULT_DATA_DIR``
    （BLUECORE_DATA_PATH または ~/.bluecore）を基準とする。
    """
    return _DEFAULT_DATA_DIR / ".pgpass"


def _write_pgpass_entry(host: str, port: int | str, db: str, user: str, password: str) -> None:
    """<data_dir>/.pgpass に host:port:db:user:password エントリを追加する（重複は追加しない）。

    O_NOFOLLOW + 0o600 でシンボリックリンク経由の差し替えを防ぐ（CWE-367/276 対策）。
    """
    pgpass = pgpass_path()
    pgpass.parent.mkdir(parents=True, exist_ok=True)
    entry = f"{host}:{port}:{db}:{user}:{password}\n"
    prefix = f"{host}:{port}:{db}:{user}:"

    if pgpass.exists():
        if pgpass.stat().st_mode & 0o777 != 0o600:
            import logging
            logging.getLogger("SETTINGS").warning(".pgpass のパーミッションを 0o600 に修正します: %s", pgpass)
            pgpass.chmod(0o600)
        existing_text = pgpass.read_text(encoding="utf-8")
        if any(line.startswith(prefix) for line in existing_text.splitlines()):
            return
        fd = os.open(str(pgpass), os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    else:
        fd = os.open(str(pgpass), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(entry)


def _strip_password_to_pgpass(url: str) -> str:
    """URL にパスワードが含まれていれば <data_dir>/.pgpass に分離し、パスワードを除いた URL を返す。

    settings.json に平文パスワードが残らないようにするためのフェイルセーフ。
    パスワードが含まれない URL はそのまま返す。
    """
    parsed = urlparse(url)
    if not parsed.password:
        return url
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    db = (parsed.path or "/").lstrip("/") or "*"
    user = unquote(parsed.username) if parsed.username else "*"
    password = unquote(parsed.password)
    _write_pgpass_entry(host, port, db, user, password)

    # パスワードを除いた netloc に再構築
    userinfo = quote(parsed.username, safe="") if parsed.username else ""
    host_part = host
    if parsed.port:
        host_part = f"{host_part}:{parsed.port}"
    new_netloc = f"{userinfo}@{host_part}" if userinfo else host_part
    return urlunparse(parsed._replace(netloc=new_netloc))


@dataclass
class ReduxSettings:
    """redux コマンド別トークン圧縮の設定（すべてハードコード既定値）"""

    enabled: bool = True
    smart_filter_enabled: bool = True
    group_lint_enabled: bool = True
    dedup_enabled: bool = True
    smart_truncate_enabled: bool = True
    max_output_len: int = 3000
    head_lines: int = 30
    tail_lines: int = 30
    dedup_threshold: int = 3


@dataclass
class SyncSettings:
    """PostgreSQL 同期設定

    ``enabled`` と ``postgres_url`` のみ settings.json に永続化される。
    その他のフィールドはハードコード定数またはランタイム状態（sync_state.json）として扱う。
    """

    enabled: bool = False
    postgres_url: str = ""
    # --- 以下はランタイム状態（sync_state.json で管理、settings.json に書かない） ---
    interval_hours: int = 3
    last_synced_at: float = 0.0
    last_sync_attempt_at: float = 0.0
    last_sync_success: bool = False


@dataclass
class TeamSettings:
    """team（PostgreSQL チーム共有メモリ検索）設定。

    すべてハードコード既定値。``sync.enabled`` が True で ``postgres_url`` が設定済みの
    場合のみ実質的に有効化される。``exclude_self=True`` は常時 True 前提だが、
    将来的な切り替え余地を残すため設定項目化している。
    """

    enabled: bool = True
    max_tokens: int = 500
    chunk_limit: int = 3
    exclude_self: bool = True


@dataclass
class Settings:
    """mem のランタイム設定

    閾値類はすべてハードコード。settings.json で上書きできるのは ``sync`` セクションの
    ``enabled`` と ``postgres_url`` のみ。
    """

    log_level: str = "info"
    excluded_projects: list[str] = field(default_factory=list)
    embedding_model: str = _DEFAULT_EMBEDDING_MODEL
    search_half_life_days: float = 30.0
    chunk_max_length: int = 2000
    context_chunk_count: int = 30
    context_max_tokens: int = 1200
    # ティアード・メモリ設定（hot=400, warm=400, archive=残り 400）
    context_hot_tokens: int = 400
    context_warm_tokens: int = 400
    context_hot_hours: int = 24
    context_warm_days: int = 7
    auto_compact_enabled: bool = True
    auto_compact_interval_days: int = 7
    last_compacted_at: float = 0.0  # ランタイム状態
    sync: SyncSettings = field(default_factory=SyncSettings)
    redux: ReduxSettings = field(default_factory=ReduxSettings)
    team: TeamSettings = field(default_factory=TeamSettings)

    # --- 導出プロパティ ---

    @property
    def data_path(self) -> Path:
        """データディレクトリ（~/.bluecore）を返す。"""
        return _DEFAULT_DATA_DIR

    @property
    def settings_path(self) -> Path:
        """settings.json の絶対パスを返す。"""
        return self.data_path / "settings.json"

    @property
    def sync_state_path(self) -> Path:
        """sync_state.json の絶対パスを返す。"""
        return self.data_path / _SYNC_STATE_FILENAME

    @property
    def sync_lock_path(self) -> Path:
        """sync.lock の絶対パスを返す。"""
        return self.data_path / "sync.lock"

    @property
    def db_path(self) -> Path:
        """mem.db の絶対パスを返す。"""
        return self.data_path / "mem.db"

    @property
    def log_dir(self) -> Path:
        """ログディレクトリの絶対パスを返す。"""
        return self.data_path / "logs"

    # --- 永続化 ---

    def save(self) -> None:
        """``sync.enabled`` と ``sync.postgres_url`` のみを settings.json に書き込む。

        他プラグインのセクションは保持する。その他のフィールドは settings.json に書き出さない
        （ハードコード済み）。ランタイム状態は :meth:`save_sync_state` で別ファイルに保存する。
        """
        self.data_path.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if self.settings_path.exists():
            try:
                existing = json.loads(self.settings_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        url_to_save = self.sync.postgres_url
        if url_to_save:
            url_to_save = _strip_password_to_pgpass(url_to_save)
        existing["mem"] = {
            "sync": {
                "enabled": self.sync.enabled,
                "postgres_url": url_to_save,
            },
        }
        self.settings_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        self.settings_path.chmod(0o600)

    def save_sync_state(self) -> None:
        """ランタイム状態（last_synced_at 等）を sync_state.json に書き出す。

        永続化ファイルに設定値を混ぜないため、ユーザ設定とは別ファイルで管理する。
        """
        self.data_path.mkdir(parents=True, exist_ok=True)
        state = {
            "last_synced_at": self.sync.last_synced_at,
            "last_sync_attempt_at": self.sync.last_sync_attempt_at,
            "last_sync_success": self.sync.last_sync_success,
            "last_compacted_at": self.last_compacted_at,
        }
        tmp_path = self.sync_state_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp_path.replace(self.sync_state_path)
        self.sync_state_path.chmod(0o600)

    @classmethod
    def _load_default(cls, settings_path: Path | None) -> Settings:
        """settings.json が存在しない・読み込み失敗時のデフォルト Settings を返す。"""
        settings = cls()
        if settings_path is None:
            settings.save()
            settings._load_sync_state()
        return settings

    @classmethod
    def _migrate_postgres_url(cls, raw: dict, path: Path, postgres_url: str) -> str:
        """URL にパスワードがあれば ~/.pgpass に分離して settings.json を書き戻し、新 URL を返す。"""
        stripped = _strip_password_to_pgpass(postgres_url)
        if stripped == postgres_url:
            return postgres_url
        import logging as _logging
        _logging.getLogger("SETTINGS").info("postgres_url からパスワードを ~/.pgpass に自動移行しました")
        raw.setdefault("mem", {}).setdefault("sync", {})["postgres_url"] = stripped
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        tmp_path.replace(path)
        path.chmod(0o600)
        return stripped

    @classmethod
    def _load_sync_state_for_path(cls, settings: Settings, settings_path: Path | None, path: Path) -> None:
        """settings_path の有無に応じて sync_state を読み込む。"""
        if settings_path is None:
            settings._load_sync_state()
        else:
            state_path = path.parent / _SYNC_STATE_FILENAME
            if state_path.exists():
                settings._load_sync_state_from(state_path)

    @classmethod
    def load(cls, settings_path: Path | None = None) -> Settings:
        """設定ファイルを読み込む。

        settings.json からは ``mem.sync.enabled`` / ``mem.sync.postgres_url`` のみ読む。
        ランタイム状態は ``sync_state.json`` から復元する。存在しなければデフォルトで作成して返す。

        Args:
            settings_path: テスト用に settings.json パスを直接指定する場合に使う。
                指定された場合、既定データディレクトリの sync_state.json は読み込まない。

        Returns:
            構築された Settings インスタンス。
        """
        path = settings_path or (_DEFAULT_DATA_DIR / "settings.json")
        if not path.exists():
            return cls._load_default(settings_path)

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls._load_default(settings_path)

        mem_raw = raw.get("mem", {})
        sync_raw = mem_raw.get("sync", {}) if isinstance(mem_raw, dict) else {}
        postgres_url = str(sync_raw.get("postgres_url", "") or "")
        if postgres_url:
            postgres_url = cls._migrate_postgres_url(raw, path, postgres_url)

        sync_settings = SyncSettings(
            enabled=bool(sync_raw.get("enabled", False)),
            postgres_url=postgres_url,
        )
        settings = cls(sync=sync_settings)
        cls._load_sync_state_for_path(settings, settings_path, path)
        return settings

    # --- 内部ヘルパ ---

    def _load_sync_state(self) -> None:
        """既定データディレクトリの sync_state.json をベストエフォートで読み込む。"""
        if self.sync_state_path.exists():
            self._load_sync_state_from(self.sync_state_path)

    def reload_sync_state(self) -> None:
        """sync_state.json を再読込する。"""
        self._load_sync_state()

    def _load_sync_state_from(self, path: Path) -> None:
        """指定パスの sync_state.json を読み込んで self に反映する。"""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(raw, dict):
            return
        self.sync.last_synced_at = float(raw.get("last_synced_at", 0.0) or 0.0)
        self.sync.last_sync_attempt_at = float(raw.get("last_sync_attempt_at", 0.0) or 0.0)
        self.sync.last_sync_success = bool(raw.get("last_sync_success", False))
        self.last_compacted_at = float(raw.get("last_compacted_at", 0.0) or 0.0)
