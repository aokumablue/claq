"""コーディングエージェントハーネスの入力形式差分を吸収する汎用パーサ。

host 判定は一切行わない。フィールド名の union を無条件に受理する寛容パーサ
のみを提供する（tool_input / toolArgs、tool_name / toolName 等の複数命名を
同一の意味へ正規化する）。すべて純 stdlib のみに依存する（venv 不在時の
フォールバック実行を保証するため）。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 各ハーネスのツール名 → Claude Code 相当ツール名。
#
# Codex の apply_patch は Edit に対応する。Copilot CLI はフックイベントに
# lowercase の runtime tool 名（write/edit/bash 等）を渡すため、大文字小文字を
# 区別しない照合で Claude Code 表記へ正規化する。
# shell は一部ハーネスが bash 相当として使う runtime 名。
_TOOL_NAME_MAP = {
    "apply_patch": "Edit",
    "agent": "Agent",
    "bash": "Bash",
    "shell": "Bash",
    "edit": "Edit",
    "glob": "Glob",
    "grep": "Grep",
    "multiedit": "MultiEdit",
    "notebookedit": "NotebookEdit",
    "read": "Read",
    "task": "Agent",
    "view": "Read",
    "write": "Write",
    # Grok の runtime tool 名。read_file/list_dir は書き込み系ゲート
    # （config_protection の _WRITE_TOOL_NAMES）の対象外だが、
    # observe.py の観測レコード（tool フィールド）がハーネス横断で
    # 正規化名を記録するために正規化する。
    "search_replace": "Edit",
    "run_terminal_command": "Bash",
    "spawn_subagent": "Agent",
    "read_file": "Read",
    "list_dir": "Glob",
}

# ハーネスごとの tool 入力コンテナキー。存在するキーを順に走査する。
# hooks 側（config_protection / block_no_verify）もこの 1 か所を参照する
# ——以前は config_protection が同じ内容を独自に持っており、Grok の
# ``toolInput`` を足す改修が片方だけに入って取りこぼす原因になっていた。
INPUT_CONTAINER_KEYS = ("tool_input", "toolInput", "toolArgs", "tool_args")

# 構造化パッチテキストのファイル操作マーカー（Codex apply_patch 形式）
_PATCH_FILE_MARKERS = ("*** Add File: ", "*** Update File: ", "*** Delete File: ")


def extract_tool_input(payload: dict[str, Any]) -> Any:
    """フック payload から tool_input / toolInput / toolArgs を正規化して返す。

    Claude / VS Code 互換は ``tool_input``、Grok camelCase は ``toolInput``、
    Copilot camelCase は ``toolArgs``（JSON 文字列のことが多い）。文字列で
    JSON オブジェクトに見える場合はパースして dict を返す。パース不能なら
    元の文字列を返す。

    ``toolInput`` を落とすと Grok の PreToolUse で tool 入力が取れず、
    ``block_no_verify`` が「判定不能」として全 ``run_terminal_command`` を
    exit 2 で拒否する（セッションが実質使用不能になる）。tool 名側の
    ``extract_raw_tool_name`` は既に camelCase ``toolName`` を受理しており、
    入力側だけ snake_case 限定なのは非対称な取りこぼしだった。

    Args:
        payload: フック stdin を JSON として読んだ dict。

    Returns:
        正規化後の tool 入力（dict / str / その他）、キーが無ければ None。

    Raises:
        例外は発生しません。
    """
    for key in INPUT_CONTAINER_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if not isinstance(value, str):
            return value
        stripped = value.lstrip()
        if not stripped.startswith(("{", "[")):
            return value
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return None


def extract_raw_tool_name(payload: dict[str, Any]) -> str:
    """フック payload から正規化前の生ツール名を返す。

    非空の ``tool_name`` を優先し、無ければ非空の ``toolName`` を使う。
    空文字・非文字列は無効として次候補へ倒す。どちらも無効なら空文字。

    Args:
        payload: フック stdin を JSON として読んだ dict。

    Returns:
        ``normalize_tool_name()`` 適用前の生文字列。取れなければ ``""``。

    Raises:
        例外は発生しません。
    """
    tool_name = payload.get("tool_name")
    if isinstance(tool_name, str) and tool_name:
        return tool_name
    camel_name = payload.get("toolName")
    if isinstance(camel_name, str) and camel_name:
        return camel_name
    return ""


def extract_bash_command(payload: dict[str, Any]) -> str:
    """フック payload から Bash/shell の command 文字列を取り出す。

    ``tool_input.command`` / ``toolArgs``（JSON 文字列含む）/ 生文字列を吸収する。
    非 dict の tool_input に対して ``.get`` して AttributeError にならない。

    list 形状（``{"tool_input": [{"command": "..."}]}`` や
    ``{"tool_input": ["..."]}``）は要素ごとに取り出して改行で連結する。
    ここで空文字列を返すと、``extract_tool_input`` は list を返して「必須
    フィールド欠落」の fail-closed を素通りするため、コマンドが 1 つも無い
    payload として静かに許可されてしまう（実測: ``git commit --no-verify``
    を list に包むと ``block_no_verify`` が exit 0）。同じ ``extract_bash_command``
    を使う ``config_protection`` / ``pre_bash_commit_quality`` も同型だったため、
    フック側ではなく共有層で塞ぐ。

    Args:
        payload: フック stdin を JSON として読んだ dict。

    Returns:
        コマンド文字列。取れなければ空文字列。

    Raises:
        例外は発生しません。
    """
    return _command_from_tool_input(extract_tool_input(payload))


def _command_from_tool_input(tool_input: Any) -> str:
    """正規化済み tool 入力から command 文字列を取り出す。

    Args:
        tool_input: `extract_tool_input` の戻り値。

    Returns:
        コマンド文字列。取れなければ空文字列。list は要素ごとの結果を
        改行で連結する（空要素は落とす）。

    Raises:
        例外は発生しません。
    """
    if isinstance(tool_input, dict):
        for key in ("command", "cmd"):
            cmd = tool_input.get(key)
            if isinstance(cmd, str):
                return cmd
        return ""
    if isinstance(tool_input, str):
        return tool_input
    if isinstance(tool_input, list):
        return "\n".join(filter(None, (_command_from_tool_input(item) for item in tool_input)))
    return ""


def normalize_tool_name(tool_name: str) -> str:
    """ハーネス固有のツール名を Claude Code 相当のツール名へ正規化する。

    Codex の apply_patch は Edit に対応する。Copilot CLI はフックイベントに
    lowercase の runtime tool 名（write/edit/bash 等）を渡すため、大文字小文字を
    区別しない照合で Claude Code 表記へ正規化する。Grok は search_replace /
    run_terminal_command / spawn_subagent / read_file / list_dir という
    固有名を使うため、同様に Claude Code 表記へ正規化する。Claude Code に apply_patch
    というツールは存在せず、Claude Code 自身のツール名は既に正規形のため、
    ハーネス判定なしの無条件マッピングで安全。

    Args:
        tool_name: フック stdin の tool_name フィールド値。

    Returns:
        正規化後のツール名。マッピング対象外はそのまま返す。

    Raises:
        例外は発生しません。
    """
    return _TOOL_NAME_MAP.get(tool_name.lower(), tool_name)


def _extract_patch_text(tool_input: dict | str | None) -> str | None:
    """入力から構造化パッチ本文候補を取り出す。

    Copilot CLI では生のパッチ文字列、他ハーネスでは {"input": "..."} の
    ような dict で渡ることがあるため、両方を吸収する。JSON 文字列化された
    dict が来た場合も input フィールドを復元する。ツール名に関わらず、
    渡された入力の「形」だけから候補テキストを取り出す（判定は呼び出し側）。
    """
    if isinstance(tool_input, dict):
        patch_text = tool_input.get("input")
        return patch_text if isinstance(patch_text, str) else None

    if not isinstance(tool_input, str):
        return None

    stripped = tool_input.lstrip()
    if not stripped.startswith("{"):
        return tool_input
    try:
        parsed = json.loads(tool_input)
    except (json.JSONDecodeError, TypeError):
        return tool_input
    patch_text = parsed.get("input")
    return patch_text if isinstance(patch_text, str) else tool_input


def _has_patch_markers(patch_text: str) -> bool:
    """テキストが構造化パッチのファイル操作マーカー行を 1 つ以上含むか判定する。"""
    return any(line.startswith(marker) for line in patch_text.splitlines() for marker in _PATCH_FILE_MARKERS)


def extract_file_paths(tool_name: str, tool_input: dict | str | None) -> list[str] | None:
    """ツール入力から操作対象のファイルパス一覧を抽出する。

    Edit/Write/MultiEdit は file_path フィールド（旧名 ``file`` も同義）を
    使う。構造化パッチ（Copilot/Codex の apply_patch 等）はパッチテキストの
    ファイル操作マーカー行をパースする。パッチかどうかは tool_name の文字列
    一致だけに頼らず、生入力の内容（マーカー行の有無）でも判定する。
    ハーネスごとの命名差異でツール名が "apply_patch" と一致しない
    場合でも、構造化パッチの内容が検査対象から漏れないようにするため。

    list 形状（``[{"file_path": "..."}]`` や ``["..."]``）と生文字列形状
    （``"ruff.toml"``）も展開する。ここで空リストを返すと、呼び出し側
    （``config_protection``）は「対象ファイルが 1 つも無い書き込み」として
    静かに許可してしまう（実測: ``{"tool_input": [{"file_path": "ruff.toml"}]}``
    と ``{"tool_input": "ruff.toml"}`` がいずれも exit 0 で保護を素通り）。
    ``_command_from_tool_input`` が同じ取りこぼしを共有層で塞いだのと同型の
    修正であり、フック側ではなくここで閉じる。

    Args:
        tool_name: フック stdin の tool_name フィールド値（正規化前）。
        tool_input: フック stdin の tool_input フィールド値。dict / list /
            文字列 / None。

    Returns:
        ファイルパスのリスト。判定不能（パッチ本文と分かっているのに
        マーカーが 1 つも見つからない等）の場合は None を返す。呼び出し側は
        None を fail-closed として扱うこと。

    Raises:
        例外は発生しません。
    """
    patch_text = _extract_patch_text(tool_input)
    is_declared_patch_tool = tool_name == "apply_patch"
    if isinstance(patch_text, str) and (is_declared_patch_tool or _has_patch_markers(patch_text)):
        paths = [
            line[len(marker) :].strip()
            for line in patch_text.splitlines()
            for marker in _PATCH_FILE_MARKERS
            if line.startswith(marker)
        ]
        return paths or None

    if isinstance(tool_input, list):
        paths: list[str] = []
        for item in tool_input:
            item_paths = extract_file_paths(tool_name, item)
            if item_paths is None:
                return None
            paths.extend(item_paths)
        if is_declared_patch_tool and not paths:
            # apply_patch と明示されているのに対象を 1 つも取り出せない: 判定不能
            return None
        return paths

    if is_declared_patch_tool:
        # apply_patch と明示されているのにパッチ本文を取り出せない: 判定不能
        return None

    if isinstance(tool_input, str):
        return [tool_input] if tool_input else []

    if not isinstance(tool_input, dict):
        return []

    for key in ("file_path", "file"):
        file_path = tool_input.get(key)
        if isinstance(file_path, str) and file_path:
            return [file_path]
    return []


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
#   中身は他エージェントの命令文（「〜せよ」「〜に触れるな」）であり、これを
#   依頼として引き継ぐと次セッションへ疑似ユーザー指示として再注入される。
_SCAFFOLD_TAGS = (
    "local-command-caveat",
    "local-command-stdout",
    "local-command-stderr",
    "task-notification",
    "system-reminder",
    "agent-message",
)

# スラッシュコマンド起動の足場タグ。中身は依頼そのものなので捨てずに畳む。
_COMMAND_TAGS = ("command-name", "command-message", "command-args")

# 診断側（ci/scan_scaffold_drift.py）が「既知タグ」を組み立てるための公開名。
# 値は上の定義から導出する。写経すると片方だけ更新されて誤報が出る。
SCAFFOLD_TAGS = _SCAFFOLD_TAGS
COMMAND_TAGS = _COMMAND_TAGS

# タグ名の直後に「名前の終わり」を要求する先読み。これが無いと `[^>]*` が
# 属性を許すつもりで、タグ**名**が足場名で始まるだけの別タグ（`<system-reminders>`
# や `<local-command-stdout-parser>`）まで一致し、無関係な依頼をメッセージごと
# 破棄してしまう。属性付き（`<task-notification id="7">`）は従来どおり通す。
_TAG_NAME_END = r"(?=[\s/>])"

# タグの属性部に許す最大文字数。`[^>]*` を無界にすると、`>` を 1 個も含まない入力
# （`"<system-reminder " * N`）で各開始位置が末尾まで走査して二次オーダーになる。
# 実測（撤去した件数ガードでは防げていなかった経路）: 498KB で 8.8 秒、996KB で
# 35.7 秒、2MB は 120 秒でも終わらない。SessionEnd で毎回通る経路なので有界化する。
# 実在のタグ属性がこの長さを超えることはなく、超えた時点でタグとして扱わない。
_MAX_TAG_ATTR_CHARS = 512
_TAG_ATTRS = rf"[^>]{{0,{_MAX_TAG_ATTR_CHARS}}}"

# 中身は「同種の開始タグを含まない任意の文字列」に限る。単純な `.*?` だと、
# 対を成さない開始タグから後続の別ブロックの閉じタグまで貫通し、その間にある
# 実依頼まで巻き込んで消してしまう（ブロック除去という宣言と実挙動がずれる）。
# 実測では走査コストも桁違いで、閉じタグ無しの開始タグ 8000 個に対し
# 単純 `.*?` が 4.70 秒かかるのに対し否定先読み版は 0.0012 秒で頭打ちになる。
# タグごとに自分自身の開始タグを否定先読みする必要があるため、他の足場タグ用
# パターンと違い 1 本の alternation にまとめられずタグ単位のリストになる。
_SCAFFOLD_BLOCK_PATTERNS = [
    re.compile(
        rf"<{tag}{_TAG_NAME_END}{_TAG_ATTRS}>(?:(?!<{tag}{_TAG_NAME_END})[\s\S])*?</{tag}\s*>",
        re.IGNORECASE,
    )
    for tag in _SCAFFOLD_TAGS
]

# ペア除去後に残った足場タグ。ハーネスは足場タグを必ず対で書き、`_read_tail` の
# 前方切り詰めで壊れるのは先頭 1 行だけでその行は `_parse_entry` が不正 JSON
# として捨てるため、この段階で片側だけ残っているタグは足場の中身に閉じタグ
# リテラルを混ぜてブロックを早期終端させた細工とみなす。best-effort な引き継ぎで
# 断片を救う利得より、細工した文字列が「直近の依頼」として次セッションへ
# 注入される損失のほうが大きいため、メッセージごと破棄する。
_SCAFFOLD_ORPHAN_PATTERN = re.compile(
    r"</?(?:" + "|".join(_SCAFFOLD_TAGS) + r")" + _TAG_NAME_END + _TAG_ATTRS + r">", re.IGNORECASE
)

# スラッシュコマンド起動の足場。`<command-name>` と `<command-args>` の中身は
# ユーザーが実際に入力した依頼そのものなので、捨てずに `/name args` へ畳む。
# `<command-message>` はコマンド名の再掲であり情報を持たないため捨てる。
# 中身に同種の開始タグを含ませない点は足場ブロックと同じ。単純な `.*?` のままだと
# 閉じタグを伴わない `<command-name>` 8000 個で 8.5 秒かかる（実測）。
_COMMAND_NAME_PATTERN = re.compile(
    rf"<command-name{_TAG_NAME_END}{_TAG_ATTRS}>\s*((?:(?!<command-name{_TAG_NAME_END})[\s\S])*?)\s*</command-name\s*>",
    re.IGNORECASE,
)
_COMMAND_ARGS_PATTERN = re.compile(
    rf"<command-args{_TAG_NAME_END}{_TAG_ATTRS}>\s*((?:(?!<command-args{_TAG_NAME_END})[\s\S])*?)\s*</command-args\s*>",
    re.IGNORECASE,
)
_COMMAND_SCAFFOLD_PATTERN = re.compile(
    r"</?(?:" + "|".join(_COMMAND_TAGS) + r")" + _TAG_NAME_END + _TAG_ATTRS + r">", re.IGNORECASE
)
# 畳み込みで中身ごと落とすブロック。`command-message` はコマンド名の再掲、
# `command-args` は畳んだ文字列側へ取り込み済みのため元の位置には残さない。
_COMMAND_MESSAGE_BLOCK_PATTERN = re.compile(
    rf"<command-message{_TAG_NAME_END}{_TAG_ATTRS}>(?:(?!<command-message{_TAG_NAME_END})[\s\S])*?</command-message\s*>",
    re.IGNORECASE,
)
_COMMAND_ARGS_BLOCK_PATTERN = re.compile(
    rf"<command-args{_TAG_NAME_END}{_TAG_ATTRS}>(?:(?!<command-args{_TAG_NAME_END})[\s\S])*?</command-args\s*>",
    re.IGNORECASE,
)


def _drop_scaffold_blocks(text: str) -> str | None:
    """依頼ではないハーネス足場タグを中身ごと除去する。

    ペア除去後に片側だけのタグが残った場合は、細工された入力とみなして
    メッセージ自体を破棄する（fail closed）。

    かつては「足場タグの総数が上限を超えたら破棄する」ガードも持っていたが、
    その根拠だった `.*?` の O(N·n) 走査は各パターンへ否定先読みを入れたことで
    解消済みで（閉じタグ無しの開始タグ 20000 個でも合計 0.005 秒、線形）、
    除去回避のほうは孤立タグ検出が 1 個でも破棄するため上限に依存しない。
    根拠を失った上限は「対になった足場ブロックを大量に含む正当な依頼」を
    丸ごと落とす誤検知だけが残るため撤去した。

    Args:
        text: ユーザー発話として transcript に載っていた生テキスト。

    Returns:
        足場タグとその中身を除いたテキスト。メッセージごと破棄すべきなら None。

    Raises:
        例外は発生しません。
    """
    for pattern in _SCAFFOLD_BLOCK_PATTERNS:
        text = pattern.sub("", text)
    if _SCAFFOLD_ORPHAN_PATTERN.search(text):
        return None
    return text


def _fold_command_invocation(text: str) -> str:
    """スラッシュコマンド起動の足場を ``/name args`` へ畳んで元の位置へ差し込む。

    メッセージ全体を ``/name args`` で置き換えてはならない。同一メッセージに
    コマンド起動と地の文が同居する場合に地の文が消えるうえ、注入済みの
    ``<bluecore-memory>`` ブロック内に前回の ``<command-name>`` が残っていると、
    そのエコーが実依頼を押し退けてしまう（後段の ``strip_tags`` が記憶ブロックを
    中身ごと落とせるよう、畳んだ結果もブロックの内側に留める必要がある）。

    畳み込みは**起動単位**で行う。1 メッセージに起動が 2 組あるとき、最初の
    ``<command-name>`` と最初の ``<command-args>`` を無条件にペアリングすると、
    後続の起動が持つ引数が前の起動へ付け替わり、その引数を含む実依頼が
    記憶ブロックごと落ちて消える。各 ``<command-name>`` から次の
    ``<command-name>`` の直前までを 1 起動の範囲とみなし、その範囲内でだけ
    引数を探す。

    コマンド名が取れない場合でも、残った ``command-*`` タグ自体は依頼本文では
    ないため無条件に落とす。

    Args:
        text: ``<command-name>`` を含みうるテキスト。

    Returns:
        各コマンド起動部分だけを ``/name args`` に置き換えたテキスト。

    Raises:
        例外は発生しません。
    """
    matches = list(_COMMAND_NAME_PATTERN.finditer(text))
    if not matches:
        return _COMMAND_SCAFFOLD_PATTERN.sub("", text)

    parts: list[str] = [text[: matches[0].start()]]
    for index, match in enumerate(matches):
        region_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        region = text[match.end() : region_end]
        name = match.group(1).strip().lstrip("/")
        if name:
            args_match = _COMMAND_ARGS_PATTERN.search(region)
            args = args_match.group(1).strip() if args_match else ""
            parts.append(f"/{name} {args}".strip())
            region = _COMMAND_ARGS_BLOCK_PATTERN.sub("", region, count=1)
            region = _COMMAND_MESSAGE_BLOCK_PATTERN.sub("", region)
        parts.append(region)
    return _COMMAND_SCAFFOLD_PATTERN.sub("", "".join(parts))


def normalize_user_message(text: str) -> str:
    """ユーザー発話からハーネス生成の足場を落として依頼本文だけを残す。

    transcript の ``user`` エントリにはユーザーの依頼だけでなく、ハーネスが
    自分で生成した足場（ローカルコマンドの注意書き・その stdout・サブ
    エージェント完了通知・system-reminder・スラッシュコマンドの起動タグ）も
    同じ形で載る。これらを依頼として引き継ぐと、引き継ぎ本文が足場だけで
    埋まって実際の依頼が押し出されるうえ、注意書きに含まれる指示文が次
    セッションへ疑似ユーザー指示として再注入される。

    足場のうち中身が無価値なものは丸ごと捨て、スラッシュコマンド起動だけは
    ``/name args`` へ畳んで依頼としての情報を残す。足場の中身に閉じタグを
    混ぜて除去を回避しようとした入力はメッセージごと破棄する。

    Args:
        text: transcript の ``user`` エントリから取り出した生テキスト。

    Returns:
        依頼本文。足場しか含まれていない、または細工が検出された場合は空文字列。

    Raises:
        例外は発生しません。
    """
    if not text:
        return text
    stripped = _drop_scaffold_blocks(text)
    if stripped is None:
        return ""
    return _fold_command_invocation(stripped).strip()
