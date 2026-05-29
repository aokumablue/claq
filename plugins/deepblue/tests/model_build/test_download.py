"""download モジュールのユニットテスト。"""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import io

import pytest

from model_build import download as download_mod


def _write_config(
    tmp_path: Path,
    *,
    enabled: bool,
    model_url: str,
    sha256: str = "",
    max_download_bytes: int = download_mod._DEFAULT_MAX_DOWNLOAD_BYTES,
    max_extract_bytes: int = download_mod._DEFAULT_MAX_EXTRACT_BYTES,
    extra_allowed_hosts: list[str] | None = None,
    allow_http: bool | None = None,
    ssl_no_verify: bool | None = None,
) -> Path:
    """onnx.json を作成して返す。"""
    config_path = tmp_path / "onnx.json"
    download_section: dict = {
        "enabled": enabled,
        "model_url": model_url,
        "sha256": sha256,
        "max_download_bytes": max_download_bytes,
        "max_extract_bytes": max_extract_bytes,
    }
    if extra_allowed_hosts is not None:
        download_section["extra_allowed_hosts"] = extra_allowed_hosts
    if allow_http is not None:
        download_section["allow_http"] = allow_http
    if ssl_no_verify is not None:
        download_section["ssl_no_verify"] = ssl_no_verify
    config_path.write_text(
        json.dumps({"onnx": {"download": download_section}}),
        encoding="utf-8",
    )
    return config_path


