"""model_build.extract のユニットテスト。"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np
import pytest

from model_build.extract import extract_embeddings, read_embedding_table
from tests.model_build.conftest import make_safetensors

_VOCAB = 8
_SOURCE_DIM = 6


@pytest.fixture
def table() -> np.ndarray:
    """テスト用の埋め込みテーブル（値が一意になる連番）を返す。"""
    return np.arange(_VOCAB * _SOURCE_DIM, dtype=np.float32).reshape(_VOCAB, _SOURCE_DIM)


@pytest.fixture
def st_path(tmp_path: Path, table: np.ndarray) -> Path:
    """テーブルを書き込んだ safetensors ファイルのパスを返す。"""
    path = tmp_path / "model.safetensors"
    make_safetensors(path, table)
    return path


class TestReadEmbeddingTable:
    """read_embedding_table のテスト。"""

    def test_reads_table_as_mmap(self, st_path: Path, table: np.ndarray) -> None:
        """safetensors からテーブルを正しく読み込む。"""
        loaded = read_embedding_table(st_path, vocab_size=_VOCAB, source_dim=_SOURCE_DIM)
        assert loaded.shape == (_VOCAB, _SOURCE_DIM)
        np.testing.assert_array_equal(np.asarray(loaded), table)

    def test_rejects_oversized_header(self, tmp_path: Path) -> None:
        """ヘッダサイズが上限を超えると ValueError。"""
        path = tmp_path / "evil.safetensors"
        with path.open("wb") as f:
            f.write(struct.pack("<Q", 17 * 1024 * 1024))
        with pytest.raises(ValueError, match="ヘッダが大きすぎます"):
            read_embedding_table(path, vocab_size=_VOCAB, source_dim=_SOURCE_DIM)

    def test_rejects_missing_tensor(self, tmp_path: Path, table: np.ndarray) -> None:
        """embedding.weight テンソルがなければ ValueError。"""
        path = tmp_path / "wrong.safetensors"
        make_safetensors(path, table, tensor_name="other.weight")
        with pytest.raises(ValueError, match="embedding.weight"):
            read_embedding_table(path, vocab_size=_VOCAB, source_dim=_SOURCE_DIM)

    def test_rejects_wrong_dtype(self, tmp_path: Path, table: np.ndarray) -> None:
        """F32 以外の dtype は ValueError。"""
        path = tmp_path / "fp16.safetensors"
        make_safetensors(path, table.astype(np.float16))
        with pytest.raises(ValueError, match="dtype 不一致"):
            read_embedding_table(path, vocab_size=_VOCAB, source_dim=_SOURCE_DIM)

    def test_rejects_wrong_shape(self, st_path: Path) -> None:
        """期待 shape と不一致なら ValueError。"""
        with pytest.raises(ValueError, match="shape 不一致"):
            read_embedding_table(st_path, vocab_size=_VOCAB + 1, source_dim=_SOURCE_DIM)

    def test_rejects_inconsistent_data_offsets(self, tmp_path: Path, table: np.ndarray) -> None:
        """data_offsets のサイズが shape と矛盾していれば ValueError。"""
        path = tmp_path / "broken.safetensors"
        header = {
            "embedding.weight": {
                "dtype": "F32",
                "shape": [_VOCAB, _SOURCE_DIM],
                "data_offsets": [0, table.nbytes - 4],
            }
        }
        header_bytes = json.dumps(header).encode("utf-8")
        with path.open("wb") as f:
            f.write(struct.pack("<Q", len(header_bytes)))
            f.write(header_bytes)
            f.write(table.tobytes()[:-4])
        with pytest.raises(ValueError, match="データサイズ不一致"):
            read_embedding_table(path, vocab_size=_VOCAB, source_dim=_SOURCE_DIM)


class TestExtractEmbeddings:
    """extract_embeddings のテスト。"""

    def test_truncates_to_embedding_dim(self, st_path: Path, tmp_path: Path, table: np.ndarray) -> None:
        """先頭 embedding_dim 列に切り詰めて保存する。"""
        npy_path = tmp_path / "embeddings.npy"
        extract_embeddings(st_path, npy_path, vocab_size=_VOCAB, source_dim=_SOURCE_DIM, embedding_dim=3)
        loaded = np.load(npy_path)
        assert loaded.shape == (_VOCAB, 3)
        assert loaded.dtype == np.float32
        np.testing.assert_array_equal(loaded, table[:, :3])

    def test_full_dim_keeps_all_columns(self, st_path: Path, tmp_path: Path, table: np.ndarray) -> None:
        """embedding_dim == source_dim なら全列を保持する。"""
        npy_path = tmp_path / "embeddings.npy"
        extract_embeddings(st_path, npy_path, vocab_size=_VOCAB, source_dim=_SOURCE_DIM, embedding_dim=_SOURCE_DIM)
        np.testing.assert_array_equal(np.load(npy_path), table)

    @pytest.mark.parametrize("bad_dim", [0, -1, _SOURCE_DIM + 1])
    def test_rejects_out_of_range_dim(self, st_path: Path, tmp_path: Path, bad_dim: int) -> None:
        """embedding_dim が範囲外なら ValueError。"""
        with pytest.raises(ValueError, match="embedding_dim"):
            extract_embeddings(
                st_path, tmp_path / "embeddings.npy",
                vocab_size=_VOCAB, source_dim=_SOURCE_DIM, embedding_dim=bad_dim,
            )

    def test_no_tmp_file_remains_on_success(self, st_path: Path, tmp_path: Path) -> None:
        """成功時に一時ファイルが残らない。"""
        npy_path = tmp_path / "embeddings.npy"
        extract_embeddings(st_path, npy_path, vocab_size=_VOCAB, source_dim=_SOURCE_DIM, embedding_dim=3)
        assert npy_path.exists()
        assert not npy_path.with_suffix(".npy.tmp").exists()
