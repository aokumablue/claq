"""ONNX モデルダウンロード — onnx.json に従い配布アーカイブを取得する。

install.sh から `python -m bluecore.onnx_download` で呼び出す。
stdlib のみ使用するため venv 構築前でも動作する。

exit code:
  0  ダウンロード成功（または既に model.onnx が存在）
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
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

_REQUIRED_FILES = ("model.onnx", "tokenizer.json", "config.json", "manifest.json")

_DEFAULT_MAX_DOWNLOAD_BYTES: int = 2 * 1024 * 1024 * 1024  # 2 GB
_DEFAULT_MAX_EXTRACT_BYTES: int = 500 * 1024 * 1024  # 500 MB per file
_CHUNK_SIZE: int = 1024 * 1024  # 1 MB
# ダウンロード全体のウォールクロック上限。urlopen の timeout は socket 単位の
# 無通信検出のみで、低速送信を続けるサーバーには全体時間の上限が効かない。
# 終わらないダウンロードは flock を占有し以後のリトライを恒久停止させるため、
# ここで打ち切って次回セッションのリトライに委ねる。
_MAX_DOWNLOAD_SECONDS: int = 1800  # 30 分

# detached 実行の警告はログファイルにしか届かないため、マーカーへ書き残して
# 次回 SessionStart で session_install がユーザーへ 1 回通知する
_WARNING_MARKER = Path.home() / ".bluecore" / "onnx_download_warning"
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
    平文 HTTP へのダウングレードを拒否する。http の利用は onnx.json で
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
        download: onnx.json の download セクション。
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


def _load_download_settings(config_path: Path) -> tuple[bool, str, str, int, int, bool]:
    """onnx.json から download 設定を読み込む。

    Returns:
        (enabled, model_url, expected_sha256, max_download_bytes, max_extract_bytes, ssl_no_verify)

    Raises:
        ValueError: enabled / ssl_no_verify が JSON boolean でない場合。
    """
    if not config_path.is_file():
        return False, "", "", _DEFAULT_MAX_DOWNLOAD_BYTES, _DEFAULT_MAX_EXTRACT_BYTES, False

    data = json.loads(config_path.read_text(encoding="utf-8"))
    download = data.get("onnx", {}).get("download", {})
    enabled = _require_bool(download, "enabled")
    model_url = str(download.get("model_url", "") or "")
    expected_sha256 = str(download.get("sha256", "") or "")
    max_download_bytes = int(download.get("max_download_bytes", _DEFAULT_MAX_DOWNLOAD_BYTES))
    max_extract_bytes = int(download.get("max_extract_bytes", _DEFAULT_MAX_EXTRACT_BYTES))
    ssl_no_verify = _require_bool(download, "ssl_no_verify")
    return enabled, model_url, expected_sha256, max_download_bytes, max_extract_bytes, ssl_no_verify


def _download_archive(
    model_url: str,
    archive_path: Path,
    max_bytes: int,
    *,
    ssl_no_verify: bool = False,
) -> None:
    """URL からアーカイブをダウンロードする。リダイレクト先も再検証する。

    初回 URL が https の場合、リダイレクト先での平文 HTTP への
    ダウングレードは拒否する（http は明示設定時のみ許可）。
    """
    allow_http = urllib.parse.urlparse(model_url).scheme == "http"
    handlers: list[urllib.request.BaseHandler] = [_ValidatingRedirectHandler(allow_http=allow_http)]
    if ssl_no_verify:
        _warn("SSL 証明書検証を無効化しています（中間者攻撃のリスク）")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(model_url, headers={"User-Agent": "bluecore-install/1.0"})
    downloaded = 0
    deadline = time.monotonic() + _MAX_DOWNLOAD_SECONDS
    with opener.open(request, timeout=600) as response, archive_path.open("wb") as out:
        while chunk := response.read(_CHUNK_SIZE):
            if time.monotonic() > deadline:
                raise ValueError(f"Download exceeded time limit of {_MAX_DOWNLOAD_SECONDS} seconds")
            downloaded += len(chunk)
            if downloaded > max_bytes:
                raise ValueError(f"Download size exceeded limit of {max_bytes} bytes")
            out.write(chunk)


def _verify_archive_sha256(archive_path: Path, expected_sha256: str) -> None:
    """ダウンロード済みアーカイブの SHA-256 を展開前に検証する。"""
    h = hashlib.sha256()
    with archive_path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK_SIZE), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"Archive SHA-256 mismatch: expected {expected_sha256!r}, got {actual!r}")


def _copy_with_size_limit(src: object, out: object, max_bytes: int, name: str) -> None:
    """src から out へコピーしながら抽出サイズ上限を強制する。"""
    written = 0
    while chunk := src.read(_CHUNK_SIZE):  # type: ignore[union-attr]
        written += len(chunk)
        if written > max_bytes:
            raise ValueError(f"Extracted file {name!r} exceeds size limit of {max_bytes} bytes")
        out.write(chunk)  # type: ignore[union-attr]


def _require_unique_members(found: dict[str, list], names: tuple[str, ...]) -> None:
    """必須ファイルがアーカイブ内にちょうど 1 つずつ存在することを検証する。

    Args:
        found: basename → 一致した member リストの辞書。
        names: 必須ファイル名のタプル。

    Raises:
        ValueError: 必須ファイルが 0 件または複数件の場合。
    """
    for name in names:
        count = len(found.get(name, []))
        if count != 1:
            raise ValueError(f"Expected exactly one {name} in archive, got {count}")


def _extract_required_files(
    archive_path: Path,
    names: tuple[str, ...],
    destination_dir: Path,
    max_bytes: int,
) -> None:
    """アーカイブを 1 回だけ開き、必須ファイル一式を安全に抽出する。

    tar.gz はシーク不可で getmembers() のたびに全ストリームを解凍走査するため、
    1 パスで全 member を収集し、アーカイブ内の出現順に抽出して再走査を避ける。

    Args:
        archive_path: ダウンロード済みアーカイブのパス。
        names: 抽出する必須ファイル名（basename）のタプル。
        destination_dir: 抽出先ディレクトリ。
        max_bytes: ファイルごとの抽出サイズ上限。

    Raises:
        ValueError: 非対応フォーマット、必須ファイルの欠落・重複、
            読み取り不能エントリ、サイズ上限超過の場合。
    """
    destination_dir.mkdir(parents=True, exist_ok=True)
    if tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path) as archive:
            found_tar: dict[str, list[tarfile.TarInfo]] = {}
            for member in archive.getmembers():
                if member.isfile() and Path(member.name).name in names:
                    found_tar.setdefault(Path(member.name).name, []).append(member)
            _require_unique_members(found_tar, names)
            # gzip ストリームの巻き戻しを避けるためオフセット順に抽出する
            for member in sorted((found_tar[name][0] for name in names), key=lambda m: m.offset):
                src = archive.extractfile(member)
                if src is None:
                    raise ValueError(f"Archive entry is not readable: {member.name}")
                with src, (destination_dir / Path(member.name).name).open("wb") as out:
                    _copy_with_size_limit(src, out, max_bytes, member.name)
        return

    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as archive:
            found_zip: dict[str, list[zipfile.ZipInfo]] = {}
            for info in archive.infolist():
                if not info.is_dir() and Path(info.filename).name in names:
                    found_zip.setdefault(Path(info.filename).name, []).append(info)
            _require_unique_members(found_zip, names)
            for name in names:
                info = found_zip[name][0]
                with archive.open(info) as src, (destination_dir / name).open("wb") as out:
                    _copy_with_size_limit(src, out, max_bytes, info.filename)
        return

    raise ValueError(f"Unsupported archive format: {archive_path}")


def download_model_bundle(config_path: Path, output_dir: Path) -> int:
    """onnx.json に従って配布アーカイブを取得し、出力先へ展開する。

    Returns:
        0: download 成功または既に model.onnx が存在する
        3: download が無効、または設定ファイルが存在しない

    Raises:
        ValueError: 設定不備やアーカイブ不正など、build へ切り替えるべきでない場合
        OSError / urllib.error.URLError: ダウンロードやファイル操作の失敗
    """
    model_path = output_dir / "model.onnx"
    if model_path.exists():
        print(f"[download] ONNX model already present (skipping): {model_path}")
        return 0

    enabled, model_url, expected_sha256, max_download_bytes, max_extract_bytes, ssl_no_verify = _load_download_settings(config_path)
    if not enabled:
        return 3
    if not model_url:
        raise ValueError(f"model_url is empty in {config_path}")

    _validate_url(model_url)

    if not expected_sha256:
        raise ValueError(f"sha256 is required when download is enabled in {config_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bluecore_onnx_", dir=str(output_dir.parent)) as temp_root:
        temp_root_path = Path(temp_root)
        archive_path = temp_root_path / "bundle.archive"
        extracted_dir = temp_root_path / "extracted"
        extracted_dir.mkdir()

        print(f"[download] Fetching ONNX bundle: {model_url}")
        _download_archive(model_url, archive_path, max_download_bytes, ssl_no_verify=ssl_no_verify)
        _verify_archive_sha256(archive_path, expected_sha256)

        _extract_required_files(archive_path, _REQUIRED_FILES, extracted_dir, max_extract_bytes)

        # model.onnx の存在が「インストール完了」の判定マーカーのため必ず最後に
        # 配置する。途中失敗時に部分インストールが完了扱いで恒久化されるのを防ぐ。
        # temp_root は output_dir.parent 配下にあり同一ファイルシステムなので
        # os.replace はファイル単位でアトミックに働く。
        install_order = sorted(_REQUIRED_FILES, key=lambda name: name == "model.onnx")
        for required_name in install_order:
            os.replace(extracted_dir / required_name, output_dir / required_name)

    print(f"[download] Installed ONNX bundle into: {output_dir}")
    return 0


def _parse_args() -> argparse.Namespace:
    """CLI 引数を解析する。"""
    parser = argparse.ArgumentParser(
        prog="python -m bluecore.onnx_download",
        description="ONNX モデルを onnx.json に従いダウンロードする",
    )
    parser.add_argument("--config", type=Path, required=True, help="download 設定の JSON")
    parser.add_argument("--out", type=Path, required=True, help="出力ディレクトリ")
    return parser.parse_args()


def main() -> None:
    """CLI エントリポイント。"""
    args = _parse_args()
    try:
        rc = download_model_bundle(args.config, args.out)
        sys.exit(rc)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
