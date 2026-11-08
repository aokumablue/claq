"""重要な設定ファイルを意図しない変更から保護します。

トリガー: pre:edit, pre:write
入力: 変更されるファイルパスを含むJSON
出力: 保護されたファイルが変更される場合は host 非依存の合併出力でブロック
終了: 0 (許可) または 2 (ブロック。emit_block_output が stderr の理由と
      stdout の permissionDecision: deny JSON を同時に出す)

launcher は stdin を代読しないため、入力切り捨て時のブロックも本モジュール自身が判定する。
内容次第で保護するファイルの検査は `conditional_protection` に置く。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from claq.hooks.conditional_protection import conditional_block_reason, git_config_block_reason
from claq.hooks.git_config_text import GLOBAL_GIT_CONFIG_DISPLAY_NAMES, global_git_config_path
from claq.hooks.hook_common import (
    basename,
    emit_block_output,
    fold_path_parts,
    memoized_in_scan,
    normalize_protected_name,
    resolve_effective_target,
    resolve_repo_root,
    scan_memo,
)
from claq.hooks.stdin_io import (
    MAX_STDIN_BYTES,
    StdinUnavailableError,
    parse_json_object,
    read_raw_stdin_with_truncation,
    stdin_unreadable_message,
)
from claq.lib.core_utils import get_plugin_root
from claq.lib.harness import (
    extract_file_paths,
    extract_raw_tool_name,
    is_known_tool_outside,
    iter_tool_input_containers,
)

# matcher（hooks.json）は書込み系ツールに限定されているが、matcher の綴りがズレても
# 本体側で書込み系以外を早期 return する多重防御。
_WRITE_TOOL_NAMES = frozenset({"write", "edit", "multiedit"})

# 対象ファイルを確定できない入力全般に出す fail-closed の理由。構造化パッチ本文が
# 読めない場合だけでなく、`file_path` がパスとして解釈できない値の場合も来るため、
# 文言を「パッチ」に限定しない。
_UNDECIDABLE_TARGET_MESSAGE = "BLOCKED: Could not determine target files from tool input."


# frozenset で持つ。判定に使うのは下の `*_FOLDED`（import 時に 1 回だけ畳んだ複製）で、
# 実行時にこちらへ追加しても保護は増えない。その食い違いを黙って通さないよう、
# 変更を AttributeError で落ちる形にしておく。
PROTECTED_FILES = frozenset({
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    "eslint.config.ts",
    "eslint.config.mts",
    "eslint.config.cts",
    ".prettierrc",
    ".prettierrc.js",
    ".prettierrc.cjs",
    ".prettierrc.json",
    ".prettierrc.yml",
    ".prettierrc.yaml",
    "prettier.config.js",
    "prettier.config.cjs",
    "prettier.config.mjs",
    "biome.json",
    "biome.jsonc",
    ".ruff.toml",
    "ruff.toml",
    ".shellcheckrc",
    ".stylelintrc",
    ".stylelintrc.json",
    ".stylelintrc.yml",
    ".stylelintrc.yaml",
    ".markdownlint.json",
    ".markdownlint.yaml",
    ".markdownlint.yml",
    ".markdownlintrc",
    # コミット前検査そのものの定義。弱めれば lint 設定を弱めるのと同じ効果を
    # 得られるため、個別の lint 設定と同じ重みで保護する。
    ".pre-commit-config.yaml",
    ".pre-commit-config.yml",
})

# ディレクトリ単位で保護する path。basename ``pre-commit`` を一律ブロックすると無関係な
# 同名ファイルを巻き込むため、実体 path の連続した要素で見る。
#
# ``.git/hooks/`` は git 自身のフック本体で、直接書き換えれば ``--no-verify`` と同じ結果
# になる。``hooks/hooks.json`` はこのプラグイン自身の PreToolUse ガードの登録元で、1 回の
# Edit で全部を無効化できる。片側だけ塞ぐと、塞いだ経路の存在が誤った安心になる。
# basename ``hooks.json`` では判定しない（consumer 側の ``.claude/hooks.json`` は
# `/harness --apply` が正当に書き込む先）。
_PROTECTED_PATH_SEGMENTS = ((".git", "hooks"), ("hooks", "hooks.json"))

# 大小無視で比較するための畳み済み複製。`.GIT/HOOKS` は APFS / NTFS では
# `.git/hooks` そのものを指すため、素の比較では保護 path が素通りする。
# 表示名には畳む前の綴りを使うので、両者を対で持つ。
_PROTECTED_PATH_SEGMENTS_FOLDED = tuple(fold_path_parts(segments) for segments in _PROTECTED_PATH_SEGMENTS)

# 内容次第で保護する git config のパス。remote / branch / user の通常編集が正当にあるため
# 無条件では塞がず、置換後の全文を解析して ``core.hooksPath`` / ``include`` / ``alias`` のように
# フック実行を左右する key（`block_no_verify` が ``git config`` 経由で塞ぐものと同じ集合）が
# 新しく現れる、または値が変わるときだけ塞ぐ。Bash 経由の書込みは内容を解釈できないため
# `bash_config_protection` が書込み先位置に現れた時点で無条件に塞ぐ。
#
# リポジトリ内の設定ファイル（``*`` は worktree 名）。
_GIT_CONFIG_REPO_SEGMENTS = (
    (".git", "config"),
    (".git", "config.worktree"),
    (".git", "worktrees", "*", "config"),
    (".git", "worktrees", "*", "config.worktree"),
)
_GIT_CONFIG_REPO_SEGMENTS_FOLDED = tuple(fold_path_parts(segments) for segments in _GIT_CONFIG_REPO_SEGMENTS)

# git config として扱う保護 path の表示名の全体（ホーム側を含む）。ブロック文言を選ぶ鍵として公開する。
GIT_CONFIG_DISPLAY_NAMES = frozenset(
    {"/".join(segments) for segments in _GIT_CONFIG_REPO_SEGMENTS} | GLOBAL_GIT_CONFIG_DISPLAY_NAMES
)

# ファイル名だけでは保護できない汎用設定ファイル。version bump・依存追加等の正当な
# 編集が頻繁なため、書き込み内容が保護対象の設定を弱めうる場合のみブロックする。
CONDITIONALLY_PROTECTED_FILES = frozenset({
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    "package.json",
    # ホスト設定。保護フックを止める手段を持つ（`hooks` の差し替え、プラグインの無効化、
    # インタプリタ解決を乗っ取る `env`）。`model` / `permissions` / `env` の `DEBUG` 等の正当な
    # 編集を壊さないよう、置換前後の全文で `conditional_protection._SETTINGS_GUARDED_*` が変わるときだけ塞ぐ。
    "settings.json",
    "settings.local.json",
})

# 上 2 集合の大小無視の照合用複製。判定は必ずこちらを使う（`bash_config_protection` も
# 畳み済みの側を import して、両フックの判定軸を 1 本に保つ）。
PROTECTED_FILES_FOLDED = frozenset(normalize_protected_name(name) for name in PROTECTED_FILES)
CONDITIONALLY_PROTECTED_FILES_FOLDED = frozenset(
    normalize_protected_name(name) for name in CONDITIONALLY_PROTECTED_FILES
)

# 実行中の plugin root から見て守るディレクトリ。`runtime/` は hooks.json が起動する
# `claq-hook`、`src/claq/` はフック本体で、書き換えると次のツール呼び出しから全保護フックが外れる。
_RUNNING_PLUGIN_GUARDED_DIRS = (("runtime",), ("src", "claq"))

# 上の表示名。`blocked_message_for_file` がブロック文言を選ぶ鍵（Edit/Write と Bash で共有）。
RUNNING_PLUGIN_DISPLAY_NAMES = frozenset("/".join(segments) + "/" for segments in _RUNNING_PLUGIN_GUARDED_DIRS)


@memoized_in_scan
def _match_path_segments(
    file_path: str,
    candidates: tuple[tuple[str, ...], ...],
    folded_candidates: tuple[tuple[str, ...], ...],
) -> str | None:
    """パスの連続した要素が候補のいずれかに一致するか判定する。

    symlink は解決してから判定し、解決不能なら生パスで判定する。比較は畳んだ側だけで行い、
    返す表示名は畳む前の綴りを使う。候補の要素 ``*`` は任意の 1 要素に一致する。

    Args:
        file_path: 検査対象の生パス文字列。
        candidates: 表示名用の候補（畳む前の綴り）。
        folded_candidates: ``candidates`` を同じ順序で畳んだ複製。

    Returns:
        一致した候補の表示名（``/`` 区切り）。一致しなければ None。

    Raises:
        例外は発生しません。
    """
    resolved = resolve_effective_target(file_path)
    parts = resolved.parts if resolved is not None else Path(file_path).parts
    folded_parts = fold_path_parts(parts)
    for segments, folded_segments in zip(candidates, folded_candidates, strict=True):
        width = len(segments)
        for index in range(len(folded_parts) - width + 1):
            window = folded_parts[index : index + width]
            if all(want in ("*", got) for want, got in zip(folded_segments, window, strict=True)):
                return "/".join(segments)
    return None


def protected_path_segment(file_path: str) -> str | None:
    """パスがディレクトリ単位の保護対象配下か、保護ディレクトリ自身かを判定する。

    `bash_config_protection` が Bash 経路へ同じ判定を伝播させるため公開名にしている。
    保護ディレクトリ自身（``.git/hooks``）も該当扱いにする。ディレクトリごと消す・退避する
    操作（``rm -rf .git/hooks``）は配下ファイルの書換えと同じ結果になるため。

    Args:
        file_path: 検査対象の生パス文字列。

    Returns:
        該当した保護 path の表示名（``.git/hooks``）。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    return _match_path_segments(file_path, _PROTECTED_PATH_SEGMENTS, _PROTECTED_PATH_SEGMENTS_FOLDED)


