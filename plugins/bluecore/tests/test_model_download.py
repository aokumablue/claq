"""bluecore.model_download のユニットテスト。"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import bluecore.model_download as mod


@pytest.fixture(autouse=True)
def _isolate_warning_marker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """警告マーカーの書き込み先を実 HOME からテスト用ディレクトリへ隔離する。"""
    monkeypatch.setattr(mod, "_WARNING_MARKER", tmp_path / "marker" / "model_download_warning")


def _sha256_of(data: bytes) -> str:
    """バイト列の SHA-256 ダイジェストを返す。"""
    return hashlib.sha256(data).hexdigest()


_FILE_CONTENTS: dict[str, bytes] = {
    "model.safetensors": b"safetensors-bytes",
    "tokenizer.json": b"{}",
}


def _default_files(base_url: str = "https://huggingface.co/x/resolve/rev") -> list[dict]:
    """テスト用の files エントリを返す。"""
    return [
        {"name": name, "url": f"{base_url}/{name}", "sha256": _sha256_of(content)}
        for name, content in _FILE_CONTENTS.items()
    ]


def _write_config(
    tmp_path: Path,
    *,
    enabled: bool,
    files: list[dict] | None = None,
    max_download_bytes: int = mod._DEFAULT_MAX_DOWNLOAD_BYTES,
    ssl_no_verify: bool | None = None,
) -> Path:
    """model.json を作成して返す。"""
    config_path = tmp_path / "model.json"
    download_section: dict = {
        "enabled": enabled,
        "files": _default_files() if files is None else files,
        "max_download_bytes": max_download_bytes,
    }
    if ssl_no_verify is not None:
        download_section["ssl_no_verify"] = ssl_no_verify
    config_path.write_text(
        json.dumps({"model": {"download": download_section}}),
        encoding="utf-8",
    )
    return config_path


def _fake_download_factory(contents: dict[str, bytes] | None = None):
    """URL 末尾のファイル名に応じた内容を書き込む fake _download_file を返す。"""
    table = _FILE_CONTENTS if contents is None else contents

    def _fake_download(url: str, dest_path: Path, _max_bytes: int, *, ssl_no_verify: bool = False) -> None:
        dest_path.write_bytes(table[url.rsplit("/", 1)[-1]])

    return _fake_download


class TestValidatingRedirectHandler:
    """_ValidatingRedirectHandler のテスト。"""

    def test_accepts_https_redirect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTPS リダイレクトは super に委譲する。"""
        called: list[str] = []

        def fake_super(self: object, req: object, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
            called.append(newurl)

        monkeypatch.setattr(urllib.request.HTTPRedirectHandler, "redirect_request", fake_super)
        handler = mod._ValidatingRedirectHandler()
        handler.redirect_request(None, None, 301, "Moved", {}, "https://huggingface.co/file.safetensors")
        assert called == ["https://huggingface.co/file.safetensors"]

    def test_accepts_http_redirect_with_warning_when_opted_in(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """初回 URL が http（明示オプトイン）なら HTTP リダイレクトも警告付きで通過する。"""
        called: list[str] = []

        def fake_super(self: object, req: object, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
            called.append(newurl)

        monkeypatch.setattr(urllib.request.HTTPRedirectHandler, "redirect_request", fake_super)
        handler = mod._ValidatingRedirectHandler(allow_http=True)
        handler.redirect_request(None, None, 301, "Moved", {}, "http://internal.corp/file.safetensors")
        assert called == ["http://internal.corp/file.safetensors"]
        assert "WARNING" in capsys.readouterr().out

    def test_rejects_https_to_http_downgrade_redirect(self) -> None:
        """初回 URL が https の場合、平文 HTTP へのダウングレードリダイレクトを拒否する。"""
        handler = mod._ValidatingRedirectHandler(allow_http=False)
        with pytest.raises(ValueError, match="Refusing redirect downgrade"):
            handler.redirect_request(None, None, 301, "Moved", {}, "http://evil.example/file.safetensors")

    def test_rejects_ftp_redirect(self) -> None:
        """FTP など http/https 以外のリダイレクトは拒否される。"""
        handler = mod._ValidatingRedirectHandler()
        with pytest.raises(ValueError, match="HTTPS or HTTP scheme"):
            handler.redirect_request(None, None, 301, "Moved", {}, "ftp://huggingface.co/file.safetensors")


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
        mod._validate_url("https://huggingface.co/x/resolve/rev/model.safetensors")

    def test_accepts_arbitrary_host(self) -> None:
        """ホスト制限はないため任意ホストの HTTPS URL も通過する。"""
        mod._validate_url("https://example.invalid/file.safetensors")

    def test_accepts_http_with_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        """HTTP は通過するが平文警告を出す。"""
        mod._validate_url("http://internal.corp/file.safetensors")
        assert "WARNING" in capsys.readouterr().out

    def test_accepts_ip_address_with_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        """IP アドレス指定は通過するが警告を出す。"""
        mod._validate_url("https://192.168.1.1/file.safetensors")
        assert "WARNING" in capsys.readouterr().out

    def test_accepts_ipv6_with_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        """IPv6 アドレス指定は通過するが警告を出す。"""
        mod._validate_url("https://[::1]/file.safetensors")
        assert "WARNING" in capsys.readouterr().out

    def test_rejects_ftp(self) -> None:
        """http/https 以外の scheme は拒否される。"""
        with pytest.raises(ValueError, match="HTTPS or HTTP scheme"):
            mod._validate_url("ftp://huggingface.co/file.safetensors")

    def test_rejects_empty_host(self) -> None:
        """ホストなし URL は拒否される。"""
        with pytest.raises(ValueError, match="no valid hostname"):
            mod._validate_url("https:///path/file.safetensors")


class TestVerifyFileSha256:
    """_verify_file_sha256 のテスト。"""

    def test_passes_when_sha256_matches(self, tmp_path: Path) -> None:
        """SHA-256 が一致すれば例外なし。"""
        data = b"test file content"
        path = tmp_path / "file.bin"
        path.write_bytes(data)
        mod._verify_file_sha256(path, _sha256_of(data), "file.bin")

    def test_raises_when_sha256_mismatch(self, tmp_path: Path) -> None:
        """SHA-256 が不一致なら ValueError。"""
        path = tmp_path / "file.bin"
        path.write_bytes(b"real content")
        with pytest.raises(ValueError, match="SHA-256 mismatch"):
            mod._verify_file_sha256(path, "a" * 64, "file.bin")


class TestParseFileSpec:
    """_parse_file_spec のテスト。"""

    def test_parses_valid_entry(self) -> None:
        """name/url/sha256 が揃ったエントリを _FileSpec に変換する。"""
        spec = mod._parse_file_spec({"name": "model.safetensors", "url": "https://x/m", "sha256": "a" * 64})
        assert spec == mod._FileSpec(name="model.safetensors", url="https://x/m", sha256="a" * 64)

    @pytest.mark.parametrize("missing", ["name", "url", "sha256"])
    def test_rejects_missing_field(self, missing: str) -> None:
        """name/url/sha256 のいずれかが欠落していれば ValueError。"""
        entry = {"name": "f.bin", "url": "https://x/f", "sha256": "a" * 64}
        del entry[missing]
        with pytest.raises(ValueError, match="requires name/url/sha256"):
            mod._parse_file_spec(entry)

    @pytest.mark.parametrize("bad_name", ["../evil.bin", "sub/dir.bin", "/abs.bin", ".", ".."])
    def test_rejects_path_traversal_name(self, bad_name: str) -> None:
        """パス区切りや相対参照を含む name は拒否される。"""
        with pytest.raises(ValueError, match="bare filename"):
            mod._parse_file_spec({"name": bad_name, "url": "https://x/f", "sha256": "a" * 64})


class TestLoadDownloadSettings:
    """_load_download_settings のテスト。"""

    def test_returns_disabled_when_file_missing(self, tmp_path: Path) -> None:
        """設定ファイルがない場合は disabled を返す。"""
        settings = mod._load_download_settings(tmp_path / "missing.json")
        assert settings.enabled is False
        assert settings.files == ()
        assert settings.max_download_bytes == mod._DEFAULT_MAX_DOWNLOAD_BYTES
        assert settings.ssl_no_verify is False

    def test_reads_all_fields(self, tmp_path: Path) -> None:
        """全フィールドを正しく読み取る。"""
        config_path = _write_config(tmp_path, enabled=True, max_download_bytes=100)
        settings = mod._load_download_settings(config_path)
        assert settings.enabled is True
        assert [f.name for f in settings.files] == ["model.safetensors", "tokenizer.json"]
        assert settings.max_download_bytes == 100
        assert settings.ssl_no_verify is False

    def test_uses_defaults_when_size_field_absent(self, tmp_path: Path) -> None:
        """max_download_bytes がなければデフォルト値を使う。"""
        config_path = tmp_path / "model.json"
        config_path.write_text(
            json.dumps({"model": {"download": {"enabled": True, "files": _default_files()}}}),
            encoding="utf-8",
        )
        settings = mod._load_download_settings(config_path)
        assert settings.max_download_bytes == mod._DEFAULT_MAX_DOWNLOAD_BYTES

    def test_reads_ssl_no_verify(self, tmp_path: Path) -> None:
        """ssl_no_verify: true が True として返る。"""
        config_path = _write_config(tmp_path, enabled=True, ssl_no_verify=True)
        assert mod._load_download_settings(config_path).ssl_no_verify is True

    def test_defaults_ssl_no_verify_false_when_absent(self, tmp_path: Path) -> None:
        """ssl_no_verify フィールドがない場合は False を返す。"""
        config_path = _write_config(tmp_path, enabled=True)
        assert mod._load_download_settings(config_path).ssl_no_verify is False

    @pytest.mark.parametrize("bad_value", ["false", "true", "0", 1, [], {}])
    def test_rejects_non_bool_ssl_no_verify(self, tmp_path: Path, bad_value: object) -> None:
        """ssl_no_verify が JSON boolean でなければ fail-closed で拒否する。"""
        config_path = tmp_path / "model.json"
        config_path.write_text(
            json.dumps(
                {"model": {"download": {"enabled": True, "files": _default_files(), "ssl_no_verify": bad_value}}}
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="ssl_no_verify must be a JSON boolean"):
            mod._load_download_settings(config_path)

    def test_rejects_non_bool_enabled(self, tmp_path: Path) -> None:
        """enabled が文字列 \"false\"（truthy）の場合も拒否する。"""
        config_path = tmp_path / "model.json"
        config_path.write_text(
            json.dumps({"model": {"download": {"enabled": "false", "files": _default_files()}}}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="enabled must be a JSON boolean"):
            mod._load_download_settings(config_path)


class TestDownloadFileSizeLimit:
    """_download_file のサイズ上限テスト。"""

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
                mod._download_file(
                    "https://huggingface.co/x",
                    tmp_path / "out.bin",
                    mod._CHUNK_SIZE,
                )

    def test_empty_response_exits_loop_immediately(self, tmp_path: Path) -> None:
        """空レスポンスは while ループを即時終了し空ファイルを作成する。"""
        out_file = tmp_path / "out.bin"
        mock_opener = self._make_mock_opener(b"")
        with patch("urllib.request.build_opener", return_value=mock_opener):
            mod._download_file("https://huggingface.co/x", out_file, 1024)
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
                mod._download_file("https://huggingface.co/x", tmp_path / "out.bin", 1024)

    def test_ssl_no_verify_adds_https_handler(self, tmp_path: Path) -> None:
        """ssl_no_verify=True のとき HTTPSHandler が opener に渡される。"""
        out_file = tmp_path / "out.bin"
        mock_opener = self._make_mock_opener(b"")
        with patch("urllib.request.build_opener", return_value=mock_opener) as mock_build:
            mod._download_file("https://huggingface.co/x", out_file, 1024, ssl_no_verify=True)
        handlers = mock_build.call_args[0]
        assert any(isinstance(h, urllib.request.HTTPSHandler) for h in handlers)


class TestDownloadModelFiles:
    """download_model_files のテスト。"""

    def test_returns_zero_when_built_model_exists(self, tmp_path: Path) -> None:
        """embeddings.npy が既にある場合は即時 0。"""
        output_dir = tmp_path / "models"
        output_dir.mkdir()
        (output_dir / "embeddings.npy").write_bytes(b"built")
        result = mod.download_model_files(tmp_path / "any.json", output_dir)
        assert result == 0

    def test_returns_three_when_config_missing(self, tmp_path: Path) -> None:
        """設定ファイル未作成なら 3。"""
        output_dir = tmp_path / "models"
        result = mod.download_model_files(tmp_path / "missing.json", output_dir)
        assert result == 3

    def test_returns_three_when_disabled(self, tmp_path: Path) -> None:
        """enabled=false なら 3。"""
        config_path = _write_config(tmp_path, enabled=False)
        output_dir = tmp_path / "models"
        result = mod.download_model_files(config_path, output_dir)
        assert result == 3

    def test_raises_when_enabled_without_files(self, tmp_path: Path) -> None:
        """enabled=true かつ files 空なら ValueError。"""
        config_path = _write_config(tmp_path, enabled=True, files=[])
        output_dir = tmp_path / "models"
        with pytest.raises(ValueError, match="files is empty"):
            mod.download_model_files(config_path, output_dir)

    def test_downloads_all_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """全ファイルをダウンロードして配置する。"""
        config_path = _write_config(tmp_path, enabled=True)
        output_dir = tmp_path / "models"
        monkeypatch.setattr(mod, "_download_file", _fake_download_factory())

        result = mod.download_model_files(config_path, output_dir)
        assert result == 0
        for name, content in _FILE_CONTENTS.items():
            assert (output_dir / name).read_bytes() == content

    def test_skips_file_with_matching_sha256(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """SHA-256 が一致する既存ファイルは再ダウンロードしない。"""
        config_path = _write_config(tmp_path, enabled=True)
        output_dir = tmp_path / "models"
        output_dir.mkdir()
        (output_dir / "model.safetensors").write_bytes(_FILE_CONTENTS["model.safetensors"])

        downloaded: list[str] = []

        def _tracking_download(url: str, dest_path: Path, _max_bytes: int, *, ssl_no_verify: bool = False) -> None:
            downloaded.append(url.rsplit("/", 1)[-1])
            dest_path.write_bytes(_FILE_CONTENTS[url.rsplit("/", 1)[-1]])

        monkeypatch.setattr(mod, "_download_file", _tracking_download)
        result = mod.download_model_files(config_path, output_dir)
        assert result == 0
        assert downloaded == ["tokenizer.json"]

    def test_redownloads_file_with_stale_sha256(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """SHA-256 が一致しない既存ファイルは上書きダウンロードする。"""
        config_path = _write_config(tmp_path, enabled=True)
        output_dir = tmp_path / "models"
        output_dir.mkdir()
        (output_dir / "model.safetensors").write_bytes(b"stale-content")
        monkeypatch.setattr(mod, "_download_file", _fake_download_factory())

        result = mod.download_model_files(config_path, output_dir)
        assert result == 0
        assert (output_dir / "model.safetensors").read_bytes() == _FILE_CONTENTS["model.safetensors"]

    def test_raises_when_sha256_mismatch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """ダウンロード内容の SHA-256 が不一致なら ValueError。"""
        files = [{"name": "model.safetensors", "url": "https://x/model.safetensors", "sha256": "a" * 64}]
        config_path = _write_config(tmp_path, enabled=True, files=files)
        output_dir = tmp_path / "models"
        monkeypatch.setattr(mod, "_download_file", _fake_download_factory())

        with pytest.raises(ValueError, match="SHA-256 mismatch"):
            mod.download_model_files(config_path, output_dir)

    def test_failure_leaves_no_partial_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """2 ファイル目の検証失敗時、1 ファイル目も出力先に配置されない。"""
        files = _default_files()
        files[1]["sha256"] = "a" * 64  # tokenizer.json の検証を必ず失敗させる
        config_path = _write_config(tmp_path, enabled=True, files=files)
        output_dir = tmp_path / "models"
        monkeypatch.setattr(mod, "_download_file", _fake_download_factory())

        with pytest.raises(ValueError, match="SHA-256 mismatch"):
            mod.download_model_files(config_path, output_dir)
        assert not (output_dir / "model.safetensors").exists()
        assert not (output_dir / "tokenizer.json").exists()

    def test_downloads_from_arbitrary_host(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """ホスト制限がないため社内サーバ URL でもダウンロードできる。"""
        files = _default_files(base_url="https://internal.corp/models")
        config_path = _write_config(tmp_path, enabled=True, files=files)
        output_dir = tmp_path / "models"
        monkeypatch.setattr(mod, "_download_file", _fake_download_factory())

        result = mod.download_model_files(config_path, output_dir)
        assert result == 0
        for name in _FILE_CONTENTS:
            assert (output_dir / name).exists()

    def test_downloads_with_ssl_no_verify_true(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """ssl_no_verify=true が _download_file に伝わる。"""
        config_path = _write_config(tmp_path, enabled=True, ssl_no_verify=True)
        output_dir = tmp_path / "models"

        received_ssl: list[bool] = []

        def _tracking_download(url: str, dest_path: Path, _max_bytes: int, *, ssl_no_verify: bool = False) -> None:
            received_ssl.append(ssl_no_verify)
            dest_path.write_bytes(_FILE_CONTENTS[url.rsplit("/", 1)[-1]])

        monkeypatch.setattr(mod, "_download_file", _tracking_download)
        result = mod.download_model_files(config_path, output_dir)
        assert result == 0
        assert received_ssl == [True, True]


class TestMain:
    """main() および _parse_args() のテスト。"""

    def test_main_exits_zero_on_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """download_model_files が 0 を返すと sys.exit(0)。"""
        monkeypatch.setattr(mod, "download_model_files", lambda *_: 0)
        monkeypatch.setattr("sys.argv", ["prog", "--config", str(tmp_path / "model.json"), "--out", str(tmp_path / "models")])
        with pytest.raises(SystemExit) as exc_info:
            mod.main()
        assert exc_info.value.code == 0

    def test_main_exits_three_when_disabled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """download_model_files が 3 を返すと sys.exit(3)。"""
        monkeypatch.setattr(mod, "download_model_files", lambda *_: 3)
        monkeypatch.setattr("sys.argv", ["prog", "--config", str(tmp_path / "model.json"), "--out", str(tmp_path / "models")])
        with pytest.raises(SystemExit) as exc_info:
            mod.main()
        assert exc_info.value.code == 3

    def test_main_exits_one_on_exception(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """download_model_files が例外を投げると sys.exit(1)。"""
        monkeypatch.setattr(mod, "download_model_files", lambda *_: (_ for _ in ()).throw(ValueError("fail")))
        monkeypatch.setattr("sys.argv", ["prog", "--config", str(tmp_path / "model.json"), "--out", str(tmp_path / "models")])
        with pytest.raises(SystemExit) as exc_info:
            mod.main()
        assert exc_info.value.code == 1
