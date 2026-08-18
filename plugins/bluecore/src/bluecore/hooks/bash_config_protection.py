"""Bash 経由の重要な設定ファイル直接書き換えを保護します（A-06 対応）。

トリガー: pre:bash
入力: bashコマンドを含むJSON
出力: 保護対象ファイルへの書き込みが検出された場合は host 非依存の合併出力でブロック
終了: 0 (許可) または 2 (ブロック)

`config_protection` は Edit/Write/MultiEdit 系 matcher にしか登録されておらず、
`printf x > pyproject.toml` のような Bash 経由の直接書き換えを検査しない
（監査 A-06）。本モジュールは同じ保護対象定義（`PROTECTED_FILES` /
`CONDITIONALLY_PROTECTED_FILES`、`config_protection` から import して共有）を
Bash コマンド文字列に対して適用する。

`config_protection` を Bash matcher に相乗りさせない理由:
    `config_protection.main()` の fail 姿勢（空入力 0 / malformed JSON deny /
    truncated deny）は、コメントが明記するとおり「matcher が書込み系ツールに
    限定されている」ことを根拠に較正されている。Bash は最も広い matcher で
    あり、同じ matcher に入れると malformed/truncated な非設定ファイル操作の
    Bash 呼び出しまで deny することになり可用性が壊れる。そのため本モジュール
    は独立した hooks.json エントリとして追加し、判定ロジックだけを分ける。

判定方式:
    `hook_common.tokenize`/`split_segments`（block_no_verify と共有するトーク
    ナイザ）でセグメント分割し、各セグメント内で保護対象ファイルの basename
    が**書き込み先トークンとして現れた場合のみ** deny する:
        - `>` / `>>` リダイレクト先
        - `tee` の出力先引数
        - `sed -i` の対象引数（in-place 編集）
    「検査不能なら deny」には倒さない（可用性が死ぬ）。JSON が壊れている場合
    のみ、`pre_bash_commit_quality.evaluate()` と同じ姿勢（生テキストに保護対象
    basename + 書き込み指示が両方見えるときだけ deny、それ以外は 0）を採る。

非目標: `cp`/`mv`/`install` 等の他コマンド経由の書き込み、`$(...)`・変数展開・
    パイプ越しの間接書き込み、シェルエイリアス・ラッパースクリプト経由の
    呼び出し。POSIX シェルの完全解釈は行わず、うっかり書き換えの抑止であって
    敵対的回避への防壁ではない（`block_no_verify` と同じ設計判断）。
"""

from __future__ import annotations

from bluecore.hooks.config_protection import (
    CONDITIONALLY_PROTECTED_FILES,
    PROTECTED_FILES,
    blocked_message_for_file,
)
from bluecore.hooks.hook_common import (
    MAX_STDIN_BYTES,
    basename,
    emit_block_output,
    parse_json_object,
    read_raw_stdin_with_truncation,
    split_segments,
    tokenize,
)
from bluecore.lib.harness import extract_bash_command, extract_raw_tool_name, normalize_tool_name

# matcher（hooks.json）は Bash 系エイリアスにアンカーされた正規表現。matcher の
# 綴りが将来ズレても本体側で対象外ツールを確実に早期 return するための多重防御。
_BASH_TOOL_NAMES = frozenset({"bash"})

# リダイレクト演算子。`hook_common.tokenize` は shlex の既定 punctuation_chars
# （``();<>|&``）を使うため、``>`` / ``>>`` は密着していても独立トークンになる。
_REDIRECT_OPERATORS = frozenset({">", ">>"})

_ALL_PROTECTED_BASENAMES = PROTECTED_FILES | CONDITIONALLY_PROTECTED_FILES


def _protected_basename(token: str) -> str | None:
    """トークンの basename が保護対象ファイル名なら返す。

    Args:
        token: 検査対象のトークン（パスの可能性がある）。

    Returns:
        保護対象ファイル名。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    name = basename(token)
    return name if name in _ALL_PROTECTED_BASENAMES else None


def _redirect_target(segment: list[str]) -> str | None:
    """セグメント内の `>` / `>>` リダイレクト先が保護対象ならその basename を返す。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        保護対象ファイル名。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    for index, token in enumerate(segment):
        if token in _REDIRECT_OPERATORS and index + 1 < len(segment):
            found = _protected_basename(segment[index + 1])
            if found:
                return found
    return None


