"""フック実装パッケージ。

各モジュールがハーネスから呼ばれるフック本体です。共通判定と出力は `hook_common`、シェル文字列の解析は
`shell_tokens` に集約します。stdin の読み取りと JSON 化（`stdin_io`）と host 非依存の合併出力生成
（`output_adapter`）は `claq.lib` にあります。
"""
