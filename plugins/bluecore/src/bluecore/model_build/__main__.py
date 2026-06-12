"""model_build CLI — `python3 -m bluecore.model_build <subcommand>` で実行する。

サブコマンド:
  build     model.safetensors から embeddings.npy 抽出 → manifest 生成を一括実行

build は bluecore.model_download がダウンロード済みの model.safetensors /
tokenizer.json を入力とする。numpy のみで動作し torch を必要としない。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_BUILD_CONFIG_PATH = Path(__file__).resolve().parent / "build_config.json"
_DEFAULT_OUT = Path.home() / ".bluecore" / "models"


def _load_build_config() -> dict:
    """build_config.json を読み込み、モデルメタデータを返す。"""
    if not _BUILD_CONFIG_PATH.exists():
        raise FileNotFoundError(f"build_config.json が見つかりません: {_BUILD_CONFIG_PATH}")
    config = json.loads(_BUILD_CONFIG_PATH.read_text(encoding="utf-8"))
    for key in ("model_name", "hf_revision", "vocab_size", "source_embedding_dim", "embedding_dim"):
        if key not in config:
            raise ValueError(f"build_config.json に必須キーがありません: '{key}'")
    return config


def _write_manifest(output_dir: Path, build_cfg: dict) -> None:
    """embeddings.npy・tokenizer.json の SHA256 を含む manifest.json を書き出す。"""
    from datetime import UTC, datetime
    from importlib.metadata import version

    from bluecore.mem._paths import sha256_file

    manifest = {
        "model_name": build_cfg["model_name"],
        "hf_revision": build_cfg["hf_revision"],
        "embedding_dim": build_cfg["embedding_dim"],
        "vocab_size": build_cfg["vocab_size"],
        "embeddings_sha256": sha256_file(output_dir / "embeddings.npy"),
        "auxiliary_files": [
            {"name": "tokenizer.json", "sha256": sha256_file(output_dir / "tokenizer.json")},
        ],
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tool_version": f"bluecore/{version('bluecore')}",
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[build] manifest: {manifest_path}", flush=True)


def _cmd_build(args: argparse.Namespace) -> None:
    """埋め込みテーブル抽出 → manifest 生成を一括実行する。

    入力の model.safetensors は抽出完了後に削除する
    （embeddings.npy があれば再ビルド不要のため保持する理由がない）。
    """
    from bluecore.model_build.extract import extract_embeddings

    build_cfg = _load_build_config()
    output_dir: Path = args.out

    st_path = output_dir / "model.safetensors"
    tok_path = output_dir / "tokenizer.json"
    if not st_path.exists():
        raise FileNotFoundError(f"model.safetensors が見つかりません: {st_path}\npython3 -m bluecore.model_download を先に実行してください。")
    if not tok_path.exists():
        raise FileNotFoundError(f"tokenizer.json が見つかりません: {tok_path}\npython3 -m bluecore.model_download を先に実行してください。")

    print(f"[build] Step 1/2: extract embeddings ({build_cfg['model_name']}@{build_cfg['hf_revision'][:8]}, dim={build_cfg['embedding_dim']})", flush=True)
    extract_embeddings(
        st_path,
        output_dir / "embeddings.npy",
        vocab_size=build_cfg["vocab_size"],
        source_dim=build_cfg["source_embedding_dim"],
        embedding_dim=build_cfg["embedding_dim"],
    )

    print("[build] Step 2/2: write manifest", flush=True)
    _write_manifest(output_dir, build_cfg)

    st_path.unlink()
    print("[build] complete", flush=True)


def _build_main_parser() -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    """CLI 用の ArgumentParser を構築し、サブコマンド引数を返す。"""
    parser = argparse.ArgumentParser(
        prog="python3 -m bluecore.model_build",
        description="bluecore 静的埋め込みモデル ビルドツール",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="ダウンロード済みファイルから embeddings.npy と manifest を生成")
    p_build.add_argument("--out", type=Path, default=_DEFAULT_OUT, help="model.safetensors の配置先 兼 出力ディレクトリ")

    return parser, parser.parse_args()


def main() -> None:
    """CLI エントリポイント。"""
    parser, args = _build_main_parser()

    try:
        if args.command == "build":
            _cmd_build(args)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