def _tee_target(segment: list[str]) -> str | None:
    """セグメント内の `tee` の出力先引数が保護対象ならその basename を返す。

    `-a`（追記）等のオプショントークンは読み飛ばし、非オプション引数を
    出力先候補として検査する。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        保護対象ファイル名。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    tee_index = next(
        (i for i, token in enumerate(segment) if token.rsplit("/", 1)[-1] == "tee"), None
    )
    if tee_index is None:
        return None
    for token in segment[tee_index + 1 :]:
        if token.startswith("-"):
            continue
        found = _protected_basename(token)
        if found:
            return found
    return None


def _sed_inplace_target(segment: list[str]) -> str | None:
    """セグメント内の `sed -i`（in-place 編集）の対象引数が保護対象ならその basename を返す。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        保護対象ファイル名。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    has_sed = any(token.rsplit("/", 1)[-1] == "sed" for token in segment)
    if not has_sed:
        return None
    has_inplace = any(token == "-i" or token.startswith("-i") for token in segment if token.startswith("-"))
    if not has_inplace:
        return None
    for token in segment:
        found = _protected_basename(token)
        if found:
            return found
    return None


def _write_target_in_segment(segment: list[str]) -> str | None:
    """セグメント内の書き込み先（リダイレクト/tee/sed -i）が保護対象ならその basename を返す。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        保護対象ファイル名。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    return _redirect_target(segment) or _tee_target(segment) or _sed_inplace_target(segment)


def find_protected_write(command: str) -> str | None:
    """コマンド文字列内に保護対象ファイルへの書き込みがあればその basename を返す。

    Args:
        command: 検査対象のシェルコマンド文字列。

    Returns:
        保護対象ファイル名。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    for segment in split_segments(tokenize(command)):
        found = _write_target_in_segment(segment)
        if found:
            return found
    return None


def _raw_text_write_risk(raw_input: str) -> str | None:
    """JSON が壊れている場合の fallback: 生テキストに保護対象 basename と書き込み指示が両方見えるかを判定する。

    `pre_bash_commit_quality.evaluate()` の malformed JSON 姿勢と同じく、
    「壊れているというだけで deny」にはせず、保護対象ファイル名と書き込み
    指示（`>`/`tee`/`-i`）の両方が生文字列上に見える場合のみ deny する。

    Args:
        raw_input: フックへ渡された生の入力文字列。

    Returns:
        保護対象ファイル名。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    if not any(indicator in raw_input for indicator in (">", "tee", "-i")):
        return None
    for name in sorted(_ALL_PROTECTED_BASENAMES):
        if name in raw_input:
            return name
    return None


_TRUNCATED_INPUT_MESSAGE = (
    f"[Hook] BLOCKED: input exceeded {MAX_STDIN_BYTES} bytes for pre:bash-config-protection. "
    "Refusing to evaluate a possibly-truncated bash command for protected config writes. "
    "Retry with a smaller command."
)

_MALFORMED_INPUT_MESSAGE = (
    "[Hook] BLOCKED: hook input could not be parsed as JSON, but the raw command "
    "text appears to write to a protected configuration file. Refusing to allow an "
    "unverifiable write through."
)


def main() -> int:
    """Bash コマンドによる保護対象設定ファイルへの直接書き込みを検知してブロックする。

    Args:
        引数はありません（標準入力から読み取る）。

    Returns:
        終了コード（0: 許可、2: ブロック）

    Raises:
        例外は発生しません。
    """
    raw, truncated = read_raw_stdin_with_truncation()
    if truncated:
        return emit_block_output(_TRUNCATED_INPUT_MESSAGE)

    if not raw:
        return 0

    data = parse_json_object(raw)
    if data is None:
        found = _raw_text_write_risk(raw)
        if found:
            return emit_block_output(_MALFORMED_INPUT_MESSAGE)
        return 0

    tool_name = extract_raw_tool_name(data)
    if normalize_tool_name(tool_name).lower() not in _BASH_TOOL_NAMES:
        return 0

    command = extract_bash_command(data)
    found = find_protected_write(command)
    if found:
        return emit_block_output(blocked_message_for_file(found))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
