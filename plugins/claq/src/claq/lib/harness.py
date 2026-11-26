"""コーディングエージェントハーネスの入力形式差分を吸収する汎用パーサ。

host 判定は一切行わない。フィールド名の union を無条件に受理する寛容パーサ
のみを提供する（tool_input / toolArgs、tool_name / toolName 等の複数命名を
同一の意味へ正規化する）。標準ライブラリだけに依存する（ランタイムは venv を作らない）。
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterable, Iterator

# `typing` の import は約 1 ms かかり、全保護フックの起動ごとに載る。注釈は `from __future__ import annotations` で評価されないので、型検査器にだけ見せる。
TYPE_CHECKING = False
if TYPE_CHECKING:
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
    # Windows の Claude Code の PowerShell ツール（Copilot CLI の powershell も同名）。
    # 保護フックは PowerShell の書き方も読むので、Bash と同じ判定に通す。
    "powershell": "Bash",
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
# （同じ内容を各所で持つと、キーを足す改修が片方だけに入って取りこぼす）。
INPUT_CONTAINER_KEYS = ("tool_input", "toolInput", "toolArgs", "tool_args")

# 構造化パッチテキストのファイル操作マーカー（Codex apply_patch 形式）
_PATCH_FILE_MARKERS = ("*** Add File: ", "*** Update File: ", "*** Delete File: ")

# ``json.loads`` が「この文字列は JSON として読めない」ことを示すために送出しうる
# 例外。本モジュールと ``stdin_io.parse_json_object`` の JSON パースは全て
# この 1 タプルで受ける（片側だけ塞ぐと、同型の穴が別の呼び出し箇所に残る）。
#
# ``RecursionError`` を含めるのは必須。CPython の JSON デコーダは深くネストした
# 配列/オブジェクトに対して ``RecursionError`` を送出する。これを取りこぼすと
# 保護フックが例外で exit 1（= PreToolUse の non-blocking error。ツールはそのまま
# 実行される）へ倒れ、fail-open になる。パース失敗として扱えば生文字列が返り、
# 以降のトークナイザ検査（fail-closed 側）に載る。
# ``ValueError`` で受けるのも同じ理由。4300 桁を超える整数は ``JSONDecodeError`` ではなく
# 素の ``ValueError`` になる（``JSONDecodeError`` は ``ValueError`` の派生なので両方を受ける）。
JSON_PARSE_FAILURES = (ValueError, TypeError, RecursionError)


def extract_tool_input(payload: dict[str, Any]) -> Any:
    """フック payload から tool_input / toolInput / toolArgs を正規化して返す。

    Claude / VS Code 互換は ``tool_input``、Grok camelCase は ``toolInput``、
    Copilot camelCase は ``toolArgs``（JSON 文字列のことが多い）。文字列で
    JSON オブジェクトに見える場合はパースして返し、パース不能なら元の文字列を返す。
    ``toolInput`` を落とすと Grok の PreToolUse で tool 入力が取れず、
    ``block_no_verify`` が全 ``run_terminal_command`` を exit 2 で拒否する。

    Returns:
        正規化後の tool 入力（dict / str / その他）、キーが無ければ None。
    """
    return next(iter_tool_input_containers(payload), None)


def extract_raw_tool_name(payload: dict[str, Any]) -> str:
    """フック payload から正規化前の生ツール名を返す。

    非空の ``tool_name`` を優先し、無ければ非空の ``toolName`` を使う。
    空文字・非文字列は無効として次候補へ倒す。どちらも無効なら空文字。
    """
    for key in ("tool_name", "toolName"):
        tool_name = payload.get(key)
        if isinstance(tool_name, str) and tool_name:
            return tool_name
    return ""


def iter_tool_input_containers(payload: dict[str, Any]) -> Iterator[Any]:
    """payload に存在する全コンテナキーの正規化済み tool 入力を順に返す。

    ``extract_tool_input`` は先勝ちで 1 キーだけを返す。それを単独で使うと、
    複数のコンテナキーを同時に載せた payload で先頭の無害な側だけを検査し、
    後続キーに入った危険な入力を素通りさせる。全キー走査は本関数を単一情報源とする。
    """
    for key in INPUT_CONTAINER_KEYS:
        if key in payload:
            yield _parse_container_value(payload[key])


def _parse_container_value(value: Any) -> Any:
    """コンテナキーの値を正規化する。JSON に見える文字列はパースし、読めなければ元の文字列を返す。"""
    if not isinstance(value, str):
        return value
    # 判定とパースは同じ文字列に対して行う。lstrip 後の値で「JSON らしさ」を
    # 見ながら元の文字列をパースすると、JSON が許容しない空白（\x0c 等）を
    # 1 文字前置しただけで「{ で始まるがパースできない」状態になり、生コマンド
    # 文字列として扱われる。その文字列の中では危険なコマンドが JSON のダブル
    # クォート内に入るため、トークナイザからは実行トークンに見えず素通りする。
    stripped = value.lstrip()
    if not stripped.startswith(("{", "[")):
        return value
    try:
        return json.loads(stripped)
    except JSON_PARSE_FAILURES:
        return value


def iter_bash_command_groups(payload: dict[str, Any]) -> Iterator[tuple[str, ...]]:
    """フック payload に含まれる Bash/shell の command をグループ単位で返す。

    ``tool_input.command`` / ``toolArgs``（JSON 文字列含む）/ 生文字列 / list 形状を
    吸収し、コンテナキーは 1 つも取りこぼさず走査する。

    グループは「同じ 1 回の呼び出しを表す候補文字列の組」である。全て str で 2 要素以上の
    list（argv 配列）は（各要素…, ``shlex.join`` で結合した 1 文字列）の 1 グループになり、
    それ以外の command 文字列は 1 要素のグループになる。要素と結合は同じ呼び出しの
    別表現なので、呼び出し側が「1 コマンド」を数える（commit の数え上げ等）ときは
    グループを単位にする。長さ 2 以上のグループは必ず末尾が結合文字列である。

    1 つも yield しない場合の扱いは呼び出し側の責務。コンテナキー自体が無い場合の
    fail-closed は ``block_no_verify`` が ``extract_tool_input`` の None 判定で別途行う。

    Yields:
        非空の command 文字列からなるグループ。
    """
    for container in iter_tool_input_containers(payload):
        yield from _commands_from_tool_input(container)


def iter_bash_commands(payload: dict[str, Any]) -> Iterator[str]:
    """フック payload に含まれる Bash/shell の command 文字列を全て返す。

    ``iter_bash_command_groups`` を平坦化したもの。「どれか 1 つでも該当すれば deny」
    の判定（``block_no_verify`` / ``bash_config_protection``）は、候補の個数に意味が
    無いのでこちらを使う。list 形状を取りこぼすと、コマンドが 1 つも無い payload として
    静かに許可されてしまう。

    Yields:
        非空の command 文字列。
    """
    for group in iter_bash_command_groups(payload):
        yield from group


def _commands_from_tool_input(tool_input: Any) -> list[tuple[str, ...]]:
    """正規化済み tool 入力から command 文字列を**すべて**グループ単位で取り出す。

    フィールド別名（``command`` / ``cmd``）は 1 つも取りこぼさず走査する。先勝ちに
    すると、無害な ``command`` を 1 つ足すだけで ``cmd`` の危険なコマンドが検査から
    外れる（dict の挿入順ではなくキーの順序が結果を決めるため、並びを問わない）。

    Returns:
        非空の command 文字列のグループのリスト。文字列は 1 要素のグループ。要素が 2 つ
        以上で全て str の list（argv 配列）は、非空の要素に ``shlex.join`` で 1 コマンドへ
        結合した文字列を末尾に足した 1 グループ。要素単位でしか見ないと
        ``["git", "commit", "--no-verify"]`` の各要素は単独では無害に読め、全フックを
        素通りする。それ以外の list は要素ごとに再帰して連結する。
    """
    if isinstance(tool_input, dict):
        # 値は文字列とは限らない。``{"command": ["bash", "-lc", "git commit
        # --no-verify -m x"]}`` のように argv 配列で渡す host があり、文字列
        # 以外を捨てるとコマンドが 1 つも無い payload として静かに許可される。
        return [
            group
            for key in ("command", "cmd")
            if key in tool_input
            for group in _commands_from_tool_input(tool_input[key])
        ]
    if isinstance(tool_input, str):
        return [(tool_input,)] if tool_input else []
    if isinstance(tool_input, list):
        if len(tool_input) > 1 and all(isinstance(item, str) for item in tool_input):
            return [(*(item for item in tool_input if item), shlex.join(tool_input))]
        return [group for item in tool_input for group in _commands_from_tool_input(item)]
    return []


def normalize_tool_name(tool_name: str) -> str:
    """ハーネス固有のツール名を Claude Code 相当のツール名へ正規化する。

    大文字小文字を区別せず ``_TOOL_NAME_MAP`` で引き、対象外はそのまま返す。
    Claude Code に apply_patch というツールは存在せず、Claude Code 自身のツール名は
    既に正規形のため、ハーネス判定なしの無条件マッピングで安全。
    """
    return _TOOL_NAME_MAP.get(tool_name.lower(), tool_name)


# 本パーサが「どのツールか特定できた」と言える正規化後の名前（小文字比較）。
# `_TOOL_NAME_MAP` の値から導出する。写経すると片方だけ更新されて判定がずれる。
_IDENTIFIABLE_TOOL_NAMES = frozenset(name.lower() for name in _TOOL_NAME_MAP.values())


def is_known_tool_outside(tool_name: str, scope: frozenset[str]) -> bool:
    """ツール名が既知のツールで、かつ検査対象 *scope* の外にあるかを判定する。

    ツール名で検査対象を絞るフックは、名前を特定できない payload（空文字へ倒れた
    ``{"tool_name": 123}`` や未知の ``mcp__fs__write``）を「対象外」と読み替えては
    ならない。ツール名の照合はハーネスの matcher が既に済ませているため、「既知の
    対象外ツール」だけを skip し、特定できない名前は検査側へ倒す
    （docs/adr/shell-analysis-boundary.md: 誤検出 > 誤通過）。

    Args:
        tool_name: 正規化前の生ツール名。
        scope: 検査対象とする正規化後のツール名（小文字）。

    Returns:
        skip してよい（既知かつ対象外の）ツールなら True。
    """
    normalized = normalize_tool_name(tool_name).lower()
    return normalized not in scope and normalized in _IDENTIFIABLE_TOOL_NAMES


def _extract_patch_text(tool_input: dict | str | None) -> str | None:
    """入力から構造化パッチ本文候補を取り出す。

    Copilot CLI では生のパッチ文字列、他ハーネスでは {"input": "..."} のような dict、
    あるいはその JSON 文字列で渡るため、すべて吸収する。判定は呼び出し側が行う。

    判定とパースは同じ ``lstrip`` 後の文字列に対して行う。``\\x0c`` / ``\\x0b`` は
    Python の空白だが JSON の空白ではないため、元の文字列をパースすると 1 文字前置
    するだけでパースが失敗し、patch 本文ではなく生の JSON 文字列がパス候補として返る。
    ``[`` で始まる文字列はパースせず生のパッチとして返す（`_parse_container_value` と違い
    dict の JSON だけを読む）。
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


def _concat_or_none(parts: Iterable[list[str] | None]) -> list[str] | None:
    """パス一覧を連結する。1 つでも判定不能（None）があれば None を返す。"""
    paths: list[str] = []
    for nested in parts:
        if nested is None:
            return None
        paths.extend(nested)
    return paths


def _path_strings(value: Any) -> list[str] | None:
    """`file_path` / `file` フィールドの値をパス文字列のリストへ平坦化する。

    値は文字列とは限らない。``{"file_path": ["ruff.toml"]}`` のように配列で渡す host が
    あり、文字列以外を捨てると「対象ファイルが 1 つも無い書き込み」として静かに許可される。

    Returns:
        パス文字列のリスト（空文字は除く）。文字列でも list でもない値はパスとして
        解釈できないため None を返し、呼び出し側で fail-closed に倒す
        （docs/adr/shell-analysis-boundary.md: 誤検出 > 誤通過）。
    """
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list):
        return None
    return _concat_or_none(_path_strings(item) for item in value)


