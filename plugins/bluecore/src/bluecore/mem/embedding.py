"""静的埋め込み推論 — 埋め込み生成。

sentence-transformers / torch / onnxruntime に依存しない。
モデルは ~/.bluecore/models/embeddings.npy（語彙×次元の静的テーブル）を使用する。
install.sh が bluecore.model_download → model_build build でモデルを配置する。

推論は sentence-transformers の StaticEmbedding と同仕様:
add_special_tokens=False でトークン化し、トークン埋め込みの平均を
L2 正規化して文ベクトルとする。テーブルは mmap で開くため、
プロセスあたりの実メモリ消費は参照したページ分のみに収まる。
"""

from __future__ import annotations

import hmac
import json
import sys
import threading
from pathlib import Path
from typing import Any

from bluecore.lib.constants import BASE_DIR_NAME
from bluecore.mem._paths import sha256_file as _sha256_file
from bluecore.mem._paths import validate_sha256_format as _validate_sha256_format
from bluecore.mem.logger import get as _get_logger
from bluecore.mem.settings import _DEFAULT_EMBEDDING_MODEL, _DEFAULT_EMBEDDING_REVISION

log = _get_logger("EMBEDDING")

# ビルド済み embeddings.npy は ~/.bluecore/models/ に格納（install.sh が配置）
_MODELS_DIR = Path.home() / BASE_DIR_NAME / "models"

# テーブルとトークナイザはプロセス内でシングルトン（スレッドセーフ）
_table: Any = None
_tokenizer: Any = None
_lock = threading.Lock()
# embeddings.npy 不在警告を 1 度だけ出す（ダウンロード中の抑制）
_model_unavailable_warned: bool = False


