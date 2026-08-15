#!/bin/sh
# Grok Build TUI 用: ~/.grok/plugins/bluecore を ~/.grok/installed-plugins/bluecore-<hash>
# （Grok CLI のインストール実体）へ symlink する。
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
# このスクリプトは自身の配置場所（$0）には依存せず、常に
# ~/.grok/installed-plugins/bluecore-* を直接探索して実体を見つける。
# そのため `curl ... | sh` のようにパイプ経由で実行され $0 が bash/sh に
# なる場合でも正しく動く（ダウンロードと同時実行が可能）。
#
# 使い方（ローカル実行）:
#   sh ~/.grok/installed-plugins/bluecore-*/scripts/grok.sh
# 実行ビットが立っていれば直接実行も可:
#   ~/.grok/installed-plugins/bluecore-*/scripts/grok.sh
# 配布スクリプトのダウンロードと同時実行も可（$0 に依存しないため成立する）:
#   curl -fsSL <配布スクリプトのURL> | sh
#   curl -fsSL <配布スクリプトのURL> | bash

INSTALLED_DIR="$HOME/.grok/installed-plugins"

SRC_DIR=""
for candidate in "$INSTALLED_DIR"/bluecore-*; do
    if [ -f "$candidate/src/bluecore/lib/grok_plugin_root.py" ]; then
        SRC_DIR="$candidate/src"
        break
    fi
done

if [ -z "$SRC_DIR" ]; then
    echo "no installed bluecore-* found under ~/.grok/installed-plugins"
    exit 0
fi

PYTHONPATH="$SRC_DIR" python3 -c '
from bluecore.lib.grok_plugin_root import ensure_grok_plugin_root_symlink

result = ensure_grok_plugin_root_symlink()
if result:
    print(f"linked -> {result}")
else:
    print("no installed bluecore-* found under ~/.grok/installed-plugins")
'
