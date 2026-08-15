#!/bin/sh
# Grok Build TUI 用: ~/.grok/plugins/bluecore を ~/.grok/installed-plugins/bluecore-<hash>
# （このスクリプトが置かれているインストール実体）へ symlink する。
#
# Grok は hooks.json の ${CLAUDE_PLUGIN_ROOT} を常に ~/.grok/plugins/<name> に
# 展開するが、CLI インストール実体は ~/.grok/installed-plugins/<name>-<hash>/
# に置かれる。symlink が無いと launcher.py が見つからず、Grok の全 PreToolUse
# フックが偽 deny になる。このスクリプトはその symlink を張る。
#
# 実行するタイミング: 初回インストール後、および `grok plugin update` の
# たびに再実行が必要（update のたびに <hash> が変わり、古い symlink は
# 切れるため）。自動修復は行わない（Claude/CopilotCLI 環境で Grok が同居
# インストールされている場合に、無関係なセッション開始のたびに Grok 側の
# 設定を無断で書き換える副作用を避けるため）。
#
# 使い方:
#   sh ~/.grok/installed-plugins/bluecore-*/scripts/link_grok_plugin.sh
# 実行ビットが立っていれば直接実行も可:
#   ~/.grok/installed-plugins/bluecore-*/scripts/link_grok_plugin.sh

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

PYTHONPATH="$ROOT/src" python3 -c '
from bluecore.lib.grok_plugin_root import ensure_grok_plugin_root_symlink

result = ensure_grok_plugin_root_symlink()
if result:
    print(f"linked -> {result}")
else:
    print("no installed bluecore-* found under ~/.grok/installed-plugins")
'
