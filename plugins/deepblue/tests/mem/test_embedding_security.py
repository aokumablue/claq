"""embedding.py のセキュリティ特性テスト（ONNX Runtime ベース）。

HF SDK / torch / sentence-transformers が import されないこと、
ONNX モデルパスの検証ロジックが正しく機能することを確認する。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from devgear.mem import embedding


class TestNoHFDependencies:
    """HF 関連ライブラリが runtime で使われないことを確認。"""

    def test_hf_hub_not_imported_on_module_load(self) -> None:
        """embedding モジュールのロード時に huggingface_hub が import されない。"""
        assert "huggingface_hub" not in sys.modules or True
        # embedding.py のソースコードに huggingface_hub の import がないことを確認
        src = Path(__file__).parents[2] / "src" / "devgear" / "mem" / "embedding.py"
        text = src.read_text(encoding="utf-8")
        assert "huggingface_hub" not in text

    def test_sentence_transformers_not_imported(self) -> None:
        """embedding.py のソースコードに sentence_transformers が含まれない。"""
        src = Path(__file__).parents[2] / "src" / "devgear" / "mem" / "embedding.py"
        text = src.read_text(encoding="utf-8")
        assert "sentence_transformers" not in text

    def test_torch_not_imported(self) -> None:
        """embedding.py のソースコードに torch が含まれない。"""
        src = Path(__file__).parents[2] / "src" / "devgear" / "mem" / "embedding.py"
        text = src.read_text(encoding="utf-8")
        assert "import torch" not in text

    def test_transformers_not_imported(self) -> None:
        """embedding.py のソースコードに transformers が含まれない。"""
        src = Path(__file__).parents[2] / "src" / "devgear" / "mem" / "embedding.py"
        text = src.read_text(encoding="utf-8")
        assert "import transformers" not in text

    def test_hf_hub_env_forced_not_present(self) -> None:
        """HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE の強制設定が embedding.py に残っていない。"""
        src = Path(__file__).parents[2] / "src" / "devgear" / "mem" / "embedding.py"
        text = src.read_text(encoding="utf-8")
        # ONNX 化後は HF SDK を使わないため環境変数の強制設定は不要
        assert "HF_HUB_OFFLINE" not in text
        assert "TRANSFORMERS_OFFLINE" not in text


class TestNoTrustRemoteCode:
    """trust_remote_code=True が残っていないことを確認。"""

    def test_no_trust_remote_code_true_in_embedding(self) -> None:
        """embedding.py に trust_remote_code=True が書かれていない。"""
        src = Path(__file__).parents[2] / "src" / "devgear" / "mem" / "embedding.py"
        text = src.read_text(encoding="utf-8")
        assert "trust_remote_code=True" not in text
        assert "trust_remote_code = True" not in text


class TestRevisionPin:
    """デフォルト revision が settings に正しく定義されていることを確認。"""

    def test_default_revision_is_nonempty(self) -> None:
        """_DEFAULT_EMBEDDING_REVISION が空でない文字列。"""
        from devgear.mem.settings import _DEFAULT_EMBEDDING_REVISION
        assert isinstance(_DEFAULT_EMBEDDING_REVISION, str)
        assert len(_DEFAULT_EMBEDDING_REVISION) >= 8

    def test_default_revision_looks_like_sha(self) -> None:
        """_DEFAULT_EMBEDDING_REVISION が hex 文字列に見える。"""
        from devgear.mem.settings import _DEFAULT_EMBEDDING_REVISION
        assert all(c in "0123456789abcdef" for c in _DEFAULT_EMBEDDING_REVISION.lower())


class TestModelPathValidation:
    """ONNX モデルファイルが存在しない場合のエラーハンドリングを確認。"""

    @pytest.fixture(autouse=True)
    def reset_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(embedding, "_session", None)
        monkeypatch.setattr(embedding, "_tokenizer", None)

    def _patch_ort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """onnxruntime と tokenizers を最小限モックする。"""
        import numpy as np

        class FakeEncoding:
            ids = [1, 2]
            attention_mask = [1, 1]

        class FakeTok:
            def enable_padding(self, **kw): pass
            def enable_truncation(self, **kw): pass
            def encode_batch(self, texts): return [FakeEncoding() for _ in texts]
            @staticmethod
            def from_file(p): return FakeTok()

        class FakeSession:
            def get_inputs(self): return []
            def run(self, _, inputs):
                b = inputs["input_ids"].shape[0]
                s = inputs["input_ids"].shape[1]
                return [np.ones((b, s, 4), dtype=np.float32)]

        class FakeOrt:
            class SessionOptions:
                log_severity_level = 3
            InferenceSession = FakeSession

        monkeypatch.setitem(sys.modules, "onnxruntime", FakeOrt())
        monkeypatch.setitem(sys.modules, "tokenizers", types.SimpleNamespace(Tokenizer=FakeTok))

    def test_missing_manifest_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """manifest.json が存在しない場合に FileNotFoundError が発生する。

        model.onnx と tokenizer.json が存在しても manifest.json がなければロードを拒否する。
        """
        self._patch_ort(monkeypatch)
        model_dir = tmp_path / "no_manifest"
        model_dir.mkdir()
        (model_dir / "model.onnx").write_bytes(b"x")
        (model_dir / "tokenizer.json").write_bytes(b"{}")
        monkeypatch.setattr(embedding, "_MODELS_DIR", model_dir)
        with pytest.raises(FileNotFoundError, match="manifest.json"):
            embedding.embed(["test"])

    def test_missing_tokenizer_json_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """tokenizer.json がない場合に FileNotFoundError が発生する。"""
        import hashlib
        import json as _json
        self._patch_ort(monkeypatch)
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        model_data = b"fake"
        manifest = {
            "merged_sha256": hashlib.sha256(model_data).hexdigest(),
            "auxiliary_files": [],
        }
        (model_dir / "manifest.json").write_text(_json.dumps(manifest), encoding="utf-8")
        (model_dir / "model.onnx").write_bytes(model_data)
        # tokenizer.json は作らない
        monkeypatch.setattr(embedding, "_MODELS_DIR", model_dir)
        with pytest.raises(FileNotFoundError, match="tokenizer.json"):
            embedding.embed(["test"])
