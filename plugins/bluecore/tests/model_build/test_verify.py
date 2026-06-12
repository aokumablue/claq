"""verify モジュールのユニットテスト（ネットワーク不要）。"""

from __future__ import annotations

import json
import math
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

import model_build.verify as verifymod
from model_build.verify import (
    _check_dim,
    _check_l2_norm,
    _check_reproducibility,
    _check_semantics,
    _cosine_similarity,
    _infer_embedding,
    _run_inference_check,
    main,
    verify,
)


def _make_tokenizer(ids_by_text: dict[str, list[int]] | None = None, default_ids: list[int] | None = None) -> MagicMock:
    """テキストごとに指定 ids を返すトークナイザモックを作成する。"""
    tok = MagicMock()

    def encode(text: str, add_special_tokens: bool = True) -> MagicMock:
        enc = MagicMock()
        if ids_by_text is not None and text in ids_by_text:
            enc.ids = ids_by_text[text]
        else:
            enc.ids = [0, 1] if default_ids is None else default_ids
        return enc

    tok.encode.side_effect = encode
    return tok


class TestCosineSimilarity:
    """_cosine_similarity のテスト。"""

    def test_identical_vectors(self) -> None:
        """同一ベクトルのコサイン類似度は 1.0。"""
        v = [1.0, 0.0, 0.0]
        assert _cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self) -> None:
        """直交ベクトルのコサイン類似度は 0.0。"""
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert _cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self) -> None:
        """逆向きベクトルのコサイン類似度は -1.0。"""
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert _cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_zero_vector_returns_zero(self) -> None:
        """零ベクトルが含まれる場合は 0.0 を返す。"""
        a = [0.0, 0.0]
        b = [1.0, 0.0]
        assert _cosine_similarity(a, b) == 0.0


class TestInferEmbedding:
    """_infer_embedding のテスト。"""

    def test_mean_pooling_and_l2_norm(self) -> None:
        """トークン埋め込みの平均を L2 正規化して返す。"""
        table = np.array([[2.0, 0.0], [0.0, 2.0]], dtype=np.float32)
        tok = _make_tokenizer(default_ids=[0, 1])

        result = _infer_embedding(table, tok, "text")

        # 平均 (1.0, 1.0) → 正規化 (1/√2, 1/√2)
        assert result == pytest.approx([1.0 / math.sqrt(2)] * 2)

    def test_uses_add_special_tokens_false(self) -> None:
        """encode は add_special_tokens=False で呼ばれる（StaticEmbedding 仕様）。"""
        table = np.eye(2, dtype=np.float32)
        tok = _make_tokenizer(default_ids=[0])
        _infer_embedding(table, tok, "text")
        assert tok.encode.call_args.kwargs == {"add_special_tokens": False}

    def test_empty_ids_returns_zero_vector(self) -> None:
        """トークンが得られない場合はゼロベクトルを返す。"""
        table = np.eye(3, dtype=np.float32)
        tok = _make_tokenizer(default_ids=[])
        assert _infer_embedding(table, tok, "") == [0.0, 0.0, 0.0]

    def test_zero_norm_returns_zero_vector(self) -> None:
        """埋め込みの平均が零ベクトルの場合もゼロベクトルを返す。"""
        table = np.zeros((2, 3), dtype=np.float32)
        tok = _make_tokenizer(default_ids=[0, 1])
        assert _infer_embedding(table, tok, "text") == [0.0, 0.0, 0.0]


class TestCheckDim:
    """_check_dim のテスト。"""

    def test_correct_dim(self, capsys: pytest.CaptureFixture) -> None:
        """期待次元と一致すれば例外なし。"""
        vectors = [[0.1] * 256, [0.2] * 256]
        _check_dim(vectors, 256)
        captured = capsys.readouterr()
        assert "OK" in captured.out

    def test_wrong_dim_raises(self) -> None:
        """期待次元と異なれば ValueError。"""
        vectors = [[0.1] * 512]
        with pytest.raises(ValueError, match="次元数不一致"):
            _check_dim(vectors, 256)


