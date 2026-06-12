"""embedding モジュールのテスト（静的埋め込みテーブルベース）。"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from bluecore.mem import embedding

# テスト用テーブル: 4 語彙 × 3 次元
_TABLE = np.array(
    [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
        [2.0, 2.0, 2.0],
    ],
    dtype=np.float32,
)

# fake tokenizer が返すトークン ids（未登録テキストは [0, 1]）
_IDS_BY_TEXT: dict[str, list[int]] = {
    "empty": [],
    "single": [3],
}


def _make_fake_tokenizers():
    """tokenizers のモック。テキストごとに固定 ids を返す。"""

    class FakeEncoding:
        def __init__(self, ids: list[int]) -> None:
            self.ids = ids

    class FakeTokenizer:
        encode_batch_kwargs: dict = {}

        def encode_batch(self, texts, add_special_tokens=True):
            FakeTokenizer.encode_batch_kwargs = {"add_special_tokens": add_special_tokens}
            return [FakeEncoding(_IDS_BY_TEXT.get(t, [0, 1])) for t in texts]

        @staticmethod
        def from_file(path: str) -> FakeTokenizer:
            return FakeTokenizer()

    return types.SimpleNamespace(Tokenizer=FakeTokenizer)


def _write_fake_model_dir(model_dir: Path, table: np.ndarray = _TABLE) -> None:
    """テスト用のモデルディレクトリ（embeddings.npy + manifest.json + tokenizer.json）を作成する。"""
    model_dir.mkdir(parents=True, exist_ok=True)
    npy_path = model_dir / "embeddings.npy"
    np.save(str(npy_path), table)
    tok_data = b"{}"
    manifest = {
        "model_name": "hotchpotch/static-embedding-japanese",
        "hf_revision": "abc123",
        "embedding_dim": table.shape[1],
        "vocab_size": table.shape[0],
        "embeddings_sha256": hashlib.sha256(npy_path.read_bytes()).hexdigest(),
        "auxiliary_files": [{"name": "tokenizer.json", "sha256": hashlib.sha256(tok_data).hexdigest()}],
    }
    (model_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (model_dir / "tokenizer.json").write_bytes(tok_data)


@pytest.fixture(autouse=True)
def reset_embedding_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """各テスト前に内部シングルトンをリセットし、モデルパスを tmp_path に向ける。"""
    monkeypatch.setattr(embedding, "_table", None)
    monkeypatch.setattr(embedding, "_tokenizer", None)
    model_dir = tmp_path / "models"
    _write_fake_model_dir(model_dir)
    monkeypatch.setattr(embedding, "_MODELS_DIR", model_dir)


def _patch_tokenizers(monkeypatch: pytest.MonkeyPatch):
    """tokenizers を monkeypatch でモックに差し替える（numpy は実物を使う）。"""
    fake_tok = _make_fake_tokenizers()
    monkeypatch.setitem(sys.modules, "tokenizers", fake_tok)
    return fake_tok


def _expected_vector(ids: list[int]) -> list[float]:
    """テーブル参照の平均 + L2 正規化の期待値を計算する。"""
    mean = _TABLE[ids].mean(axis=0)
    return (mean / np.linalg.norm(mean)).tolist()


class TestEmbed:
    """embed() の動作テスト。"""

    def test_empty_input_returns_empty_list(self, monkeypatch: pytest.MonkeyPatch):
        """空リストを渡すとモデルをロードせずに空リストを返す。"""
        _patch_tokenizers(monkeypatch)
        assert embedding.embed([]) == []
        assert embedding._table is None

    def test_embed_returns_list_of_vectors(self, monkeypatch: pytest.MonkeyPatch):
        """テキストリストを渡すとベクトルのリストを返す。"""
        _patch_tokenizers(monkeypatch)
        result = embedding.embed(["hello", "world"])
        assert len(result) == 2
        assert isinstance(result[0], list)

    def test_embed_vectors_are_l2_normalized(self, monkeypatch: pytest.MonkeyPatch):
        """返されるベクトルが L2 正規化されている（norm ≈ 1.0）。"""
        _patch_tokenizers(monkeypatch)
        result = embedding.embed(["test"])
        norm = math.sqrt(sum(x * x for x in result[0]))
        assert abs(norm - 1.0) < 1e-5

    def test_embed_computes_mean_of_token_embeddings(self, monkeypatch: pytest.MonkeyPatch):
        """トークン埋め込みの平均を L2 正規化した値を返す（StaticEmbedding 仕様）。"""
        _patch_tokenizers(monkeypatch)
        # "hello" → ids [0, 1] → mean (0.5, 0.5, 0.0) → 正規化 (1/√2, 1/√2, 0)
        result = embedding.embed(["hello"])
        assert result[0] == pytest.approx(_expected_vector([0, 1]))

    def test_embed_single_token_text(self, monkeypatch: pytest.MonkeyPatch):
        """単一トークンのテキストはそのトークンの正規化ベクトルを返す。"""
        _patch_tokenizers(monkeypatch)
        result = embedding.embed(["single"])
        assert result[0] == pytest.approx(_expected_vector([3]))

    def test_embed_empty_tokens_returns_zero_vector(self, monkeypatch: pytest.MonkeyPatch):
        """トークンが得られないテキストはゼロベクトルになる。"""
        _patch_tokenizers(monkeypatch)
        result = embedding.embed(["empty"])
        assert result[0] == [0.0, 0.0, 0.0]

    def test_encode_batch_called_without_special_tokens(self, monkeypatch: pytest.MonkeyPatch):
        """encode_batch は add_special_tokens=False で呼ばれる（StaticEmbedding 仕様）。"""
        fake_tok = _patch_tokenizers(monkeypatch)
        embedding.embed(["test"])
        assert fake_tok.Tokenizer.encode_batch_kwargs == {"add_special_tokens": False}

    def test_model_loaded_lazily(self, monkeypatch: pytest.MonkeyPatch):
        """embed([]) ではモデルがロードされない（遅延ロード）。"""
        _patch_tokenizers(monkeypatch)
        embedding.embed([])
        assert embedding._table is None

    def test_table_cached_on_second_call(self, monkeypatch: pytest.MonkeyPatch):
        """2 回目の embed() でテーブルが再ロードされない（シングルトン）。"""
        _patch_tokenizers(monkeypatch)
        embedding.embed(["a"])
        table_first = embedding._table
        embedding.embed(["b"])
        assert embedding._table is table_first

    def test_table_opened_as_mmap(self, monkeypatch: pytest.MonkeyPatch):
        """テーブルは mmap モードで開かれる（全体をメモリに載せない）。"""
        _patch_tokenizers(monkeypatch)
        embedding.embed(["a"])
        assert isinstance(embedding._table, np.memmap)

    def test_str_input_raises_type_error(self, monkeypatch: pytest.MonkeyPatch):
        """文字列を直接渡すと明確な TypeError が出る（内部ライブラリの不明瞭なエラーを防ぐ）。"""
        _patch_tokenizers(monkeypatch)
        with pytest.raises(TypeError, match="expects list\\[str\\]"):
            embedding.embed("not a list")


class TestEmbedQuery:
    """embed_query() の動作テスト。"""

    def test_returns_single_vector(self, monkeypatch: pytest.MonkeyPatch):
        """単一クエリを渡すと 1 つのベクトルを返す。"""
        _patch_tokenizers(monkeypatch)
        result = embedding.embed_query("テスト", "hotchpotch/static-embedding-japanese")
        assert isinstance(result, list)
        assert isinstance(result[0], float)

    def test_query_vector_is_l2_normalized(self, monkeypatch: pytest.MonkeyPatch):
        """クエリベクトルが L2 正規化されている。"""
        _patch_tokenizers(monkeypatch)
        vec = embedding.embed_query("検索テスト", "hotchpotch/static-embedding-japanese")
        norm = math.sqrt(sum(x * x for x in vec))
        assert abs(norm - 1.0) < 1e-5

    def test_query_embedded_without_prefix(self, monkeypatch: pytest.MonkeyPatch):
        """クエリはプレフィックスなしでそのまま埋め込まれる（本モデルは prompt なし）。"""
        _patch_tokenizers(monkeypatch)
        # "single" がそのまま渡れば ids [3]、プレフィックス付きなら既定 [0, 1] になる
        vec = embedding.embed_query("single", "hotchpotch/static-embedding-japanese")
        assert vec == pytest.approx(_expected_vector([3]))

    def test_returns_empty_list_when_model_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """embeddings.npy が不在の場合は空リストを返す。"""
        _patch_tokenizers(monkeypatch)
        bad_dir = tmp_path / "no_model"
        bad_dir.mkdir()
        monkeypatch.setattr(embedding, "_MODELS_DIR", bad_dir)
        monkeypatch.setattr(embedding, "_model_unavailable_warned", False)
        result = embedding.embed_query("test query", "hotchpotch/static-embedding-japanese")
        assert result == []


class TestModelNotFound:
    """モデルファイル未配置時の動作テスト。"""

    def test_missing_embeddings_npy_returns_empty_list(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        """embeddings.npy が存在しない場合は例外を出さず空リストを返す（ダウンロード中）。"""
        _patch_tokenizers(monkeypatch)
        bad_dir = tmp_path / "no_model"
        bad_dir.mkdir()
        (bad_dir / "tokenizer.json").write_bytes(b"{}")
        monkeypatch.setattr(embedding, "_MODELS_DIR", bad_dir)
        monkeypatch.setattr(embedding, "_model_unavailable_warned", False)
        result = embedding.embed(["test"])
        assert result == []
        assert "model not ready" in capsys.readouterr().err

    def test_missing_embeddings_npy_warns_only_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        """embeddings.npy が存在しない場合、警告は 1 度だけ出力される。"""
        _patch_tokenizers(monkeypatch)
        bad_dir = tmp_path / "no_model"
        bad_dir.mkdir()
        (bad_dir / "tokenizer.json").write_bytes(b"{}")
        monkeypatch.setattr(embedding, "_MODELS_DIR", bad_dir)
        monkeypatch.setattr(embedding, "_model_unavailable_warned", False)
        embedding.embed(["test"])
        embedding.embed(["test2"])
        err = capsys.readouterr().err
        assert err.count("model not ready") == 1

    def test_missing_manifest_raises_file_not_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """manifest.json が存在しない場合は FileNotFoundError。"""
        _patch_tokenizers(monkeypatch)
        bad_dir = tmp_path / "no_manifest"
        bad_dir.mkdir()
        # embeddings.npy と tokenizer.json は必要（_verify_model_sha に到達するため）
        np.save(str(bad_dir / "embeddings.npy"), _TABLE)
        (bad_dir / "tokenizer.json").write_bytes(b"{}")
        # manifest.json は置かない → _verify_model_sha が FileNotFoundError を出す
        monkeypatch.setattr(embedding, "_MODELS_DIR", bad_dir)
        with pytest.raises(FileNotFoundError, match="manifest.json"):
            embedding.embed(["test"])

    def test_missing_tokenizer_raises_file_not_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """tokenizer.json が存在しない場合は FileNotFoundError（embeddings.npy は存在する異常状態）。"""
        _patch_tokenizers(monkeypatch)
        bad_dir = tmp_path / "no_tok"
        bad_dir.mkdir()
        np.save(str(bad_dir / "embeddings.npy"), _TABLE)
        monkeypatch.setattr(embedding, "_MODELS_DIR", bad_dir)
        with pytest.raises(FileNotFoundError, match="tokenizer.json"):
            embedding.embed(["test"])

    def test_embeddings_sha256_mismatch_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """embeddings.npy の SHA256 が manifest と不一致なら ValueError。"""
        _patch_tokenizers(monkeypatch)
        bad_dir = tmp_path / "bad_sha"
        _write_fake_model_dir(bad_dir)
        # embeddings.npy を改竄（SHA が変わる）
        (bad_dir / "embeddings.npy").write_bytes(b"tampered-data")
        monkeypatch.setattr(embedding, "_MODELS_DIR", bad_dir)
        with pytest.raises(ValueError, match="SHA256 不一致"):
            embedding.embed(["test"])

    def test_tokenizer_sha256_mismatch_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """tokenizer.json の SHA256 が manifest と不一致なら ValueError。"""
        _patch_tokenizers(monkeypatch)
        bad_dir = tmp_path / "bad_tok_sha"
        _write_fake_model_dir(bad_dir)
        # tokenizer.json を改竄
        (bad_dir / "tokenizer.json").write_bytes(b"tampered-tokenizer")
        monkeypatch.setattr(embedding, "_MODELS_DIR", bad_dir)
        with pytest.raises(ValueError, match="tokenizer.json SHA256 不一致"):
            embedding.embed(["test"])


class TestEncodeArray:
    """_encode_array() の内部 API テスト。"""

    def test_returns_numpy_array(self, monkeypatch: pytest.MonkeyPatch):
        """_encode_array は numpy 配列を返す（.tolist() 前の内部表現）。"""
        _patch_tokenizers(monkeypatch)
        result = embedding._encode_array(["hello"])
        assert isinstance(result, np.ndarray)

    def test_encode_array_shape(self, monkeypatch: pytest.MonkeyPatch):
        """返される配列の shape は (batch, embedding_dim)。"""
        _patch_tokenizers(monkeypatch)
        result = embedding._encode_array(["a", "b"])
        assert result.shape == (2, 3)

    def test_encode_is_tolist_of_encode_array(self, monkeypatch: pytest.MonkeyPatch):
        """_encode の結果は _encode_array().tolist() と一致する。"""
        _patch_tokenizers(monkeypatch)
        arr = embedding._encode_array(["test"])
        lst = embedding._encode(["test"])
        assert lst == arr.tolist()


class TestVerifyTokenizer:
    """_verify_tokenizer のエッジケーステスト。"""

    def test_raises_when_no_tokenizer_entry_in_manifest(self, tmp_path: Path):
        """manifest に tokenizer.json エントリがない場合は ValueError。"""
        model_dir = tmp_path / "models"
        model_dir.mkdir(exist_ok=True)
        manifest = {
            "embeddings_sha256": "a" * 64,
            "auxiliary_files": [{"name": "config.json", "sha256": "b" * 64}],
        }
        (model_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        tok = model_dir / "tokenizer.json"
        tok.write_bytes(b"{}")

        with pytest.raises(ValueError, match="tokenizer.json のエントリがありません"):
            embedding._verify_tokenizer(tok, model_dir)

    def test_skips_non_tokenizer_auxiliary_entries(self, tmp_path: Path):
        """manifest に config.json エントリがあっても tokenizer.json を正しく検証する。"""
        tok_data = b"{}"
        tok_sha = hashlib.sha256(tok_data).hexdigest()
        model_dir = tmp_path / "models"
        model_dir.mkdir(exist_ok=True)
        manifest = {
            "embeddings_sha256": "a" * 64,
            "auxiliary_files": [
                {"name": "config.json", "sha256": "b" * 64},
                {"name": "tokenizer.json", "sha256": tok_sha},
            ],
        }
        (model_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        tok = model_dir / "tokenizer.json"
        tok.write_bytes(tok_data)

        # 例外が発生しなければ OK（config.json をスキップして tokenizer.json を検証）
        embedding._verify_tokenizer(tok, model_dir)


class TestTwoPhaseInitRollback:
    """2-phase 初期化ロールバックのテスト。"""

    def test_table_reset_when_tokenizer_fails(self, monkeypatch: pytest.MonkeyPatch):
        """トークナイザ構築が失敗した場合 _table と _tokenizer が None にリセットされる。"""

        class BrokenTokenizer:
            @staticmethod
            def from_file(path: str) -> None:
                raise RuntimeError("tokenizer broken")

        monkeypatch.setitem(sys.modules, "tokenizers", types.SimpleNamespace(Tokenizer=BrokenTokenizer))

        with pytest.raises(RuntimeError, match="tokenizer broken"):
            embedding.embed(["test"])

        # 失敗後はシングルトンがリセットされている
        assert embedding._table is None
        assert embedding._tokenizer is None

    def test_table_load_failure_resets_singletons(self, monkeypatch: pytest.MonkeyPatch):
        """テーブルロードが例外を出すとシングルトンが None リセットされる。"""
        _patch_tokenizers(monkeypatch)

        def _fail(_path: object) -> None:
            raise RuntimeError("bad npy")

        monkeypatch.setattr(embedding, "_build_table", _fail)

        with pytest.raises(RuntimeError, match="bad npy"):
            embedding.embed(["test"])

        assert embedding._table is None
        assert embedding._tokenizer is None


def test_embed_query_non_default_model_warns(monkeypatch) -> None:
    """既定と異なる embedding_model を渡すと警告しつつ既定モデルで処理する。"""
    monkeypatch.setattr(embedding, "_encode", lambda texts: [[0.1, 0.2]])
    result = embedding.embed_query("q", "other-model-xyz")
    assert result == [0.1, 0.2]
