"""redux — コマンド別・宣言的トークン節約フィルタエンジン。

Bash ツール出力をコマンド種別に応じて圧縮し、LLM へ渡すトークンを削減する。
宣言的フィルタ（``filters/*.toml``）でコマンドごとの圧縮ルールを定義し、
``command_pattern`` に一致しないコマンドは ``default.toml`` の汎用戦略で圧縮する。

実装起点:
  - :mod:`bluecore.redux.engine`     — パイプライン適用・フィルタ選択
  - :mod:`bluecore.redux.loader`     — TOML フィルタ定義の読み込み（優先順位付き）
  - :mod:`bluecore.redux.strategies` — アルゴリズム的圧縮戦略（smart_filter/dedup ほか）
  - :mod:`bluecore.redux.config`     — 圧縮パイプライン設定
"""
