"""quantize モジュールのユニットテスト（ネットワーク不要）。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from model_build.quantize import DEFAULT_QUANT, QUANT_CHOICES, _to_int8, quantize


class TestConstants:
    """定数のテスト。"""

    def test_quant_choices(self) -> None:
        """QUANT_CHOICES が期待値を含む。"""
        assert "fp16" in QUANT_CHOICES
        assert "fp32" in QUANT_CHOICES
        assert "int8" in QUANT_CHOICES

    def test_default_quant(self) -> None:
        """DEFAULT_QUANT が fp16 である。"""
        assert DEFAULT_QUANT == "fp16"


class TestQuantize:
    """quantize() のテスト。"""

    def test_invalid_quant_raises(self, tmp_path: Path) -> None:
        """不正な quant 指定は ValueError。"""
        src = tmp_path / "src.onnx"
        src.write_bytes(b"fake")
        with pytest.raises(ValueError, match="いずれかを指定"):
            quantize(src, tmp_path / "dst.onnx", "bf16")

    def test_fp32_copies_file(self, tmp_path: Path) -> None:
        """fp32 は src をそのまま dst にコピーする。"""
        src = tmp_path / "src.onnx"
        src.write_bytes(b"onnx-data")
        dst = tmp_path / "out" / "dst.onnx"
        result = quantize(src, dst, "fp32")
        assert result == dst
        assert dst.read_bytes() == b"onnx-data"

    def test_fp32_creates_parent_dir(self, tmp_path: Path) -> None:
        """fp32 は dst の親ディレクトリを自動作成する。"""
        src = tmp_path / "src.onnx"
        src.write_bytes(b"x")
        dst = tmp_path / "deep" / "nested" / "dst.onnx"
        quantize(src, dst, "fp32")
        assert dst.exists()

    def test_fp16_calls_ort_optimizer(self, tmp_path: Path) -> None:
        """fp16 は ort optimizer を呼び出す。"""
        import types
        src = tmp_path / "src.onnx"
        src.write_bytes(b"fake")
        dst = tmp_path / "dst_fp16.onnx"

        mock_opt_model = MagicMock()

        def _save(path: str) -> None:
            Path(path).write_bytes(b"fp16-onnx")

        mock_opt_model.save_model_to_file.side_effect = _save

        # _to_fp16 内の `from onnxruntime.transformers import optimizer` をモック
        mock_optimizer_mod = MagicMock()
        mock_optimizer_mod.optimize_model.return_value = mock_opt_model
        fake_ort_transformers = types.SimpleNamespace(optimizer=mock_optimizer_mod)

        with patch.dict("sys.modules", {
            "onnxruntime.transformers": fake_ort_transformers,
            "onnxruntime.transformers.optimizer": mock_optimizer_mod,
        }):
            result = quantize(src, dst, "fp16")

        mock_optimizer_mod.optimize_model.assert_called_once()
        mock_opt_model.convert_float_to_float16.assert_called_once_with(keep_io_types=True)
        assert result == dst

    def test_int8_calls_quantize_dynamic(self, tmp_path: Path) -> None:
        """int8 は onnxruntime.quantization.quantize_dynamic を呼び出す。"""
        import types
        src = tmp_path / "src.onnx"
        src.write_bytes(b"fake")
        dst = tmp_path / "dst_int8.onnx"

        def _qd(*args: object, **_kwargs: object) -> None:
            Path(str(args[1])).write_bytes(b"int8-onnx")

        mock_quant = MagicMock()
        mock_quant.quantize_dynamic = _qd
        mock_quant.QuantType = types.SimpleNamespace(QInt8="QInt8")
        fake_ort_quant = types.SimpleNamespace(
            quantize_dynamic=_qd,
            QuantType=mock_quant.QuantType,
        )
        with patch.dict("sys.modules", {"onnxruntime.quantization": fake_ort_quant}):
            result = _to_int8(src, dst)

        assert result == dst


class TestQuantizeImports:
    """量子化バックエンドの import 分岐テスト。"""

    def test_fp16_import_error_propagates(self, tmp_path: Path) -> None:
        """onnxruntime.transformers が存在しない環境では ImportError が伝播する。"""
        src = tmp_path / "src.onnx"
        src.write_bytes(b"fake")
        dst = tmp_path / "dst.onnx"
        with patch.dict("sys.modules", {"onnxruntime.transformers": None, "onnxruntime.transformers.optimizer": None}):
            with pytest.raises((ImportError, TypeError)):
                quantize(src, dst, "fp16")
