"""検証 — model.onnx を読み込んで推論・品質チェックを実行する。"""

from __future__ import annotations

import hmac
import json
import math
from pathlib import Path

from model_build._paths import safe_join as _safe_join
from model_build._paths import sha256_file as _sha256_file
from model_build._paths import validate_sha256_format as _validate_sha256_format


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """2 ベクトルのコサイン類似度を返す。"""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _infer_embedding(session: object, tokenizer: object, text: str) -> list[float]:
    """テキストを ONNX 推論でベクトル化し、mean pooling + L2 正規化を適用して返す。"""
    import numpy as np

    enc = tokenizer.encode(text)  # type: ignore[union-attr]
    input_ids = np.array([enc.ids], dtype=np.int64)
    attention_mask = np.array([enc.attention_mask], dtype=np.int64)
    inputs: dict = {"input_ids": input_ids, "attention_mask": attention_mask}
    if any(inp.name == "token_type_ids" for inp in session.get_inputs()):  # type: ignore[union-attr]
        inputs["token_type_ids"] = np.zeros_like(input_ids)

    outputs = session.run(None, inputs)  # type: ignore[union-attr]
    token_embs = outputs[0]  # (1, seq_len, hidden_dim)
    mask = attention_mask[0].astype(np.float32)[:, np.newaxis]
    summed = (token_embs[0] * mask).sum(axis=0)
    mean_vec = summed / mask.sum().clip(min=1e-9)
    norm = np.linalg.norm(mean_vec)
    return (mean_vec / norm).tolist()


def verify(model_dir: Path, cosine_threshold: float = 0.999) -> None:
    """manifest.json を読み込んで model.onnx を検証し、推論で品質を確認する。

    cosine_threshold: 再推論間のベクトル最低 cosine 類似度
    """
    manifest_path = model_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest.json が見つかりません: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # 1. model.onnx の SHA256 検証
    model_path = _safe_join(model_dir, "model.onnx")
    _validate_sha256_format(manifest["merged_sha256"], "merged_sha256")
    actual = _sha256_file(model_path)
    if not hmac.compare_digest(actual, manifest["merged_sha256"]):
        raise ValueError(
            f"model.onnx SHA256 不一致\n"
            f"  expected: {manifest['merged_sha256']}\n"
            f"  actual:   {actual}"
        )
    print("[verify] model.onnx SHA256 verification OK", flush=True)

    # 2. 補助ファイルの SHA256 検証
    for aux in manifest["auxiliary_files"]:
        _validate_sha256_format(aux["sha256"], aux["name"])
        aux_path = _safe_join(model_dir, aux["name"])
        actual_aux = _sha256_file(aux_path)
        if not hmac.compare_digest(actual_aux, aux["sha256"]):
            raise ValueError(
                f"補助ファイル SHA256 不一致: {aux['name']}\n"
                f"  expected: {aux['sha256']}\n"
                f"  actual:   {actual_aux}"
            )
    print("[verify] auxiliary file SHA256 verification OK", flush=True)

    # 3. 推論テスト（onnxruntime + tokenizers）
    model_bytes = model_path.read_bytes()
    _run_inference_check(model_bytes, model_dir, manifest, cosine_threshold)


def _check_dim(vectors: list[list[float]], dim: int) -> None:
    """推論結果の次元数が manifest と一致することを確認する。"""
    if len(vectors[0]) != dim:
        raise ValueError(f"次元数不一致: expected {dim}, got {len(vectors[0])}")
    print(f"[verify] inference OK: dim={dim}", flush=True)


def _check_l2_norm(vectors: list[list[float]]) -> None:
    """各ベクトルが L2 正規化済み（norm ≈ 1.0）であることを確認する。"""
    for i, vec in enumerate(vectors):
        norm_val = math.sqrt(sum(x * x for x in vec))
        if abs(norm_val - 1.0) >= 1e-3:
            raise ValueError(f"L2 ノルム不正 (vec {i}): {norm_val}")
    print("[verify] L2 norm verification OK", flush=True)


def _check_reproducibility(
    session: object,
    tokenizer: object,
    text: str,
    ref_vec: list[float],
    threshold: float,
) -> None:
    """同一入力で 2 回推論し、cosine 類似度が閾値以上であることを確認する。

    threshold 0.999 は CPU FP16 丸め誤差の上限として設定している。
    FP32 では完全一致（≈1.0）が期待できるが、INT8/FP16 ではわずかな誤差を許容する。
    """
    vec2 = _infer_embedding(session, tokenizer, text)
    sim = _cosine_similarity(ref_vec, vec2)
    if sim < threshold:
        raise ValueError(f"再現性チェック失敗: cosine={sim:.6f} < {threshold}")
    print(f"[verify] reproducibility check OK: cosine={sim:.6f}", flush=True)


def _run_inference_check(
    model_bytes: bytes,
    model_dir: Path,
    manifest: dict,
    cosine_threshold: float,
) -> None:
    """メモリ上の ONNX バイト列でサンプル推論を実行し、次元・正規化・再現性を検証する。"""
    import onnxruntime as ort  # type: ignore[import-untyped]
    from tokenizers import Tokenizer  # type: ignore[import-untyped]

    import onnx  # type: ignore[import-untyped]

    tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
    tokenizer.enable_padding(pad_token="[PAD]", length=manifest["tokenizer_max_length"])
    tokenizer.enable_truncation(max_length=manifest["tokenizer_max_length"])

    # InferenceSession より前に ONNX 構造を検証する（不正モデルの早期検知）
    try:
        onnx.checker.check_model(onnx.load_from_string(model_bytes))
    except Exception as exc:
        raise ValueError(f"ONNX 構造検証失敗: {exc}") from exc

    sess_opts = ort.SessionOptions()
    sess_opts.log_severity_level = 3
    session = ort.InferenceSession(model_bytes, sess_opts, providers=["CPUExecutionProvider"])

    test_texts = ["検索クエリ: 日本語のテスト文", "検索クエリ: ベクトル品質確認"]
    vectors = [_infer_embedding(session, tokenizer, t) for t in test_texts]

    _check_dim(vectors, manifest["embedding_dim"])
    _check_l2_norm(vectors)
    _check_reproducibility(session, tokenizer, test_texts[0], vectors[0], cosine_threshold)
    print("[verify] all verifications PASSED", flush=True)


def main(argv: list[str] | None = None) -> int:
    """CLI エントリポイント。--models-dir または DEVGEAR_MODELS_DIR 環境変数でパス上書き可能。"""
    import argparse
    import os
    import sys
    import traceback

    parser = argparse.ArgumentParser(description="model.onnx 品質検証")
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=Path(os.environ.get("DEVGEAR_MODELS_DIR", Path.home() / ".devgear" / "models")),
    )
    args = parser.parse_args(argv)
    try:
        verify(args.models_dir)
        return 0
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
