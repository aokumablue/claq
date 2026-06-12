"""埋め込みモデルダウンロード — model.json に従い配布ファイルを取得する。

install.sh から `python -m bluecore.model_download` で呼び出す。
stdlib のみ使用するため venv 構築前でも動作する。

model.json の files に列挙された各ファイルを SHA-256 検証付きでダウンロードする。
URL は Hugging Face でも社内サーバ（IP/HTTP）でも同じ仕組みで動作する。

exit code:
  0  ダウンロード成功（または既にビルド済みモデルが存在）
  3  download が無効または設定ファイルが存在しない
  1  エラー
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import ssl
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# ビルド完了の判定マーカー（model_build build の最終成果物）
_BUILT_MARKER = "embeddings.npy"

_DEFAULT_MAX_DOWNLOAD_BYTES: int = 256 * 1024 * 1024  # 256 MB
_CHUNK_SIZE: int = 1024 * 1024  # 1 MB
# ダウンロード全体のウォールクロック上限。urlopen の timeout は socket 単位の
# 無通信検出のみで、低速送信を続けるサーバーには全体時間の上限が効かない。
_MAX_DOWNLOAD_SECONDS: int = 1800  # 30 分

# detached 実行の警告はログファイルにしか届かないため、マーカーへ書き残して
# 次回 SessionStart で session_install がユーザーへ 1 回通知する
_WARNING_MARKER = Path.home() / ".bluecore" / "model_download_warning"
_MAX_MARKER_BYTES: int = 16 * 1024


def _warn(message: str) -> None:
    """セキュリティ警告を stdout と通知マーカーの両方へ記録する。

    Args:
        message: 警告メッセージ（WARNING プレフィックスなし）。

    Raises:
        例外は発生しません（マーカー書き込みは best-effort）。
    """
    print(f"[download] WARNING: {message}")
    try:
        _WARNING_MARKER.parent.mkdir(parents=True, exist_ok=True)
        if _WARNING_MARKER.exists() and _WARNING_MARKER.stat().st_size > _MAX_MARKER_BYTES:
            return
        with _WARNING_MARKER.open("a", encoding="utf-8") as fh:
            fh.write(message + "\n")
    except OSError:
        pass  # 通知は付随機能。ダウンロード本体を止めない


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """リダイレクト先 URL を再検証するカスタムハンドラー。

    allow_http が False（初回 URL が https）のとき、リダイレクトによる
    平文 HTTP へのダウングレードを拒否する。http の利用は model.json で
    明示的に http URL を設定した場合のオプトインに限定する。
    """

    def __init__(self, *, allow_http: bool = False) -> None:
        """ハンドラーを初期化する。

        Args:
            allow_http: リダイレクト先に平文 HTTP を許可するか。
        """
        super().__init__()
        self._allow_http = allow_http

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]  # noqa: PLR0913
        """リダイレクト URL を _validate_url で再検証してから親クラスに委譲する。"""
        _validate_url(newurl, allow_http=self._allow_http)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_url(url: str, *, allow_http: bool = True) -> None:
    """URL の形式を最低限検証する。

    scheme が http/https のいずれかで hostname を持つことのみ必須とする。
    HTTP（平文）と IP アドレス指定は許可するが、安全性が低いため警告を出す。
    ホスト制限は行わない。

    Args:
        url: 検証対象の URL。
        allow_http: 平文 HTTP を許可するか。False の場合（https 開始の
            ダウンロードのリダイレクト先検証）は http を拒否する。
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ValueError(f"URL must use HTTPS or HTTP scheme: {url!r}")

    host = parsed.hostname or ""
    if not host:
        raise ValueError(f"URL has no valid hostname: {url!r}")

    if parsed.scheme == "http":
        if not allow_http:
            raise ValueError(f"Refusing redirect downgrade from HTTPS to plaintext HTTP: {url!r}")
        _warn(f"平文 HTTP で取得します（中間者攻撃のリスク）: {url!r}")

    try:
        ipaddress.ip_address(host)
        _warn(f"IP アドレス指定の URL です（証明書検証が機能しません）: {host!r}")
    except ValueError:
        pass


def _require_bool(download: dict, key: str) -> bool:
    """download 設定から JSON boolean を厳格に読み取る。

    文字列 "false" 等は truthy のため `bool()` で読むと意図と逆の値になる
    （特に ssl_no_verify では検証の意図しない無効化につながる）。
    bool 型以外は fail-closed で拒否する。

    Args:
        download: model.json の download セクション。
        key: 読み取るキー。

    Returns:
        キーの bool 値。未設定は False。

    Raises:
        ValueError: 値が JSON boolean でない場合。
    """
    value = download.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a JSON boolean (true/false), got {value!r}")
    return value


@dataclass(frozen=True)
class _FileSpec:
    """ダウンロード対象 1 ファイルの設定。"""

    name: str
    url: str
    sha256: str


@dataclass(frozen=True)
class _DownloadSettings:
    """model.json の download セクション全体。"""

    enabled: bool
    files: tuple[_FileSpec, ...]
    max_download_bytes: int
    ssl_no_verify: bool


def _parse_file_spec(entry: dict) -> _FileSpec:
    """files 配列の 1 エントリを検証して _FileSpec に変換する。

    name はベース名のみ許可し、パス区切りを含む値（パストラバーサル）を拒否する。

    Raises:
        ValueError: name / url / sha256 のいずれかが欠落・不正な場合。
    """
    name = str(entry.get("name", "") or "")
    url = str(entry.get("url", "") or "")
    sha256 = str(entry.get("sha256", "") or "")
    if not name or not url or not sha256:
        raise ValueError(f"files entry requires name/url/sha256: {entry!r}")
    if Path(name).name != name or name in (".", ".."):
        raise ValueError(f"files entry name must be a bare filename: {name!r}")
    return _FileSpec(name=name, url=url, sha256=sha256)


