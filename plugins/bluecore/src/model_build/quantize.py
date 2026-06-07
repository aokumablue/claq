"""量子化 — ONNX モデルを FP16 / INT8 に変換する。FP32 はパススルー。

FP16 変換には onnxruntime.transformers.optimizer を使用する（CPU 対応）。
optimum の dtype="fp16" export は CUDA 必須だが、こちらは CPU でも動作する。
"""

from __future__ import annotations

import shutil
from pathlib import Path

# サポートする量子化レベル（CLI 引数で受け付ける値）
QUANT_CHOICES = ("fp32", "fp16", "int8")
DEFAULT_QUANT = "fp16"


def quantize(src: Path, dst: Path, quant: str, *, num_heads: int = 16, hidden_size: int = 1024) -> Path:
    """src の ONNX ファイルを指定量子化で dst に書き出し、出力パスを返す。

    quant: "fp32" | "fp16" | "int8"
    num_heads / hidden_size: FP16 変換時のモデル構造パラメータ（build_config.json から渡す）
    """
    if quant not in QUANT_CHOICES:
        raise ValueError(f"quant は {QUANT_CHOICES} のいずれかを指定してください。")

    dst.parent.mkdir(parents=True, exist_ok=True)

    if quant == "fp32":
        shutil.copy2(src, dst)
        print(f"[quantize] FP32: {dst} ({dst.stat().st_size / 1024**2:.1f} MB)", flush=True)
        return dst

    if quant == "fp16":
        return _to_fp16(src, dst, num_heads=num_heads, hidden_size=hidden_size)

    return _to_int8(src, dst)


def _to_fp16(src: Path, dst: Path, *, num_heads: int, hidden_size: int) -> Path:
    """FP32 ONNX → FP16 ONNX に変換する（CPU 対応）。

    onnxruntime.transformers.optimizer を使用する。
    optimum の dtype="fp16" export は CUDA 必須だが、こちらは CPU でも動作する。
    num_heads / hidden_size は build_config.json 由来の値を使う。
    """
    from onnxruntime.transformers import optimizer  # type: ignore[import-untyped]

    # ruri-v3 は XLM-RoBERTa 派生（bert 系として扱う）
    opt_model = optimizer.optimize_model(
        str(src),
        model_type="bert",
        num_heads=num_heads,
        hidden_size=hidden_size,
        opt_level=0,  # グラフ最適化なし（変換のみ）
    )
    opt_model.convert_float_to_float16(keep_io_types=True)
    opt_model.save_model_to_file(str(dst))
    print(f"[quantize] FP16: {dst} ({dst.stat().st_size / 1024**2:.1f} MB)", flush=True)
    return dst


def _to_int8(src: Path, dst: Path) -> Path:
    """FP32 ONNX → 動的量子化 INT8 ONNX に変換する。"""
    from onnxruntime.quantization import QuantType, quantize_dynamic  # type: ignore[import-untyped]

    quantize_dynamic(
        str(src),
        str(dst),
        weight_type=QuantType.QInt8,
        per_channel=False,
        reduce_range=False,
    )
    print(f"[quantize] INT8: {dst} ({dst.stat().st_size / 1024**2:.1f} MB)", flush=True)
    return dst
