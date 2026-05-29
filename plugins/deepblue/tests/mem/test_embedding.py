"""embedding モジュールのテスト（ONNX Runtime ベース）。"""

from __future__ import annotations

import hashlib
import json
import sys
import types
from pathlib import Path

import pytest

from devgear.mem import embedding

# ---- ONNX / tokenizers のモック構築ヘルパ ----

def _make_fake_ort(hidden_dim: int = 4):
    """onnxruntime のモック。encode 入力長をスコアとする偽推論を返す。"""
    import numpy as np

    class FakeInput:
        def __init__(self, name: str) -> None:
            self.name = name

    class FakeSession:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def get_inputs(self):
            return [FakeInput("input_ids"), FakeInput("attention_mask")]

        def run(self, output_names, inputs):
            # (batch, seq_len, hidden_dim) を返す。各次元は 1.0
            batch = inputs["input_ids"].shape[0]
            seq_len = inputs["input_ids"].shape[1]
            return [np.ones((batch, seq_len, hidden_dim), dtype=np.float32)]

    class FakeSessionOptions:
        log_severity_level = 3

    fake_ort = types.SimpleNamespace(
        InferenceSession=FakeSession,
        SessionOptions=FakeSessionOptions,
    )
    return fake_ort


def _make_fake_tokenizers(seq_len: int = 8):
    """tokenizers のモック。固定長の ids / attention_mask を返す。"""
    class FakeEncoding:
        ids = [1] * seq_len
        attention_mask = [1] * seq_len

    class FakeTokenizer:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def enable_padding(self, **kwargs) -> None:
            pass

        def enable_truncation(self, **kwargs) -> None:
            pass

        def encode_batch(self, texts):
            return [FakeEncoding() for _ in texts]

        @staticmethod
        def from_file(path: str) -> FakeTokenizer:
            return FakeTokenizer()

    return types.SimpleNamespace(Tokenizer=FakeTokenizer)


