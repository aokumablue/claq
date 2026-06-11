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
import shutil
import ssl
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

_REQUIRED_FILES = ("model.onnx", "tokenizer.json", "config.json", "manifest.json")

_DEFAULT_MAX_DOWNLOAD_BYTES: int = 2 * 1024 * 1024 * 1024  # 2 GB
_DEFAULT_MAX_EXTRACT_BYTES: int = 500 * 1024 * 1024  # 500 MB per file
_CHUNK_SIZE: int = 1024 * 1024  # 1 MB


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """リダイレクト先 URL を再検証するカスタムハンドラー。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]  # noqa: PLR0913
        """リダイレクト URL を _validate_url で再検証してから親クラスに委譲する。"""
        _validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_url(url: str) -> None:
    """URL の形式を最低限検証する。

    scheme が http/https のいずれかで hostname を持つことのみ必須とする。
    HTTP（平文）と IP アドレス指定は許可するが、安全性が低いため警告を出す。
    ホスト制限は行わない。
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ValueError(f"URL must use HTTPS or HTTP scheme: {url!r}")

    host = parsed.hostname or ""
    if not host:
        raise ValueError(f"URL has no valid hostname: {url!r}")

    if parsed.scheme == "http":
        print(f"[download] WARNING: 平文 HTTP で取得します（中間者攻撃のリスク）: {url!r}")

    try:
        ipaddress.ip_address(host)
        print(f"[download] WARNING: IP アドレス指定の URL です（証明書検証が機能しません）: {host!r}")
    except ValueError:
        pass


def _load_download_settings(config_path: Path) -> tuple[bool, str, str, int, int, bool]:
    """onnx.json から download 設定を読み込む。

    Returns:
        (enabled, model_url, expected_sha256, max_download_bytes, max_extract_bytes, ssl_no_verify)
    """
    if not config_path.is_file():
        return False, "", "", _DEFAULT_MAX_DOWNLOAD_BYTES, _DEFAULT_MAX_EXTRACT_BYTES, False

    data = json.loads(config_path.read_text(encoding="utf-8"))
    download = data.get("onnx", {}).get("download", {})
    enabled = bool(download.get("enabled", False))
    model_url = str(download.get("model_url", "") or "")
    expected_sha256 = str(download.get("sha256", "") or "")
    max_download_bytes = int(download.get("max_download_bytes", _DEFAULT_MAX_DOWNLOAD_BYTES))
    max_extract_bytes = int(download.get("max_extract_bytes", _DEFAULT_MAX_EXTRACT_BYTES))
    ssl_no_verify = bool(download.get("ssl_no_verify", False))
    return enabled, model_url, expected_sha256, max_download_bytes, max_extract_bytes, ssl_no_verify


def _download_archive(
    model_url: str,
    archive_path: Path,
    max_bytes: int,
    *,
    ssl_no_verify: bool = False,
) -> None:
    """URL からアーカイブをダウンロードする。リダイレクト先も再検証する。"""
    handlers: list[urllib.request.BaseHandler] = [_ValidatingRedirectHandler()]
    if ssl_no_verify:
        print("[download] WARNING: SSL 証明書検証を無効化しています（中間者攻撃のリスク）")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(model_url, headers={"User-Agent": "bluecore-install/1.0"})
    downloaded = 0
    with opener.open(request, timeout=600) as response, archive_path.open("wb") as out:
        while chunk := response.read(_CHUNK_SIZE):
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


def _collect_archive_members(archive_path: Path, required_name: str) -> list[tuple[str, object]]:
    """アーカイブ内で required_name に一致するファイル候補を集める。"""
    candidates: list[tuple[str, object]] = []
    if tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path) as archive:
            for member in archive.getmembers():
                if member.isfile() and Path(member.name).name == required_name:
                    candidates.append(("tar", member))
        return candidates

    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                if not member.is_dir() and Path(member.filename).name == required_name:
                    candidates.append(("zip", member))
        return candidates

    raise ValueError(f"Unsupported archive format: {archive_path}")


def _extract_required_file(
    archive_path: Path,
    archive_kind: str,
    member: object,
    destination: Path,
    max_bytes: int,
) -> None:
    """アーカイブから 1 ファイルだけ安全に抽出する。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if archive_kind == "tar":
        with tarfile.open(archive_path) as archive, destination.open("wb") as out:
            assert isinstance(member, tarfile.TarInfo)
            src = archive.extractfile(member)
            if src is None:
                raise ValueError(f"Archive entry is not readable: {member.name}")
            with src:
                _copy_with_size_limit(src, out, max_bytes, member.name)
        return

    if archive_kind == "zip":
        with zipfile.ZipFile(archive_path) as archive, destination.open("wb") as out:
            assert isinstance(member, zipfile.ZipInfo)
            with archive.open(member) as src:
                _copy_with_size_limit(src, out, max_bytes, member.filename)
        return

    raise ValueError(f"Unsupported archive kind: {archive_kind}")


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

        for required_name in _REQUIRED_FILES:
            candidates = _collect_archive_members(archive_path, required_name)
            if len(candidates) != 1:
                raise ValueError(f"Expected exactly one {required_name} in archive, got {len(candidates)}")
            archive_kind, member = candidates[0]
            _extract_required_file(archive_path, archive_kind, member, extracted_dir / required_name, max_extract_bytes)

        for required_name in _REQUIRED_FILES:
            shutil.copy2(extracted_dir / required_name, output_dir / required_name)

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
