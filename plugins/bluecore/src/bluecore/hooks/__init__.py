"""フック実装パッケージ。

各モジュールがハーネスから呼ばれるフック本体です。stdin / JSON / 出力は
`hook_common`、ハーネス差分は `output_adapter` に集約します。
"""
