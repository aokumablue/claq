"""model_build テスト共通フィクスチャ・ヘルパ。"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any


def make_safetensors(path: Path, array: Any, tensor_name: str = "embedding.weight") -> None:
    """numpy 配列から最小構成の safetensors ファイルを作成する。"""
    dtype_name = {"float32": "F32", "float16": "F16"}[array.dtype.name]
    header = {
        tensor_name: {
            "dtype": dtype_name,
            "shape": list(array.shape),
            "data_offsets": [0, array.nbytes],
        }
    }
    header_bytes = json.dumps(header).encode("utf-8")
    with path.open("wb") as f:
        f.write(struct.pack("<Q", len(header_bytes)))
        f.write(header_bytes)
        f.write(array.tobytes())
