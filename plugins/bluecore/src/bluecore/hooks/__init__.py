"""フック実装パッケージ。

各モジュールがハーネスから呼ばれるフック本体です。stdin / JSON / 出力は
`hook_common`、host 非依存の合併出力生成は `output_adapter` に集約します。
"""