def repo_git_config_path(file_path: str) -> str | None:
    """パスがリポジトリ内の git config（``.git/config`` 等）かを判定する。

    Edit/Write は置換後の全文で、Bash は `bash_config_protection` が書込み先位置に現れた時点で
    塞ぐ。後者のため公開名にしている。

    Args:
        file_path: 検査対象の生パス文字列。

    Returns:
        該当した保護 path の表示名（``.git/config`` 等）。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    return _match_path_segments(file_path, _GIT_CONFIG_REPO_SEGMENTS, _GIT_CONFIG_REPO_SEGMENTS_FOLDED)


def _effective_basename(file_path: str) -> str:
    """symlink を解決した実体 basename を返す。

    raw path の basename だけを見ると ``alias -> pyproject.toml`` のような symlink 経由の
    書込みがすり抜ける。解決不能（壊れた・循環した symlink 等）な場合は raw path の
    basename にフォールバックする。解決不能を deny に倒すと正当な編集まで巻き込むため。

    Args:
        file_path: 検査対象の生パス文字列。

    Returns:
        判定に使う basename。

    Raises:
        例外は発生しません。
    """
    resolved = resolve_effective_target(file_path)
    return resolved.name if resolved is not None else basename(file_path)


def blocked_message_for_file(file_name: str) -> str:
    """保護されたファイルに対するブロックメッセージを生成する。

    実行中の plugin root の保護ディレクトリ（`RUNNING_PLUGIN_DISPLAY_NAMES`）には専用の文言を返す。
    Edit/Write と Bash（`bash_config_protection`）が同じ表示名から同じ文言を得るため、選択をここに置く。

    Args:
        file_name: ブロックされたファイル名、または保護 path の表示名。

    Returns:
        ブロックメッセージ文字列

    Raises:
        例外は発生しません。
    """
    if file_name in RUNNING_PLUGIN_DISPLAY_NAMES:
        return _running_plugin_blocked_message(file_name)
    return (
        f"BLOCKED: Modifying {file_name} is not allowed. "
        "Fix the source code to satisfy linter/formatter rules instead of "
        "weakening the config. If this change is genuinely necessary, "
        "ask the user to make it — do not disable the protection hook yourself."
    )


@memoized_in_scan
def _running_plugin_root() -> Path:
    """実行中の plugin root を返す（`scan_memo` の間は 1 回だけ解決する）。

    Returns:
        `get_plugin_root` の値。
    """
    return get_plugin_root()


@memoized_in_scan
def running_plugin_root_is_guarded() -> bool:
    """実行中の plugin root を保護するか（作業中リポジトリ内の開発用 checkout でないか）を判定する。

    claq 自身を開発する checkout（作業中リポジトリの内側にあり、ランタイムを追跡している）では
    `runtime/` と `src/claq/` の編集が本来の作業なので守らない。toplevel が決まらない（リポジトリ外・
    git 不在・タイムアウト）ときは除外しない。git を起動するので、
    書込み先が保護ディレクトリに当たった後でだけ呼び、`scan_memo` の間は 1 回だけ判定する
    （保護ディレクトリに当たる書込み先が多い入力で、git を書込み先の数だけ起動しない）。

    Returns:
        保護するなら True。

    Raises:
        例外は発生しません。
    """
    repo_root = resolve_repo_root()
    if repo_root is None:
        return True
    plugin_root = _running_plugin_root()
    repo_parts = fold_path_parts(repo_root.resolve().parts)
    if fold_path_parts(plugin_root.parts)[: len(repo_parts)] != repo_parts:
        return True
    # ホームや ~/.claude を git で管理していると、キャッシュの plugin root も toplevel の内側に入る。
    # 内側にあるだけでなく、そのリポジトリがランタイムを追跡しているときだけ開発用 checkout とみなす。
    return not _plugin_runtime_is_tracked(repo_root, plugin_root)


def _plugin_runtime_is_tracked(repo_root: Path, plugin_root: Path) -> bool:
    """plugin root のランタイム入口 ``runtime/claq-hook`` がリポジトリで追跡されているかを返す。

    Args:
        repo_root: 作業中リポジトリの toplevel。
        plugin_root: 実行中の plugin root。

    Returns:
        index に載っていれば True。git が無い・タイムアウト・追跡外なら False（保護を外さない）。

    Raises:
        例外は発生しません。
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "--error-unmatch", "--", str(plugin_root / "runtime" / "claq-hook")],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def running_plugin_guarded_dir(resolved: Path, *, include_ancestors: bool) -> str | None:
    """実体パスが実行中の plugin root の保護ディレクトリに当たるかを判定する。

    比較は畳んだ要素列の前方一致で行う（APFS / NTFS では `resolve()` が大小を正規化しない）。

    Args:
        resolved: symlink を解決した書込み先の実体パス。
        include_ancestors: 保護ディレクトリ自身・祖先も当てるか（Bash の削除・退避）。

    Returns:
        当たった保護ディレクトリの表示名（``runtime/`` / ``src/claq/``）。当たらない、または
        開発用 checkout で保護しない場合は None。

    Raises:
        例外は発生しません。
    """
    target = fold_path_parts(resolved.parts)
    for segments in _RUNNING_PLUGIN_GUARDED_DIRS:
        guarded = fold_path_parts(_running_plugin_root().joinpath(*segments).parts)
        if target[: len(guarded)] == guarded or (include_ancestors and guarded[: len(target)] == target):
            return "/".join(segments) + "/" if running_plugin_root_is_guarded() else None
    return None


