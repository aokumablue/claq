"""bluecore.onnx_download のユニットテスト。"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import bluecore.onnx_download as mod


@pytest.fixture(autouse=True)
def _isolate_warning_marker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """警告マーカーの書き込み先を実 HOME からテスト用ディレクトリへ隔離する。"""
    monkeypatch.setattr(mod, "_WARNING_MARKER", tmp_path / "marker" / "onnx_download_warning")


def _write_config(
    tmp_path: Path,
    *,
    enabled: bool,
    model_url: str,
    sha256: str = "",
    max_download_bytes: int = mod._DEFAULT_MAX_DOWNLOAD_BYTES,
    max_extract_bytes: int = mod._DEFAULT_MAX_EXTRACT_BYTES,
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

    def test_accepts_https_redirect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTPS リダイレクトは super に委譲する。"""
        called: list[str] = []

        def fake_super(self: object, req: object, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
            called.append(newurl)

        monkeypatch.setattr(urllib.request.HTTPRedirectHandler, "redirect_request", fake_super)
        handler = mod._ValidatingRedirectHandler()
        handler.redirect_request(None, None, 301, "Moved", {}, "https://github.com/file.zip")
        assert called == ["https://github.com/file.zip"]

    def test_accepts_http_redirect_with_warning_when_opted_in(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """初回 URL が http（明示オプトイン）なら HTTP リダイレクトも警告付きで通過する。"""
        called: list[str] = []

        def fake_super(self: object, req: object, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
            called.append(newurl)

        monkeypatch.setattr(urllib.request.HTTPRedirectHandler, "redirect_request", fake_super)
        handler = mod._ValidatingRedirectHandler(allow_http=True)
        handler.redirect_request(None, None, 301, "Moved", {}, "http://internal.corp/file.tar.gz")
        assert called == ["http://internal.corp/file.tar.gz"]
        assert "WARNING" in capsys.readouterr().out

    def test_rejects_https_to_http_downgrade_redirect(self) -> None:
        """初回 URL が https の場合、平文 HTTP へのダウングレードリダイレクトを拒否する。"""
        handler = mod._ValidatingRedirectHandler(allow_http=False)
        with pytest.raises(ValueError, match="Refusing redirect downgrade"):
            handler.redirect_request(None, None, 301, "Moved", {}, "http://evil.example/file.zip")

    def test_rejects_ftp_redirect(self) -> None:
        """FTP など http/https 以外のリダイレクトは拒否される。"""
        handler = mod._ValidatingRedirectHandler()
        with pytest.raises(ValueError, match="HTTPS or HTTP scheme"):
            handler.redirect_request(None, None, 301, "Moved", {}, "ftp://github.com/file.zip")


class TestWarn:
    """_warn のテスト。"""

    def test_writes_warning_to_stdout_and_marker(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """警告を stdout とマーカーファイルの両方へ記録する。"""
        mod._warn("first")
        mod._warn("second")
        assert "WARNING: first" in capsys.readouterr().out
        assert mod._WARNING_MARKER.read_text(encoding="utf-8") == "first\nsecond\n"

    def test_stops_appending_when_marker_exceeds_cap(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """マーカーが上限超過なら追記しない（stdout 出力は維持）。"""
        mod._WARNING_MARKER.parent.mkdir(parents=True, exist_ok=True)
        mod._WARNING_MARKER.write_text("x" * (mod._MAX_MARKER_BYTES + 1), encoding="utf-8")
        mod._warn("overflow")
        assert "WARNING: overflow" in capsys.readouterr().out
        assert "overflow" not in mod._WARNING_MARKER.read_text(encoding="utf-8")

    def test_marker_write_failure_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """マーカー書き込み失敗でも警告出力自体は成功する。"""
        blocked = tmp_path / "not-a-dir"
        blocked.write_text("file", encoding="utf-8")
        monkeypatch.setattr(mod, "_WARNING_MARKER", blocked / "marker")
        mod._warn("best-effort")
        assert "WARNING: best-effort" in capsys.readouterr().out


class TestValidateUrl:
    """_validate_url のテスト。"""

    def test_accepts_https_url(self) -> None:
        """HTTPS URL は通過する。"""
        mod._validate_url("https://github.com/owner/repo/releases/download/v1/model.tar.gz")

    def test_accepts_arbitrary_host(self) -> None:
        """ホスト制限はないため任意ホストの HTTPS URL も通過する。"""
        mod._validate_url("https://example.invalid/file.zip")

    def test_accepts_http_with_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        """HTTP は通過するが平文警告を出す。"""
        mod._validate_url("http://github.com/file.zip")
        assert "WARNING" in capsys.readouterr().out

    def test_accepts_ip_address_with_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        """IP アドレス指定は通過するが警告を出す。"""
        mod._validate_url("https://192.168.1.1/file.zip")
        assert "WARNING" in capsys.readouterr().out

    def test_accepts_ipv6_with_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        """IPv6 アドレス指定は通過するが警告を出す。"""
        mod._validate_url("https://[::1]/file.zip")
        assert "WARNING" in capsys.readouterr().out

    def test_rejects_ftp(self) -> None:
        """http/https 以外の scheme は拒否される。"""
        with pytest.raises(ValueError, match="HTTPS or HTTP scheme"):
            mod._validate_url("ftp://github.com/file.zip")

    def test_rejects_empty_host(self) -> None:
        """ホストなし URL は拒否される。"""
        with pytest.raises(ValueError, match="no valid hostname"):
            mod._validate_url("https:///path/file.zip")


class TestVerifyArchiveSha256:
    """_verify_archive_sha256 のテスト。"""

    def test_passes_when_sha256_matches(self, tmp_path: Path) -> None:
        """SHA-256 が一致すれば例外なし。"""
        data = b"test archive content"
        archive = tmp_path / "bundle.bin"
        archive.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        mod._verify_archive_sha256(archive, expected)

    def test_raises_when_sha256_mismatch(self, tmp_path: Path) -> None:
        """SHA-256 が不一致なら ValueError。"""
        archive = tmp_path / "bundle.bin"
        archive.write_bytes(b"real content")
        with pytest.raises(ValueError, match="SHA-256 mismatch"):
            mod._verify_archive_sha256(archive, "a" * 64)


class TestLoadDownloadSettings:
    """_load_download_settings のテスト。"""

    def test_returns_disabled_when_file_missing(self, tmp_path: Path) -> None:
        """設定ファイルがない場合は disabled を返す。"""
        enabled, model_url, sha256, max_dl, max_ex, ssl_no_verify = mod._load_download_settings(tmp_path / "missing.json")
        assert enabled is False
        assert model_url == ""
        assert sha256 == ""
        assert max_dl == mod._DEFAULT_MAX_DOWNLOAD_BYTES
        assert max_ex == mod._DEFAULT_MAX_EXTRACT_BYTES
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
        enabled, model_url, sha256, max_dl, max_ex, ssl_no_verify = mod._load_download_settings(config_path)
        assert enabled is True
        assert model_url == "https://github.com/owner/repo/model.zip"
        assert sha256 == "abc123"
        assert max_dl == 100
        assert max_ex == 50
        assert ssl_no_verify is False

    def test_uses_defaults_when_size_fields_absent(self, tmp_path: Path) -> None:
        """size フィールドがなければデフォルト値を使う。"""
        config_path = tmp_path / "onnx.json"
        config_path.write_text(
            json.dumps({"onnx": {"download": {"enabled": True, "model_url": "https://github.com/x"}}}),
            encoding="utf-8",
        )
        _, _, _, max_dl, max_ex, _ = mod._load_download_settings(config_path)
        assert max_dl == mod._DEFAULT_MAX_DOWNLOAD_BYTES
        assert max_ex == mod._DEFAULT_MAX_EXTRACT_BYTES

    def test_reads_ssl_no_verify(self, tmp_path: Path) -> None:
        """ssl_no_verify: true が True として返る。"""
        config_path = _write_config(tmp_path, enabled=True, model_url="https://github.com/x", ssl_no_verify=True)
        _, _, _, _, _, ssl_no_verify = mod._load_download_settings(config_path)
        assert ssl_no_verify is True

    def test_defaults_ssl_no_verify_false_when_absent(self, tmp_path: Path) -> None:
        """ssl_no_verify フィールドがない場合は False を返す。"""
        config_path = _write_config(tmp_path, enabled=True, model_url="https://github.com/x")
        _, _, _, _, _, ssl_no_verify = mod._load_download_settings(config_path)
        assert ssl_no_verify is False

    @pytest.mark.parametrize("bad_value", ["false", "true", "0", 1, [], {}])
    def test_rejects_non_bool_ssl_no_verify(self, tmp_path: Path, bad_value: object) -> None:
        """ssl_no_verify が JSON boolean でなければ fail-closed で拒否する。"""
        config_path = tmp_path / "onnx.json"
        config_path.write_text(
            json.dumps(
                {"onnx": {"download": {"enabled": True, "model_url": "https://github.com/x", "ssl_no_verify": bad_value}}}
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="ssl_no_verify must be a JSON boolean"):
            mod._load_download_settings(config_path)

    def test_rejects_non_bool_enabled(self, tmp_path: Path) -> None:
        """enabled が文字列 \"false\"（truthy）の場合も拒否する。"""
        config_path = tmp_path / "onnx.json"
        config_path.write_text(
            json.dumps({"onnx": {"download": {"enabled": "false", "model_url": "https://github.com/x"}}}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="enabled must be a JSON boolean"):
            mod._load_download_settings(config_path)


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
        mock_opener = self._make_mock_opener([b"x" * mod._CHUNK_SIZE, b"extra", b""])
        with patch("urllib.request.build_opener", return_value=mock_opener):
            with pytest.raises(ValueError, match="Download size exceeded limit"):
                mod._download_archive(
                    "https://github.com/x",
                    tmp_path / "out.bin",
                    mod._CHUNK_SIZE,
                )

    def test_empty_response_exits_loop_immediately(self, tmp_path: Path) -> None:
        """空レスポンスは while ループを即時終了し空ファイルを作成する。"""
        out_file = tmp_path / "out.bin"
        mock_opener = self._make_mock_opener(b"")
        with patch("urllib.request.build_opener", return_value=mock_opener):
            mod._download_archive("https://github.com/x", out_file, 1024)
        assert out_file.read_bytes() == b""

    def test_raises_when_download_exceeds_time_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ウォールクロック上限を超えたダウンロードは ValueError で打ち切る。"""
        # 1 回目: deadline 計算、2 回目: 上限超過判定
        clock = iter([0.0, mod._MAX_DOWNLOAD_SECONDS + 1.0])
        monkeypatch.setattr(mod.time, "monotonic", lambda: next(clock))
        mock_opener = self._make_mock_opener([b"x", b""])
        with patch("urllib.request.build_opener", return_value=mock_opener):
            with pytest.raises(ValueError, match="Download exceeded time limit"):
                mod._download_archive("https://github.com/x", tmp_path / "out.bin", 1024)

    def test_ssl_no_verify_adds_https_handler(self, tmp_path: Path) -> None:
        """ssl_no_verify=True のとき HTTPSHandler が opener に渡される。"""
        out_file = tmp_path / "out.bin"
        mock_opener = self._make_mock_opener(b"")
        with patch("urllib.request.build_opener", return_value=mock_opener) as mock_build:
            mod._download_archive("https://github.com/x", out_file, 1024, ssl_no_verify=True)
        handlers = mock_build.call_args[0]
        assert any(isinstance(h, urllib.request.HTTPSHandler) for h in handlers)


class TestExtractRequiredFiles:
    """_extract_required_files のテスト。"""

    def test_extracts_all_files_from_zip(self, tmp_path: Path) -> None:
        """zip から必須ファイル一式を抽出する。"""
        archive_path = tmp_path / "bundle.zip"
        _create_zip_bundle(archive_path)
        out_dir = tmp_path / "out"
        mod._extract_required_files(archive_path, mod._REQUIRED_FILES, out_dir, 1024 * 1024)
        assert (out_dir / "model.onnx").read_bytes() == b"onnx"
        for name in mod._REQUIRED_FILES:
            assert (out_dir / name).exists()

    def test_extracts_all_files_from_tar(self, tmp_path: Path) -> None:
        """tar から必須ファイル一式を抽出する。"""
        archive_path = tmp_path / "bundle.tar.gz"
        _create_tar_bundle(archive_path)
        out_dir = tmp_path / "out"
        mod._extract_required_files(archive_path, mod._REQUIRED_FILES, out_dir, 1024 * 1024)
        assert (out_dir / "model.onnx").read_bytes() == b"onnx"
        for name in mod._REQUIRED_FILES:
            assert (out_dir / name).exists()

    def test_scans_tar_members_only_once(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """tar.gz の全ストリーム走査（getmembers）は 1 回だけ実行される。"""
        archive_path = tmp_path / "bundle.tar.gz"
        _create_tar_bundle(archive_path)
        scans: list[int] = []
        original_getmembers = tarfile.TarFile.getmembers

        def counting_getmembers(self: tarfile.TarFile):
            scans.append(1)
            return original_getmembers(self)

        monkeypatch.setattr(tarfile.TarFile, "getmembers", counting_getmembers)
        mod._extract_required_files(archive_path, mod._REQUIRED_FILES, tmp_path / "out", 1024 * 1024)
        assert len(scans) == 1

    def test_raises_for_unsupported_archive(self, tmp_path: Path) -> None:
        """非対応フォーマットでは ValueError。"""
        archive_path = tmp_path / "bundle.txt"
        archive_path.write_text("not-archive", encoding="utf-8")
        with pytest.raises(ValueError, match="Unsupported archive format"):
            mod._extract_required_files(archive_path, mod._REQUIRED_FILES, tmp_path / "out", 1024)

    def test_raises_when_required_file_missing(self, tmp_path: Path) -> None:
        """必須ファイル欠落（0 件）は ValueError。"""
        archive_path = tmp_path / "bundle.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("bundle/model.onnx", b"onnx")
        with pytest.raises(ValueError, match="Expected exactly one tokenizer.json in archive, got 0"):
            mod._extract_required_files(archive_path, mod._REQUIRED_FILES, tmp_path / "out", 1024)

    def test_raises_when_required_file_duplicated_in_tar(self, tmp_path: Path) -> None:
        """tar 内の必須ファイル重複は ValueError。"""
        archive_path = tmp_path / "bundle.tar.gz"
        src = tmp_path / "tar_src"
        src.mkdir()
        for name in mod._REQUIRED_FILES:
            (src / name).write_bytes(b"x")
        with tarfile.open(archive_path, "w:gz") as archive:
            for name in mod._REQUIRED_FILES:
                archive.add(src / name, arcname=f"a/{name}")
            archive.add(src / "config.json", arcname="b/config.json")
        with pytest.raises(ValueError, match="Expected exactly one config.json in archive, got 2"):
            mod._extract_required_files(archive_path, mod._REQUIRED_FILES, tmp_path / "out", 1024)

    def test_raises_when_extract_exceeds_limit(self, tmp_path: Path) -> None:
        """抽出サイズが上限を超えたら ValueError。"""
        archive_path = tmp_path / "bundle.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("bundle/model.onnx", b"x" * 200)
            archive.writestr("bundle/tokenizer.json", "{}")
            archive.writestr("bundle/config.json", "{}")
            archive.writestr("bundle/manifest.json", "{}")
        with pytest.raises(ValueError, match="exceeds size limit"):
            mod._extract_required_files(archive_path, mod._REQUIRED_FILES, tmp_path / "out", 10)

    def test_copy_with_size_limit_empty_source(self) -> None:
        """空ソースは何も書かず正常終了する。"""
        src = io.BytesIO(b"")
        out = io.BytesIO()
        mod._copy_with_size_limit(src, out, 1024, "test.bin")
        assert out.getvalue() == b""

    def test_raises_when_extractfile_returns_none(self, tmp_path: Path) -> None:
        """extractfile が None を返す場合は ValueError。"""
        archive_path = tmp_path / "bundle.tar.gz"
        _create_tar_bundle(archive_path)
        with patch.object(tarfile.TarFile, "extractfile", return_value=None):
            with pytest.raises(ValueError, match="not readable"):
                mod._extract_required_files(archive_path, mod._REQUIRED_FILES, tmp_path / "out", 1024 * 1024)


class TestDownloadModelBundle:
    """download_model_bundle のテスト。"""

    def test_returns_zero_when_model_exists(self, tmp_path: Path) -> None:
        """model.onnx が既にある場合は即時 0。"""
        output_dir = tmp_path / "models"
        output_dir.mkdir()
        (output_dir / "model.onnx").write_bytes(b"exists")
        result = mod.download_model_bundle(tmp_path / "any.json", output_dir)
        assert result == 0

    def test_returns_three_when_config_missing(self, tmp_path: Path) -> None:
        """設定ファイル未作成なら 3（fallback 指示）。"""
        output_dir = tmp_path / "models"
        result = mod.download_model_bundle(tmp_path / "missing.json", output_dir)
        assert result == 3

    def test_returns_three_when_disabled(self, tmp_path: Path) -> None:
        """enabled=false なら 3（fallback 指示）。"""
        config_path = _write_config(tmp_path, enabled=False, model_url="https://github.com/x/model.zip")
        output_dir = tmp_path / "models"
        result = mod.download_model_bundle(config_path, output_dir)
        assert result == 3

    def test_raises_when_enabled_without_url(self, tmp_path: Path) -> None:
        """enabled=true かつ model_url 空なら ValueError。"""
        config_path = _write_config(tmp_path, enabled=True, model_url="")
        output_dir = tmp_path / "models"
        with pytest.raises(ValueError, match="model_url is empty"):
            mod.download_model_bundle(config_path, output_dir)

    def test_raises_when_sha256_empty_and_enabled(self, tmp_path: Path) -> None:
        """enabled=true かつ sha256 が空なら ValueError。"""
        config_path = _write_config(tmp_path, enabled=True, model_url="https://github.com/x/model.zip", sha256="")
        output_dir = tmp_path / "models"
        with pytest.raises(ValueError, match="sha256 is required"):
            mod.download_model_bundle(config_path, output_dir)

    def test_downloads_and_extracts_zip_bundle(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """zip 配布物を取得して必須4ファイルを配置する。"""
        archive_path = tmp_path / "bundle.zip"
        _create_zip_bundle(archive_path)
        config_path = _write_config(
            tmp_path, enabled=True, model_url="https://github.com/x/model.zip", sha256=_sha256_of(archive_path)
        )
        output_dir = tmp_path / "models"

        def _fake_download(_url: str, destination: Path, _max_bytes: int, *, ssl_no_verify: bool = False) -> None:
            shutil.copy2(archive_path, destination)

        monkeypatch.setattr(mod, "_download_archive", _fake_download)

        result = mod.download_model_bundle(config_path, output_dir)
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

        def _fake_download(_url: str, destination: Path, _max_bytes: int, *, ssl_no_verify: bool = False) -> None:
            shutil.copy2(archive_path, destination)

        monkeypatch.setattr(mod, "_download_archive", _fake_download)

        result = mod.download_model_bundle(config_path, output_dir)
        assert result == 0
        for name in ("model.onnx", "tokenizer.json", "config.json", "manifest.json"):
            assert (output_dir / name).exists()

    def test_installs_model_onnx_last(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """完了マーカーの model.onnx は必ず最後に配置される。"""
        archive_path = tmp_path / "bundle.zip"
        _create_zip_bundle(archive_path)
        config_path = _write_config(
            tmp_path, enabled=True, model_url="https://github.com/x/model.zip", sha256=_sha256_of(archive_path)
        )
        output_dir = tmp_path / "models"

        def _fake_download(_url: str, destination: Path, _max_bytes: int, *, ssl_no_verify: bool = False) -> None:
            shutil.copy2(archive_path, destination)

        monkeypatch.setattr(mod, "_download_archive", _fake_download)

        installed: list[str] = []
        original_replace = mod.os.replace

        def tracking_replace(src: str | Path, dst: str | Path) -> None:
            installed.append(Path(dst).name)
            original_replace(src, dst)

        monkeypatch.setattr(mod.os, "replace", tracking_replace)
        assert mod.download_model_bundle(config_path, output_dir) == 0
        assert installed[-1] == "model.onnx"
        assert set(installed) == set(mod._REQUIRED_FILES)

    def test_partial_install_failure_leaves_no_model_onnx(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """model.onnx 以外の配置失敗時、model.onnx は出力先に残らない（完了誤判定の防止）。"""
        archive_path = tmp_path / "bundle.zip"
        _create_zip_bundle(archive_path)
        config_path = _write_config(
            tmp_path, enabled=True, model_url="https://github.com/x/model.zip", sha256=_sha256_of(archive_path)
        )
        output_dir = tmp_path / "models"

        def _fake_download(_url: str, destination: Path, _max_bytes: int, *, ssl_no_verify: bool = False) -> None:
            shutil.copy2(archive_path, destination)

        monkeypatch.setattr(mod, "_download_archive", _fake_download)

        original_replace = mod.os.replace

        def failing_replace(src: str | Path, dst: str | Path) -> None:
            if Path(dst).name == "config.json":
                raise OSError("disk full")
            original_replace(src, dst)

        monkeypatch.setattr(mod.os, "replace", failing_replace)
        with pytest.raises(OSError, match="disk full"):
            mod.download_model_bundle(config_path, output_dir)
        assert not (output_dir / "model.onnx").exists()

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

        def _fake_download(_url: str, destination: Path, _max_bytes: int, *, ssl_no_verify: bool = False) -> None:
            shutil.copy2(archive_path, destination)

        monkeypatch.setattr(mod, "_download_archive", _fake_download)

        with pytest.raises(ValueError, match="Expected exactly one tokenizer.json"):
            mod.download_model_bundle(config_path, output_dir)

    def test_verifies_sha256_when_provided(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """sha256 が設定されている場合、一致するアーカイブは通過する。"""
        archive_path = tmp_path / "bundle.zip"
        _create_zip_bundle(archive_path)
        config_path = _write_config(
            tmp_path, enabled=True, model_url="https://github.com/x/model.zip", sha256=_sha256_of(archive_path)
        )
        output_dir = tmp_path / "models"

        def _fake_download(_url: str, destination: Path, _max_bytes: int, *, ssl_no_verify: bool = False) -> None:
            shutil.copy2(archive_path, destination)

        monkeypatch.setattr(mod, "_download_archive", _fake_download)
        result = mod.download_model_bundle(config_path, output_dir)
        assert result == 0

    def test_raises_when_sha256_mismatch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """sha256 が不一致なら ValueError。"""
        archive_path = tmp_path / "bundle.zip"
        _create_zip_bundle(archive_path)
        config_path = _write_config(
            tmp_path, enabled=True, model_url="https://github.com/x/model.zip", sha256="a" * 64
        )
        output_dir = tmp_path / "models"

        def _fake_download(_url: str, destination: Path, _max_bytes: int, *, ssl_no_verify: bool = False) -> None:
            shutil.copy2(archive_path, destination)

        monkeypatch.setattr(mod, "_download_archive", _fake_download)
        with pytest.raises(ValueError, match="SHA-256 mismatch"):
            mod.download_model_bundle(config_path, output_dir)

    def test_downloads_from_arbitrary_host(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """ホスト制限がないため任意ホストの HTTPS URL でもダウンロードできる。"""
        archive_path = tmp_path / "bundle.zip"
        _create_zip_bundle(archive_path)
        config_path = _write_config(
            tmp_path,
            enabled=True,
            model_url="https://internal.corp/model.zip",
            sha256=_sha256_of(archive_path),
        )
        output_dir = tmp_path / "models"

        def _fake_download(_url: str, destination: Path, _max_bytes: int, *, ssl_no_verify: bool = False) -> None:
            shutil.copy2(archive_path, destination)

        monkeypatch.setattr(mod, "_download_archive", _fake_download)

        result = mod.download_model_bundle(config_path, output_dir)
        assert result == 0
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

        def _fake_download(_url: str, destination: Path, _max_bytes: int, *, ssl_no_verify: bool = False) -> None:
            received_ssl.append(ssl_no_verify)
            shutil.copy2(archive_path, destination)

        monkeypatch.setattr(mod, "_download_archive", _fake_download)

        result = mod.download_model_bundle(config_path, output_dir)
        assert result == 0
        assert received_ssl == [True]


class TestMain:
    """main() および _parse_args() のテスト。"""

    def test_main_exits_zero_on_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """download_model_bundle が 0 を返すと sys.exit(0)。"""
        monkeypatch.setattr(mod, "download_model_bundle", lambda *_: 0)
        monkeypatch.setattr("sys.argv", ["prog", "--config", str(tmp_path / "onnx.json"), "--out", str(tmp_path / "models")])
        with pytest.raises(SystemExit) as exc_info:
            mod.main()
        assert exc_info.value.code == 0

    def test_main_exits_three_when_disabled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """download_model_bundle が 3 を返すと sys.exit(3)。"""
        monkeypatch.setattr(mod, "download_model_bundle", lambda *_: 3)
        monkeypatch.setattr("sys.argv", ["prog", "--config", str(tmp_path / "onnx.json"), "--out", str(tmp_path / "models")])
        with pytest.raises(SystemExit) as exc_info:
            mod.main()
        assert exc_info.value.code == 3

    def test_main_exits_one_on_exception(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """download_model_bundle が例外を投げると sys.exit(1)。"""
        monkeypatch.setattr(mod, "download_model_bundle", lambda *_: (_ for _ in ()).throw(ValueError("fail")))
        monkeypatch.setattr("sys.argv", ["prog", "--config", str(tmp_path / "onnx.json"), "--out", str(tmp_path / "models")])
        with pytest.raises(SystemExit) as exc_info:
            mod.main()
        assert exc_info.value.code == 1