def _load_download_settings(config_path: Path) -> _DownloadSettings:
    """model.json から download 設定を読み込む。

    Returns:
        _DownloadSettings。設定ファイルが存在しない場合は enabled=False。

    Raises:
        ValueError: enabled / ssl_no_verify が JSON boolean でない場合、
            または files エントリが不正な場合。
    """
    if not config_path.is_file():
        return _DownloadSettings(enabled=False, files=(), max_download_bytes=_DEFAULT_MAX_DOWNLOAD_BYTES, ssl_no_verify=False)

    data = json.loads(config_path.read_text(encoding="utf-8"))
    download = data.get("model", {}).get("download", {})
    enabled = _require_bool(download, "enabled")
    files = tuple(_parse_file_spec(entry) for entry in download.get("files", []))
    max_download_bytes = int(download.get("max_download_bytes", _DEFAULT_MAX_DOWNLOAD_BYTES))
    ssl_no_verify = _require_bool(download, "ssl_no_verify")
    return _DownloadSettings(enabled=enabled, files=files, max_download_bytes=max_download_bytes, ssl_no_verify=ssl_no_verify)


def _download_file(
    url: str,
    dest_path: Path,
    max_bytes: int,
    *,
    ssl_no_verify: bool = False,
) -> None:
    """URL からファイルをダウンロードする。リダイレクト先も再検証する。

    初回 URL が https の場合、リダイレクト先での平文 HTTP への
    ダウングレードは拒否する（http は明示設定時のみ許可）。
    """
    allow_http = urllib.parse.urlparse(url).scheme == "http"
    handlers: list[urllib.request.BaseHandler] = [_ValidatingRedirectHandler(allow_http=allow_http)]
    if ssl_no_verify:
        _warn("SSL 証明書検証を無効化しています（中間者攻撃のリスク）")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"User-Agent": "bluecore-install/1.0"})
    downloaded = 0
    deadline = time.monotonic() + _MAX_DOWNLOAD_SECONDS
    with opener.open(request, timeout=600) as response, dest_path.open("wb") as out:
        while chunk := response.read(_CHUNK_SIZE):
            if time.monotonic() > deadline:
                raise ValueError(f"Download exceeded time limit of {_MAX_DOWNLOAD_SECONDS} seconds")
            downloaded += len(chunk)
            if downloaded > max_bytes:
                raise ValueError(f"Download size exceeded limit of {max_bytes} bytes")
            out.write(chunk)


def _sha256_of(path: Path) -> str:
    """ファイルの SHA-256 ハッシュをストリーミング計算して返す。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_file_sha256(path: Path, expected_sha256: str, name: str) -> None:
    """ダウンロード済みファイルの SHA-256 を配置前に検証する。"""
    actual = _sha256_of(path)
    if actual != expected_sha256:
        raise ValueError(f"SHA-256 mismatch for {name!r}: expected {expected_sha256!r}, got {actual!r}")


def download_model_files(config_path: Path, output_dir: Path) -> int:
    """model.json に従って配布ファイルを取得し、出力先へ配置する。

    全ファイルを一時ディレクトリにダウンロード・SHA-256 検証してから
    os.replace で一括配置する（部分ダウンロード状態の恒久化を防ぐ）。
    既に SHA-256 が一致するファイルは再ダウンロードしない。

    Returns:
        0: download 成功または既にビルド済みモデルが存在する
        3: download が無効、または設定ファイルが存在しない

    Raises:
        ValueError: 設定不備や SHA-256 不一致の場合
        OSError / urllib.error.URLError: ダウンロードやファイル操作の失敗
    """
    if (output_dir / _BUILT_MARKER).exists():
        print(f"[download] built model already present (skipping): {output_dir / _BUILT_MARKER}")
        return 0

    settings = _load_download_settings(config_path)
    if not settings.enabled:
        return 3
    if not settings.files:
        raise ValueError(f"files is empty in {config_path}")

    for spec in settings.files:
        _validate_url(spec.url)

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bluecore_model_", dir=str(output_dir.parent)) as temp_root:
        temp_root_path = Path(temp_root)
        pending: list[tuple[Path, Path]] = []
        for spec in settings.files:
            dest = output_dir / spec.name
            if dest.exists() and _sha256_of(dest) == spec.sha256:
                print(f"[download] already present (skipping): {dest}")
                continue
            temp_path = temp_root_path / spec.name
            print(f"[download] Fetching model file: {spec.url}")
            _download_file(spec.url, temp_path, settings.max_download_bytes, ssl_no_verify=settings.ssl_no_verify)
            _verify_file_sha256(temp_path, spec.sha256, spec.name)
            pending.append((temp_path, dest))

        # temp_root は output_dir.parent 配下にあり同一ファイルシステムなので
        # os.replace はファイル単位でアトミックに働く。検証完了後に一括配置する。
        for temp_path, dest in pending:
            os.replace(temp_path, dest)

    print(f"[download] Installed model files into: {output_dir}")
    return 0


def _parse_args() -> argparse.Namespace:
    """CLI 引数を解析する。"""
    parser = argparse.ArgumentParser(
        prog="python -m bluecore.model_download",
        description="埋め込みモデルファイルを model.json に従いダウンロードする",
    )
    parser.add_argument("--config", type=Path, required=True, help="download 設定の JSON")
    parser.add_argument("--out", type=Path, required=True, help="出力ディレクトリ")
    return parser.parse_args()


def main() -> None:
    """CLI エントリポイント。"""
    args = _parse_args()
    try:
        rc = download_model_files(args.config, args.out)
        sys.exit(rc)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