def _verify_model_sha(models_dir: Path) -> None:
    """manifest.json の embeddings_sha256 と embeddings.npy の SHA256 を照合する。

    install 時に検証済みだが、起動時に 1 度だけ簡易チェックする。
    """
    manifest_path = models_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest.json が見つかりません: {manifest_path}\nplugins/bluecore/install.sh を実行してモデルを取得してください。")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = manifest["embeddings_sha256"]
    _validate_sha256_format(expected, "embeddings_sha256")
    npy_path = models_dir / "embeddings.npy"
    actual = _sha256_file(npy_path)
    if not hmac.compare_digest(actual, expected):
        raise ValueError(
            f"embeddings.npy SHA256 不一致\n"
            f"  expected: {expected}\n"
            f"  actual:   {actual}\n"
            "install.sh を再実行してモデルを再取得してください。"
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


def _check_model_files(npy_path: Any, tok_path: Any) -> bool:
    """embeddings.npy / tokenizer.json の存在を確認し、不在時に警告を出す。

    embeddings.npy 不在は True（ダウンロード・ビルド中）、
    tokenizer.json 不在は例外を送出する。両ファイル存在時は False を返す。
    """
    global _model_unavailable_warned  # noqa: PLW0603
    if not npy_path.exists():
        if not _model_unavailable_warned:
            print("[embedding] model not ready, mem temporarily unavailable", file=sys.stderr)
            _model_unavailable_warned = True
        return True
    if not tok_path.exists():
        raise FileNotFoundError(
            f"tokenizer.json が見つかりません: {tok_path}\n"
            "plugins/bluecore/install.sh を実行してモデルを取得してください。"
        )
    return False


def _build_table(npy_path: Any) -> Any:
    """埋め込みテーブルを mmap で開く（SHA 検証済み前提）。

    mmap のため数十 MB のテーブル全体を読み込まず、実際に参照した
    トークン行のページだけが実メモリに載る。
    """
    import numpy as np  # type: ignore[import-untyped]

    return np.load(str(npy_path), mmap_mode="r")


def _build_tokenizer(tok_path: Any) -> Any:
    """トークナイザを構築する。

    静的埋め込みは平均プーリングのためパディング・トランケーション不要
    （StaticEmbedding 仕様: 系列長の制約なし）。
    """
    from tokenizers import Tokenizer  # type: ignore[import-untyped]

    return Tokenizer.from_file(str(tok_path))


def _load_model_unlocked() -> tuple[Any, Any] | tuple[None, None]:
    """ロック取得済みの状態でモデル初期化を行う内部関数。

    _lock を取得済みの前提で呼ぶ。モデルファイルの存在確認は
    呼び出し元（_get_model）が実施済み。
    """
    global _table, _tokenizer  # noqa: PLW0603
    npy_path = _MODELS_DIR / "embeddings.npy"
    tok_path = _MODELS_DIR / "tokenizer.json"

    log.info("モデルロード: %s@%s", _DEFAULT_EMBEDDING_MODEL, _DEFAULT_EMBEDDING_REVISION[:8])
    try:
        _verify_model_sha(_MODELS_DIR)
        _verify_tokenizer(tok_path, _MODELS_DIR)
        new_table = _build_table(npy_path)
        new_tokenizer = _build_tokenizer(tok_path)
        _table = new_table
        _tokenizer = new_tokenizer
    except Exception:
        _table = None
        _tokenizer = None
        raise

    return _table, _tokenizer


def _get_model() -> tuple[Any, Any] | tuple[None, None]:
    """埋め込みテーブルとトークナイザをスレッドセーフにシングルトンでロードする。

    2-phase 初期化: new_table / new_tokenizer を完成させてから一括代入する。
    途中で例外が発生した場合は _table / _tokenizer を None にリセットして再 raise する。
    これにより、部分的に初期化された状態が外部から見えることを防ぐ（CWE-667 / 状態不整合防止）。

    embeddings.npy が存在しない場合（ダウンロード・ビルド中など）は (None, None) を返す。
    _model_unavailable_warned はプロセス内で 1 度だけ警告を出すフラグ。
    ビルド完了後は embeddings.npy が配置されてこの分岐を通らなくなるため問題ない。
    モデル配置後の利用はプロセス再起動後に反映される。

    旧 ONNX 実装にあったプロセス間 fcntl ロックは廃止した: テーブルは
    mmap で開くため複数プロセスが同時ロードしてもページキャッシュを
    共有し、メモリスパイクが発生しない。
    """
    with _lock:
        if _table is None or _tokenizer is None:
            if _check_model_files(_MODELS_DIR / "embeddings.npy", _MODELS_DIR / "tokenizer.json"):
                return (None, None)
            return _load_model_unlocked()
        return _table, _tokenizer


def _encode_array(texts: list[str]) -> Any:
    """テキストリストを静的テーブル参照でベクトル化し numpy 配列を返す（内部 API）。

    StaticEmbedding 仕様: add_special_tokens=False でトークン化し、
    トークン埋め込みの平均を L2 正規化する。トークンが得られない
    テキスト（空文字列など）はゼロベクトルになる。
    embeddings.npy が未配置の場合は None を返す。
    """
    import numpy as np  # type: ignore[import-untyped]

    table, tokenizer = _get_model()
    if table is None or tokenizer is None:
        return None
    encodings = tokenizer.encode_batch(texts, add_special_tokens=False)

    dim = table.shape[1]
    vectors = np.zeros((len(texts), dim), dtype=np.float32)
    for i, enc in enumerate(encodings):
        if enc.ids:
            vectors[i] = np.asarray(table[enc.ids], dtype=np.float32).mean(axis=0)

    norms = np.linalg.norm(vectors, axis=1, keepdims=True).clip(min=1e-9)
    return vectors / norms


def _encode(texts: list[str]) -> list[list[float]]:
    """テキストリストを静的テーブル参照でベクトル化し Python リストを返す。

    embeddings.npy が未配置の場合は空リストを返す。
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
    """検索クエリを埋め込みに変換する。

    embedding_model が既定モデルと異なる場合は警告を出す（ランタイムで差し替え不可）。
    embeddings.npy が未配置の場合は空リストを返す。
    """
    if embedding_model != _DEFAULT_EMBEDDING_MODEL:
        log.warning(
            "embed_query: embedding_model=%r は既定値 %r と異なります。既定モデルで処理します。",
            embedding_model,
            _DEFAULT_EMBEDDING_MODEL,
        )
    result = _encode([query])
    if not result:
        return []
    return result[0]