def _create_zip_bundle(path: Path, *, duplicate_tokenizer: bool = False) -> None:
    """必須ファイルを含む zip アーカイブを作成する。"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("bundle/model.onnx", b"onnx")
        archive.writestr("bundle/tokenizer.json", "{}")
        archive.writestr("bundle/config.json", "{}")
        archive.writestr("bundle/manifest.json", "{}")
        if duplicate_tokenizer:
            archive.writestr("another/tokenizer.json", "{}")


def _create_tar_bundle(path: Path) -> None:
    """必須ファイルを含む tar アーカイブを作成する。"""
    src = path.parent / "tar_src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "model.onnx").write_bytes(b"onnx")
    (src / "tokenizer.json").write_text("{}", encoding="utf-8")
    (src / "config.json").write_text("{}", encoding="utf-8")
    (src / "manifest.json").write_text("{}", encoding="utf-8")
    with tarfile.open(path, "w:gz") as archive:
        archive.add(src / "model.onnx", arcname="nested/model.onnx")
        archive.add(src / "tokenizer.json", arcname="nested/tokenizer.json")
        archive.add(src / "config.json", arcname="nested/config.json")
        archive.add(src / "manifest.json", arcname="nested/manifest.json")


def _sha256_of(path: Path) -> str:
    """ファイルの SHA-256 ダイジェストを返す。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestValidatingRedirectHandler:
    """_ValidatingRedirectHandler のテスト。"""

    def test_rejects_invalid_redirect_target(self) -> None:
        """許可リスト外のリダイレクト先は ValueError。"""
        handler = download_mod._ValidatingRedirectHandler()
        with pytest.raises(ValueError, match="not in the allowed hosts"):
            handler.redirect_request(None, None, 301, "Moved", {}, "https://evil.example.com/file.zip")

    def test_accepts_valid_redirect_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """許可ホストへのリダイレクトは super に委譲する。"""
        called: list[str] = []

        def fake_super(self: object, req: object, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
            called.append(newurl)

        monkeypatch.setattr(urllib.request.HTTPRedirectHandler, "redirect_request", fake_super)
        handler = download_mod._ValidatingRedirectHandler()
        handler.redirect_request(None, None, 301, "Moved", {}, "https://github.com/file.zip")
        assert called == ["https://github.com/file.zip"]

    def test_accepts_extra_allowed_host_redirect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """extra_allowed_hosts に含まれるホストへのリダイレクトは通過する。"""
        called: list[str] = []

        def fake_super(self: object, req: object, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
            called.append(newurl)

        monkeypatch.setattr(urllib.request.HTTPRedirectHandler, "redirect_request", fake_super)
        handler = download_mod._ValidatingRedirectHandler(extra_allowed_hosts=frozenset({"internal.corp"}))
        handler.redirect_request(None, None, 301, "Moved", {}, "https://internal.corp/file.zip")
        assert called == ["https://internal.corp/file.zip"]

    def test_rejects_host_not_in_extra_list(self) -> None:
        """extra_allowed_hosts が設定されていても未知ホストは拒否される。"""
        handler = download_mod._ValidatingRedirectHandler(extra_allowed_hosts=frozenset({"internal.corp"}))
        with pytest.raises(ValueError, match="not in the allowed hosts"):
            handler.redirect_request(None, None, 301, "Moved", {}, "https://evil.example.com/file.zip")

    def test_accepts_http_redirect_when_allow_http(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """allow_http=True のハンドラーは HTTP リダイレクトを受け入れる。"""
        called: list[str] = []

        def fake_super(self: object, req: object, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
            called.append(newurl)

        monkeypatch.setattr(urllib.request.HTTPRedirectHandler, "redirect_request", fake_super)
        handler = download_mod._ValidatingRedirectHandler(
            extra_allowed_hosts=frozenset({"internal.corp"}), allow_http=True
        )
        handler.redirect_request(None, None, 301, "Moved", {}, "http://internal.corp/file.tar.gz")
        assert called == ["http://internal.corp/file.tar.gz"]

    def test_rejects_ftp_redirect_even_with_allow_http(self) -> None:
        """allow_http=True でも FTP リダイレクトは拒否される。"""
        handler = download_mod._ValidatingRedirectHandler(allow_http=True)
        with pytest.raises(ValueError, match="HTTPS or HTTP scheme"):
            handler.redirect_request(None, None, 301, "Moved", {}, "ftp://github.com/file.zip")


class TestValidateUrl:
    """_validate_url のテスト。"""

    def test_accepts_allowed_github_url(self) -> None:
        """github.com の HTTPS URL は通過する。"""
        download_mod._validate_url("https://github.com/owner/repo/releases/download/v1/model.tar.gz")

    def test_accepts_objects_githubusercontent(self) -> None:
        """objects.githubusercontent.com の HTTPS URL は通過する。"""
        download_mod._validate_url("https://objects.githubusercontent.com/path/file.zip")

    def test_rejects_http(self) -> None:
        """HTTP は拒否される。"""
        with pytest.raises(ValueError, match="HTTPS scheme"):
            download_mod._validate_url("http://github.com/file.zip")

    def test_rejects_ftp(self) -> None:
        """FTP は拒否される。"""
        with pytest.raises(ValueError, match="HTTPS scheme"):
            download_mod._validate_url("ftp://github.com/file.zip")

    def test_rejects_ip_address(self) -> None:
        """IP アドレス直接指定は拒否される。"""
        with pytest.raises(ValueError, match="IP address"):
            download_mod._validate_url("https://192.168.1.1/file.zip")

    def test_rejects_localhost_ip(self) -> None:
        """loopback IP は拒否される。"""
        with pytest.raises(ValueError, match="IP address"):
            download_mod._validate_url("https://127.0.0.1/file.zip")

    def test_rejects_ipv6(self) -> None:
        """IPv6 アドレスは拒否される。"""
        with pytest.raises(ValueError, match="IP address"):
            download_mod._validate_url("https://[::1]/file.zip")

    def test_rejects_unknown_host(self) -> None:
        """許可リスト外ホストは拒否される。"""
        with pytest.raises(ValueError, match="not in the allowed hosts"):
            download_mod._validate_url("https://example.invalid/file.zip")

    def test_rejects_empty_host(self) -> None:
        """ホストなし URL は拒否される。"""
        with pytest.raises(ValueError, match="no valid hostname"):
            download_mod._validate_url("https:///path/file.zip")

    def test_accepts_extra_allowed_host(self) -> None:
        """extra_allowed_hosts に含まれるホストは通過する。"""
        download_mod._validate_url("https://internal.corp/model.tar.gz", frozenset({"internal.corp"}))

    def test_accepts_http_when_allow_http_true(self) -> None:
        """allow_http=True のとき HTTP URL は通過する。"""
        download_mod._validate_url("http://github.com/file.zip", allow_http=True)

    def test_rejects_ftp_even_with_allow_http_true(self) -> None:
        """allow_http=True でも FTP は拒否される。"""
        with pytest.raises(ValueError, match="HTTPS or HTTP scheme"):
            download_mod._validate_url("ftp://github.com/file.zip", allow_http=True)


class TestVerifyArchiveSha256:
    """_verify_archive_sha256 のテスト。"""

    def test_passes_when_sha256_matches(self, tmp_path: Path) -> None:
        """SHA-256 が一致すれば例外なし。"""
        data = b"test archive content"
        archive = tmp_path / "bundle.bin"
        archive.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        download_mod._verify_archive_sha256(archive, expected)

    def test_raises_when_sha256_mismatch(self, tmp_path: Path) -> None:
        """SHA-256 が不一致なら ValueError。"""
        archive = tmp_path / "bundle.bin"
        archive.write_bytes(b"real content")
        with pytest.raises(ValueError, match="SHA-256 mismatch"):
            download_mod._verify_archive_sha256(archive, "a" * 64)


class TestLoadDownloadSettings:
    """_load_download_settings のテスト。"""

    def test_returns_disabled_when_file_missing(self, tmp_path: Path) -> None:
        """設定ファイルがない場合は disabled を返す。"""
        enabled, model_url, sha256, max_dl, max_ex, extra_hosts, allow_http, ssl_no_verify = download_mod._load_download_settings(tmp_path / "missing.json")
        assert enabled is False
        assert model_url == ""
        assert sha256 == ""
        assert max_dl == download_mod._DEFAULT_MAX_DOWNLOAD_BYTES
        assert max_ex == download_mod._DEFAULT_MAX_EXTRACT_BYTES
        assert extra_hosts == frozenset()
        assert allow_http is False
        assert ssl_no_verify is False

    def test_reads_all_fields(self, tmp_path: Path) -> None:
        """全フィールドを正しく読み取る。"""
        config_path = _write_config(
            tmp_path,
            enabled=True,
            model_url="https://github.com/owner/repo/model.zip",
            sha256="abc123",
            max_download_bytes=100,
            max_extract_bytes=50,
        )
        enabled, model_url, sha256, max_dl, max_ex, extra_hosts, allow_http, ssl_no_verify = download_mod._load_download_settings(config_path)
        assert enabled is True
        assert model_url == "https://github.com/owner/repo/model.zip"
        assert sha256 == "abc123"
        assert max_dl == 100
        assert max_ex == 50
        assert extra_hosts == frozenset()
        assert allow_http is False
        assert ssl_no_verify is False

    def test_uses_defaults_when_size_fields_absent(self, tmp_path: Path) -> None:
        """size フィールドがなければデフォルト値を使う。"""
        config_path = tmp_path / "onnx.json"
        config_path.write_text(
            json.dumps({"onnx": {"download": {"enabled": True, "model_url": "https://github.com/x"}}}),
            encoding="utf-8",
        )
        _, _, _, max_dl, max_ex, _, _, _ = download_mod._load_download_settings(config_path)
        assert max_dl == download_mod._DEFAULT_MAX_DOWNLOAD_BYTES
        assert max_ex == download_mod._DEFAULT_MAX_EXTRACT_BYTES

    def test_reads_extra_allowed_hosts(self, tmp_path: Path) -> None:
        """extra_allowed_hosts フィールドを frozenset として返す。"""
        config_path = _write_config(
            tmp_path,
            enabled=True,
            model_url="https://github.com/x",
            extra_allowed_hosts=["a.corp", "b.corp"],
        )
        _, _, _, _, _, extra_hosts, _, _ = download_mod._load_download_settings(config_path)
        assert extra_hosts == frozenset({"a.corp", "b.corp"})

    def test_defaults_extra_allowed_hosts_when_absent(self, tmp_path: Path) -> None:
        """extra_allowed_hosts フィールドがない場合は frozenset() を返す。"""
        config_path = _write_config(tmp_path, enabled=True, model_url="https://github.com/x")
        _, _, _, _, _, extra_hosts, _, _ = download_mod._load_download_settings(config_path)
        assert extra_hosts == frozenset()

    def test_reads_allow_http(self, tmp_path: Path) -> None:
        """allow_http: true が True として返る。"""
        config_path = _write_config(tmp_path, enabled=True, model_url="https://github.com/x", allow_http=True)
        _, _, _, _, _, _, allow_http, _ = download_mod._load_download_settings(config_path)
        assert allow_http is True

    def test_defaults_allow_http_false_when_absent(self, tmp_path: Path) -> None:
        """allow_http フィールドがない場合は False を返す。"""
        config_path = _write_config(tmp_path, enabled=True, model_url="https://github.com/x")
        _, _, _, _, _, _, allow_http, _ = download_mod._load_download_settings(config_path)
        assert allow_http is False

    def test_reads_ssl_no_verify(self, tmp_path: Path) -> None:
        """ssl_no_verify: true が True として返る。"""
        config_path = _write_config(tmp_path, enabled=True, model_url="https://github.com/x", ssl_no_verify=True)
        _, _, _, _, _, _, _, ssl_no_verify = download_mod._load_download_settings(config_path)
        assert ssl_no_verify is True

    def test_defaults_ssl_no_verify_false_when_absent(self, tmp_path: Path) -> None:
        """ssl_no_verify フィールドがない場合は False を返す。"""
        config_path = _write_config(tmp_path, enabled=True, model_url="https://github.com/x")
        _, _, _, _, _, _, _, ssl_no_verify = download_mod._load_download_settings(config_path)
        assert ssl_no_verify is False


class TestDownloadArchiveSizeLimit:
    """_download_archive のサイズ上限テスト。"""

    def _make_mock_opener(self, read_return: object) -> MagicMock:
        """指定した read 戻り値を持つ mock opener を返す。"""
        mock_response = MagicMock()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        if isinstance(read_return, list):
            mock_response.read.side_effect = read_return
        else:
            mock_response.read.return_value = read_return
        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_response
        return mock_opener

    def test_raises_when_download_exceeds_limit(self, tmp_path: Path) -> None:
        """ダウンロードサイズが上限を超えたら ValueError。"""
        mock_opener = self._make_mock_opener([b"x" * download_mod._CHUNK_SIZE, b"extra", b""])
        with patch("urllib.request.build_opener", return_value=mock_opener):
            with pytest.raises(ValueError, match="Download size exceeded limit"):
                download_mod._download_archive(
                    "https://github.com/x",
                    tmp_path / "out.bin",
                    download_mod._CHUNK_SIZE,
                )

    def test_empty_response_exits_loop_immediately(self, tmp_path: Path) -> None:
        """空レスポンスは while ループを即時終了し空ファイルを作成する。"""
        out_file = tmp_path / "out.bin"
        mock_opener = self._make_mock_opener(b"")
        with patch("urllib.request.build_opener", return_value=mock_opener):
            download_mod._download_archive("https://github.com/x", out_file, 1024)
        assert out_file.read_bytes() == b""

    def test_ssl_no_verify_adds_https_handler(self, tmp_path: Path) -> None:
        """ssl_no_verify=True のとき HTTPSHandler が opener に渡される。"""
        out_file = tmp_path / "out.bin"
        mock_opener = self._make_mock_opener(b"")
        with patch("urllib.request.build_opener", return_value=mock_opener) as mock_build:
            download_mod._download_archive("https://github.com/x", out_file, 1024, ssl_no_verify=True)
        handlers = mock_build.call_args[0]
        assert any(isinstance(h, urllib.request.HTTPSHandler) for h in handlers)


class TestCollectArchiveMembers:
    """_collect_archive_members のテスト。"""

    def test_collects_zip_member(self, tmp_path: Path) -> None:
        """zip から一致する basename の要素を列挙する。"""
        archive_path = tmp_path / "bundle.zip"
        _create_zip_bundle(archive_path)
        members = download_mod._collect_archive_members(archive_path, "tokenizer.json")
        assert len(members) == 1
        assert members[0][0] == "zip"

    def test_collects_tar_member(self, tmp_path: Path) -> None:
        """tar から一致する basename の要素を列挙する。"""
        archive_path = tmp_path / "bundle.tar.gz"
        _create_tar_bundle(archive_path)
        members = download_mod._collect_archive_members(archive_path, "config.json")
        assert len(members) == 1
        assert members[0][0] == "tar"

    def test_raises_for_unsupported_archive(self, tmp_path: Path) -> None:
        """非対応フォーマットでは ValueError。"""
        archive_path = tmp_path / "bundle.txt"
        archive_path.write_text("not-archive", encoding="utf-8")
        with pytest.raises(ValueError, match="Unsupported archive format"):
            download_mod._collect_archive_members(archive_path, "model.onnx")


class TestExtractRequiredFile:
    """_extract_required_file のテスト。"""

    def test_extracts_tar_member(self, tmp_path: Path) -> None:
        """tar メンバーを指定先へ抽出する。"""
        archive_path = tmp_path / "bundle.tar.gz"
        _create_tar_bundle(archive_path)
        member = download_mod._collect_archive_members(archive_path, "model.onnx")[0][1]
        destination = tmp_path / "out" / "model.onnx"
        download_mod._extract_required_file(archive_path, "tar", member, destination, 1024 * 1024)
        assert destination.read_bytes() == b"onnx"

    def test_extracts_zip_member(self, tmp_path: Path) -> None:
        """zip メンバーを指定先へ抽出する。"""
        archive_path = tmp_path / "bundle.zip"
        _create_zip_bundle(archive_path)
        member = download_mod._collect_archive_members(archive_path, "model.onnx")[0][1]
        destination = tmp_path / "out" / "model.onnx"
        download_mod._extract_required_file(archive_path, "zip", member, destination, 1024 * 1024)
        assert destination.read_bytes() == b"onnx"

    def test_raises_for_unsupported_kind(self, tmp_path: Path) -> None:
        """非対応 kind では ValueError。"""
        archive_path = tmp_path / "bundle.zip"
        _create_zip_bundle(archive_path)
        with pytest.raises(ValueError, match="Unsupported archive kind"):
            download_mod._extract_required_file(archive_path, "unknown", object(), tmp_path / "out.bin", 1024 * 1024)

    def test_raises_when_extract_exceeds_limit(self, tmp_path: Path) -> None:
        """抽出サイズが上限を超えたら ValueError。"""
        archive_path = tmp_path / "bundle.zip"
        large_content = b"x" * 200
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("bundle/model.onnx", large_content)
            archive.writestr("bundle/tokenizer.json", "{}")
            archive.writestr("bundle/config.json", "{}")
            archive.writestr("bundle/manifest.json", "{}")
        member = download_mod._collect_archive_members(archive_path, "model.onnx")[0][1]
        destination = tmp_path / "out" / "model.onnx"
        with pytest.raises(ValueError, match="exceeds size limit"):
            download_mod._extract_required_file(archive_path, "zip", member, destination, 10)

    def test_copy_with_size_limit_empty_source(self) -> None:
        """空ソースは何も書かず正常終了する。"""
        src = io.BytesIO(b"")
        out = io.BytesIO()
        download_mod._copy_with_size_limit(src, out, 1024, "test.bin")
        assert out.getvalue() == b""

    def test_raises_when_extractfile_returns_none(self, tmp_path: Path) -> None:
        """extractfile が None を返す場合は ValueError。"""
        archive_path = tmp_path / "bundle.tar.gz"
        _create_tar_bundle(archive_path)
        member = download_mod._collect_archive_members(archive_path, "model.onnx")[0][1]
        destination = tmp_path / "out" / "model.onnx"
        with patch.object(tarfile.TarFile, "extractfile", return_value=None):
            with pytest.raises(ValueError, match="not readable"):
                download_mod._extract_required_file(archive_path, "tar", member, destination, 1024 * 1024)


class TestDownloadModelBundle:
    """download_model_bundle のテスト。"""

    def test_returns_zero_when_model_exists(self, tmp_path: Path) -> None:
        """model.onnx が既にある場合は即時 0。"""
        output_dir = tmp_path / "models"
        output_dir.mkdir()
        (output_dir / "model.onnx").write_bytes(b"exists")
        result = download_mod.download_model_bundle(tmp_path / "any.json", output_dir)
        assert result == 0

    def test_returns_three_when_config_missing(self, tmp_path: Path) -> None:
        """設定ファイル未作成なら 3（fallback 指示）。"""
        output_dir = tmp_path / "models"
        result = download_mod.download_model_bundle(tmp_path / "missing.json", output_dir)
        assert result == 3

    def test_returns_three_when_disabled(self, tmp_path: Path) -> None:
        """enabled=false なら 3（fallback 指示）。"""
        config_path = _write_config(tmp_path, enabled=False, model_url="https://github.com/x/model.zip")
        output_dir = tmp_path / "models"
        result = download_mod.download_model_bundle(config_path, output_dir)
        assert result == 3

    def test_raises_when_enabled_without_url(self, tmp_path: Path) -> None:
        """enabled=true かつ model_url 空なら ValueError。"""
        config_path = _write_config(tmp_path, enabled=True, model_url="")
        output_dir = tmp_path / "models"
        with pytest.raises(ValueError, match="model_url is empty"):
            download_mod.download_model_bundle(config_path, output_dir)

    def test_raises_when_url_fails_validation(self, tmp_path: Path) -> None:
        """許可リスト外 URL なら ValueError（ダウンロード前に検証）。"""
        config_path = _write_config(tmp_path, enabled=True, model_url="https://evil.example.com/model.zip")
        output_dir = tmp_path / "models"
        with pytest.raises(ValueError, match="not in the allowed hosts"):
            download_mod.download_model_bundle(config_path, output_dir)

    def test_raises_when_url_is_http(self, tmp_path: Path) -> None:
        """HTTP URL なら ValueError。"""
        config_path = _write_config(tmp_path, enabled=True, model_url="http://github.com/model.zip")
        output_dir = tmp_path / "models"
        with pytest.raises(ValueError, match="HTTPS scheme"):
            download_mod.download_model_bundle(config_path, output_dir)

    def test_raises_when_sha256_empty_and_enabled(self, tmp_path: Path) -> None:
        """enabled=true かつ sha256 が空なら ValueError。"""
        config_path = _write_config(tmp_path, enabled=True, model_url="https://github.com/x/model.zip", sha256="")
        output_dir = tmp_path / "models"
        with pytest.raises(ValueError, match="sha256 is required"):
            download_mod.download_model_bundle(config_path, output_dir)

    def test_downloads_and_extracts_zip_bundle(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """zip 配布物を取得して必須4ファイルを配置する。"""
        archive_path = tmp_path / "bundle.zip"
        _create_zip_bundle(archive_path)
        config_path = _write_config(
            tmp_path, enabled=True, model_url="https://github.com/x/model.zip", sha256=_sha256_of(archive_path)
        )
        output_dir = tmp_path / "models"

        def _fake_download(_url: str, destination: Path, _max_bytes: int, _extra_hosts: frozenset[str] = frozenset(), *, allow_http: bool = False, ssl_no_verify: bool = False) -> None:
            shutil.copy2(archive_path, destination)

        monkeypatch.setattr(download_mod, "_download_archive", _fake_download)

        result = download_mod.download_model_bundle(config_path, output_dir)
        assert result == 0
        for name in ("model.onnx", "tokenizer.json", "config.json", "manifest.json"):
            assert (output_dir / name).exists()

    def test_downloads_and_extracts_tar_bundle(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """tar 配布物を取得して必須4ファイルを配置する。"""
        archive_path = tmp_path / "bundle.tar.gz"
        _create_tar_bundle(archive_path)
        config_path = _write_config(
            tmp_path, enabled=True, model_url="https://github.com/x/model.tar.gz", sha256=_sha256_of(archive_path)
        )
        output_dir = tmp_path / "models"

        def _fake_download(_url: str, destination: Path, _max_bytes: int, _extra_hosts: frozenset[str] = frozenset(), *, allow_http: bool = False, ssl_no_verify: bool = False) -> None:
            shutil.copy2(archive_path, destination)

        monkeypatch.setattr(download_mod, "_download_archive", _fake_download)

        result = download_mod.download_model_bundle(config_path, output_dir)
        assert result == 0
        for name in ("model.onnx", "tokenizer.json", "config.json", "manifest.json"):
            assert (output_dir / name).exists()

    def test_raises_when_archive_contains_duplicate_required_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """必須ファイル重複時は ValueError。"""
        archive_path = tmp_path / "bundle.zip"
        _create_zip_bundle(archive_path, duplicate_tokenizer=True)
        config_path = _write_config(
            tmp_path, enabled=True, model_url="https://github.com/x/model.zip", sha256=_sha256_of(archive_path)
        )
        output_dir = tmp_path / "models"

        def _fake_download(_url: str, destination: Path, _max_bytes: int, _extra_hosts: frozenset[str] = frozenset(), *, allow_http: bool = False, ssl_no_verify: bool = False) -> None:
            shutil.copy2(archive_path, destination)

        monkeypatch.setattr(download_mod, "_download_archive", _fake_download)

        with pytest.raises(ValueError, match="Expected exactly one tokenizer.json"):
            download_mod.download_model_bundle(config_path, output_dir)

    def test_verifies_sha256_when_provided(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """sha256 が設定されている場合、一致するアーカイブは通過する。"""
        archive_path = tmp_path / "bundle.zip"
        _create_zip_bundle(archive_path)
        config_path = _write_config(
            tmp_path, enabled=True, model_url="https://github.com/x/model.zip", sha256=_sha256_of(archive_path)
        )
        output_dir = tmp_path / "models"

        def _fake_download(_url: str, destination: Path, _max_bytes: int, _extra_hosts: frozenset[str] = frozenset(), *, allow_http: bool = False, ssl_no_verify: bool = False) -> None:
            shutil.copy2(archive_path, destination)

        monkeypatch.setattr(download_mod, "_download_archive", _fake_download)
        result = download_mod.download_model_bundle(config_path, output_dir)
        assert result == 0

    def test_raises_when_sha256_mismatch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """sha256 が不一致なら ValueError。"""
        archive_path = tmp_path / "bundle.zip"
        _create_zip_bundle(archive_path)
        config_path = _write_config(
            tmp_path, enabled=True, model_url="https://github.com/x/model.zip", sha256="a" * 64
        )
        output_dir = tmp_path / "models"

        def _fake_download(_url: str, destination: Path, _max_bytes: int, _extra_hosts: frozenset[str] = frozenset(), *, allow_http: bool = False, ssl_no_verify: bool = False) -> None:
            shutil.copy2(archive_path, destination)

        monkeypatch.setattr(download_mod, "_download_archive", _fake_download)
        with pytest.raises(ValueError, match="SHA-256 mismatch"):
            download_mod.download_model_bundle(config_path, output_dir)

    def test_downloads_with_extra_allowed_host(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """extra_allowed_hosts に指定したホストの URL でダウンロードが成功する。"""
        archive_path = tmp_path / "bundle.zip"
        _create_zip_bundle(archive_path)
        config_path = _write_config(
            tmp_path,
            enabled=True,
            model_url="https://internal.corp/model.zip",
            sha256=_sha256_of(archive_path),
            extra_allowed_hosts=["internal.corp"],
        )
        output_dir = tmp_path / "models"

        received_extra_hosts: list[frozenset[str]] = []

        def _fake_download(_url: str, destination: Path, _max_bytes: int, _extra_hosts: frozenset[str] = frozenset(), *, allow_http: bool = False, ssl_no_verify: bool = False) -> None:
            received_extra_hosts.append(_extra_hosts)
            shutil.copy2(archive_path, destination)

        monkeypatch.setattr(download_mod, "_download_archive", _fake_download)

        result = download_mod.download_model_bundle(config_path, output_dir)
        assert result == 0
        assert received_extra_hosts == [frozenset({"internal.corp"})]
        for name in ("model.onnx", "tokenizer.json", "config.json", "manifest.json"):
            assert (output_dir / name).exists()

    def test_downloads_with_allow_http_true(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """allow_http=true かつ extra_allowed_hosts に指定した HTTP URL でダウンロードが成功する。"""
        archive_path = tmp_path / "bundle.zip"
        _create_zip_bundle(archive_path)
        config_path = _write_config(
            tmp_path,
            enabled=True,
            model_url="http://internal.corp/model.zip",
            sha256=_sha256_of(archive_path),
            extra_allowed_hosts=["internal.corp"],
            allow_http=True,
        )
        output_dir = tmp_path / "models"

        received_allow_http: list[bool] = []

        def _fake_download(_url: str, destination: Path, _max_bytes: int, _extra_hosts: frozenset[str] = frozenset(), *, allow_http: bool = False, ssl_no_verify: bool = False) -> None:
            received_allow_http.append(allow_http)
            shutil.copy2(archive_path, destination)

        monkeypatch.setattr(download_mod, "_download_archive", _fake_download)

        result = download_mod.download_model_bundle(config_path, output_dir)
        assert result == 0
        assert received_allow_http == [True]
        for name in ("model.onnx", "tokenizer.json", "config.json", "manifest.json"):
            assert (output_dir / name).exists()

    def test_downloads_with_ssl_no_verify_true(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """ssl_no_verify=true が _download_archive に伝わる。"""
        archive_path = tmp_path / "bundle.zip"
        _create_zip_bundle(archive_path)
        config_path = _write_config(
            tmp_path,
            enabled=True,
            model_url="https://github.com/x/model.zip",
            sha256=_sha256_of(archive_path),
            ssl_no_verify=True,
        )
        output_dir = tmp_path / "models"

        received_ssl: list[bool] = []

        def _fake_download(_url: str, destination: Path, _max_bytes: int, _extra_hosts: frozenset[str] = frozenset(), *, allow_http: bool = False, ssl_no_verify: bool = False) -> None:
            received_ssl.append(ssl_no_verify)
            shutil.copy2(archive_path, destination)

        monkeypatch.setattr(download_mod, "_download_archive", _fake_download)

        result = download_mod.download_model_bundle(config_path, output_dir)
        assert result == 0
        assert received_ssl == [True]
