"""Edit/Write 系ツール入力を適用した後のファイル全文を組み立てる。

`conditional_protection` が git config とホスト設定（settings.json）の「置換前の全文」と
「置換後の全文」を比べるために使う。ファイル形式には依存しない。
"""

from __future__ import annotations

from typing import Any

from claq.hooks.hook_common import resolve_effective_target
from claq.lib.harness import normalize_tool_name

# MultiEdit の編集件数の上限。置換 1 件ごとに全文を走査するため、件数でも検査時間を抑える
# （上限が無いと 1 MiB の payload に 13,981 件入り、256 KiB の全文で 5.2 秒かかった。64 件なら数 ms）。
_MAX_EDITS = 64


def read_current_text(file_path: str, limit: int) -> str:
    """対象ファイルの現在の内容を先頭 ``limit`` 文字まで読む。

    改行は変換しない。git config は CRLF だけを改行に畳み、単独の CR は空白として読むため、
    universal newlines で読むと実ファイルと違う全文を解析してしまう。読めない・存在しない・
    ディレクトリの場合は空文字列（新規ファイルと同じ扱い）。呼び出し側が走査上限を超えたことを
    長さで検出できるよう、上限より 1 文字多い値を ``limit`` に渡す。

    Args:
        file_path: 対象ファイルパス。
        limit: 読む最大文字数。

    Returns:
        現在の内容。

    Raises:
        例外は発生しません。
    """
    resolved = resolve_effective_target(file_path)
    if resolved is None:
        return ""
    try:
        with resolved.open(encoding="utf-8", errors="replace", newline="") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def _apply_edit(text: str, old: object, new: str, *, replace_all: bool, max_chars: int) -> str | None:
    """Edit 1 件の置換を全文へ適用する。

    置換の結果が ``max_chars`` を超える場合は、文字列を作る前に打ち切る。置換の連鎖で全文を
    膨らませて検査を時間切れ（＝ allow）へ追い込む入力を許さないため。

    Args:
        text: 置換前の全文。
        old: 置換対象。
        new: 置換後の文字列。
        replace_all: 全出現を置換するか（False は最初の 1 件）。
        max_chars: 置換後の全文の最大文字数。

    Returns:
        置換後の全文。置換対象が空・文字列でない・全文に見つからない・結果が上限を超えるなら None。

    Raises:
        例外は発生しません。
    """
    if not isinstance(old, str) or not old or old not in text:
        return None
    count = text.count(old) if replace_all else 1
    if len(text) + count * (len(new) - len(old)) > max_chars:
        return None
    return text.replace(old, new, count)


def text_after_edit(tool_name: str, container: Any, before: str, max_chars: int) -> str | None:
    """書込み系入力を適用した後の全文を組み立てる。

    Write は content。Edit は置換を当てた全文、MultiEdit は編集を順に当てた全文（件数は
    `_MAX_EDITS` まで）。対象が空・存在しない・読めない場合は、Write と同じく new_string だけを
    断片として扱う。内容のあるファイルで置換対象が見つからない場合は、実際のツールが一致させる
    可能性（引用符の正規化など）を排除できないため全文を作らない。apply_patch も全文を作れない。

    Args:
        tool_name: 正規化前の生ツール名。
        container: extract_tool_input 相当の 1 コンテナ値。
        before: 対象ファイルの現在の内容。
        max_chars: 組み立てる全文の最大文字数。

    Returns:
        置換後の全文。組み立てられなければ None（呼び出し側は fail-closed とする）。

    Raises:
        例外は発生しません。
    """
    if tool_name == "apply_patch" or not isinstance(container, dict):
        return None
    normalized = normalize_tool_name(tool_name).lower()
    if normalized == "write":
        content = container.get("content")
        return content if isinstance(content, str) else None
    edits = container.get("edits") if normalized == "multiedit" else [container]
    if not isinstance(edits, list) or not 0 < len(edits) <= _MAX_EDITS:
        return None
    if not all(isinstance(edit, dict) and isinstance(edit.get("new_string"), str) for edit in edits):
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
            max_chars=max_chars,
        )
        if text is None:
            return None
    return text
