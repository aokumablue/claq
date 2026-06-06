"""verify モジュールのユニットテスト（ネットワーク不要）。"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import model_build.verify as verifymod
from model_build.verify import (
    _check_dim,
    _check_l2_norm,
    _check_reproducibility,
    _cosine_similarity,
    _infer_embedding,
    _run_inference_check,
    main,
    verify,
)


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


class TestCheckDim:
    """_check_dim のテスト。"""

    def test_correct_dim(self, capsys: pytest.CaptureFixture) -> None:
        """期待次元と一致すれば例外なし。"""
        vectors = [[0.1] * 768, [0.2] * 768]
        _check_dim(vectors, 768)
        captured = capsys.readouterr()
        assert "OK" in captured.out

    def test_wrong_dim_raises(self) -> None:
        """期待次元と異なれば ValueError。"""
        vectors = [[0.1] * 512]
        with pytest.raises(ValueError, match="次元数不一致"):
            _check_dim(vectors, 768)


class TestCheckL2Norm:
    """_check_l2_norm のテスト。"""

    def test_unit_vectors_pass(self, capsys: pytest.CaptureFixture) -> None:
        """L2 norm ≈ 1.0 のベクトルは通過する。"""
        v = [1.0 / math.sqrt(768)] * 768
        _check_l2_norm([v])
        captured = capsys.readouterr()
        assert "OK" in captured.out

    def test_non_unit_vector_raises(self) -> None:
        """L2 norm が 1.0 から外れたベクトルは ValueError。"""
        v = [1.0] * 768  # norm = sqrt(768) ≈ 27.7
        with pytest.raises(ValueError, match="L2 ノルム不正"):
            _check_l2_norm([v])


class TestCheckReproducibility:
    """_check_reproducibility のテスト。"""

    def _make_session_and_tokenizer(self, vec: list[float]) -> tuple[MagicMock, MagicMock]:
        """指定ベクトルを返す推論モックを作成する。"""
        import numpy as np

        mock_session = MagicMock()
        mock_session.get_inputs.return_value = []
        token_embs = np.array([[vec]], dtype=np.float32)
        mock_session.run.return_value = [token_embs]

        mock_tok = MagicMock()
        enc = MagicMock()
        enc.ids = [1, 2, 3]
        enc.attention_mask = [1, 1, 1]
        mock_tok.encode.return_value = enc

        return mock_session, mock_tok

    def test_identical_output_passes(self, capsys: pytest.CaptureFixture) -> None:
        """2 回同じ推論結果なら再現性チェック通過。"""
        vec = [1.0 / math.sqrt(3)] * 3
        ref_vec = list(vec)
        session, tokenizer = self._make_session_and_tokenizer(vec)

        _check_reproducibility(session, tokenizer, "test text", ref_vec, threshold=0.999)
        captured = capsys.readouterr()
        assert "OK" in captured.out

    def test_diverged_output_raises(self) -> None:
        """cosine 類似度が閾値未満なら ValueError。"""
        ref_vec = [1.0, 0.0, 0.0]
        diverged_vec = [0.0, 1.0, 0.0]
        session, tokenizer = self._make_session_and_tokenizer(diverged_vec)

        with pytest.raises(ValueError, match="再現性チェック失敗"):
            _check_reproducibility(session, tokenizer, "text", ref_vec, threshold=0.999)


class TestRunInferenceCheckOnnxChecker:
    """_run_inference_check の onnx.checker 検証テスト。"""

    def _make_tokenizer_json(self, tmp_path: Path) -> None:
        """テスト用 tokenizer.json を tmp_path に生成する。"""
        from tokenizers import Tokenizer  # type: ignore[import-untyped]
        from tokenizers.models import BPE  # type: ignore[import-untyped]

        tok = Tokenizer(BPE())
        tok.save(str(tmp_path / "tokenizer.json"))

    def test_invalid_onnx_raises_value_error(self, tmp_path: Path) -> None:
        """不正 ONNX バイナリで onnx.checker が例外を出すと ValueError になる。"""
        self._make_tokenizer_json(tmp_path)
        model_bytes = b"invalid_onnx_bytes"
        manifest = {"tokenizer_max_length": 512, "embedding_dim": 768}

        with pytest.raises(ValueError, match="ONNX 構造検証失敗"):
            _run_inference_check(model_bytes, tmp_path, manifest, cosine_threshold=0.999)

    def test_check_model_exception_is_wrapped(self, tmp_path: Path) -> None:
        """onnx.checker.check_model の任意例外が ValueError でラップされる。"""
        self._make_tokenizer_json(tmp_path)
        model_bytes = b"any_bytes"
        manifest = {"tokenizer_max_length": 512, "embedding_dim": 768}

        # 関数内 import の onnx を sys.modules 経由で差し替える
        import sys
        mock_onnx = MagicMock()
        mock_onnx.load_from_string.return_value = MagicMock()
        mock_onnx.checker.check_model.side_effect = RuntimeError("bad model")
        with patch.dict(sys.modules, {"onnx": mock_onnx}):
            with pytest.raises(ValueError, match="ONNX 構造検証失敗"):
                _run_inference_check(model_bytes, tmp_path, manifest, cosine_threshold=0.999)


class TestInferEmbeddingTokenTypeIds:
    """_infer_embedding の token_type_ids 入力分岐のテスト。"""

    def test_adds_token_type_ids_when_required(self) -> None:
        """モデルが token_type_ids を要求すれば inputs に追加して推論する。"""
        import numpy as np

        session = MagicMock()
        tt_input = MagicMock()
        tt_input.name = "token_type_ids"
        session.get_inputs.return_value = [tt_input]
        session.run.return_value = [np.array([[[1.0, 0.0]]], dtype=np.float32)]

        tok = MagicMock()
        enc = MagicMock()
        enc.ids = [1, 2, 3]
        enc.attention_mask = [1, 1, 1]
        tok.encode.return_value = enc

        result = _infer_embedding(session, tok, "text")
        assert isinstance(result, list)
        # token_type_ids を含む inputs で run が呼ばれている
        called_inputs = session.run.call_args.args[1]
        assert "token_type_ids" in called_inputs


class TestVerify:
    """verify のエンドツーエンド分岐テスト。"""

    def test_manifest_missing_raises(self, tmp_path: Path) -> None:
        """manifest.json が無ければ FileNotFoundError。"""
        with pytest.raises(FileNotFoundError, match="manifest.json"):
            verify(tmp_path)

    def test_model_sha_mismatch_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """model.onnx の SHA256 が不一致なら ValueError。"""
        manifest = {"merged_sha256": "a" * 64, "auxiliary_files": []}
        (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (tmp_path / "model.onnx").write_bytes(b"x")
        monkeypatch.setattr(verifymod, "_validate_sha256_format", lambda *a: None)
        monkeypatch.setattr(verifymod, "_sha256_file", lambda p: "b" * 64)
        with pytest.raises(ValueError, match="model.onnx SHA256 不一致"):
            verify(tmp_path)

    def test_aux_sha_mismatch_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """補助ファイルの SHA256 が不一致なら ValueError。"""
        manifest = {
            "merged_sha256": "m" * 64,
            "auxiliary_files": [{"name": "tokenizer.json", "sha256": "t" * 64}],
        }
        (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (tmp_path / "model.onnx").write_bytes(b"x")
        (tmp_path / "tokenizer.json").write_bytes(b"y")
        monkeypatch.setattr(verifymod, "_validate_sha256_format", lambda *a: None)
        monkeypatch.setattr(verifymod, "_sha256_file", lambda p: "m" * 64 if Path(p).name == "model.onnx" else "x" * 64)
        with pytest.raises(ValueError, match="補助ファイル SHA256 不一致"):
            verify(tmp_path)

    def test_full_verify_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """全 SHA256 が一致すれば推論検証へ進む。"""
        manifest = {
            "merged_sha256": "m" * 64,
            "auxiliary_files": [{"name": "tokenizer.json", "sha256": "t" * 64}],
        }
        (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (tmp_path / "model.onnx").write_bytes(b"x")
        (tmp_path / "tokenizer.json").write_bytes(b"y")
        sha_map = {"model.onnx": "m" * 64, "tokenizer.json": "t" * 64}
        monkeypatch.setattr(verifymod, "_validate_sha256_format", lambda *a: None)
        monkeypatch.setattr(verifymod, "_sha256_file", lambda p: sha_map[Path(p).name])
        ran = []
        monkeypatch.setattr(verifymod, "_run_inference_check", lambda *a: ran.append(a))
        verify(tmp_path)
        assert ran
        assert "SHA256 verification OK" in capsys.readouterr().out


class TestRunInferenceCheckSuccess:
    """_run_inference_check の正常フローのテスト。"""

    def test_full_inference_flow(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """ONNX 検証通過後に推論・各種チェックを順に呼ぶ。"""
        manifest = {"tokenizer_max_length": 512, "embedding_dim": 768}

        mock_ort = MagicMock()
        mock_session = MagicMock()
        mock_ort.InferenceSession.return_value = mock_session
        mock_onnx = MagicMock()  # load_from_string / checker.check_model は成功
        mock_tokenizers = MagicMock()
        mock_tokenizers.Tokenizer.from_file.return_value = MagicMock()

        monkeypatch.setattr(verifymod, "_infer_embedding", lambda s, t, txt: [0.1] * 768)
        monkeypatch.setattr(verifymod, "_check_dim", lambda v, d: None)
        monkeypatch.setattr(verifymod, "_check_l2_norm", lambda v: None)
        monkeypatch.setattr(verifymod, "_check_reproducibility", lambda *a: None)

        with patch.dict(sys.modules, {"onnxruntime": mock_ort, "onnx": mock_onnx, "tokenizers": mock_tokenizers}):
            _run_inference_check(b"bytes", tmp_path, manifest, cosine_threshold=0.999)
        assert "all verifications PASSED" in capsys.readouterr().out


class TestMain:
    """main の CLI 動作テスト。"""

    def test_main_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """verify が成功すれば 0 を返す。"""
        monkeypatch.setattr(verifymod, "verify", lambda d: None)
        assert main(["--models-dir", str(tmp_path)]) == 0

    def test_main_default_dir_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--models-dir 省略時は DEEPBLUE_MODELS_DIR を使う。"""
        monkeypatch.setenv("DEEPBLUE_MODELS_DIR", str(tmp_path))
        received = []
        monkeypatch.setattr(verifymod, "verify", lambda d: received.append(d))
        assert main([]) == 0
        assert received[0] == tmp_path

    def test_main_error_returns_one(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """verify が例外を送出すれば traceback を出して 1 を返す。"""
        monkeypatch.setattr(verifymod, "verify", MagicMock(side_effect=RuntimeError("boom")))
        assert main([]) == 1
        assert "boom" in capsys.readouterr().err
