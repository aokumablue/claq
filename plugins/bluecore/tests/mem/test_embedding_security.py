"""embedding.py のセキュリティ特性テスト（静的埋め込みテーブルベース）。

HF SDK / torch / sentence-transformers / onnxruntime が import されないこと、
モデルファイルの検証ロジックが正しく機能することを確認する。
"""

from __future__ import annotations

import types
from pathlib import Path

import numpy as np
import pytest

from bluecore.mem import embedding


def _embedding_source() -> str:
    """embedding.py のソースコードを返す。"""
    src = Path(__file__).parents[2] / "src" / "bluecore" / "mem" / "embedding.py"
    return src.read_text(encoding="utf-8")


class TestNoHeavyDependencies:
    """重量級ライブラリが runtime で使われないことを確認。"""

    def test_hf_hub_not_imported(self) -> None:
        """embedding.py のソースコードに huggingface_hub が含まれない。"""
        assert "huggingface_hub" not in _embedding_source()

    def test_sentence_transformers_not_imported(self) -> None:
        """embedding.py のソースコードに sentence_transformers が含まれない。"""
        assert "sentence_transformers" not in _embedding_source()

    def test_torch_not_imported(self) -> None:
        """embedding.py のソースコードに torch が含まれない。"""
        assert "import torch" not in _embedding_source()

    def test_transformers_not_imported(self) -> None:
        """embedding.py のソースコードに transformers が含まれない。"""
        assert "import transformers" not in _embedding_source()

    def test_onnxruntime_not_imported(self) -> None:
        """embedding.py のソースコードに onnxruntime の import がない（静的テーブル化済み）。"""
        assert "import onnxruntime" not in _embedding_source()

    def test_hf_hub_env_forced_not_present(self) -> None:
        """HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE の強制設定が embedding.py に残っていない。"""
        text = _embedding_source()
        assert "HF_HUB_OFFLINE" not in text
        assert "TRANSFORMERS_OFFLINE" not in text


class TestNoTrustRemoteCode:
    """trust_remote_code=True が残っていないことを確認。"""

    def test_no_trust_remote_code_true_in_embedding(self) -> None:
        """embedding.py に trust_remote_code=True が書かれていない。"""
        text = _embedding_source()
        assert "trust_remote_code=True" not in text
        assert "trust_remote_code = True" not in text


class TestRevisionPin:
    """デフォルト revision が settings に正しく定義されていることを確認。"""

    def test_default_revision_is_nonempty(self) -> None:
        """_DEFAULT_EMBEDDING_REVISION が空でない文字列。"""
        from bluecore.mem.settings import _DEFAULT_EMBEDDING_REVISION
        assert isinstance(_DEFAULT_EMBEDDING_REVISION, str)
        assert len(_DEFAULT_EMBEDDING_REVISION) >= 8

    def test_default_revision_looks_like_sha(self) -> None:
        """_DEFAULT_EMBEDDING_REVISION が hex 文字列に見える。"""
        from bluecore.mem.settings import _DEFAULT_EMBEDDING_REVISION
        assert all(c in "0123456789abcdef" for c in _DEFAULT_EMBEDDING_REVISION.lower())


class TestModelPathValidation:
    """モデルファイルが存在しない場合のエラーハンドリングを確認。"""

    @pytest.fixture(autouse=True)
    def reset_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(embedding, "_table", None)
        monkeypatch.setattr(embedding, "_tokenizer", None)

    def _patch_tokenizers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """tokenizers を最小限モックする。"""

        class FakeEncoding:
            ids = [0, 1]

        class FakeTok:
            def encode_batch(self, texts, add_special_tokens=True):
                return [FakeEncoding() for _ in texts]

            @staticmethod
            def from_file(p):
                return FakeTok()

        import sys

        monkeypatch.setitem(sys.modules, "tokenizers", types.SimpleNamespace(Tokenizer=FakeTok))

    def test_missing_manifest_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """manifest.json が存在しない場合に FileNotFoundError が発生する。

        embeddings.npy と tokenizer.json が存在しても manifest.json がなければロードを拒否する。
        """
        self._patch_tokenizers(monkeypatch)
        model_dir = tmp_path / "no_manifest"
        model_dir.mkdir()
        np.save(str(model_dir / "embeddings.npy"), np.zeros((2, 2), dtype=np.float32))
        (model_dir / "tokenizer.json").write_bytes(b"{}")
        monkeypatch.setattr(embedding, "_MODELS_DIR", model_dir)
        with pytest.raises(FileNotFoundError, match="manifest.json"):
            embedding.embed(["test"])

    def test_missing_tokenizer_json_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """tokenizer.json がない場合に FileNotFoundError が発生する。"""
        import hashlib
        import json as _json

        self._patch_tokenizers(monkeypatch)
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        npy_path = model_dir / "embeddings.npy"
        np.save(str(npy_path), np.zeros((2, 2), dtype=np.float32))
        manifest = {
            "embeddings_sha256": hashlib.sha256(npy_path.read_bytes()).hexdigest(),
            "auxiliary_files": [],
        }
        (model_dir / "manifest.json").write_text(_json.dumps(manifest), encoding="utf-8")
        # tokenizer.json は作らない
        monkeypatch.setattr(embedding, "_MODELS_DIR", model_dir)
        with pytest.raises(FileNotFoundError, match="tokenizer.json"):
            embedding.embed(["test"])
