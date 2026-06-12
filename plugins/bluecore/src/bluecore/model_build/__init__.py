"""bluecore.model_build — 静的埋め込みモデル ビルドツール。

bluecore.model_download がダウンロードした model.safetensors から
埋め込みテーブル（embeddings.npy）と manifest.json を生成する。
numpy のみで動作し torch / onnxruntime を必要としない。
"""
