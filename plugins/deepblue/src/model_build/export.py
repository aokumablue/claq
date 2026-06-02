"""ONNX エクスポート — ruri-v3 を HF Hub から取得して FP32 ONNX 形式に変換する。

optimum.exporters.onnx.main_export を直接呼び出す（CLI には `--revision` がないため）。
量子化（FP16/INT8）は後段の quantize.py で行う。
メンテナ環境でのみ実行される。
"""

from __future__ import annotations

import contextlib
import logging
import warnings
from collections.abc import Iterator
from pathlib import Path

# torch >= 2.9.0 では private 名と submodule 参照が wildcard import から除外されたため、
# optimum が torch.onnx.symbolic_opset14 から直接 import できなくなった。
# 内部パスから取得して公開モジュールに注入する。
_OPSET14_COMPAT_NAMES = [
    "_attention_scale",
    "_causal_attention_mask",
    "_onnx_symbolic",
    "_type_utils",
    "jit_utils",
    "symbolic_helper",
]


def _patch_torch_onnx_symbolic_opset14() -> None:
    """torch.onnx.symbolic_opset14 互換パッチを適用する。

    torch >= 2.9.0 で optimum が参照する名前が公開モジュールから消えたため、
    内部実装から取得して注入する。すでに存在する名前はスキップする。
    """
    import torch.onnx.symbolic_opset14 as _pub
    from torch.onnx._internal.torchscript_exporter import symbolic_opset14 as _internal

    for name in _OPSET14_COMPAT_NAMES:
        if not hasattr(_pub, name):
            setattr(_pub, name, getattr(_internal, name))


@contextlib.contextmanager
def _suppress_onnx_warnings() -> Iterator[None]:
    """ONNX エクスポート中に発生する既知の無害な警告を抑制するコンテキストマネージャ。"""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*already registered.*", category=UserWarning)
        warnings.filterwarnings("ignore", category=UserWarning, module="torch.onnx")
        warnings.filterwarnings("ignore", message=".*torch.tensor results are registered as constants.*")
        warnings.filterwarnings("ignore", message=".*dynamic_axes.*", category=UserWarning)
        warnings.filterwarnings("ignore", message=".*LeafSpec.*", category=FutureWarning)
        yield


def _run_main_export(model_name: str, revision: str, opset: int, onnx_out: Path) -> None:
    """optimum の main_export を呼び出し、ONNX ファイルを生成する。

    optimum バグによる FileNotFoundError は model.onnx が存在する場合のみ無視する。
    """
    from optimum.exporters.onnx import main_export  # type: ignore[import-untyped]  # noqa: PLC0415

    print(f"[export] main_export(model={model_name}, revision={revision[:8]}, opset={opset})", flush=True)
    try:
        main_export(
            model_name_or_path=model_name,
            output=onnx_out,
            task="feature-extraction",
            opset=opset,
            revision=revision,
            trust_remote_code=False,
            framework="pt",
            do_validation=False,
        )
    except FileNotFoundError as exc:
        # optimum バグ: torch.onnx dynamo エクスポーターがグラフ最適化時に
        # model.onnx.data をインライン化して削除するが、optimum のクリーンアップが
        # その後も削除しようとする。model.onnx が存在すればエクスポートは成功している。
        if not (onnx_out / "model.onnx").exists():
            raise
        print(f"[export] Skipping stale external data cleanup: {exc}", flush=True)


def export_to_onnx(
    model_name: str,
    revision: str,
    output_dir: Path,
    opset: int = 18,
) -> Path:
    """指定モデルを FP32 ONNX 形式にエクスポートし、生成された ONNX ファイルのパスを返す。

    optimum.exporters.onnx.main_export を直接呼び出す。
    出力は output_dir/onnx_export/ に生成される。
    """
    _patch_torch_onnx_symbolic_opset14()
    # torch.onnx._internal の StreamHandler が torchvision 未インストール等の WARNING を stderr に出力するため抑制
    logging.getLogger("torch.onnx._internal").setLevel(logging.ERROR)

    onnx_out = output_dir / "onnx_export"
    onnx_out.mkdir(parents=True, exist_ok=True)

    with _suppress_onnx_warnings():
        _run_main_export(model_name, revision, opset, onnx_out)

    candidates = list(onnx_out.glob("*.onnx"))
    if not candidates:
        raise FileNotFoundError(f"ONNX ファイルが {onnx_out} に見つかりません。")

    onnx_path = next((p for p in candidates if p.name == "model.onnx"), candidates[0])
    size_mb = onnx_path.stat().st_size / 1024**2
    print(f"[export] ONNX model: {onnx_path} ({size_mb:.1f} MB)", flush=True)
    return onnx_path
