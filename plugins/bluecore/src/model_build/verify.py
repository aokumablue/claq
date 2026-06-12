"""検証 — embeddings.npy を読み込んで推論・品質チェックを実行する。

bluecore.mem.embedding と同じ推論仕様（add_special_tokens=False のトークン化 →
テーブル参照の平均 → L2 正規化）で検証する。model_build は配布物として
独立しているため bluecore パッケージは import せず、同一仕様を実装する。
"""

from __future__ import annotations

import hmac
import json
import math
from pathlib import Path
from typing import Any

from model_build._paths import safe_join as _safe_join
from model_build._paths import sha256_file as _sha256_file
from model_build._paths import validate_sha256_format as _validate_sha256_format

# 意味的サニティチェック: 類似ペアの cosine が非類似ペアを上回ることを確認する
_SIMILAR_PAIR = ("今日の天気は晴れです", "本日は快晴です")
_DISSIMILAR_TEXT = "データベースのインデックス設計"


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """2 ベクトルのコサイン類似度を返す。"""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _infer_embedding(table: Any, tokenizer: Any, text: str) -> list[float]:
    """テキストを静的埋め込みテーブルでベクトル化し、L2 正規化して返す。

    sentence-transformers の StaticEmbedding と同仕様:
    add_special_tokens=False でトークン化し、トークン埋め込みの平均を取る。
    """
    import numpy as np  # type: ignore[import-untyped]

    enc = tokenizer.encode(text, add_special_tokens=False)
    if not enc.ids:
        return [0.0] * table.shape[1]
    mean_vec = np.asarray(table[enc.ids], dtype=np.float32).mean(axis=0)
    norm = np.linalg.norm(mean_vec)
    if norm < 1e-9:
        return [0.0] * table.shape[1]
    return (mean_vec / norm).tolist()


def verify(model_dir: Path, cosine_threshold: float = 0.999) -> None:
    """manifest.json を読み込んで embeddings.npy を検証し、推論で品質を確認する。

    cosine_threshold: 再推論間のベクトル最低 cosine 類似度
    """
    manifest_path = model_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest.json が見つかりません: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # 1. embeddings.npy の SHA256 検証
    npy_path = _safe_join(model_dir, "embeddings.npy")
    _validate_sha256_format(manifest["embeddings_sha256"], "embeddings_sha256")
    actual = _sha256_file(npy_path)
    if not hmac.compare_digest(actual, manifest["embeddings_sha256"]):
        raise ValueError(
            f"embeddings.npy SHA256 不一致\n"
            f"  expected: {manifest['embeddings_sha256']}\n"
            f"  actual:   {actual}"
        )
    print("[verify] embeddings.npy SHA256 verification OK", flush=True)

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

    # 3. 推論テスト（numpy + tokenizers）
    _run_inference_check(model_dir, manifest, cosine_threshold)


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
    table: Any,
    tokenizer: Any,
    text: str,
    ref_vec: list[float],
    threshold: float,
) -> None:
    """同一入力で 2 回推論し、cosine 類似度が閾値以上であることを確認する。

    静的テーブル参照は決定的なため、ほぼ完全一致（cosine ≈ 1.0）が期待できる。
    """
    vec2 = _infer_embedding(table, tokenizer, text)
    sim = _cosine_similarity(ref_vec, vec2)
    if sim < threshold:
        raise ValueError(f"再現性チェック失敗: cosine={sim:.6f} < {threshold}")
    print(f"[verify] reproducibility check OK: cosine={sim:.6f}", flush=True)


def _check_semantics(table: Any, tokenizer: Any) -> None:
    """類似文ペアの cosine が非類似ペアを上回ることを確認する。

    テーブル・トークナイザの組み合わせ間違い（語彙とテーブルの不整合）は
    SHA 検証では検出できず、推論結果の意味的な崩れとして現れるため、
    既知の類似/非類似ペアで簡易チェックする。
    """
    vec_a = _infer_embedding(table, tokenizer, _SIMILAR_PAIR[0])
    vec_b = _infer_embedding(table, tokenizer, _SIMILAR_PAIR[1])
    vec_c = _infer_embedding(table, tokenizer, _DISSIMILAR_TEXT)
    sim_ab = _cosine_similarity(vec_a, vec_b)
    sim_ac = _cosine_similarity(vec_a, vec_c)
    if sim_ab <= sim_ac:
        raise ValueError(f"意味的サニティチェック失敗: similar={sim_ab:.4f} <= dissimilar={sim_ac:.4f}")
    print(f"[verify] semantic sanity check OK: similar={sim_ab:.4f} > dissimilar={sim_ac:.4f}", flush=True)


def _run_inference_check(
    model_dir: Path,
    manifest: dict,
    cosine_threshold: float,
) -> None:
    """embeddings.npy でサンプル推論を実行し、次元・正規化・再現性・意味を検証する。"""
    import numpy as np  # type: ignore[import-untyped]
    from tokenizers import Tokenizer  # type: ignore[import-untyped]

    tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
    table = np.load(model_dir / "embeddings.npy", mmap_mode="r")

    if table.shape != (manifest["vocab_size"], manifest["embedding_dim"]):
        raise ValueError(f"テーブル shape 不一致: expected {(manifest['vocab_size'], manifest['embedding_dim'])}, got {table.shape}")

    test_texts = ["日本語のテスト文", "ベクトル品質確認"]
    vectors = [_infer_embedding(table, tokenizer, t) for t in test_texts]

    _check_dim(vectors, manifest["embedding_dim"])
    _check_l2_norm(vectors)
    _check_reproducibility(table, tokenizer, test_texts[0], vectors[0], cosine_threshold)
    _check_semantics(table, tokenizer)
    print("[verify] all verifications PASSED", flush=True)


def main(argv: list[str] | None = None) -> int:
    """CLI エントリポイント。--models-dir または BLUECORE_MODELS_DIR 環境変数でパス上書き可能。"""
    import argparse
    import os
    import sys
    import traceback

    parser = argparse.ArgumentParser(description="embeddings.npy 品質検証")
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=Path(os.environ.get("BLUECORE_MODELS_DIR", Path.home() / ".bluecore" / "models")),
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
