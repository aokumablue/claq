"""Edit/Write 系ツール入力を適用した後のファイル全文を組み立てる。

`conditional_protection` が git config とホスト設定（settings.json）の「置換前の全文」と
「置換後の全文」を比べるために使う。ファイル形式には依存しない。
"""

from __future__ import annotations

from typing import Any

from claq.hooks.hook_common import resolve_effective_target
from claq.lib.harness import normalize_tool_name

# git config・ホスト設定の置換前後の全文の上限。置換前はディスクから読むので stdin の上限が効かず、
# 置換後は MultiEdit の replace_all を重ねると倍々に伸びる（`a`→`aa` を 20 回で 1 KiB が 1 GiB）。
# 超えたら検査できないとして呼び出し側が deny する（hook が host timeout で kill されると許可へ倒れるため）。
FULL_TEXT_MAX_BYTES = 256 * 1024

# MultiEdit の編集件数の上限。置換 1 件ごと・断片 1 つごとに全文を走査するため、件数でも検査時間を抑える
# （上限が無いと 1 MiB の payload に 13,981 件入り、256 KiB の全文で 5.2 秒かかった。64 件なら数 ms）。
# `conditional_protection` の断片検査も同じ上限を使う（約 1 MB の本文に 15,000 件で 13.5 秒かかり、
# hook の timeout に近づいた）。
MAX_EDITS = 64


def read_current_text(file_path: str) -> str:
    """対象ファイルの現在の内容を先頭 ``FULL_TEXT_MAX_BYTES + 1`` 文字まで読む。

    改行は変換しない。git config は CRLF だけを改行に畳み、単独の CR は空白として読むため、
    universal newlines で読むと実ファイルと違う全文を解析してしまう。読めない・存在しない・
    ディレクトリの場合は空文字列（新規ファイルと同じ扱い）。呼び出し側が走査上限を超えたことを
    長さで検出できるよう、上限より 1 文字多く読む。

    Args:
        file_path: 対象ファイルパス。

    Returns:
        現在の内容。
    """
    resolved = resolve_effective_target(file_path)
    if resolved is None:
        return ""
    try:
        with resolved.open(encoding="utf-8", errors="replace", newline="") as handle:
            return handle.read(FULL_TEXT_MAX_BYTES + 1)
    except OSError:
        return ""


def _apply_edit(text: str, old: object, new: str, *, replace_all: bool) -> str | None:
    """Edit 1 件の置換を全文へ適用する。

    置換の結果が ``FULL_TEXT_MAX_BYTES`` を超える場合は、文字列を作る前に打ち切る。置換の連鎖で全文を
    膨らませて検査を時間切れ（＝ allow）へ追い込む入力を許さないため。

    Args:
        text: 置換前の全文。
        old: 置換対象。
        new: 置換後の文字列。
        replace_all: 全出現を置換するか（False は最初の 1 件）。

    Returns:
        置換後の全文。置換対象が空・文字列でない・全文に見つからない・結果が上限を超えるなら None。
    """
    if not isinstance(old, str) or not old or old not in text:
        return None
    count = text.count(old) if replace_all else 1
    if len(text) + count * (len(new) - len(old)) > FULL_TEXT_MAX_BYTES:
        return None
    return text.replace(old, new, count)


def is_write_tool(tool_name: str) -> bool:
    """ファイル全体を content で置き換える Write 系ツールかを返す。

    Args:
        tool_name: 正規化前の生ツール名。

    Returns:
        Write 系なら True（apply_patch は Edit へ正規化されるので含まない）。
    """
    return normalize_tool_name(tool_name).lower() == "write"


def edit_entries(tool_name: str, container: dict[str, Any]) -> list[Any] | None:
    """Edit 系入力の編集の並びを返す。

    MultiEdit は ``edits[]``、edit / search_replace 等はコンテナ自身を 1 件の Edit として扱う。
    件数は `MAX_EDITS` までに抑える（全文の組み立ても断片の検査も、1 件ごとに全文を走査するため）。
    各要素の形の検査は呼び出し側が行う。

    Args:
        tool_name: 正規化前の生ツール名。
        container: extract_tool_input 相当の 1 コンテナ値（dict）。

    Returns:
        編集の並び。``edits`` が list でない・件数が上限を超えるなら None（呼び出し側は fail-closed とする）。
    """
    edits = container.get("edits") if normalize_tool_name(tool_name).lower() == "multiedit" else [container]
    if not isinstance(edits, list) or len(edits) > MAX_EDITS:
        return None
    return edits


def text_after_edit(tool_name: str, container: Any, before: str) -> str | None:
    """書込み系入力を適用した後の全文を組み立てる。

    Write は content。Edit は置換を当てた全文、MultiEdit は編集を順に当てた全文（件数は
    `MAX_EDITS` まで）。対象が空・存在しない・読めない場合は、Write と同じく new_string だけを
    断片として扱う。内容のあるファイルで置換対象が見つからない場合は、実際のツールが一致させる
    可能性（引用符の正規化など）を排除できないため全文を作らない。apply_patch も全文を作れない。

    Args:
        tool_name: 正規化前の生ツール名。
        container: extract_tool_input 相当の 1 コンテナ値。
        before: 対象ファイルの現在の内容。

    Returns:
        置換後の全文。組み立てられなければ None（呼び出し側は fail-closed とする）。
    """
    if tool_name == "apply_patch" or not isinstance(container, dict):
        return None
    if is_write_tool(tool_name):
        content = container.get("content")
        return content if isinstance(content, str) else None
    edits = edit_entries(tool_name, container)
    if not edits or not all(isinstance(edit, dict) and isinstance(edit.get("new_string"), str) for edit in edits):
        return None
    if not before:
        return "\n".join(edit["new_string"] for edit in edits)
    text: str | None = before
    for edit in edits:
        text = _apply_edit(
            text,
            edit.get("old_string"),
            edit["new_string"],
            replace_all=edit.get("replace_all") is True,
        )
        if text is None:
            return None
    return text