def _write_fake_model_dir(model_dir: Path, model_data: bytes = b"fake-onnx") -> None:
    """テスト用のモデルディレクトリ（model.onnx + manifest.json + tokenizer.json）を作成する。"""
    model_dir.mkdir(parents=True, exist_ok=True)
    merged_sha = hashlib.sha256(model_data).hexdigest()
    tok_data = b"{}"
    tok_sha = hashlib.sha256(tok_data).hexdigest()
    manifest = {
        "model_name": "cl-nagoya/ruri-v3-310m",
        "hf_revision": "abc123",
        "quantization": "fp16",
        "embedding_dim": 4,
        "tokenizer_max_length": 512,
        "merged_sha256": merged_sha,
        "auxiliary_files": [{"name": "tokenizer.json", "sha256": tok_sha}],
    }
    (model_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (model_dir / "model.onnx").write_bytes(model_data)
    (model_dir / "tokenizer.json").write_bytes(tok_data)


@pytest.fixture(autouse=True)
def reset_embedding_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """各テスト前に内部シングルトンをリセットし、モデルパスを tmp_path に向ける。"""
    monkeypatch.setattr(embedding, "_session", None)
    monkeypatch.setattr(embedding, "_tokenizer", None)
    model_dir = tmp_path / "models"
    _write_fake_model_dir(model_dir)
    monkeypatch.setattr(embedding, "_MODELS_DIR", model_dir)


def _patch_backends(monkeypatch: pytest.MonkeyPatch, hidden_dim: int = 4):
    """onnxruntime / tokenizers / onnx を monkeypatch でモックに差し替える。"""
    fake_ort = _make_fake_ort(hidden_dim)
    fake_tok = _make_fake_tokenizers()
    # onnx.checker.check_model を no-op にする（テスト用偽 ONNX は検証スキップ）
    mock_onnx = types.SimpleNamespace(
        checker=types.SimpleNamespace(check_model=lambda *_a, **_kw: None)
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setitem(sys.modules, "tokenizers", fake_tok)
    monkeypatch.setitem(sys.modules, "onnx", mock_onnx)
    # numpy は実物を使う（軽量なので問題なし）
    return fake_ort, fake_tok


class TestEmbed:
    """embed() の動作テスト。"""

    def test_empty_input_returns_empty_list(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """空リストを渡すとモデルをロードせずに空リストを返す。"""
        _patch_backends(monkeypatch)
        assert embedding.embed([]) == []
        assert embedding._session is None

    def test_embed_returns_list_of_vectors(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """テキストリストを渡すとベクトルのリストを返す。"""
        _patch_backends(monkeypatch, hidden_dim=4)
        result = embedding.embed(["hello", "world"])
        assert len(result) == 2
        assert isinstance(result[0], list)

    def test_embed_vectors_are_l2_normalized(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """返されるベクトルが L2 正規化されている（norm ≈ 1.0）。"""
        import math
        _patch_backends(monkeypatch, hidden_dim=4)
        result = embedding.embed(["test"])
        norm = math.sqrt(sum(x * x for x in result[0]))
        assert abs(norm - 1.0) < 1e-5

    def test_model_loaded_lazily(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """embed([]) ではモデルがロードされない（遅延ロード）。"""
        _patch_backends(monkeypatch)
        embedding.embed([])
        assert embedding._session is None

    def test_session_cached_on_second_call(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """2 回目の embed() でセッションが再ロードされない（シングルトン）。"""
        _patch_backends(monkeypatch)
        embedding.embed(["a"])
        session_first = embedding._session
        embedding.embed(["b"])
        assert embedding._session is session_first

    def test_str_input_raises_type_error(self, monkeypatch: pytest.MonkeyPatch):
        """文字列を直接渡すと明確な TypeError が出る（内部ライブラリの不明瞭なエラーを防ぐ）。"""
        _patch_backends(monkeypatch)
        with pytest.raises(TypeError, match="expects list\\[str\\]"):
            embedding.embed("not a list")


class TestEmbedQuery:
    """embed_query() の動作テスト。"""

    def test_returns_single_vector(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """単一クエリを渡すと 1 つのベクトルを返す。"""
        _patch_backends(monkeypatch, hidden_dim=4)
        result = embedding.embed_query("テスト", "cl-nagoya/ruri-v3-310m")
        assert isinstance(result, list)
        assert isinstance(result[0], float)

    def test_query_vector_is_l2_normalized(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """クエリベクトルが L2 正規化されている。"""
        import math
        _patch_backends(monkeypatch, hidden_dim=4)
        vec = embedding.embed_query("検索テスト", "cl-nagoya/ruri-v3-310m")
        norm = math.sqrt(sum(x * x for x in vec))
        assert abs(norm - 1.0) < 1e-5

    def test_returns_empty_list_when_model_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """model.onnx が不在の場合は空リストを返す。"""
        _patch_backends(monkeypatch)
        bad_dir = tmp_path / "no_model"
        bad_dir.mkdir()
        monkeypatch.setattr(embedding, "_MODELS_DIR", bad_dir)
        monkeypatch.setattr(embedding, "_onnx_unavailable_warned", False)
        result = embedding.embed_query("test query", "cl-nagoya/ruri-v3-310m")
        assert result == []


class TestModelNotFound:
    """モデルファイル未展開時の動作テスト。"""

    def test_missing_model_onnx_returns_empty_list(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        """model.onnx が存在しない場合は例外を出さず空リストを返す（ONNX バックグラウンドビルド中）。"""
        _patch_backends(monkeypatch)
        bad_dir = tmp_path / "no_model"
        bad_dir.mkdir()
        (bad_dir / "tokenizer.json").write_bytes(b"{}")
        monkeypatch.setattr(embedding, "_MODELS_DIR", bad_dir)
        monkeypatch.setattr(embedding, "_onnx_unavailable_warned", False)
        result = embedding.embed(["test"])
        assert result == []
        assert "onnx building" in capsys.readouterr().err

    def test_missing_model_onnx_warns_only_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        """model.onnx が存在しない場合、警告は 1 度だけ出力される。"""
        _patch_backends(monkeypatch)
        bad_dir = tmp_path / "no_model"
        bad_dir.mkdir()
        (bad_dir / "tokenizer.json").write_bytes(b"{}")
        monkeypatch.setattr(embedding, "_MODELS_DIR", bad_dir)
        monkeypatch.setattr(embedding, "_onnx_unavailable_warned", False)
        embedding.embed(["test"])
        embedding.embed(["test2"])
        err = capsys.readouterr().err
        assert err.count("onnx building") == 1

    def test_missing_manifest_raises_file_not_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """manifest.json が存在しない場合は FileNotFoundError。"""
        _patch_backends(monkeypatch)
        bad_dir = tmp_path / "no_manifest"
        bad_dir.mkdir()
        # model.onnx と tokenizer.json は必要（_verify_model_sha に到達するため）
        (bad_dir / "model.onnx").write_bytes(b"fake")
        (bad_dir / "tokenizer.json").write_bytes(b"{}")
        # manifest.json は置かない → _verify_model_sha が FileNotFoundError を出す
        monkeypatch.setattr(embedding, "_MODELS_DIR", bad_dir)
        with pytest.raises(FileNotFoundError, match="manifest.json"):
            embedding.embed(["test"])

    def test_missing_tokenizer_raises_file_not_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """tokenizer.json が存在しない場合は FileNotFoundError（model.onnx は存在する異常状態）。"""
        _patch_backends(monkeypatch)
        bad_dir = tmp_path / "no_tok"
        bad_dir.mkdir()
        (bad_dir / "model.onnx").write_bytes(b"fake")
        monkeypatch.setattr(embedding, "_MODELS_DIR", bad_dir)
        with pytest.raises(FileNotFoundError, match="tokenizer.json"):
            embedding.embed(["test"])

    def test_model_sha256_mismatch_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """model.onnx の SHA256 が manifest と不一致なら ValueError。"""
        _patch_backends(monkeypatch)
        bad_dir = tmp_path / "bad_sha"
        bad_dir.mkdir()
        # model.onnx を改竄（SHA が変わる）
        _write_fake_model_dir(bad_dir)
        (bad_dir / "model.onnx").write_bytes(b"tampered-data")
        monkeypatch.setattr(embedding, "_MODELS_DIR", bad_dir)
        with pytest.raises(ValueError, match="SHA256 不一致"):
            embedding.embed(["test"])

    def test_tokenizer_sha256_mismatch_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """tokenizer.json の SHA256 が manifest と不一致なら ValueError。"""
        _patch_backends(monkeypatch)
        bad_dir = tmp_path / "bad_tok_sha"
        bad_dir.mkdir()
        _write_fake_model_dir(bad_dir)
        # tokenizer.json を改竄
        (bad_dir / "tokenizer.json").write_bytes(b"tampered-tokenizer")
        monkeypatch.setattr(embedding, "_MODELS_DIR", bad_dir)
        with pytest.raises(ValueError, match="tokenizer.json SHA256 不一致"):
            embedding.embed(["test"])


class TestTokenTypeIdsBranch:
    """token_type_ids が必要なモデルの分岐をカバーするテスト。"""

    def test_token_type_ids_inserted_when_required(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """token_type_ids 入力を持つモデルでは zeros_like が渡される。"""
        import numpy as np

        captured: dict = {}

        class FakeInput:
            def __init__(self, name: str) -> None:
                self.name = name

        class FakeSession:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def get_inputs(self):
                return [
                    FakeInput("input_ids"),
                    FakeInput("attention_mask"),
                    FakeInput("token_type_ids"),
                ]

            def run(self, output_names, inputs):
                captured.update(inputs)
                batch = inputs["input_ids"].shape[0]
                seq_len = inputs["input_ids"].shape[1]
                return [np.ones((batch, seq_len, 4), dtype=np.float32)]

        fake_ort = types.SimpleNamespace(
            InferenceSession=FakeSession,
            SessionOptions=type("SO", (), {"log_severity_level": 3}),
        )
        mock_onnx = types.SimpleNamespace(
            checker=types.SimpleNamespace(check_model=lambda *_a, **_kw: None)
        )
        monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
        monkeypatch.setitem(sys.modules, "tokenizers", _make_fake_tokenizers())
        monkeypatch.setitem(sys.modules, "onnx", mock_onnx)

        result = embedding.embed(["x"])
        assert "token_type_ids" in captured
        assert len(result) == 1


class TestEncodeArray:
    """_encode_array() の内部 API テスト。"""

    def test_returns_numpy_array(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """_encode_array は numpy 配列を返す（.tolist() 前の内部表現）。"""
        import numpy as np
        _patch_backends(monkeypatch, hidden_dim=4)
        result = embedding._encode_array(["hello"])
        assert isinstance(result, np.ndarray)

    def test_encode_array_shape(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """返される配列の shape は (batch, hidden_dim)。"""
        _patch_backends(monkeypatch, hidden_dim=4)
        result = embedding._encode_array(["a", "b"])
        assert result.shape == (2, 4)

    def test_encode_is_tolist_of_encode_array(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """_encode の結果は _encode_array().tolist() と一致する。"""
        _patch_backends(monkeypatch, hidden_dim=4)
        arr = embedding._encode_array(["test"])
        lst = embedding._encode(["test"])
        assert lst == arr.tolist()


class TestVerifyTokenizer:
    """_verify_tokenizer のエッジケーステスト。"""

    def test_raises_when_no_tokenizer_entry_in_manifest(self, tmp_path: Path):
        """manifest に tokenizer.json エントリがない場合は ValueError。"""
        import json

        model_dir = tmp_path / "models"
        model_dir.mkdir(exist_ok=True)
        manifest = {
            "merged_sha256": "a" * 64,
            "auxiliary_files": [{"name": "config.json", "sha256": "b" * 64}],
        }
        (model_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        tok = model_dir / "tokenizer.json"
        tok.write_bytes(b"{}")

        with pytest.raises(ValueError, match="tokenizer.json のエントリがありません"):
            embedding._verify_tokenizer(tok, model_dir)

    def test_skips_non_tokenizer_auxiliary_entries(self, tmp_path: Path):
        """manifest に config.json エントリがあっても tokenizer.json を正しく検証する。"""
        import hashlib
        import json

        tok_data = b"{}"
        tok_sha = hashlib.sha256(tok_data).hexdigest()
        model_dir = tmp_path / "models"
        model_dir.mkdir(exist_ok=True)
        manifest = {
            "merged_sha256": "a" * 64,
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
    """2-phase 初期化ロールバック（H-4）のテスト。"""

    def test_session_reset_when_tokenizer_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """トークナイザ構築が失敗した場合 _session と _tokenizer が None にリセットされる。"""
        import numpy as np

        class FakeSessionOptions:
            log_severity_level = 3
            enable_mem_pattern = False
            intra_op_num_threads = 1

        class FakeInput:
            def __init__(self, name: str) -> None:
                self.name = name

        class FakeSession:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def get_inputs(self):
                return [FakeInput("input_ids"), FakeInput("attention_mask")]

            def run(self, output_names, inputs):
                batch = inputs["input_ids"].shape[0]
                seq_len = inputs["input_ids"].shape[1]
                return [np.ones((batch, seq_len, 4), dtype=np.float32)]

        class BrokenTokenizer:
            @staticmethod
            def from_file(path: str) -> None:
                raise RuntimeError("tokenizer broken")

        fake_ort = types.SimpleNamespace(
            InferenceSession=FakeSession,
            SessionOptions=FakeSessionOptions,
        )
        mock_onnx = types.SimpleNamespace(
            checker=types.SimpleNamespace(check_model=lambda *_a, **_kw: None)
        )
        monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
        monkeypatch.setitem(sys.modules, "tokenizers", types.SimpleNamespace(Tokenizer=BrokenTokenizer))
        monkeypatch.setitem(sys.modules, "onnx", mock_onnx)

        with pytest.raises(RuntimeError, match="tokenizer broken"):
            embedding.embed(["test"])

        # 失敗後はシングルトンがリセットされている
        assert embedding._session is None
        assert embedding._tokenizer is None


class TestOnnxCheckerIntegration:
    """onnx.checker.check_model の統合テスト（Phase 5 LS-2）。"""

    def test_invalid_onnx_resets_singletons(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """onnx.checker が例外を出すとシングルトンが None リセットされる。"""
        # autouse フィクスチャが _MODELS_DIR を tmp_path/models に設定済みだが、
        # このテストでは別の model_dir を使う
        model_dir = tmp_path / "onnx_check_test"
        _write_fake_model_dir(model_dir)
        monkeypatch.setattr(embedding, "_MODELS_DIR", model_dir)
        monkeypatch.setattr(embedding, "_session", None)
        monkeypatch.setattr(embedding, "_tokenizer", None)

        # onnx.checker.check_model を失敗させるモック
        def _fail(*_a: object, **_kw: object) -> None:
            raise RuntimeError("bad onnx")

        mock_onnx = types.SimpleNamespace(
            checker=types.SimpleNamespace(check_model=_fail)
        )
        monkeypatch.setitem(sys.modules, "onnx", mock_onnx)

        with pytest.raises(RuntimeError, match="bad onnx"):
            embedding.embed(["test"])

        assert embedding._session is None
        assert embedding._tokenizer is None

    def test_valid_onnx_passes_checker(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """onnx.checker が成功すれば通常の推論フローに進む。"""
        model_dir = tmp_path / "onnx_pass_test"
        _write_fake_model_dir(model_dir)
        monkeypatch.setattr(embedding, "_MODELS_DIR", model_dir)
        monkeypatch.setattr(embedding, "_session", None)
        monkeypatch.setattr(embedding, "_tokenizer", None)

        fake_ort = _make_fake_ort(hidden_dim=4)
        fake_tok = _make_fake_tokenizers(seq_len=8)

        # onnx.checker を no-op に差し替える
        mock_onnx = types.SimpleNamespace(
            checker=types.SimpleNamespace(check_model=lambda *_a, **_kw: None)
        )
        monkeypatch.setitem(sys.modules, "onnx", mock_onnx)
        monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
        monkeypatch.setitem(sys.modules, "tokenizers", fake_tok)

        result = embedding.embed(["hello"])
        assert isinstance(result, list)
        assert len(result) == 1
