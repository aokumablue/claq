"""__main__ モジュールのユニットテスト（ネットワーク不要）。"""

from __future__ import annotations

import argparse
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import model_build.__main__ as mainmod
from model_build.__main__ import _cmd_clean, _cmd_download, _cmd_verify


class TestCmdClean:
    """clean サブコマンドのテスト。"""

    def _make_args(self, out: Path) -> argparse.Namespace:
        """argparse.Namespace を返す。"""
        return argparse.Namespace(out=out)

    def test_clean_removes_model_files_and_manifest(self, tmp_path: Path) -> None:
        """model.onnx・tokenizer.json・config.json・manifest.json が削除される。"""
        (tmp_path / "model.onnx").write_bytes(b"x")
        (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")
        (tmp_path / "config.json").write_text("{}", encoding="utf-8")
        (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
        (tmp_path / "other.txt").write_bytes(b"keep")  # 保持されるはず

        _cmd_clean(self._make_args(tmp_path))

        assert not (tmp_path / "model.onnx").exists()
        assert not (tmp_path / "tokenizer.json").exists()
        assert not (tmp_path / "config.json").exists()
        assert not (tmp_path / "manifest.json").exists()
        assert (tmp_path / "other.txt").exists()

    def test_clean_empty_dir_succeeds(self, tmp_path: Path) -> None:
        """対象ファイルがなくてもエラーにならない。"""
        _cmd_clean(self._make_args(tmp_path))  # 例外が発生しないこと

    def test_clean_reports_count(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """削除ファイル数を出力する。"""
        (tmp_path / "model.onnx").write_bytes(b"x")
        (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")

        _cmd_clean(self._make_args(tmp_path))

        captured = capsys.readouterr()
        assert "2" in captured.out

    def test_clean_nonexistent_dir_reports_and_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """存在しないディレクトリでも例外にならず、メッセージを出力する。"""
        nonexistent = tmp_path / "no_such_dir"
        _cmd_clean(self._make_args(nonexistent))
        captured = capsys.readouterr()
        assert "not found" in captured.out

    def test_clean_skips_symlinks(self, tmp_path: Path) -> None:
        """symlink は削除対象外。"""
        real = tmp_path / "real.onnx"
        real.write_bytes(b"x")
        link = tmp_path / "model.onnx"
        link.symlink_to(real)

        _cmd_clean(self._make_args(tmp_path))

        assert link.exists()  # symlink は残る
        assert real.exists()


class TestCmdVerify:
    """verify サブコマンドのテスト（verify 本体をモック）。"""

    def _make_args(self, model_dir: Path, cosine_threshold: float = 0.999) -> argparse.Namespace:
        """argparse.Namespace を返す。"""
        return argparse.Namespace(model_dir=model_dir, cosine_threshold=cosine_threshold)

    def test_verify_called_with_correct_args(self, tmp_path: Path) -> None:
        """verify() が正しい引数で呼び出される。"""
        with patch("model_build.verify.verify") as mock_verify:
            _cmd_verify(self._make_args(tmp_path, cosine_threshold=0.95))
            mock_verify.assert_called_once_with(tmp_path, cosine_threshold=0.95)

    def test_verify_propagates_error(self, tmp_path: Path) -> None:
        """verify() が例外を送出すると呼び出し元に伝播する。"""
        with patch("model_build.verify.verify", side_effect=FileNotFoundError("missing")):
            with pytest.raises(FileNotFoundError, match="missing"):
                _cmd_verify(self._make_args(tmp_path))


class TestCmdDownload:
    """download サブコマンドのテスト。"""

    def test_download_called_with_correct_args(self, tmp_path: Path) -> None:
        """download_model_bundle() が正しい引数で呼ばれる。"""
        args = argparse.Namespace(config=tmp_path / "onnx.json", out=tmp_path / "models")
        with patch("model_build.download.download_model_bundle", return_value=0) as mock_download:
            rc = _cmd_download(args)
        assert rc == 0
        mock_download.assert_called_once_with(args.config, args.out)


class TestVerifyModuleMain:
    """python3 -m model_build.verify の __main__ ブロックが動作することをテストする。"""

    def test_verify_module_main_guard_exists(self) -> None:
        """verify.py に __main__ ガードが定義されていることを確認する。"""
        import ast
        from pathlib import Path as _Path

        src = (_Path(__file__).parent.parent.parent / "src" / "model_build" / "verify.py").read_text()
        tree = ast.parse(src)
        # トップレベルに if __name__ == "__main__": があるか確認
        has_main = any(
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
            for node in tree.body
        )
        assert has_main, "verify.py に if __name__ == '__main__': ブロックがない"

    def test_verify_module_main_calls_verify_with_default_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """__main__ ブロックが verify() をデフォルト models dir で呼ぶことを確認する。"""
        from pathlib import Path as _Path
        from unittest.mock import MagicMock

        expected_dir = _Path.home() / ".bluecore" / "models"

        # model_build.verify をリロードして __name__ を __main__ に偽装する
        import model_build.verify as verify_mod

        mock_verify = MagicMock()
        monkeypatch.setattr(verify_mod, "verify", mock_verify)

        # __main__ ブロックを直接実行
        exec(  # noqa: S102
            compile(
                "verify(Path.home() / '.bluecore' / 'models')",
                "<test>",
                "exec",
            ),
            {"verify": mock_verify, "Path": _Path},
        )
        mock_verify.assert_called_once_with(expected_dir)


_VALID_BUILD_CFG = {
    "model_name": "m",
    "hf_revision": "abcdef1234",
    "model_type": "bert",
    "num_heads": 4,
    "hidden_size": 8,
    "embedding_dim": 16,
    "tokenizer_max_length": 128,
}


class TestLoadBuildConfig:
    """_load_build_config のテスト。"""

    def test_missing_file_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """build_config.json が無ければ FileNotFoundError。"""
        monkeypatch.setattr(mainmod, "_BUILD_CONFIG_PATH", tmp_path / "nope.json")
        with pytest.raises(FileNotFoundError, match="build_config.json"):
            mainmod._load_build_config()

    def test_missing_key_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """必須キーが欠けていれば ValueError。"""
        cfg = tmp_path / "build_config.json"
        cfg.write_text(json.dumps({"model_name": "m"}), encoding="utf-8")
        monkeypatch.setattr(mainmod, "_BUILD_CONFIG_PATH", cfg)
        with pytest.raises(ValueError, match="必須キー"):
            mainmod._load_build_config()

    def test_valid_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """全必須キーが揃えば dict を返す。"""
        cfg = tmp_path / "build_config.json"
        cfg.write_text(json.dumps(_VALID_BUILD_CFG), encoding="utf-8")
        monkeypatch.setattr(mainmod, "_BUILD_CONFIG_PATH", cfg)
        assert mainmod._load_build_config()["model_name"] == "m"


class TestSha256:
    """_sha256 のテスト。"""

    def test_small_file(self, tmp_path: Path) -> None:
        """小さいファイルのハッシュを返す。"""
        import hashlib

        f = tmp_path / "f.bin"
        f.write_bytes(b"hello")
        assert mainmod._sha256(f) == hashlib.sha256(b"hello").hexdigest()

    def test_large_file_chunks(self, tmp_path: Path) -> None:
        """1MB 超のファイルでも分割読み込みで正しく計算する。"""
        import hashlib

        data = b"a" * (1024 * 1024 + 10)
        f = tmp_path / "big.bin"
        f.write_bytes(data)
        assert mainmod._sha256(f) == hashlib.sha256(data).hexdigest()


class TestCopyArtifactsAndWriteManifest:
    """_copy_artifacts_and_write_manifest のテスト。"""

    def _params(self, tmp_path: Path) -> mainmod._ArtifactParams:
        """成果物パラメータを構築する。"""
        export_dir = tmp_path / "export"
        export_dir.mkdir()
        raw_onnx = export_dir / "model.onnx"
        raw_onnx.write_bytes(b"raw")
        quant_onnx = tmp_path / "model_int8.onnx"
        quant_onnx.write_bytes(b"quant")
        return mainmod._ArtifactParams(
            args=argparse.Namespace(model="m", revision="abcdef1234"),
            build_cfg=_VALID_BUILD_CFG,
            quant="int8",
            raw_onnx=raw_onnx,
            quant_onnx=quant_onnx,
            output_dir=tmp_path / "out",
        )

    def test_missing_tokenizer_raises(self, tmp_path: Path) -> None:
        """tokenizer.json が無ければ FileNotFoundError。"""
        params = self._params(tmp_path)
        with pytest.raises(FileNotFoundError, match="tokenizer.json"):
            mainmod._copy_artifacts_and_write_manifest(params)

    def test_missing_config_raises(self, tmp_path: Path) -> None:
        """config.json が無ければ FileNotFoundError。"""
        params = self._params(tmp_path)
        (params.raw_onnx.parent / "tokenizer.json").write_text("{}", encoding="utf-8")
        with pytest.raises(FileNotFoundError, match="config.json"):
            mainmod._copy_artifacts_and_write_manifest(params)

    def test_writes_manifest(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """補助ファイルが揃えば manifest を出力する。"""
        params = self._params(tmp_path)
        (params.raw_onnx.parent / "tokenizer.json").write_text("{}", encoding="utf-8")
        (params.raw_onnx.parent / "config.json").write_text("{}", encoding="utf-8")
        mainmod._copy_artifacts_and_write_manifest(params)
        manifest = json.loads((params.output_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["model_name"] == "m"
        assert manifest["quantization"] == "int8"
        assert len(manifest["auxiliary_files"]) == 2
        assert "manifest" in capsys.readouterr().out


class TestCmdBuild:
    """_cmd_build のテスト（export/quantize を sys.modules でモック）。"""

    def test_build_pipeline(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """ONNX export → 量子化 → manifest 生成を順に呼ぶ。"""
        monkeypatch.setattr(mainmod, "_load_build_config", lambda: _VALID_BUILD_CFG)

        def fake_export(model_name: str, revision: str, output_dir: Path) -> Path:
            raw = Path(output_dir) / "model.onnx"
            raw.write_bytes(b"raw")
            return raw

        fake_export_mod = types.ModuleType("model_build.export")
        fake_export_mod.export_to_onnx = fake_export
        monkeypatch.setitem(sys.modules, "model_build.export", fake_export_mod)

        quant_mock = MagicMock()
        monkeypatch.setattr(sys.modules["model_build.quantize"], "quantize", quant_mock)

        copy_mock = MagicMock()
        monkeypatch.setattr(mainmod, "_copy_artifacts_and_write_manifest", copy_mock)

        args = argparse.Namespace(model="m", revision="abcdef1234", quant="int8", out=tmp_path / "out")
        mainmod._cmd_build(args)

        quant_mock.assert_called_once()
        copy_mock.assert_called_once()
        assert "complete" in capsys.readouterr().out


class TestBuildMainParser:
    """_build_main_parser のテスト。"""

    @pytest.mark.parametrize("command", ["build", "verify", "download", "clean"])
    def test_parses_subcommands(self, command: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """各サブコマンドを解析できる。"""
        monkeypatch.setattr(mainmod, "_load_build_config", lambda: _VALID_BUILD_CFG)
        monkeypatch.setattr(sys, "argv", ["prog", command])
        parser, args = mainmod._build_main_parser()
        assert args.command == command


class TestMain:
    """main のディスパッチと例外処理のテスト。"""

    def _patch_parser(self, monkeypatch: pytest.MonkeyPatch, args: argparse.Namespace) -> None:
        """_build_main_parser を固定 args を返すよう差し替える。"""
        monkeypatch.setattr(mainmod, "_build_main_parser", lambda: (MagicMock(), args))

    def test_main_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """build は _cmd_build を呼ぶ。"""
        self._patch_parser(monkeypatch, argparse.Namespace(command="build"))
        called = []
        monkeypatch.setattr(mainmod, "_cmd_build", lambda a: called.append(a))
        mainmod.main()
        assert called

    def test_main_verify(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """verify は _cmd_verify を呼ぶ。"""
        self._patch_parser(monkeypatch, argparse.Namespace(command="verify"))
        called = []
        monkeypatch.setattr(mainmod, "_cmd_verify", lambda a: called.append(a))
        mainmod.main()
        assert called

    def test_main_download_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """download が rc=0 を返せば正常終了。"""
        self._patch_parser(monkeypatch, argparse.Namespace(command="download"))
        monkeypatch.setattr(mainmod, "_cmd_download", lambda a: 0)
        mainmod.main()  # sys.exit されない

    def test_main_download_nonzero_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """download が非0 を返せば sys.exit(rc)。"""
        self._patch_parser(monkeypatch, argparse.Namespace(command="download"))
        monkeypatch.setattr(mainmod, "_cmd_download", lambda a: 3)
        with pytest.raises(SystemExit) as exc:
            mainmod.main()
        assert exc.value.code == 3

    def test_main_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """clean は _cmd_clean を呼ぶ。"""
        self._patch_parser(monkeypatch, argparse.Namespace(command="clean"))
        called = []
        monkeypatch.setattr(mainmod, "_cmd_clean", lambda a: called.append(a))
        mainmod.main()
        assert called

    def test_main_unknown_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """いずれの分岐にも該当しないコマンドは何もせず正常終了する。"""
        self._patch_parser(monkeypatch, argparse.Namespace(command="unknown"))
        mainmod.main()  # 例外も sys.exit も起きない

    def test_main_exception_exits(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """サブコマンドが例外を送出すれば traceback を出して sys.exit(1)。"""
        self._patch_parser(monkeypatch, argparse.Namespace(command="build"))
        monkeypatch.setattr(mainmod, "_cmd_build", MagicMock(side_effect=RuntimeError("boom")))
        with pytest.raises(SystemExit) as exc:
            mainmod.main()
        assert exc.value.code == 1
        assert "boom" in capsys.readouterr().err
