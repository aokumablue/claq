"""ONNX Runtime ラッパー — 埋め込み生成。

sentence-transformers / torch / transformers に依存しない。
モデルは ~/.devgear/models/model.onnx を使用する。
install.sh が python3 -m model_build build を実行してモデルを生成する。
"""

from __future__ import annotations

import hmac
import json
import sys
import threading
from pathlib import Path
from typing import Any

from devgear.mem._paths import sha256_file as _sha256_file
from devgear.mem._paths import validate_sha256_format as _validate_sha256_format
from devgear.mem.logger import get as _get_logger
from devgear.mem.settings import _DEFAULT_EMBEDDING_MODEL, _DEFAULT_EMBEDDING_REVISION

log = _get_logger("EMBEDDING")


def _mean_pool_l2(token_embs: Any, attention_mask: Any) -> Any:
    """mean pooling + L2 正規化を適用する（ruri-v3 仕様）。"""
    import numpy as np  # type: ignore[import-untyped]

    mask = attention_mask.astype(np.float32)[:, :, np.newaxis]
    summed = (token_embs * mask).sum(axis=1)
    counts = mask.sum(axis=1).clip(min=1e-9)
    mean_vecs = summed / counts
    norms = np.linalg.norm(mean_vecs, axis=1, keepdims=True).clip(min=1e-9)
    return mean_vecs / norms


# 統合済み model.onnx は ~/.devgear/models/ に格納（install.sh が配置）
_MODELS_DIR = Path.home() / ".devgear" / "models"

# セッションはプロセス内でシングルトン（スレッドセーフ）
_session: Any = None
_tokenizer: Any = None
_lock = threading.Lock()
# model.onnx 不在警告を 1 度だけ出す（バックグラウンドビルド中の抑制）
_onnx_unavailable_warned: bool = False

# ruri-v3 の最大トークン長
_MAX_LENGTH = 512


def _verify_model_sha(models_dir: Path) -> None:
    """manifest.json の merged_sha256 と model.onnx の SHA256 を照合する。

    install 時に検証済みだが、起動時に 1 度だけ簡易チェックする。
    """
    manifest_path = models_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest.json が見つかりません: {manifest_path}\nplugins/devgear/install.sh を実行してモデルをビルドしてください。")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = manifest["merged_sha256"]
    _validate_sha256_format(expected, "merged_sha256")
    model_path = models_dir / "model.onnx"
    actual = _sha256_file(model_path)
    if not hmac.compare_digest(actual, expected):
        raise ValueError(
            f"model.onnx SHA256 不一致\n"
            f"  expected: {expected}\n"
            f"  actual:   {actual}\n"
            "install.sh を再実行してモデルを再統合してください。"
        )


def _verify_tokenizer(tok_path: Path, models_dir: Path) -> None:
    """manifest.json の auxiliary_files を参照して tokenizer.json の SHA256 を検証する。"""
    manifest_path = models_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for aux in manifest.get("auxiliary_files", []):
        if aux["name"] != "tokenizer.json":
            continue
        expected = aux["sha256"]
        _validate_sha256_format(expected, "tokenizer.json")
        actual = _sha256_file(tok_path)
        if not hmac.compare_digest(actual, expected):
            raise ValueError(
                f"tokenizer.json SHA256 不一致\n"
                f"  expected: {expected}\n"
                f"  actual:   {actual}"
            )
        return
    raise ValueError("manifest.json に tokenizer.json のエントリがありません")