class TestCheckL2Norm:
    """_check_l2_norm のテスト。"""

    def test_unit_vectors_pass(self, capsys: pytest.CaptureFixture) -> None:
        """L2 norm ≈ 1.0 のベクトルは通過する。"""
        v = [1.0 / math.sqrt(256)] * 256
        _check_l2_norm([v])
        captured = capsys.readouterr()
        assert "OK" in captured.out

    def test_non_unit_vector_raises(self) -> None:
        """L2 norm が 1.0 から外れたベクトルは ValueError。"""
        v = [1.0] * 256  # norm = 16
        with pytest.raises(ValueError, match="L2 ノルム不正"):
            _check_l2_norm([v])


class TestCheckReproducibility:
    """_check_reproducibility のテスト。"""

    def test_identical_output_passes(self, capsys: pytest.CaptureFixture) -> None:
        """2 回同じ推論結果なら再現性チェック通過。"""
        table = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
        tok = _make_tokenizer(default_ids=[0])
        ref_vec = _infer_embedding(table, tok, "test text")

        _check_reproducibility(table, tok, "test text", ref_vec, threshold=0.999)
        captured = capsys.readouterr()
        assert "OK" in captured.out

    def test_diverged_output_raises(self) -> None:
        """cosine 類似度が閾値未満なら ValueError。"""
        table = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
        tok = _make_tokenizer(default_ids=[0])
        ref_vec = [0.0, 1.0]  # 推論結果 (1.0, 0.0) と直交

        with pytest.raises(ValueError, match="再現性チェック失敗"):
            _check_reproducibility(table, tok, "text", ref_vec, threshold=0.999)


class TestCheckSemantics:
    """_check_semantics のテスト。"""

    def test_similar_pair_above_dissimilar_passes(self, capsys: pytest.CaptureFixture) -> None:
        """類似ペアの cosine が非類似ペアを上回れば通過する。"""
        table = np.array([[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]], dtype=np.float32)
        tok = _make_tokenizer(ids_by_text={
            verifymod._SIMILAR_PAIR[0]: [0],
            verifymod._SIMILAR_PAIR[1]: [1],
            verifymod._DISSIMILAR_TEXT: [2],
        })
        _check_semantics(table, tok)
        assert "OK" in capsys.readouterr().out

    def test_similar_pair_below_dissimilar_raises(self) -> None:
        """類似ペアの cosine が非類似ペア以下なら ValueError。"""
        table = np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]], dtype=np.float32)
        tok = _make_tokenizer(ids_by_text={
            verifymod._SIMILAR_PAIR[0]: [0],
            verifymod._SIMILAR_PAIR[1]: [1],
            verifymod._DISSIMILAR_TEXT: [2],
        })
        with pytest.raises(ValueError, match="意味的サニティチェック失敗"):
            _check_semantics(table, tok)


