"""フック実装パッケージ。

各モジュールがハーネスから呼ばれるフック本体です。stdin の読み取りと JSON 化は
`stdin_io`、共通判定と出力は `hook_common`、host 非依存の合併出力生成は `output_adapter` に集約します。
"""
