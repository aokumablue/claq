#!/bin/sh
# Grok Build TUI 用: ~/.grok/plugins/claq を ~/.grok/installed-plugins/claq-<hash>
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
# ~/.grok/installed-plugins/claq-* を直接探索して実体を見つける。
# 複数インストールが同居する場合は mtime 最新を選ぶ（Python 側の
# find_latest_installed_claq と同じ規則。両者が食い違うと、実行される
# bootstrap コードと symlink の張り先が別バージョンになる）。
# そのため `curl ... | sh` のようにパイプ経由で実行され $0 が bash/sh に
# なる場合でも正しく動く（ダウンロードと同時実行が可能）。
#
# 使い方（ローカル実行）:
#   curl -fsSL <配布スクリプトのURL> | sh
#   curl -fsSL <配布スクリプトのURL> | bash

# 候補の選択規則は grok_plugin_root.find_latest_installed_claq と揃える
# （mtime 最新）。以前はシェル側が glob の先頭＝辞書順を採っていたため、複数
# インストールが同居すると「実行される bootstrap コード」と「symlink の張り先」が
# 別バージョンになりえた（F-12）。選択は stdlib だけで済むので、PYTHONPATH を
# 用意する前に python3 へ委ねる。
SRC_DIR="$(python3 -c '
import pathlib
import sys

root = pathlib.Path.home() / ".grok" / "installed-plugins"
candidates = [
    path
    for path in (root.glob("claq-*") if root.is_dir() else [])
    if (path / "src" / "claq" / "lib" / "grok_plugin_root.py").is_file()
]
if not candidates:
    sys.exit(1)
print(max(candidates, key=lambda path: path.stat().st_mtime) / "src")
')" || {
    echo "no installed claq-* found under ~/.grok/installed-plugins" >&2
    exit 1
}

# 未導入・import 失敗・link 失敗はいずれも非 0 で返す。以前は「何もできなかった」
# 場合も exit 0 だったため、呼び出し側が成功と区別できなかった（F-12）。
PYTHONPATH="$SRC_DIR" python3 -c '
import sys

from claq.lib.grok_plugin_root import ensure_grok_plugin_root_symlink

result = ensure_grok_plugin_root_symlink()
if not result:
    print("could not link ~/.grok/plugins/claq", file=sys.stderr)
    sys.exit(1)
print(f"linked -> {result}")
'