def _get_session() -> tuple[Any, Any] | tuple[None, None]:
    """ONNX セッションとトークナイザをスレッドセーフにシングルトンでロードする。

    2-phase 初期化: new_session / new_tokenizer を完成させてから一括代入する。
    途中で例外が発生した場合は _session / _tokenizer を None にリセットして再 raise する。
    これにより、部分的に初期化された状態が外部から見えることを防ぐ（CWE-667 / 状態不整合防止）。

    model.onnx が存在しない場合（バックグラウンドビルド中など）は (None, None) を返す。
    _onnx_unavailable_warned はプロセス内で 1 度だけ警告を出すフラグ。
    ビルド完了後は model.onnx が配置されてこの分岐を通らなくなるため問題ない。
    ONNX ビルド完了後のモデル利用はプロセス再起動後に反映される。
    """
    global _session, _tokenizer, _onnx_unavailable_warned
    with _lock:
        if _session is None or _tokenizer is None:
            model_path = _MODELS_DIR / "model.onnx"
            tok_path = _MODELS_DIR / "tokenizer.json"

            if not model_path.exists():
                # model.onnx 不在はバックグラウンドビルド中として扱い、空結果で返す
                if not _onnx_unavailable_warned:
                    print("[embedding] onnx building, mem temporarily unavailable", file=sys.stderr)
                    _onnx_unavailable_warned = True
                return (None, None)
            if not tok_path.exists():
                raise FileNotFoundError(
                    f"tokenizer.json が見つかりません: {tok_path}\n"
                    "plugins/devgear/install.sh を実行してモデルを統合してください。"
                )

            import onnxruntime as ort  # type: ignore[import-untyped]
            from tokenizers import Tokenizer  # type: ignore[import-untyped]

            import onnx  # type: ignore[import-untyped]

            log.info(
                "モデルロード: %s@%s",
                _DEFAULT_EMBEDDING_MODEL,
                _DEFAULT_EMBEDDING_REVISION[:8],
            )

            try:
                # Phase 1: SHA 検証（ファイル操作のみ）
                _verify_model_sha(_MODELS_DIR)
                _verify_tokenizer(tok_path, _MODELS_DIR)

                # Phase 2: セッション構築（起動前に ONNX 構造を検証して改ざんを早期検知）
                onnx.checker.check_model(str(model_path))
                sess_opts = ort.SessionOptions()
                sess_opts.log_severity_level = 3  # ERROR のみ
                sess_opts.enable_mem_pattern = False
                sess_opts.intra_op_num_threads = 1
                new_session = ort.InferenceSession(
                    str(model_path),
                    sess_opts,
                    providers=["CPUExecutionProvider"],
                )

                # Phase 3: トークナイザ構築
                new_tokenizer = Tokenizer.from_file(str(tok_path))
                new_tokenizer.enable_padding(pad_token="[PAD]", length=_MAX_LENGTH)
                new_tokenizer.enable_truncation(max_length=_MAX_LENGTH)

                # Phase 4: 全成功時のみ一括代入
                _session = new_session
                _tokenizer = new_tokenizer

            except Exception:
                # 中途半端な状態を残さない
                _session = None
                _tokenizer = None
                raise

        return _session, _tokenizer


def _encode_array(texts: list[str]) -> Any:
    """テキストリストを ONNX 推論でベクトル化し numpy 配列を返す（内部 API）。

    mean pooling + L2 正規化を適用する（ruri-v3 仕様）。
    model.onnx が未完了の場合は None を返す。
    """
    import numpy as np  # type: ignore[import-untyped]

    session, tokenizer = _get_session()
    if session is None or tokenizer is None:
        return None
    encodings = tokenizer.encode_batch(texts)

    input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
    attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)

    inputs: dict = {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
    }
    # token_type_ids が必要なモデルにのみ渡す
    if any(inp.name == "token_type_ids" for inp in session.get_inputs()):
        inputs["token_type_ids"] = np.zeros_like(input_ids)

    outputs = session.run(None, inputs)
    token_embs = outputs[0]  # (batch, seq_len, hidden_dim)

    return _mean_pool_l2(token_embs, attention_mask)


def _encode(texts: list[str]) -> list[list[float]]:
    """テキストリストを ONNX 推論でベクトル化し Python リストを返す。

    model.onnx が未完了の場合は空リストを返す。
    """
    result = _encode_array(texts)
    if result is None:
        return []
    return result.tolist()


def embed(texts: list[str]) -> list[list[float]]:
    """テキストリストを埋め込みに変換する。"""
    if isinstance(texts, str):
        raise TypeError(f"embed() expects list[str], got str. Pass [{texts!r}] instead.")
    if not texts:
        return []
    return _encode(texts)


def embed_query(query: str, embedding_model: str) -> list[float]:
    """検索クエリを埋め込みに変換する。ruri-v3 推奨プレフィックスを付与する。

    embedding_model が既定モデルと異なる場合は警告を出す（ランタイムで差し替え不可）。
    model.onnx が未完了の場合は空リストを返す。
    """
    if embedding_model != _DEFAULT_EMBEDDING_MODEL:
        log.warning(
            "embed_query: embedding_model=%r は既定値 %r と異なります。既定モデルで処理します。",
            embedding_model,
            _DEFAULT_EMBEDDING_MODEL,
        )
    result = _encode([f"検索クエリ: {query}"])
    if not result:
        return []
    return result[0]
