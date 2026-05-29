"""model_build CLI — `python3 -m model_build <subcommand>` で実行する。

サブコマンド:
  build    ONNX 変換 → 量子化 → manifest 生成を一括実行
  verify   manifest.json を使ってモデルを検証
  clean    output_dir のモデルファイルと manifest を削除
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from model_build.quantize import DEFAULT_QUANT, QUANT_CHOICES

_BUILD_CONFIG_PATH = Path(__file__).resolve().parent / "build_config.json"
_DEFAULT_OUT = Path.home() / ".devgear" / "models"


def _load_build_config() -> dict:
    """build_config.json を読み込み、モデルメタデータを返す。"""
    if not _BUILD_CONFIG_PATH.exists():
        raise FileNotFoundError(f"build_config.json が見つかりません: {_BUILD_CONFIG_PATH}")
    config = json.loads(_BUILD_CONFIG_PATH.read_text(encoding="utf-8"))
    for key in ("model_name", "hf_revision", "model_type", "num_heads", "hidden_size", "embedding_dim", "tokenizer_max_length"):
        if key not in config:
            raise ValueError(f"build_config.json に必須キーがありません: '{key}'")
    return config


def _cmd_build(args: argparse.Namespace) -> None:
    """ONNX 変換 → 量子化 → manifest 生成を一括実行する。"""
    import hashlib
    import shutil
    import tempfile
    from datetime import UTC, datetime

    from model_build import __version__
    from model_build.export import export_to_onnx
    from model_build.quantize import quantize

    build_cfg = _load_build_config()
    output_dir: Path = args.out
    quant: str = args.quant

    with tempfile.TemporaryDirectory(prefix="devgear_build_") as tmp:
        tmp_path = Path(tmp)

        # Step 1: ONNX エクスポート（常に FP32 で取得し、後段で量子化）
        print(
            f"[build] Step 1/2: ONNX export ({args.model}@{args.revision[:8]})",
            flush=True,
        )
        raw_onnx = export_to_onnx(
            model_name=args.model,
            revision=args.revision,
            output_dir=tmp_path,
        )

        # Step 2: 量子化（fp32: コピー、fp16: ort optimizer、int8: 動的量子化）
        print(f"[build] Step 2/2: quantization ({quant}) → {output_dir}", flush=True)
        quant_onnx = tmp_path / f"model_{quant}.onnx"
        quantize(raw_onnx, quant_onnx, quant, num_heads=build_cfg["num_heads"], hidden_size=build_cfg["hidden_size"])

        # tokenizer.json / config.json を取得（エクスポート出力から）
        onnx_export_dir = raw_onnx.parent
        tokenizer_json = onnx_export_dir / "tokenizer.json"
        config_json = onnx_export_dir / "config.json"
        if not tokenizer_json.exists():
            raise FileNotFoundError(f"tokenizer.json が見つかりません: {tokenizer_json}")
        if not config_json.exists():
            raise FileNotFoundError(f"config.json が見つかりません: {config_json}")

        output_dir.mkdir(parents=True, exist_ok=True)

        # model.onnx を出力先にコピー
        dst_onnx = output_dir / "model.onnx"
        shutil.copy2(quant_onnx, dst_onnx)

        # tokenizer.json / config.json をコピーして SHA256 を計算
        def _sha256(p: Path) -> str:
            h = hashlib.sha256()
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            return h.hexdigest()

        dst_tok = output_dir / "tokenizer.json"
        dst_cfg = output_dir / "config.json"
        shutil.copy2(tokenizer_json, dst_tok)
        shutil.copy2(config_json, dst_cfg)

        manifest = {
            "model_name": args.model,
            "hf_revision": args.revision,
            "quantization": quant,
            "embedding_dim": build_cfg["embedding_dim"],
            "tokenizer_max_length": build_cfg["tokenizer_max_length"],
            "merged_sha256": _sha256(dst_onnx),
            "auxiliary_files": [
                {"name": "tokenizer.json", "sha256": _sha256(dst_tok)},
                {"name": "config.json", "sha256": _sha256(dst_cfg)},
            ],
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "tool_version": f"model_build/{__version__}",
        }
        manifest_path = output_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"[build] manifest: {manifest_path}", flush=True)

    print("[build] complete", flush=True)


def _cmd_verify(args: argparse.Namespace) -> None:
    """manifest.json を使って分割済みモデルを検証する。"""
    from model_build.verify import verify

    verify(args.model_dir, cosine_threshold=args.cosine_threshold)


def _cmd_download(args: argparse.Namespace) -> int:
    """onnx.json に従って配布アーカイブを取得する。"""
    from model_build.download import download_model_bundle

    return download_model_bundle(args.config, args.out)


def _cmd_clean(args: argparse.Namespace) -> None:
    """output_dir のモデルファイルと manifest を削除する。

    symlink は対象外（生成済みファイルは実ファイル前提）。
    """
    output_dir: Path = args.out
    if not output_dir.exists():
        print(f"[clean] Directory not found: {output_dir}", flush=True)
        return
    removed = 0
    for name in ("model.onnx", "tokenizer.json", "config.json", "manifest.json"):
        p = output_dir / name
        if p.exists() and not p.is_symlink():
            p.unlink()
            removed += 1
    print(f"[clean] Removed {removed} files: {output_dir}", flush=True)


def main() -> None:
    """CLI エントリポイント。"""
    parser = argparse.ArgumentParser(
        prog="python3 -m model_build",
        description="devgear メンテナ向け ONNX ビルドツール",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- build ---
    _build_cfg = _load_build_config()
    p_build = sub.add_parser("build", help="ONNX 変換・量子化・分割を一括実行")
    p_build.add_argument("--model", default=_build_cfg["model_name"], help="HF Hub モデル ID")
    p_build.add_argument("--revision", default=_build_cfg["hf_revision"], help="HF Hub commit SHA")
    p_build.add_argument(
        "--quant",
        default=DEFAULT_QUANT,
        choices=QUANT_CHOICES,
        help=f"量子化レベル (default: {DEFAULT_QUANT})",
    )
    p_build.add_argument("--out", type=Path, default=_DEFAULT_OUT, help="出力ディレクトリ")

    # --- verify ---
    p_verify = sub.add_parser("verify", help="分割済みモデルの検証")
    p_verify.add_argument(
        "--model-dir",
        type=Path,
        default=_DEFAULT_OUT,
        help="manifest.json が存在するディレクトリ",
    )
    p_verify.add_argument(
        "--cosine-threshold",
        type=float,
        default=0.999,
        help="再現性チェックの最低 cosine 類似度 (default: 0.999)",
    )

    # --- download ---
    p_download = sub.add_parser("download", help="外部配布アーカイブからモデルを取得")
    p_download.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "onnx" / "onnx.json",
        help="download 設定の JSON",
    )
    p_download.add_argument("--out", type=Path, default=_DEFAULT_OUT, help="出力ディレクトリ")

    # --- clean ---
    p_clean = sub.add_parser("clean", help="生成済み part・manifest を削除")
    p_clean.add_argument("--out", type=Path, default=_DEFAULT_OUT, help="対象ディレクトリ")

    args = parser.parse_args()

    try:
        if args.command == "build":
            _cmd_build(args)
        elif args.command == "verify":
            _cmd_verify(args)
        elif args.command == "download":
            rc = _cmd_download(args)
            if rc != 0:
                sys.exit(rc)
        elif args.command == "clean":
            _cmd_clean(args)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
