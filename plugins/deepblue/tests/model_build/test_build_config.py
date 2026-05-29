"""build_config.json 読み込みのユニットテスト。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from model_build.__main__ import _load_build_config

_REQUIRED_KEYS = (
    "model_name", "hf_revision", "model_type",
    "num_heads", "hidden_size", "embedding_dim", "tokenizer_max_length",
)


class TestLoadBuildConfig:
    """_load_build_config のテスト。"""

    def _write_config(self, tmp_path: Path, data: dict) -> Path:
        """build_config.json を書き出して返す。"""
        p = tmp_path / "build_config.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_loads_valid_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """有効な build_config.json を正常に読み込む。"""
        data = {
            "schema_version": 1,
            "model_name": "test/model",
            "hf_revision": "a" * 40,
            "model_type": "bert",
            "num_heads": 16,
            "hidden_size": 1024,
            "embedding_dim": 768,
            "tokenizer_max_length": 512,
        }
        p = self._write_config(tmp_path, data)

        import model_build.__main__ as mm
        monkeypatch.setattr(mm, "_BUILD_CONFIG_PATH", p)

        config = _load_build_config()
        assert config["model_name"] == "test/model"
        assert config["num_heads"] == 16
        assert config["embedding_dim"] == 768

    def test_missing_file_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """ファイルが存在しないと FileNotFoundError。"""
        import model_build.__main__ as mm
        monkeypatch.setattr(mm, "_BUILD_CONFIG_PATH", tmp_path / "no_config.json")

        with pytest.raises(FileNotFoundError, match="build_config.json"):
            _load_build_config()

    @pytest.mark.parametrize("missing_key", _REQUIRED_KEYS)
    def test_missing_required_key_raises(
        self, tmp_path: Path, missing_key: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """必須キーが欠落した場合 ValueError。"""
        data = {
            "schema_version": 1,
            "model_name": "test/model",
            "hf_revision": "a" * 40,
            "model_type": "bert",
            "num_heads": 16,
            "hidden_size": 1024,
            "embedding_dim": 768,
            "tokenizer_max_length": 512,
        }
        del data[missing_key]
        p = self._write_config(tmp_path, data)

        import model_build.__main__ as mm
        monkeypatch.setattr(mm, "_BUILD_CONFIG_PATH", p)

        with pytest.raises(ValueError, match=missing_key):
            _load_build_config()
