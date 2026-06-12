"""静的埋め込みテーブル抽出 — model.safetensors から embeddings.npy を生成する。

safetensors はヘッダ（JSON）+ 生バイト列の単純なフォーマットのため、
safetensors ライブラリに依存せず stdlib + numpy のみでパースする
（torch pickle のような任意コード実行リスクがない）。

Matryoshka 学習済みモデルのため、先頭 embedding_dim 列への切り詰めで
品質をほぼ維持したまま（256 次元で全体スコアの 98.9%）テーブルを縮小できる。
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

# safetensors ヘッダの上限。正常なヘッダは数百バイトであり、
# 巨大ヘッダによるメモリ枯渇（DoS）を防ぐ。
_MAX_HEADER_BYTES = 16 * 1024 * 1024

# StaticEmbedding（sentence-transformers）の埋め込みテーブルのテンソル名
_TENSOR_NAME = "embedding.weight"


def read_embedding_table(st_path: Path, *, vocab_size: int, source_dim: int) -> Any:
    """model.safetensors から埋め込みテーブルを numpy 配列（mmap）として読み込む。

    Args:
        st_path: model.safetensors のパス。
        vocab_size: 期待する語彙数（build_config.json の値と照合）。
        source_dim: 期待する元の埋め込み次元。

    Returns:
        shape (vocab_size, source_dim) の読み取り専用 np.memmap（float32）。

    Raises:
        ValueError: ヘッダ不正、テンソル名・dtype・shape の不一致。
    """
    import numpy as np  # type: ignore[import-untyped]

    with st_path.open("rb") as f:
        header_size = struct.unpack("<Q", f.read(8))[0]
        if header_size > _MAX_HEADER_BYTES:
            raise ValueError(f"safetensors ヘッダが大きすぎます: {header_size} bytes")
        header = json.loads(f.read(header_size))

    if _TENSOR_NAME not in header:
        raise ValueError(f"テンソル {_TENSOR_NAME!r} が見つかりません: {sorted(header)}")
    meta = header[_TENSOR_NAME]
    if meta["dtype"] != "F32":
        raise ValueError(f"dtype 不一致: expected F32, got {meta['dtype']}")
    shape = tuple(meta["shape"])
    if shape != (vocab_size, source_dim):
        raise ValueError(f"shape 不一致: expected {(vocab_size, source_dim)}, got {shape}")
    start, end = meta["data_offsets"]
    expected_bytes = vocab_size * source_dim * 4
    if end - start != expected_bytes:
        raise ValueError(f"データサイズ不一致: expected {expected_bytes}, got {end - start}")

    return np.memmap(st_path, dtype=np.float32, mode="r", offset=8 + header_size + start, shape=shape)


def extract_embeddings(st_path: Path, npy_path: Path, *, vocab_size: int, source_dim: int, embedding_dim: int) -> None:
    """model.safetensors からテーブルを抽出し、先頭 embedding_dim 列に切り詰めて保存する。

    mmap 読み込みのため、メモリ使用量は切り詰め後のテーブルサイズ
    （vocab_size × embedding_dim × 4 bytes ≈ 33 MB）に収まる。

    Args:
        st_path: 入力 model.safetensors のパス。
        npy_path: 出力 embeddings.npy のパス。
        vocab_size: 期待する語彙数。
        source_dim: 元の埋め込み次元。
        embedding_dim: 切り詰め後の次元（Matryoshka 前提で先頭列を採用）。

    Raises:
        ValueError: embedding_dim が source_dim を超える場合、または safetensors 不正。
    """
    import numpy as np  # type: ignore[import-untyped]

    if not 0 < embedding_dim <= source_dim:
        raise ValueError(f"embedding_dim は 1..{source_dim} の範囲で指定してください: {embedding_dim}")

    table = read_embedding_table(st_path, vocab_size=vocab_size, source_dim=source_dim)
    truncated = np.ascontiguousarray(table[:, :embedding_dim], dtype=np.float32)

    # 部分書き込みの恒久化を防ぐため一時ファイル経由で配置する
    # （np.save はパス文字列だと .npy を自動付与するためファイルオブジェクトで渡す）
    tmp_path = npy_path.with_suffix(".npy.tmp")
    with tmp_path.open("wb") as f:
        np.save(f, truncated)
    tmp_path.replace(npy_path)
