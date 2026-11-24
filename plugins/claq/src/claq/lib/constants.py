"""プラグイン全体で共有する定数。パス・名前のリネーム時はここだけ変更する。"""

PLUGIN_NAME = "claq"
BASE_DIR_NAME = f".{PLUGIN_NAME}"  # ".claq"
HOOK_ENTRYPOINT = f"{PLUGIN_NAME}-hook"  # hooks.json が起動する POSIX の起動ファイル名
HOOK_ENTRYPOINT_CMD = f"{HOOK_ENTRYPOINT}.cmd"  # Windows の起動ファイル名
RUNTIME_DIR_NAME = "runtime"  # plugin root 配下の起動ファイル・env-template の置き場
LOG_DIR_NAME = "logs"  # ~/.claq（とデータディレクトリ）配下のログの置き場
