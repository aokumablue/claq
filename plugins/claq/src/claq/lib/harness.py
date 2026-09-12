"""コーディングエージェントハーネスの入力形式差分を吸収する汎用パーサ。

host 判定は一切行わない。フィールド名の union を無条件に受理する寛容パーサ
のみを提供する（tool_input / toolArgs、tool_name / toolName 等の複数命名を
同一の意味へ正規化する）。すべて純 stdlib のみに依存する（venv 不在時の
フォールバック実行を保証するため）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
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
    # mem/handoff.py の使用ツール記録がハーネス横断で正規化名を
    # 使うために正規化する。
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

# ``json.loads`` が「この文字列は JSON として読めない」ことを示すために送出しうる
# 例外。本モジュールと ``hook_common.parse_json_object`` の JSON パースは全て
# この 1 タプルで受ける（片側だけ塞ぐと、同型の穴が別の呼び出し箇所に残る）。
# ``INPUT_CONTAINER_KEYS`` と同じ理由で公開名にしている —— 走査・パースの
# 意味論をフック側の手書きに委ねると片側だけ緩い状態が再発する。
#
# ``RecursionError`` を含めるのは必須。CPython の JSON デコーダは再帰下降で、
# 深くネストした配列/オブジェクトに対して ``JSONDecodeError`` ではなく
# ``RecursionError`` を送出する。これを取りこぼすと保護フックが**例外で異常終了**
# し、PreToolUse の exit 1（= non-blocking error。ツールはそのまま実行される）
# へ倒れる —— つまり fail-open になる。実測: 深さ 200000 の入れ子を JSON 文字列値
# の内側に置いた 400KB の payload（``MAX_STDIN_BYTES`` = 1MiB の内側）で
# ``block_no_verify`` が exit 1。パース失敗として扱えば生文字列が返り、以降の
# トークナイザ検査（fail-closed 側）に載る。
JSON_PARSE_FAILURES = (json.JSONDecodeError, TypeError, RecursionError)


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
        # 判定とパースは同じ文字列に対して行う。lstrip 後の値で「JSON らしさ」を
        # 見ながら元の文字列をパースすると、JSON が許容しない空白（\x0c 等）を
        # 1 文字前置しただけで「{ で始まるがパースできない」状態になり、生コマンド
        # 文字列として扱われる。その文字列の中では危険なコマンドが JSON のダブル
        # クォート内に入るため、トークナイザからは実行トークンに見えず素通りする
        # （実測: block_no_verify が exit 0。正常な JSON なら exit 2）。
        stripped = value.lstrip()
        if not stripped.startswith(("{", "[")):
            return value
        try:
            return json.loads(stripped)
        except JSON_PARSE_FAILURES:
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


def iter_tool_input_containers(payload: dict[str, Any]) -> Iterator[Any]:
    """payload に存在する全コンテナキーの正規化済み tool 入力を順に返す。

    ``extract_tool_input`` は先勝ちで 1 キーだけを返す。それを単独で使うと、
    payload が複数のコンテナキーを持つ host（``tool_input`` と ``toolArgs``
    を同時に載せる形）で先頭の無害な側だけを検査し、後続キーに入った危険な
    入力を素通りさせる（実測: ``bash_config_protection`` /
    ``pre_bash_commit_quality`` が exit 0 のまま保護対象書き込みと commit を
    通した）。走査の意味論をフック側の手書きループに委ねると片側だけ緩い
    状態が再発するため、全キー走査は本関数を単一情報源とする。

    Args:
        payload: フック stdin を JSON として読んだ dict。

    Yields:
        コンテナキーごとの正規化済み tool 入力（dict / str / list など）。

    Raises:
        例外は発生しません。
    """
    for key in INPUT_CONTAINER_KEYS:
        if key not in payload:
            continue
        yield extract_tool_input({key: payload[key]})


def iter_bash_commands(payload: dict[str, Any]) -> Iterator[str]:
    """フック payload に含まれる Bash/shell の command 文字列を全て返す。

    ``tool_input.command`` / ``toolArgs``（JSON 文字列含む）/ 生文字列を吸収し、
    コンテナキーは 1 つも取りこぼさず走査する。非 dict の tool_input に対して
    ``.get`` して AttributeError にならない。

    list 形状（``{"tool_input": [{"command": "..."}]}`` や
    ``{"tool_input": ["..."]}``）は要素ごとに取り出して個別に返す。
    ここを取りこぼすと、``extract_tool_input`` は list を返して「必須
    フィールド欠落」の fail-closed を素通りするため、コマンドが 1 つも無い
    payload として静かに許可されてしまう（実測: ``git commit --no-verify``
    を list に包むと ``block_no_verify`` が exit 0）。

    **1 つも yield しない場合の扱いは呼び出し側の責務**。現状の 3 フックは
    いずれも「検査対象なし」として allow する。コンテナキー自体が無い場合の
    fail-closed は ``block_no_verify`` が ``extract_tool_input`` の None 判定で
    別途行っており、本関数はそれを代替しない。

    Args:
        payload: フック stdin を JSON として読んだ dict。

    Yields:
        非空の command 文字列。1 つも取れなければ何も yield しない。

    Raises:
        例外は発生しません。
    """
    for container in iter_tool_input_containers(payload):
        yield from _commands_from_tool_input(container)


def _commands_from_tool_input(tool_input: Any) -> list[str]:
    """正規化済み tool 入力から command 文字列を**すべて**取り出す。

    フィールド別名（``command`` / ``cmd``）は 1 つも取りこぼさず走査する。
    先勝ちで最初に一致したキーだけを返すと、無害な ``command`` を 1 つ足す
    だけで ``cmd`` の危険なコマンドが検査から外れる（実測: 4 フックすべてが
    exit 0）。コンテナキー側で塞いだ先勝ちバイパスと同型のものが 1 階層下に
    残っていた。dict の挿入順ではなくこのタプルの順序が結果を決めるため、
    ``{"cmd": 危険, "command": 無害}`` の並びでも同じく素通りしていた。

    Args:
        tool_input: `extract_tool_input` の戻り値。

    Returns:
        非空のコマンド文字列のリスト。list 形状は要素ごとに再帰して連結する。
        取れなければ空リスト。

    Raises:
        例外は発生しません。
    """
    if isinstance(tool_input, dict):
        # 値は文字列とは限らない。``{"command": ["bash", "-lc", "git commit
        # --no-verify -m x"]}`` のように argv 配列で渡す host があり、文字列
        # 以外を捨てていた頃はコマンドが 1 つも無い payload として静かに許可
        # されていた（実測: block_no_verify が exit 0）。コンテナ側の list 形状は
        # 既に展開しているのに、フィールド値側だけ文字列限定なのは非対称な
        # 取りこぼしだった。要素ごとに個別のコマンド文字列として返す。
        return [
            command
            for key in ("command", "cmd")
            if key in tool_input
            for command in _commands_from_tool_input(tool_input[key])
        ]
    if isinstance(tool_input, str):
        return [tool_input] if tool_input else []
    if isinstance(tool_input, list):
        return [command for item in tool_input for command in _commands_from_tool_input(item)]
    return []


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


# 本パーサが「どのツールか特定できた」と言える正規化後の名前（小文字比較）。
# `_TOOL_NAME_MAP` の値から導出する。写経すると片方だけ更新されて判定がずれる。
_IDENTIFIABLE_TOOL_NAMES = frozenset(name.lower() for name in _TOOL_NAME_MAP.values())


def is_unidentifiable_tool(tool_name: str) -> bool:
    """ツール名が既知のどれとも一致しないかを判定する。

    ツール名で検査対象を絞るフックは、名前を特定できない payload を
    「対象外」と読み替えてはならない。``{"tool_name": 123}`` や
    ``{"tool": "Write"}`` のように `extract_raw_tool_name` が空文字へ倒れる形、
    あるいは host 固有の未知の名前（``mcp__fs__write``）で保護が丸ごと無効に
    なっていた（実測: config_protection が保護対象への書き込みを exit 0）。

    ツール名の照合はハーネスの matcher が既に済ませており、フック内の再照合は
    冗長なゲートでしかない。したがって「既知の対象外ツール」だけを skip し、
    特定できない名前は検査側へ倒す（ADR-0002: 誤検出 > 誤通過）。

    Args:
        tool_name: `extract_raw_tool_name` が返した正規化前の生ツール名。

    Returns:
        既知のツール名へ正規化できなければ True。

    Raises:
        例外は発生しません。
    """
    return normalize_tool_name(tool_name).lower() not in _IDENTIFIABLE_TOOL_NAMES


def _extract_patch_text(tool_input: dict | str | None) -> str | None:
    """入力から構造化パッチ本文候補を取り出す。

    Copilot CLI では生のパッチ文字列、他ハーネスでは {"input": "..."} の
    ような dict で渡ることがあるため、両方を吸収する。JSON 文字列化された
    dict が来た場合も input フィールドを復元する。ツール名に関わらず、
    渡された入力の「形」だけから候補テキストを取り出す（判定は呼び出し側）。

    判定とパースは同じ文字列（``lstrip`` 後）に対して行う。``extract_tool_input``
    と同じ非対称がここにも残っていた: ``lstrip()`` 後の値で ``{`` 始まりを見て、
    パースには**元の文字列**を渡していた。``\\x0c`` / ``\\x0b`` は Python の空白
    だが JSON の空白ではないため、1 文字前置するだけで「``{`` で始まると判定
    されたのにパースは失敗する」状態になり、patch 本文ではなく生の JSON 文字列
    がそのままパス候補として返る（実測: ``apply_patch`` 以外のツール名で
    ``extract_file_paths`` が ``['\\x0c{"input": ...}']`` を返し、本来の対象
    ``.git/hooks/pre-commit`` が候補から消える）。
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
        parsed = json.loads(stripped)
    except JSON_PARSE_FAILURES:
        return tool_input
    patch_text = parsed.get("input")
    return patch_text if isinstance(patch_text, str) else tool_input


def _has_patch_markers(patch_text: str) -> bool:
    """テキストが構造化パッチのファイル操作マーカー行を 1 つ以上含むか判定する。"""
    return any(line.startswith(marker) for line in patch_text.splitlines() for marker in _PATCH_FILE_MARKERS)


def _path_strings(value: Any) -> list[str] | None:
    """`file_path` / `file` フィールドの値をパス文字列のリストへ平坦化する。

    値は文字列とは限らない。``{"file_path": ["ruff.toml"]}`` のように配列で
    渡す host があり、文字列以外を捨てていた頃は「対象ファイルが 1 つも無い
    書き込み」として静かに許可されていた（実測: config_protection が exit 0）。

    Args:
        value: `file_path` / `file` フィールドの生の値。

    Returns:
        パス文字列のリスト（空文字は除く）。文字列でも list でもない値は
        パスとして解釈できないため None を返し、呼び出し側で fail-closed に
        倒す（ADR-0002: 誤検出 > 誤通過）。

    Raises:
        例外は発生しません。
    """
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list):
        return None
    paths: list[str] = []
    for item in value:
        nested = _path_strings(item)
        if nested is None:
            return None
        paths.extend(nested)
    return paths


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
    ``_commands_from_tool_input`` が同じ取りこぼしを共有層で塞いだのと同型の
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

    # フィールド別名は 1 つも取りこぼさず走査する。先勝ちで最初に一致した
    # キーだけを返すと、無害な `file_path` を 1 つ足すだけで `file` の保護対象
    # パスが検査から外れる（実測: config_protection が exit 0）。コンテナキー
    # 側で塞いだ先勝ちバイパスと同型のものが 1 階層下に残っていた。
    # 値は文字列とは限らない。``{"file_path": ["ruff.toml"]}`` のように配列で
    # 渡す host があり、文字列以外を捨てていた頃は「対象ファイルが 1 つも無い
    # 書き込み」として静かに許可されていた（実測: config_protection が exit 0）。
    # コンテナ側の list 形状は既に展開しているので、フィールド値側も同じ規則で
    # 展開して非対称をなくす。判定不能（None）はそのまま伝播させ fail-closed。
    paths = []
    for key in ("file_path", "file"):
        if key not in tool_input:
            continue
        nested = _path_strings(tool_input[key])
        if nested is None:
            return None
        paths.extend(nested)
    return paths


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

# 孤立タグ検出専用の「名前の終わり」。除去側（`_TAG_NAME_END`）と違い**文字列の
# 終端も名前の終わりとみなす**。除去側の先読みは名前の後ろに 1 文字を要求するため、
# 発話が足場タグ名で終わる入力（`"作業完了 <system-reminder"`）はブロック除去にも
# 孤立検出にも一致せず、ADR-0015 の fail closed が発火しないまま素通りしていた
# （実測。明示 handoff 経路は他の要因なしで単独成立する）。
#
# 共有定数側を広げてはならない。除去パターンで `$` を許すと `<system-reminder` の
# ように閉じない開始タグが「名前の終わり」を満たしてしまい、消す側の精度要求
# （`<system-reminders>` を巻き込まない）と噛み合わない。広げてよいのは
# 「破棄するかどうか」だけを決める本パターンに限る。
_ORPHAN_NAME_END = r"(?=[\s/>]|$)"

# タグの属性部に許す最大文字数。`[^>]*` を無界にすると、`>` を 1 個も含まない入力
# （`"<system-reminder " * N`）で各開始位置が末尾まで走査して二次オーダーになる。
# 実測（撤去した件数ガードでは防げていなかった経路）: 498KB で 8.8 秒、996KB で
# 35.7 秒、2MB は 120 秒でも終わらない。SessionEnd で毎回通る経路なので有界化する。
# 実在のタグ属性がこの長さを超えることはなく、超えた時点でタグとして扱わない。
#
# **この上限は複雑度のための装置であって、安全性の境界ではない。** 上限超過を
# 「タグではない」と読むと ADR-0015 が宣言した fail closed に穴が開くため、
# 破棄判定を担う `_SCAFFOLD_ORPHAN_PATTERN` はこの上限を使わない（属性
# 513 文字の `<system-reminder …>` がブロック除去にも孤立タグ検出にも一致せず
# 素通りしていた）。上限を使うのは除去パターンだけで、超過して除去できなかった
# ブロックは孤立タグとして検出され、メッセージごと破棄される。
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
#
# 属性部（`_TAG_ATTRS`）と閉じ括弧は**要求しない**。要求していた頃は、属性が
# 512 文字を超える `<system-reminder …>` がブロック除去にも本検出にも一致せず、
# 「孤立タグ 1 個で破棄」が発火しないまま素通りしていた（実測）。除去できなかった
# ものほど破棄すべきなのに、除去と破棄が同じ上限を共有していたため両方が同時に
# 外れる構造だった。足場名＋名前の終わりが残っている時点で、対にならなかった
# 足場タグか細工されたタグのどちらかであり、いずれも破棄が正しい。先読みも
# 選択肢も固定幅なので走査は入力長に対して線形。
#
# 名前の終わりも除去側とは別定義（`_ORPHAN_NAME_END`）を使い、文字列の終端を
# 含める。除去側の定義を使っていた頃は `"作業完了 <system-reminder"` のように
# 足場タグ名で終わる入力が除去にも破棄にも一致しなかった（実測）。
_SCAFFOLD_ORPHAN_PATTERN = re.compile(
    r"</?(?:" + "|".join(_SCAFFOLD_TAGS) + r")" + _ORPHAN_NAME_END, re.IGNORECASE
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
    ``<claq-memory>`` ブロック内に前回の ``<command-name>`` が残っていると、
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