def _running_plugin_blocked_message(display_name: str) -> str:
    """実行中の plugin root の保護ディレクトリへの書込みに対するブロック理由を生成する。

    Args:
        display_name: 保護ディレクトリの表示名。

    Returns:
        ブロック理由文字列。

    Raises:
        例外は発生しません。
    """
    return (
        f"BLOCKED: Modifying {display_name} of the running claq plugin is not allowed. "
        "Every protection hook runs from this code, so changing it would disable all of them "
        "from the next tool call. If this change is genuinely necessary, ask the user to make it — "
        "do not disable the protection hook yourself."
    )


def _block_reason_for_file(tool_name: str, container: Any, file_path: str) -> str | None:
    """書込み先の 1 ファイルが保護対象ならブロック理由を返す。

    実行中の plugin root の保護ディレクトリ、保護 path、git config、basename の順に見る。
    git config は内容に問題が無ければ、続く basename の判定へ進む。

    Args:
        tool_name: 正規化前の生ツール名。
        container: extract_tool_input 相当の 1 コンテナ値。
        file_path: 対象ファイルパス。

    Returns:
        ブロック理由。保護対象でなければ None。

    Raises:
        例外は発生しません。
    """
    # 実体パスを解決できない file_path は位置で照合できないので、下の名前の判定に任せる。
    resolved = resolve_effective_target(file_path)
    if resolved is not None:
        running_dir = running_plugin_guarded_dir(resolved, include_ancestors=False)
        if running_dir is not None:
            return blocked_message_for_file(running_dir)
    protected_segment = protected_path_segment(file_path)
    if protected_segment is not None:
        return blocked_message_for_file(f"{protected_segment}/")
    git_config = repo_git_config_path(file_path) or global_git_config_path(file_path)
    if git_config is not None:
        reason = git_config_block_reason(tool_name, container, file_path, git_config)
        if reason:
            return reason
    file_name = _effective_basename(file_path)
    # 判定は畳んだ名前、メッセージは観測した綴り（利用者が書いた通りの名前が
    # 出ないと、なぜ止まったのかが読み取れなくなる）。
    folded_name = normalize_protected_name(file_name)
    if folded_name in PROTECTED_FILES_FOLDED:
        return blocked_message_for_file(file_name)
    if folded_name in CONDITIONALLY_PROTECTED_FILES_FOLDED:
        return conditional_block_reason(tool_name, container, file_path, file_name)
    return None


