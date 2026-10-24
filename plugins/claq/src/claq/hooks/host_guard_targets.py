"""保護フックそのものを止められる、リポジトリ外の書込み先の判定。

`bash_config_protection` が repo スコープ判定より前に呼ぶ。対象はプラグイン自身の
``hooks/hooks.json``（PreToolUse ガードの登録元）と、ホスト設定 ``settings.json`` /
``settings.local.json``（`CLAQ_PYTHON`・`hooks`・プラグインの無効化を持てる）。Edit/Write
（`config_protection`）は同じ書込みを deny するため、Bash だけ repo 外として allow にすると
片側だけ塞いだ状態になる（docs/adr/hook-failure-direction.md「`config_protection` は
プラグイン自身の `hooks.json` と、ホスト設定の無効化口を守る」）。

Bash の書込みは内容を解釈できないので、Edit/Write のような内容判定はせず位置で塞ぐ。
位置は 2 系統で見る。

1. 名前の構造一致: ``hooks/hooks.json`` と ``.claude/settings*.json`` の連続要素。書いた名前
   （raw token）と symlink の実体のどちらで一致しても塞ぐ。`cd` を含むコマンドでは解決基準が
   ずれるため、basename ``hooks.json`` だけでも塞ぐ（`.gitconfig` の basename 判定と同じ扱い）。
2. 実際の置き場所: 自プラグインの ``hooks/hooks.json`` と、ホーム・``CLAUDE_CONFIG_DIR`` 配下の
   ホスト設定を解決した実体。書込み先がその実体自身か祖先ディレクトリなら塞ぐ。実体パスで直接書く
   形（dotfiles 管理の symlink 先）と、ディレクトリごと消す・退避する形（``rm -rf <plugin>/hooks`` /
   ``mv ~/.claude x``）は名前の一致では捉えられない。

構造一致なので別リポジトリの ``.claude/settings.json`` も塞ぎ、祖先一致なのでホーム直下への
``cp x ~/`` も塞ぐ。いずれも悪用経路にならない誤検出側の広さとして許容する
（docs/adr/shell-analysis-boundary.md）。
"""

from __future__ import annotations

import os
from pathlib import Path

from claq.hooks.config_protection import protected_path_segment
from claq.hooks.hook_common import basename, normalize_protected_name, resolve_effective_target
from claq.lib.core_utils import get_home_dir, get_plugin_root

# `protected_path_segment` がプラグインの PreToolUse ガード登録元に返す表示名。
_PLUGIN_HOOKS_JSON_SEGMENT = "hooks/hooks.json"
_PLUGIN_HOOKS_JSON_DISPLAY = f"{_PLUGIN_HOOKS_JSON_SEGMENT}/"
_HOOKS_JSON_NAME = "hooks.json"

# ホスト設定の置き場（``.claude/`` 直下）と名前の構造一致。
_HOST_SETTINGS_PATH_SEGMENTS = ((".claude", "settings.json"), (".claude", "settings.local.json"))


def match_folded_path_window(
    parts: tuple[str, ...], candidates: tuple[tuple[str, ...], ...]
) -> tuple[str, ...] | None:
    """パス要素の連続した並びが候補のいずれかに一致するかを、畳んだ名前で判定する。

    Args:
        parts: 判定するパスの要素列。
        candidates: 連続一致を探す要素列の候補（畳む前の綴り）。

    Returns:
        一致した候補（畳む前の綴り）。一致しなければ None。

    Raises:
        例外は発生しません。
    """
    folded_parts = tuple(normalize_protected_name(part) for part in parts)
    for segments in candidates:
        folded_segments = tuple(normalize_protected_name(part) for part in segments)
        width = len(folded_segments)
        for index in range(len(folded_parts) - width + 1):
            if folded_parts[index : index + width] == folded_segments:
                return segments
    return None


def _guarded_locations() -> list[tuple[Path, str]]:
    """保護する実体の置き場所と表示名の組を返す。

    ホームを決められない環境ではホーム配下を省く（名前の構造一致は残る）。ここで例外を
    貫通させると hook が exit 1（PreToolUse では non-blocking＝ツール実行）で落ち、
    構造一致まで含めた判定ごと失われる。

    Returns:
        ``(解決済みの実体 path, 表示名)`` のリスト。

    Raises:
        例外は発生しません。
    """
    candidates = [(lambda: get_plugin_root() / "hooks" / _HOOKS_JSON_NAME, _PLUGIN_HOOKS_JSON_DISPLAY)]
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    config_dirs = [lambda: get_home_dir() / ".claude"]
    if configured:
        config_dirs.append(lambda: Path(configured).expanduser())
    for config_dir in config_dirs:
        for directory, name in _HOST_SETTINGS_PATH_SEGMENTS:
            candidates.append((lambda d=config_dir, n=name: d() / n, f"{directory}/{name}"))
    locations: list[tuple[Path, str]] = []
    for build, display in candidates:
        # 解決できない置き場（`~user` の未知ユーザー・symlink の循環）は省く。貫通させると上の理由で判定ごと失われる。
        try:
            locations.append((build().resolve(), display))
        except (OSError, RuntimeError):
            continue
    return locations


def find_host_guard_match(token: str, *, scope_certain: bool) -> str | None:
    """書込み先トークンがプラグインの ``hooks/hooks.json`` かホスト設定に当たるかを判定する。

    呼び出し側は repo スコープ判定を適用せず、一致した時点で deny する。

    Args:
        token: 検査対象の生トークン（パスの可能性がある）。
        scope_certain: コマンドが `cd` 系を含まず、cwd 基準の解決を信用できるなら True。

    Returns:
        ``hooks/hooks.json/`` または ``.claude/settings.json`` 形の表示名（そのまま
        `blocked_message_for_file` に渡せる）。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    if protected_path_segment(token) == _PLUGIN_HOOKS_JSON_SEGMENT:
        return _PLUGIN_HOOKS_JSON_DISPLAY
    if not scope_certain and normalize_protected_name(basename(token)) == _HOOKS_JSON_NAME:
        return _PLUGIN_HOOKS_JSON_DISPLAY
    resolved = resolve_effective_target(token)
    candidates = [Path(token).parts] if resolved is None else [Path(token).parts, resolved.parts]
    for parts in candidates:
        segments = match_folded_path_window(parts, _HOST_SETTINGS_PATH_SEGMENTS)
        if segments is not None:
            return "/".join(segments)
    if resolved is None:
        return None
    for location, display in _guarded_locations():
        if location.is_relative_to(resolved):
            return display
    return None
