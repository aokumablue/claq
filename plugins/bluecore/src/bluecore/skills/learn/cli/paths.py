"""learn CLI のパス・URL 検証および設定定数。

モジュール定数（``BLUECORE_DIR`` や ``GLOBAL_*`` など）の正本をここに置く。
``cli`` パッケージ ``__init__`` がこれらを再エクスポートし、テストは
``cli`` 名前空間側の属性を ``monkeypatch`` で差し替える。差し替えを各関数へ
反映させるため、これらの定数を参照する関数は ``cli`` パッケージ
(``_pkg``) 経由で実行時に参照する。
"""

import ipaddress
import re
import socket
import urllib.parse
import urllib.request
from pathlib import Path

import bluecore.skills.learn.cli as _pkg
from bluecore.lib.core_utils import (
    get_bluecore_dir,
    get_projects_dir,
    get_registry_file,
)

# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────

BLUECORE_DIR = get_bluecore_dir()
PROJECTS_DIR = get_projects_dir()
REGISTRY_FILE = get_registry_file()

# グローバル（プロジェクト非依存）パス
GLOBAL_INSTINCTS_DIR = BLUECORE_DIR / "instincts"
GLOBAL_PERSONAL_DIR = GLOBAL_INSTINCTS_DIR / "personal"
GLOBAL_INHERITED_DIR = GLOBAL_INSTINCTS_DIR / "inherited"
GLOBAL_EVOLVED_DIR = BLUECORE_DIR / "evolved"
GLOBAL_OBSERVATIONS_FILE = BLUECORE_DIR / "observations.jsonl"

# 自動昇格のしきい値
PROMOTE_CONFIDENCE_THRESHOLD = 0.8
PROMOTE_MIN_PROJECTS = 2
ALLOWED_INSTINCT_EXTENSIONS = (".yaml", ".yml", ".md")

# 保留中 instinct の既定 TTL（日）
PENDING_TTL_DAYS = 30
# 警告しきい値: この日数以内に期限切れになる instinct に警告を表示
PENDING_EXPIRY_WARNING_DAYS = 7


# グローバルディレクトリの存在を保証（インポート時の副作用を避けるため遅延実行）
def _ensure_global_dirs():
    """グローバル instinct 用ディレクトリ群を作成する。"""
    for d in [
        _pkg.GLOBAL_PERSONAL_DIR,
        _pkg.GLOBAL_INHERITED_DIR,
        _pkg.GLOBAL_EVOLVED_DIR / "skills",
        _pkg.GLOBAL_EVOLVED_DIR / "commands",
        _pkg.GLOBAL_EVOLVED_DIR / "agents",
    ]:
        d.mkdir(parents=True, exist_ok=True)


def _preferred_projects_dir() -> Path:
    """project instinct の保存先ディレクトリを返す。"""
    return _pkg.PROJECTS_DIR


def _preferred_registry_file() -> Path:
    """project レジストリファイルのパスを返す。"""
    return _pkg.REGISTRY_FILE


def _project_dir_for_id(project_id: str) -> Path:
    """project_id に対応する保存先ディレクトリを返す。"""
    return _pkg.PROJECTS_DIR / project_id


def _all_project_dirs() -> list[Path]:
    """既知の project ディレクトリを重複なく列挙する。"""
    dirs: list[Path] = []
    if not _pkg.PROJECTS_DIR.is_dir():
        return dirs

    for project_dir in sorted(_pkg.PROJECTS_DIR.iterdir()):
        if project_dir.is_dir():
            dirs.append(project_dir)

    return dirs


def _project_dir_score(project_dir: Path) -> int:
    """既存データ量の多い project_dir を優先するためのスコアを返す。"""
    if not project_dir.exists():
        return -1

    score = 0
    if (project_dir / "project.json").exists():
        score += 3
    if (project_dir / "observations.jsonl").exists():
        score += 2

    for relative in [
        "instincts/personal",
        "instincts/inherited",
        "evolved/skills",
        "evolved/commands",
        "evolved/agents",
    ]:
        directory = project_dir / relative
        if directory.is_dir() and any(directory.iterdir()):
            score += 1

    return score


# ─────────────────────────────────────────────
# パス検証
# ─────────────────────────────────────────────


def _validate_file_path(path_str: str, must_exist: bool = False) -> Path:
    """パストラバーサルを防ぎつつ、ファイルパスを検証して解決する。

    パスが不正または疑わしい場合は ValueError を送出する。
    """
    path = Path(path_str).expanduser().resolve()

    # システムディレクトリへ抜けるパスを遮断
    # 特定のシステムパスは遮断しつつ、一時ディレクトリ（macOS の /var/folders）は許可
    blocked_prefixes = [
        "/etc",
        "/usr",
        "/bin",
        "/sbin",
        "/proc",
        "/sys",
        "/var/log",
        "/var/run",
        "/var/lib",
        "/var/spool",
        # macOS では /etc が /private/etc に解決される
        "/private/etc",
        "/private/var/log",
        "/private/var/run",
        "/private/var/db",
    ]
    path_s = str(path)
    for prefix in blocked_prefixes:
        if path_s.startswith(prefix + "/") or path_s == prefix:
            raise ValueError(f"Path '{path}' targets a system directory")

    if must_exist and not path.exists():
        raise ValueError(f"Path does not exist: {path}")

    return path


def _assert_safe_url(url: str) -> None:
    """SSRF を防ぐため、URL のホストが公開アドレスに解決されることを検証する。

    スキームを http/https に限定し、ホストが解決される全 IP の中に
    プライベート / ループバック / リンクローカル / 予約済み / マルチキャスト /
    未指定アドレスが含まれる場合は ValueError を送出する。クラウドの
    メタデータエンドポイント（169.254.169.254 等）やローカルサービスへの
    到達を遮断する。
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"URL has no host: {url!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addrinfos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"Cannot resolve host {host!r}: {e}") from e
    for *_, sockaddr in addrinfos:
        ip = ipaddress.ip_address(sockaddr[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(f"Refusing to fetch from non-public address {ip} for host {host!r}")


class _SsrfSafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """リダイレクト先 URL を _assert_safe_url で再検証してから追従する。

    リダイレクトを使って公開ホストから内部アドレスへ誘導する SSRF
    バイパスを防ぐ。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: PLR0913
        """リダイレクト先を検証してから親クラスの処理に委譲する。"""
        _assert_safe_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_url(url: str) -> str:
    """SSRF 検証を行いつつ URL から UTF-8 テキストを取得する。"""
    _assert_safe_url(url)
    opener = urllib.request.build_opener(_SsrfSafeRedirectHandler())
    with opener.open(url) as response:
        return response.read().decode("utf-8")


def _validate_instinct_id(instinct_id: str) -> bool:
    """ファイル名に使う前に instinct ID を検証する。"""
    if not instinct_id or len(instinct_id) > 128:
        return False
    if "/" in instinct_id or "\\" in instinct_id:
        return False
    if ".." in instinct_id:
        return False
    if instinct_id.startswith("."):
        return False
    return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", instinct_id))


def _yaml_quote(value: str) -> str:
    """YAML フロントマターへ安全に直列化するため、文字列をクォートする。

    二重引用符を使い、値に引用符が含まれる場合はエスケープして
    YAML が壊れるのを防ぐ。
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
