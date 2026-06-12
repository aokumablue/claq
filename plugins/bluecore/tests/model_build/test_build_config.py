"""build_config.json 読み込みのユニットテスト。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from model_build.__main__ import _load_build_config

_REQUIRED_KEYS = (
    "model_name", "hf_revision", "vocab_size", "source_embedding_dim", "embedding_dim",
)

_VALID_DATA = {
    "schema_version": 2,
    "model_name": "test/static-model",
    "hf_revision": "a" * 40,
    "model_type": "static_embedding",
    "vocab_size": 32768,
    "source_embedding_dim": 1024,
    "embedding_dim": 256,
}


class TestLoadBuildConfig:
    """_load_build_config のテスト。"""

    def _write_config(self, tmp_path: Path, data: dict) -> Path:
        """build_config.json を書き出して返す。"""
        p = tmp_path / "build_config.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_loads_valid_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """有効な build_config.json を正常に読み込む。"""
        p = self._write_config(tmp_path, _VALID_DATA)

        import model_build.__main__ as mm
        monkeypatch.setattr(mm, "_BUILD_CONFIG_PATH", p)

        config = _load_build_config()
        assert config["model_name"] == "test/static-model"
        assert config["vocab_size"] == 32768
        assert config["embedding_dim"] == 256

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
        data = dict(_VALID_DATA)
        del data[missing_key]
        p = self._write_config(tmp_path, data)

        import model_build.__main__ as mm
        monkeypatch.setattr(mm, "_BUILD_CONFIG_PATH", p)

        with pytest.raises(ValueError, match=missing_key):
            _load_build_config()

    def test_repo_config_is_valid(self) -> None:
        """リポジトリ同梱の build_config.json が必須キーを満たしている。"""
        config = _load_build_config()
        for key in _REQUIRED_KEYS:
            assert key in config
        assert 0 < config["embedding_dim"] <= config["source_embedding_dim"]