def _block_reason_for_container(tool_name: str, container: Any) -> str | None:
    """1 つの入力コンテナが保護対象ならブロック理由を返す。

    Args:
        tool_name: 正規化前の生ツール名。
        container: extract_tool_input 相当の 1 コンテナ値。

    Returns:
        ブロック理由。保護対象でなければ None。対象ファイルを確定できない
        場合は fail-closed メッセージ。

    Raises:
        例外は発生しません。
    """
    # 旧 ``file`` キーの補完・list 形状・生文字列形状の展開は共有層が担う。ここで dict
    # 限定の補完を持つと、list 形状だけ補完が効かず保護を素通りする。
    file_paths = extract_file_paths(tool_name, container)
    if file_paths is None:
        return _UNDECIDABLE_TARGET_MESSAGE
    for file_path in file_paths:
        reason = _block_reason_for_file(tool_name, container, file_path)
        if reason:
            return reason
    return None


def _block_reason(payload: dict[str, Any]) -> str | None:
    """書込み系入力が保護対象ならブロック理由を返す。

    Args:
        payload: フック stdin の dict。

    Returns:
        ブロック理由。対象外・保護対象なしなら None。

    Raises:
        例外は発生しません。
    """
    tool_name = extract_raw_tool_name(payload)
    # 既知の「書き込みではないツール」だけを skip する。ツール名を特定
    # できない payload を対象外へ倒すと保護が丸ごと無効になる。
    if is_known_tool_outside(tool_name, _WRITE_TOOL_NAMES):
        return None
    # 最初の理由で return する（先勝ち）のは意図した短絡。非 None の戻り値はすべて deny
    # なので、残りのコンテナを評価しても結論は変わらない。
    for container in iter_tool_input_containers(payload):
        reason = _block_reason_for_container(tool_name, container)
        if reason:
            return reason
    return None