def extract_file_paths(tool_name: str, tool_input: dict | str | None) -> list[str] | None:
    """ツール入力から操作対象のファイルパス一覧を抽出する。

    Edit/Write/MultiEdit は file_path フィールド（旧名 ``file`` も同義）を使う。
    構造化パッチ（Copilot/Codex の apply_patch 等）はマーカー行をパースする。パッチか
    どうかは tool_name だけに頼らず、マーカー行の有無でも判定する（ハーネスごとの
    命名差異でツール名が "apply_patch" と一致しなくても検査から漏らさないため）。

    list 形状（``[{"file_path": "..."}]`` や ``["..."]``）と生文字列形状
    （``"ruff.toml"``）も展開する。ここで空リストを返すと、``config_protection`` は
    「対象ファイルが 1 つも無い書き込み」として静かに許可してしまう。

    Args:
        tool_name: フック stdin の tool_name フィールド値（正規化前）。
        tool_input: フック stdin の tool_input フィールド値。dict / list / 文字列 / None。

    Returns:
        ファイルパスのリスト。判定不能（パッチ本文と分かっているのにマーカーが 1 つも
        見つからない等）の場合は None。呼び出し側は None を fail-closed として扱うこと。
    """
    patch_text = _extract_patch_text(tool_input)
    is_declared_patch_tool = tool_name == "apply_patch"
    if isinstance(patch_text, str):
        paths = [
            line[len(marker) :].strip()
            for line in patch_text.splitlines()
            for marker in _PATCH_FILE_MARKERS
            if line.startswith(marker)
        ]
        # マーカーが 1 つも無い本文は、apply_patch と明示されていなければパッチではなく通常の入力として読み進める。
        if is_declared_patch_tool or paths:
            return paths or None

    if isinstance(tool_input, list):
        paths = _concat_or_none(extract_file_paths(tool_name, item) for item in tool_input)
        # apply_patch と明示されているのに対象を 1 つも取り出せない: 判定不能
        return None if is_declared_patch_tool and not paths else paths

    if is_declared_patch_tool:
        # apply_patch と明示されているのにパッチ本文を取り出せない: 判定不能
        return None

    if isinstance(tool_input, str):
        return [tool_input] if tool_input else []

    if not isinstance(tool_input, dict):
        return []

    # フィールド別名は 1 つも取りこぼさず走査する。先勝ちにすると、無害な `file_path`
    # を 1 つ足すだけで `file` の保護対象パスが検査から外れる。判定不能（None）は
    # そのまま伝播させ fail-closed。
    return _concat_or_none(_path_strings(tool_input[key]) for key in ("file_path", "file") if key in tool_input)
