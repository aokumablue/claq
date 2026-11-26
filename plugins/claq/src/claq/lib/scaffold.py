"""ハーネスが user ターンへ書く足場タグの定義と、ユーザー発話からの足場除去。

transcript の ``user`` エントリには、ハーネスが生成した足場（注意書き・コマンド出力・通知・
system-reminder・スラッシュコマンドの起動タグ）も発話と同じ形で載る。引き継ぎ（SessionEnd）が
依頼本文だけを残すために使う。保護フックは import しない。標準ライブラリだけに依存する。
"""

from __future__ import annotations

import re

# ユーザー発話として transcript に載るが、実際にはハーネスが生成した足場で
# あって依頼ではないタグ。中身ごと捨てる。
#
# - local-command-caveat: ローカルコマンド実行時の定型注意書き。「DO NOT
#   respond to these messages」という指示文を含むため、依頼として引き継ぐと
#   次セッションへ疑似ユーザー指示として再注入されてしまう。
# - local-command-stdout / local-command-stderr: コマンドの出力であって依頼ではない。
# - task-notification: サブエージェント完了通知。ユーザーの発話ではない。
# - system-reminder: ハーネスが user ターンへ差し込む指示文・ファイル内容。
# - agent-message: 別セッション / 別エージェントからの SendMessage 配信足場。
#   中身は他エージェントの命令文であり、依頼として引き継ぐと次セッションへ
#   疑似ユーザー指示として再注入される。
# - bash-input / bash-stdout / bash-stderr: Claude Code の `!` モードで実行した
#   コマンドとその出力。出力はファイル・ネットワーク由来の外部文字列であり、
#   依頼として引き継ぐと次セッションの `<claq-memory>` へ疑似ユーザー指示として注入される。
SCAFFOLD_TAGS = (
    "local-command-caveat",
    "local-command-stdout",
    "local-command-stderr",
    "task-notification",
    "system-reminder",
    "agent-message",
    "bash-input",
    "bash-stdout",
    "bash-stderr",
)

# スラッシュコマンド起動の足場タグ。中身は依頼そのものなので捨てずに畳む。
COMMAND_TAGS = ("command-name", "command-message", "command-args")

# 貼り付けを包む足場タグ。中身は依頼そのものなので、タグだけを落として中身を残す。
PASTE_TAGS = ("pasted_content",)

# タグ名の直後に「名前の終わり」を要求する先読み。これが無いと、タグ**名**が
# 足場名で始まるだけの別タグ（`<system-reminders>` や `<local-command-stdout-parser>`）
# まで一致し、無関係な依頼をメッセージごと破棄してしまう。属性付きは通す。
TAG_NAME_END = r"(?=[\s/>])"

# 孤立タグ検出専用の「名前の終わり」。除去側と違い**文字列の終端も名前の終わりと
# みなす**。除去側の先読みは名前の後ろに 1 文字を要求するため、発話が足場タグ名で
# 終わる入力（`"作業完了 <system-reminder"`）はブロック除去にも孤立検出にも一致せず、
# docs/adr/untrusted-input-prompt-boundary.md の fail closed が発火しない。
#
# 除去側を広げてはならない。`$` を許すと閉じない開始タグが「名前の終わり」を満たし、
# 消す側の精度要求（`<system-reminders>` を巻き込まない）と噛み合わない。
_ORPHAN_NAME_END = r"(?=[\s/>]|$)"

# タグの属性部に許す最大文字数。無界にすると `>` を含まない入力
# （`"<system-reminder " * N`）で二次オーダーになる（SessionEnd で毎回通る経路）。
#
# **この上限は複雑度のための装置であって、安全性の境界ではない。** 上限を使うのは
# 除去パターンだけで、破棄判定の `_SCAFFOLD_ORPHAN` は使わない。超過して除去
# できなかったブロックは孤立タグとして検出され、メッセージごと破棄される。
_MAX_TAG_ATTR_CHARS = 512
_TAG_ATTRS = rf"[^>]{{0,{_MAX_TAG_ATTR_CHARS}}}"


def tag_block_pattern(tag: str, *, capture: bool = False) -> re.Pattern[str]:
    """``<tag ...>中身</tag>`` のブロックに一致するパターンを返す。

    中身は「同種の開始タグを含まない任意の文字列」に限る。単純な `.*?` だと、対を
    成さない開始タグから後続の別ブロックの閉じタグまで貫通して間の実依頼まで巻き込む
    うえ、閉じタグ無しの開始タグが大量にあると走査コストが桁違いに膨らむ。

    Args:
        tag: タグ名。
        capture: True なら前後の空白を除いた中身をグループ 1 として捕捉する。
    """
    body = rf"(?:(?!<{tag}{TAG_NAME_END})[\s\S])*?"
    if capture:
        body = rf"\s*({body})\s*"
    return re.compile(rf"<{tag}{TAG_NAME_END}{_TAG_ATTRS}>{body}</{tag}\s*>", re.IGNORECASE)


def _tag_marker_pattern(tags: tuple[str, ...]) -> re.Pattern[str]:
    """*tags* のいずれかの開始タグ・終了タグ単体（中身は含まない）に一致するパターンを返す。"""
    return re.compile(r"</?(?:" + "|".join(tags) + r")" + TAG_NAME_END + _TAG_ATTRS + r">", re.IGNORECASE)


# タグごとに自分自身の開始タグを否定先読みする必要があるため、1 本の alternation に
# まとめられずタグ単位のタプルになる。
_SCAFFOLD_BLOCKS = tuple(tag_block_pattern(tag) for tag in SCAFFOLD_TAGS)