_TRUNCATED_INPUT_MESSAGE = (
    f"BLOCKED: Hook input exceeded {MAX_STDIN_BYTES} bytes for pre:config-protection. "
    "Refusing to bypass protection on a truncated payload. "
    "Retry with a smaller edit."
)


_UNPARSEABLE_INPUT_MESSAGE = (
    "BLOCKED: Could not parse hook input for pre:config-protection. "
    "This hook only runs for write-tool calls (Edit/Write/MultiEdit), so a "
    "non-empty payload that fails to parse as JSON cannot be verified as "
    "safe. Refusing rather than allowing an unverifiable write through."
)


def main() -> int:
    """設定ファイルの編集を検知してブロックする。

    Returns:
        終了コード（0: 許可、2: ブロック）

    Raises:
        例外は発生しません。
    """
    try:
        raw, truncated = read_raw_stdin_with_truncation()
    except StdinUnavailableError as exc:
        return emit_block_output(stdin_unreadable_message("pre:config-protection", exc))
    if truncated:
        # 切り捨てられたペイロードで保護判定をすり抜けさせない（fail-closed）。
        return emit_block_output(_TRUNCATED_INPUT_MESSAGE)

    if not raw:
        # matcher が書込み系ツールに限定しているため、空入力は tty・stdin
        # 未接続など呼び出し自体が想定外の経路。ここは非ブロッキング。
        return 0

    payload = parse_json_object(raw)
    if payload is None:
        # matcher により書込み系ツール呼び出しであることは確定している。
        # 非空 stdin が JSON として読めない場合、保護対象ファイルかどうかを
        # 判定できないため fail-closed にする。
        return emit_block_output(_UNPARSEABLE_INPUT_MESSAGE)

    with scan_memo():
        reason = _block_reason(payload)
    if reason:
        return emit_block_output(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