class TestVerify:
    """verify のエンドツーエンド分岐テスト。"""

    def test_manifest_missing_raises(self, tmp_path: Path) -> None:
        """manifest.json が無ければ FileNotFoundError。"""
        with pytest.raises(FileNotFoundError, match="manifest.json"):
            verify(tmp_path)

    def test_embeddings_sha_mismatch_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """embeddings.npy の SHA256 が不一致なら ValueError。"""
        manifest = {"embeddings_sha256": "a" * 64, "auxiliary_files": []}
        (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (tmp_path / "embeddings.npy").write_bytes(b"x")
        monkeypatch.setattr(verifymod, "_validate_sha256_format", lambda *a: None)
        monkeypatch.setattr(verifymod, "_sha256_file", lambda p: "b" * 64)
        with pytest.raises(ValueError, match="embeddings.npy SHA256 不一致"):
            verify(tmp_path)

    def test_aux_sha_mismatch_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """補助ファイルの SHA256 が不一致なら ValueError。"""
        manifest = {
            "embeddings_sha256": "m" * 64,
            "auxiliary_files": [{"name": "tokenizer.json", "sha256": "t" * 64}],
        }
        (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (tmp_path / "embeddings.npy").write_bytes(b"x")
        (tmp_path / "tokenizer.json").write_bytes(b"y")
        monkeypatch.setattr(verifymod, "_validate_sha256_format", lambda *a: None)
        monkeypatch.setattr(verifymod, "_sha256_file", lambda p: "m" * 64 if Path(p).name == "embeddings.npy" else "x" * 64)
        with pytest.raises(ValueError, match="補助ファイル SHA256 不一致"):
            verify(tmp_path)

    def test_full_verify_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """全 SHA256 が一致すれば推論検証へ進む。"""
        manifest = {
            "embeddings_sha256": "m" * 64,
            "auxiliary_files": [{"name": "tokenizer.json", "sha256": "t" * 64}],
        }
        (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (tmp_path / "embeddings.npy").write_bytes(b"x")
        (tmp_path / "tokenizer.json").write_bytes(b"y")
        sha_map = {"embeddings.npy": "m" * 64, "tokenizer.json": "t" * 64}
        monkeypatch.setattr(verifymod, "_validate_sha256_format", lambda *a: None)
        monkeypatch.setattr(verifymod, "_sha256_file", lambda p: sha_map[Path(p).name])
        ran = []
        monkeypatch.setattr(verifymod, "_run_inference_check", lambda *a: ran.append(a))
        verify(tmp_path)
        assert ran
        assert "SHA256 verification OK" in capsys.readouterr().out


class TestRunInferenceCheck:
    """_run_inference_check のテスト。"""

    def _setup_model_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, table: np.ndarray) -> dict:
        """embeddings.npy・tokenizer.json と Tokenizer モックを準備し manifest を返す。"""
        np.save(tmp_path / "embeddings.npy", table)
        (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")

        # 関数内 import の tokenizers.Tokenizer.from_file をモック
        import tokenizers

        tok = _make_tokenizer(ids_by_text={
            "日本語のテスト文": [0],
            "ベクトル品質確認": [1],
            verifymod._SIMILAR_PAIR[0]: [0],
            verifymod._SIMILAR_PAIR[1]: [2],
            verifymod._DISSIMILAR_TEXT: [3],
        })
        monkeypatch.setattr(tokenizers.Tokenizer, "from_file", staticmethod(lambda _p: tok))
        return {"vocab_size": table.shape[0], "embedding_dim": table.shape[1]}

    def test_full_inference_flow(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """テーブル読み込み後に次元・正規化・再現性・意味チェックを順に通過する。"""
        table = np.array(
            [[1.0, 0.0], [0.0, 1.0], [0.9, 0.1], [-0.5, 0.5]],
            dtype=np.float32,
        )
        manifest = self._setup_model_dir(tmp_path, monkeypatch, table)

        _run_inference_check(tmp_path, manifest, cosine_threshold=0.999)
        assert "all verifications PASSED" in capsys.readouterr().out

    def test_table_shape_mismatch_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """テーブル shape が manifest と不一致なら ValueError。"""
        table = np.eye(4, 2, dtype=np.float32)
        manifest = self._setup_model_dir(tmp_path, monkeypatch, table)
        manifest["vocab_size"] = 99

        with pytest.raises(ValueError, match="テーブル shape 不一致"):
            _run_inference_check(tmp_path, manifest, cosine_threshold=0.999)


class TestMain:
    """main の CLI 動作テスト。"""

    def test_main_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """verify が成功すれば 0 を返す。"""
        monkeypatch.setattr(verifymod, "verify", lambda d: None)
        assert main(["--models-dir", str(tmp_path)]) == 0

    def test_main_default_dir_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--models-dir 省略時は BLUECORE_MODELS_DIR を使う。"""
        monkeypatch.setenv("BLUECORE_MODELS_DIR", str(tmp_path))
        received = []
        monkeypatch.setattr(verifymod, "verify", lambda d: received.append(d))
        assert main([]) == 0
        assert received[0] == tmp_path

    def test_main_error_returns_one(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """verify が例外を送出すれば traceback を出して 1 を返す。"""
        monkeypatch.setattr(verifymod, "verify", MagicMock(side_effect=RuntimeError("boom")))
        assert main([]) == 1
        assert "boom" in capsys.readouterr().err