# ペア除去後に残った足場タグ。ハーネスは足場タグを必ず対で書き、`read_tail_text` の
# 前方切り詰めで壊れる先頭 1 行は `mem.handoff.parse_entry` が不正 JSON として捨てるため、
# この段階で片側だけ残っているタグは、足場の中身に閉じタグリテラルを混ぜてブロックを
# 早期終端させた細工とみなす。断片を救う利得より、細工した文字列が「直近の依頼」として
# 次セッションへ注入される損失のほうが大きいため、メッセージごと破棄する。
#
# 属性部（`_TAG_ATTRS`）と閉じ括弧は**要求しない**。要求すると、属性が 512 文字を
# 超えるタグがブロック除去にも本検出にも一致せず素通りする。先読みも選択肢も固定幅
# なので走査は入力長に対して線形。
_SCAFFOLD_ORPHAN = re.compile(r"</?(?:" + "|".join(SCAFFOLD_TAGS) + r")" + _ORPHAN_NAME_END, re.IGNORECASE)

# スラッシュコマンド起動の足場。`<command-name>` と `<command-args>` の中身は
# ユーザーが実際に入力した依頼そのものなので、捨てずに `/name args` へ畳む。
_COMMAND_NAME = tag_block_pattern("command-name", capture=True)
_COMMAND_ARGS = tag_block_pattern("command-args", capture=True)
_COMMAND_SCAFFOLD = _tag_marker_pattern(COMMAND_TAGS)
# 貼り付けを包むタグ（開始・終了）。中身には触れない。
_PASTE_TAG = _tag_marker_pattern(PASTE_TAGS)
# 畳み込みで中身ごと落とすブロック。`command-message` はコマンド名の再掲、
# `command-args` は畳んだ文字列側へ取り込み済みのため元の位置には残さない。
_COMMAND_MESSAGE_BLOCK = tag_block_pattern("command-message")
_COMMAND_ARGS_BLOCK = tag_block_pattern("command-args")


def _drop_scaffold_blocks(text: str) -> str | None:
    """依頼ではないハーネス足場タグを中身ごと除去する。

    ペア除去後に片側だけのタグが残った場合は、細工された入力とみなして
    メッセージ自体を破棄する（fail closed）。

    「足場タグの総数が上限を超えたら破棄する」ガードは持たない。走査は否定先読みで
    線形に収まり、除去回避は孤立タグ検出が 1 個でも破棄するため上限に依存しない。
    上限を置くと、対になった足場ブロックを大量に含む正当な依頼を落とす誤検知だけが残る。

    Returns:
        足場タグとその中身を除いたテキスト。メッセージごと破棄すべきなら None。
    """
    for pattern in _SCAFFOLD_BLOCKS:
        text = pattern.sub("", text)
    if _SCAFFOLD_ORPHAN.search(text):
        return None
    return text


def _fold_command_invocation(text: str) -> str:
    """スラッシュコマンド起動の足場を ``/name args`` へ畳んで元の位置へ差し込む。

    メッセージ全体を置き換えてはならない。コマンド起動と地の文が同居する場合に地の文が
    消えるうえ、注入済みの ``<claq-memory>`` ブロック内に残った前回の ``<command-name>``
    のエコーが実依頼を押し退ける（後段の ``strip_tags`` が記憶ブロックを中身ごと
    落とせるよう、畳んだ結果もブロックの内側に留める）。

    畳み込みは**起動単位**で行う。最初の ``<command-name>`` と最初の ``<command-args>``
    を無条件にペアリングすると、後続の起動の引数が前の起動へ付け替わり、その引数を
    含む実依頼が記憶ブロックごと消える。各 ``<command-name>`` から次の
    ``<command-name>`` の直前までを 1 起動の範囲とし、その範囲内でだけ引数を探す。

    コマンド名が取れない場合でも、残った ``command-*`` タグ自体は依頼本文ではない
    ため無条件に落とす。
    """
    matches = list(_COMMAND_NAME.finditer(text))
    if not matches:
        return _COMMAND_SCAFFOLD.sub("", text)

    parts: list[str] = [text[: matches[0].start()]]
    region_ends = [match.start() for match in matches[1:]] + [len(text)]
    for match, region_end in zip(matches, region_ends, strict=True):
        region = text[match.end() : region_end]
        name = match.group(1).strip().lstrip("/")
        if name:
            args_match = _COMMAND_ARGS.search(region)
            args = args_match.group(1).strip() if args_match else ""
            parts.append(f"/{name} {args}".strip())
            region = _COMMAND_ARGS_BLOCK.sub("", region, count=1)
            region = _COMMAND_MESSAGE_BLOCK.sub("", region)
        parts.append(region)
    return _COMMAND_SCAFFOLD.sub("", "".join(parts))


def normalize_user_message(text: str) -> str:
    """ユーザー発話からハーネス生成の足場を落として依頼本文だけを残す。

    transcript の ``user`` エントリには、ハーネスが生成した足場（ローカルコマンドの
    注意書き・その stdout・サブエージェント完了通知・system-reminder・スラッシュ
    コマンドの起動タグ）も同じ形で載る。これらを依頼として引き継ぐと、実際の依頼が
    押し出されるうえ、注意書きの指示文が次セッションへ疑似ユーザー指示として再注入される。

    中身が無価値な足場は丸ごと捨て、スラッシュコマンド起動だけは ``/name args`` へ
    畳む。貼り付けを包むタグは、タグだけを落として中身（依頼そのもの）を残す。足場の中身に閉じタグを混ぜて除去を回避しようとした入力はメッセージごと破棄する。

    Returns:
        依頼本文。足場しか含まれていない、または細工が検出された場合は空文字列。
    """
    if not text:
        return text
    stripped = _drop_scaffold_blocks(text)
    if stripped is None:
        return ""
    return _PASTE_TAG.sub("", _fold_command_invocation(stripped)).strip()
