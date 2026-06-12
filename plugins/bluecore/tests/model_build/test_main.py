"""__main__ モジュールのユニットテスト（ネットワーク不要）。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

import bluecore.model_build.__main__ as mainmod
from bluecore.model_build.__main__ import _cmd_build
from tests.model_build.conftest import make_safetensors

_VALID_BUILD_CFG = {
    "model_name": "m/static",
    "hf_revision": "abcdef1234",
    "model_type": "static_embedding",
    "vocab_size": 8,
    "source_embedding_dim": 6,
    "embedding_dim": 3,
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
        assert mainmod._load_build_config()["model_name"] == "m/static"


class TestWriteManifest:
    """_write_manifest のテスト。"""

    def test_writes_manifest_with_hashes(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """embeddings.npy と tokenizer.json の SHA256 を含む manifest を出力する。"""
        import hashlib

        (tmp_path / "embeddings.npy").write_bytes(b"npy-bytes")
        (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")

        mainmod._write_manifest(tmp_path, _VALID_BUILD_CFG)

        manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["model_name"] == "m/static"
        assert manifest["embedding_dim"] == 3
        assert manifest["vocab_size"] == 8
        assert manifest["embeddings_sha256"] == hashlib.sha256(b"npy-bytes").hexdigest()
        assert manifest["auxiliary_files"] == [
            {"name": "tokenizer.json", "sha256": hashlib.sha256(b"{}").hexdigest()},
        ]
        assert "manifest" in capsys.readouterr().out


class TestCmdBuild:
    """_cmd_build のテスト。"""

    def _setup_inputs(self, tmp_path: Path) -> Path:
        """ダウンロード済み相当の model.safetensors / tokenizer.json を配置する。"""
        out = tmp_path / "out"
        out.mkdir()
        table = np.arange(
            _VALID_BUILD_CFG["vocab_size"] * _VALID_BUILD_CFG["source_embedding_dim"],
            dtype=np.float32,
        ).reshape(_VALID_BUILD_CFG["vocab_size"], _VALID_BUILD_CFG["source_embedding_dim"])
        make_safetensors(out / "model.safetensors", table)
        (out / "tokenizer.json").write_text("{}", encoding="utf-8")
        return out

    def test_build_pipeline(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """抽出 → manifest 生成 → safetensors 削除を順に実行する。"""
        monkeypatch.setattr(mainmod, "_load_build_config", lambda: _VALID_BUILD_CFG)
        out = self._setup_inputs(tmp_path)

        _cmd_build(argparse.Namespace(out=out))

        loaded = np.load(out / "embeddings.npy")
        assert loaded.shape == (_VALID_BUILD_CFG["vocab_size"], _VALID_BUILD_CFG["embedding_dim"])
        assert (out / "manifest.json").exists()
        assert not (out / "model.safetensors").exists()
        assert "complete" in capsys.readouterr().out

    def test_build_missing_safetensors_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """model.safetensors がなければ FileNotFoundError。"""
        monkeypatch.setattr(mainmod, "_load_build_config", lambda: _VALID_BUILD_CFG)
        out = tmp_path / "out"
        out.mkdir()
        (out / "tokenizer.json").write_text("{}", encoding="utf-8")
        with pytest.raises(FileNotFoundError, match="model.safetensors"):
            _cmd_build(argparse.Namespace(out=out))

    def test_build_missing_tokenizer_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """tokenizer.json がなければ FileNotFoundError。"""
        monkeypatch.setattr(mainmod, "_load_build_config", lambda: _VALID_BUILD_CFG)
        out = self._setup_inputs(tmp_path)
        (out / "tokenizer.json").unlink()
        with pytest.raises(FileNotFoundError, match="tokenizer.json"):
            _cmd_build(argparse.Namespace(out=out))


class TestBuildMainParser:
    """_build_main_parser のテスト。"""

    @pytest.mark.parametrize("command", ["build"])
    def test_parses_subcommands(self, command: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """各サブコマンドを解析できる。"""
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
