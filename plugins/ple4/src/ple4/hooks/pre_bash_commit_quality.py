#!/usr/bin/env python3
"""
コミット前にステージ済みファイルの品質を確認します。

pre:bash で `git commit` を検出したときだけ、lint や簡易静的チェックを実行します。
問題が見つかった場合はコミットを止め、それ以外は入力をそのまま通過させます。

commit 検出は `hook_common.tokenize`/`split_segments`（`block_no_verify` と
共有する区切り記号対応トークナイザ）を用い、`git` グローバルオプション
（値の有無・既知/未知を問わずすべて読み飛ばします）や連続空白・改行・
`&&`/`||`/`;`/`|`/`&`/`(`/`)` 区切りの複合コマンドを、区切り文字がトークンへ
密着していても正しくセグメント分割した上で考慮します。`git commit -a`/`--all`/
結合短形式（例: `-am`）を検出した場合は、未ステージ変更ファイルを
**作業ツリーから**読んでスキャン対象へ加えます（`git commit -a` は
作業ツリーの内容をコミットするため、INDEX ではなく作業ツリーを読む
必要があります）。ステージ済みファイルは従来どおり INDEX
（`git show :path`）から読みます。

シークレット検出は nosec・ファイルサイズに関わらず全体を走査します
（サイズによる打ち切りはありません）。バイナリ判定されたファイルは lint を
抑制しますが secret scan は行い、抽出した印字可能文字列へ同じパターンを
当てます（ADR-0013。詳細は `commit_quality_scanner` のモジュール docstring）。

データとして書かれた heredoc 本文は、判定へ渡す前に
`hook_common.strip_data_heredoc_bodies` で落とします（ADR-0017）。`evaluate()` の
ループで 1 回だけ正規化するため、下流の 3 消費者（commit 判定・compound risk 判定・
確定後の検査）は必ず同じ文字列を見ます。本文が実行されうる形は落としません。

非目標: ラッパースクリプトやシェルエイリアス経由の `git commit` 呼び出し検出、
非シェルインタプリタ（`python3 - <<EOF`）の heredoc 本文からの間接実行、
`git commit <pathspec>` で明示指定された未ステージファイルの取り込み
（`-a`/`--all` を伴わない場合は対象外）、およびシェル展開・変数分割経由
（`git $(echo commit)` / `git${IFS}commit` 等）で `git` と `commit` が
生文字列上で隣接しない形の検出（POSIX シェル展開の模倣は原理的に不能で
あり、フェイルセーフ・ヒューリスティックの追加は過剰ブロックを招くため
行いません）。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ple4.hooks.commit_quality_scanner import (
    find_file_issues,
    new_secret_scan_deadline,
    should_lint_file,
    should_scan_secrets,
)
from ple4.hooks.hook_common import (
    MAX_STDIN_BYTES,
    command_dialect_variants,
    is_git_executable_token,
    is_inplace_edit_flag,
    normalize_executable_name,
    parse_json_object,
    resolve_repo_root,
    split_segments,
    strip_data_heredoc_bodies,
    tokenize,
    tokenize_with_status,
)
from ple4.lib.core_utils import log
from ple4.lib.harness import iter_bash_commands
from ple4.lib.subprocess_utils import run_text

_CONVENTIONAL_COMMIT = re.compile(
    r"^(feat|fix|docs|style|refactor|test|chore|build|ci|perf|revert)(\(.+\))?:\s*.+"
)

# raw stdin が JSON として壊れているが、生文字列上に git commit らしき
# パターンが見える場合の deny 理由。JSON を経由せず raw_input に直接
# 正規表現を当てる（_is_git_commit_command の regex フォールバックと同じ
# パターン）。このフックの matcher は Bash 全体（commit と無関係な呼び出し
# も含む）なので、malformed JSON というだけで一律 deny すると commit と
# 無関係な Bash 呼び出しまで巻き込む。commit と判定できた場合のみ deny する。
_MALFORMED_COMMIT_MESSAGE = (
    "[Hook] BLOCKED: hook input could not be parsed as JSON, but the raw "
    "command text matches `git commit`. Refusing to allow an unverifiable "
    "commit through rather than silently skipping the quality scan."
)

# commit と判定済みの入力に対して、判定後の処理中に想定外の例外が
# 発生した場合の deny 理由。ここに到達する時点で「commit である」ことは
# 確定しているため、検査未完了のまま通すのではなく fail-closed にする。
_SCAN_FAILURE_MESSAGE = (
    "[Hook] BLOCKED: the commit quality scan failed unexpectedly for a "
    "confirmed `git commit` call. Refusing to allow an unverifiable commit "
    "through."
)

# get_staged_files() が git 自体の失敗・timeout で None を返した場合の
# deny 理由。「ステージ済みファイル 0 件」（正常系。--allow-empty 等）とは
# 区別する（0 件は exitCode=0 のまま）。
_STAGED_FILES_UNAVAILABLE_MESSAGE = (
    "[Hook] BLOCKED: could not determine staged files for a confirmed "
    "`git commit` call (git itself failed or timed out). Refusing to allow "
    "an unverifiable commit through."
)

# `git commit -a` で未ステージ変更を作業ツリーから読む必要があるのに、
# repo root（`git rev-parse --show-toplevel`）自体が git 失敗・timeout で
# 解決できなかった場合の deny 理由。get_staged_files() の None（R-01）と
# 対称の fail-closed 扱いにする — 実際にコミットされる未ステージ変更を
# 一切検査できないまま「問題なし」を返さないため。
_REPO_ROOT_UNAVAILABLE_MESSAGE = (
    "[Hook] BLOCKED: could not resolve the repo root to scan worktree changes "
    "for a confirmed `git commit -a` call (git itself failed or timed out). "
    "Refusing to allow an unverifiable commit through."
)

# `git commit -a` の作業ツリー列挙（`git diff HEAD --name-only`）が git 自体の
# 失敗・timeout で完了しなかった場合の deny 理由。HEAD が無い初回コミット
# （正常系。空リストで続行）とは区別する。実際にコミットされる未ステージ変更を
# 1 件も検査できない状態で「対象ファイルなし」を返さないため（ADR-0001）。
_WORKTREE_FILES_UNAVAILABLE_MESSAGE = (
    "[Hook] BLOCKED: could not enumerate worktree changes for a confirmed "
    "`git commit -a` call (git itself failed or timed out). Refusing to allow "
    "an unverifiable commit through."
)

# 1 回の Bash 呼び出しに複数の `git commit` が含まれる場合の deny 理由。
# PreToolUse フックは実行前の index/worktree しか観測できないため、
# 2 つ目以降の commit が何をコミットするかを原理的に検査できない。
_MULTIPLE_COMMITS_MESSAGE = (
    "[Hook] BLOCKED: this command contains more than one `git commit`. A "
    "PreToolUse hook can only inspect the index as it is before the command "
    "runs, so any commit after the first one cannot be scanned. Split them "
    "into separate tool calls."
)

# commit より前のセグメントが作業ツリーまたは index を変更する場合の deny 理由。
# 例: `printf ... > secret.py && git add secret.py && git commit -m x`。
# 検査時点の index は空でも、実行時には secret.py が commit される。
_MUTATION_BEFORE_COMMIT_MESSAGE = (
    "[Hook] BLOCKED: this command modifies the worktree or the index before "
    "the `git commit` in the same call, so the quality scan would run against "
    "state that is not what gets committed. Run the file changes and "
    "`git add` in a separate tool call, then commit."
)

# stdin が MAX_STDIN_BYTES を超えて切り捨てられた場合の deny 理由。切り捨て後の
# JSON は不完全になりうる（commit かどうかの判定自体が信用できない）ため、
# block_no_verify / config_protection と同じく fail-closed にする（A-05 相当対応）。
_TRUNCATED_INPUT_MESSAGE = (
    f"[Hook] BLOCKED: input exceeded {MAX_STDIN_BYTES} bytes for pre:bash-commit-quality. "
    "Refusing to evaluate a possibly-truncated payload for a git commit quality scan. "
    "Retry with a smaller command."
)


def _git_name_only(git_args: list[str]) -> list[str] | None:
    """`git ... --name-only` の出力を非空行リストにする。

    「git は成功したがファイル 0 件」（`None` ではなく `[]`）と「git 自体が
    失敗・timeout した」（`None`）を区別する。両者を同じ `[]` に潰すと、
    呼び出し側が「対象ファイルなし」と「検査不能」を見分けられず、後者を
    無言で見逃す（R-01 残余）。

    subprocess は `subprocess_utils.run_text` 経由で呼ぶ。`text=True` だけで
    encoding を指定しないと locale 依存のデコードになり、**ステージ済み
    ファイル名**に非 ASCII バイトが含まれる場合（日本語ファイル名は珍しくない）
    に `UnicodeDecodeError` が下の except を貫通して本 docstring の
    「例外は発生しません」が破れる。origin URL のような限られた入力と違い
    ファイル名は利用者が日常的に作るため、発火確率はこちらの方が高い。
    `run_text` は `encoding="utf-8", errors="replace"` を集約済み。

    発火条件は `core.quotePath=false`。git の既定（`true`）は非 ASCII パスを
    `"\350\250\255…"` の 8 進エスケープへ潰して出力するため ASCII に収まるが、
    日本語ファイル名を `git status` で読める形にするため `quotePath=false` を
    global 設定に入れる運用は珍しくない。その環境で `LC_ALL=C` だと git が
    生の UTF-8 バイトを返し、旧実装は `UnicodeDecodeError` を送出していた
    （実測で再現・修正後の解消を確認済み）。

    `errors="replace"` はデコード不能なバイトを置換文字へ潰すため、その
    ファイル名は `git show :path` で引けず内容を読めない。それでも例外で
    フック全体を落とすより良い —— 落とせば commit は無検査のまま通る
    （fail-open）のに対し、置換された 1 件はスキャン対象から外れるだけで、
    残りのステージ済みファイルは従来どおり検査されるため。

    except タプルからは `CalledProcessError` を外す。`check=False` で呼ぶ限り
    送出されない死んだ分岐だった。代わりに `OSError` を捕まえる —— 従来の
    `FileNotFoundError` は `OSError` の部分集合に過ぎず、`PermissionError` や
    実行形式不正（`OSError`）で git を起動できない場合を取りこぼしていた。
    `TimeoutExpired` は `OSError` の部分集合ではないため個別に残す。

    Args:
        git_args: subprocess に渡す git コマンド列。

    Returns:
        ファイルパスのリスト。git 自体の失敗・timeout 時は None。

    Raises:
        例外は発生しません。
    """
    try:
        result = run_text(git_args, timeout=5)
        if result.returncode != 0:
            return None
        return [f for f in result.stdout.strip().split("\n") if f]
    except (OSError, subprocess.TimeoutExpired):
        return None


def get_staged_files() -> list[str] | None:
    """ステージング済みファイルの一覧を取得します。

    Returns:
        ステージングされたファイルパスのリストを返します。git 自体の
        失敗・timeout で取得できない場合は None（「0 件」とは区別する）。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    return _git_name_only(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"])


def _head_exists() -> bool | None:
    """HEAD が解決可能か（＝初回コミットではないか）を判定します。

    `_git_name_only` と同じ理由で `subprocess_utils.run_text` 経由にし、
    except タプルも揃える（理由は `_git_name_only` の docstring）。本関数が
    読むのは returncode だけだが、`text=True` は stderr もデコードするため、
    非 UTF-8 ロケールで git が翻訳済みエラーメッセージを出すと同じ
    `UnicodeDecodeError` 貫通が起きる。同一ファイル内で片方だけ直すと、
    次に触る人がどちらが正なのか判断できなくなるため揃える。

    Returns:
        HEAD があれば True、初回コミット等で無ければ False。git 自体が
        失敗・timeout して判定できなければ None。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    try:
        result = run_text(["git", "rev-parse", "--verify", "--quiet", "HEAD"], timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return True
    # --quiet 指定時、HEAD が単に存在しない場合の終了コードは 1。
    # それ以外（リポジトリ外・git 内部エラー等）は「判定不能」として扱う。
    return False if result.returncode == 1 else None


def get_unstaged_modified_files() -> list[str] | None:
    """`git commit -a` 相当で追加取り込む作業ツリーの変更ファイル一覧を取得します。

    `git diff HEAD --name-only --diff-filter=ACMR` の結果を返します。

    列挙に失敗したときは「HEAD が無い（初回コミット）」と「git 自体が失敗した」
    を区別します。前者は正常系なので空リスト、後者は None を返して呼び出し元で
    fail-closed にします。両方を空リストへ潰すと、実際にコミットされる未ステージ
    変更を 1 件も検査できていない状態を「対象ファイルなし」と report してしまい、
    ADR-0001 の「検査対象確定後の失敗は fail-closed」に反します。

    Returns:
        変更されている作業ツリーファイルパスのリスト。初回コミットで HEAD が
        無い場合は空リスト。git 自体の失敗・timeout で取得できない場合は None。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    result = _git_name_only(["git", "diff", "HEAD", "--name-only", "--diff-filter=ACMR"])
    if result is not None:
        return result
    return [] if _head_exists() is False else None


def _find_git_commit_args_in_segment(segment: list[str]) -> list[str] | None:
    """1 セグメント（シェル区切りを含まないトークン列）内の `git commit` 呼び出しを探します。

    `git` トークン（`hook_common.is_git_executable_token` で絶対パス・`.exe`・
    大小を正規化して判定。`block_no_verify` と共有する実装で、2 箇所へ別々に
    実装すると正規化の齟齬が再発するため一元化しています）の後は、既知/未知を
    問わずグローバルオプション・その値トークンを区別せず単純に読み飛ばし、
    `commit` サブコマンドに到達するかを判定します（allowlist に無い
    `--exec-path <path>` / `--super-prefix <path>` 等の値トークンで走査が
    打ち切られ検出漏れになる問題を避けるため、過剰検出側に倒しています）。
    セグメントは呼び出し元（`hook_common.split_segments`）が既に
    `&&`/`;`/`|`/`&`/`(`/`)` で分割済みのため、本関数はセグメント内に区切り
    トークンが存在しない前提で走査します（A-01 対応: `status;echo` の
    ようにシェル区切りがトークンに密着した非 commit コマンドを、区切り前に
    セグメントを分けることで取り逃さないようにします）。

    Args:
        segment: `hook_common.tokenize` + `split_segments` で得た 1 セグメント分のトークン列です。

    Returns:
        commit 呼び出しの引数トークンリスト。見つからなければ None を返します。

    Raises:
        例外は発生しません。
    """
    for i, token in enumerate(segment):
        if not is_git_executable_token(token):
            continue
        rest = segment[i + 1 :]
        for offset, tok in enumerate(rest):
            if tok == "commit":
                return rest[offset + 1 :]
    return None


def _detect_git_commit(command: str) -> tuple[str, list[str]] | None:
    """2 つのシェル方言の読み方で `git commit` 起動を探す。

    `block_no_verify` / `bash_config_protection` と同じ
    `command_dialect_variants` を使う。POSIX 読みだけを見ていた頃は、
    Windows の絶対パス起動（``C:\\Git\\bin\\git.exe commit -m x``）が
    `shlex(posix=True)` で ``C:Gitbingit.exe`` に潰れ、commit と認識できず
    品質ゲートが丸ごと素通りしていた（`block_no_verify` では検出される
    のに本フックだけ通る非対称。ADR-0020）。

    Args:
        command: 検査対象のコマンド文字列（heredoc 本文は除去済み）。

    Returns:
        commit を検出した読み方と、その commit 引数のタプル。
        どちらの読み方でも commit でなければ None。

    Raises:
        例外は発生しません（`_is_git_commit_command` の契約に従う）。
    """
    for variant in command_dialect_variants(command):
        is_commit, commit_args = _is_git_commit_command(variant)
        if is_commit:
            return variant, commit_args
    return None


def _is_git_commit_command(command: str) -> tuple[bool, list[str]]:
    """コマンド文字列が `git commit` 呼び出しかを判定し、commit 引数トークンを返します。

    `hook_common.tokenize`（区切り記号を独立トークン化する `shlex`）でトークン化し、
    `hook_common.split_segments` で `&&`/`||`/`;`/`|`/`&`/`(`/`)` ごとのセグメントに
    分割してから、セグメントごとに `git` → グローバルオプション → `commit` の並びを
    探します。セグメント分割により、``git status;echo commit`` のように区切り文字が
    トークンへ密着した非 commit コマンドを誤って commit と判定しません
    （`block_no_verify.has_bypass_flag` と同じトークナイザを共有する A-01 対応）。

    トークン化がクォート不整合（heredoc 等）で空白分割へフォールバックした場合
    **だけ**、`re.search(r"\\bgit\\s+commit\\b", command)` で最終判定します。
    解析できなかった入力に対して過剰検出側へ倒すフェイルセーフです。

    `shlex` が最後まで解析できた場合は、その結果を信頼して生文字列の正規表現を
    当てません（F-08）。当てていた頃は `copilot -p 'Explain why a git commit
    command may fail'` のような引用文まで commit と判定し、無関係な Bash 呼び出しが
    index の状態次第でブロックされた。同じ入力を `block_no_verify` は無視して
    おり、2 つのフックが「commit とは何か」で食い違っていた。この非対称を
    どの ADR も正当化していない。ADR-0002 の「誤検出 > 誤通過」は解析できない
    構文についての規定であり、解析できた構文にまで適用する根拠にはならない。

    非目標: シェル展開・変数分割経由（`git $(echo commit)` / `git${IFS}commit`
    等）で `git` と `commit` が生文字列上で隣接しない形の検出。POSIX シェル
    展開を文字列解析だけで模倣するのは原理的に不能であり、無理に検出しようと
    するとヒューリスティックが過剰ブロックを招くため対応しません。

    Args:
        command: 検査対象のコマンド文字列です。

    Returns:
        (is_commit, commit_args) のタプル。is_commit が False の場合、
        commit_args は空リストです。

    Raises:
        例外は発生しません。
    """
    tokens, parsed_cleanly = tokenize_with_status(command)
    for segment in split_segments(tokens):
        commit_args = _find_git_commit_args_in_segment(segment)
        if commit_args is not None:
            return True, commit_args

    if not parsed_cleanly and re.search(r"\bgit\s+commit\b", command):
        return True, []
    return False, []


# commit より前に実行されると検査結果を無効化する操作。ADR-0002 の
# 「解析できないケースは誤検出を誤通過より選ぶ」に従い、判定に迷う構文は
# 「変更あり」側へ倒す。対象は ADR-0002 が「引数位置に書き込み先が明示される
# ＝解析できる範囲」として既に対応済みと宣言している集合に、index 操作を
# 加えたもの。
_INDEX_MUTATING_GIT_SUBCOMMANDS = frozenset(
    {
        "add",
        "am",
        "apply",
        "checkout",
        "cherry-pick",
        "merge",
        "mv",
        "pull",
        "rebase",
        "reset",
        "restore",
        "revert",
        "rm",
        "stash",
        "switch",
    }
)
_WORKTREE_MUTATING_EXECUTABLES = frozenset(
    {
        "chmod",
        "chown",
        "cp",
        "dd",
        "install",
        "ln",
        "mkdir",
        "mv",
        "patch",
        "rm",
        "shred",
        "tee",
        "touch",
        "truncate",
        "unlink",
    }
)
# `-i` を伴うときだけ書き込みになるコマンド。`-i` 無しは標準出力へ流すだけ。
_INPLACE_EDIT_EXECUTABLES = frozenset({"sed", "perl", "ed"})
_MUTATING_REDIRECT_OPERATORS = frozenset({">", ">>", "&>", ">|", "1>", "2>", "1>>", "2>>", ">&"})


def _segment_mutates_worktree_or_index(segment: list[str]) -> bool:
    """1 セグメントが作業ツリーまたは git index を変更しうるかを判定します。

    ADR-0002 に従い誤検出側へ倒します。ここでの誤検出のコストは「commit を
    別の tool call へ分けてもらう」ことであり、誤通過のコスト（未検査の内容が
    commit される）より小さいためです。

    Args:
        segment: `hook_common.split_segments` で得た 1 セグメント分のトークン列です。

    Returns:
        変更しうるなら True を返します。

    Raises:
        例外は発生しません。
    """
    if any(token in _MUTATING_REDIRECT_OPERATORS for token in segment):
        return True

    for i, token in enumerate(segment):
        if is_git_executable_token(token):
            for sub in segment[i + 1 :]:
                if sub.startswith("-"):
                    continue
                return sub in _INDEX_MUTATING_GIT_SUBCOMMANDS
            return False

        # 実行名の正規化は `is_git_executable_token` と同じ共有 helper へ通す。
        # ここだけ素の basename（大小区別・`.exe` 残し）で照合していたため、
        # APFS で実 `cp` を起動する ``CP evil.py app.py && git commit`` や
        # Windows の ``cp.exe`` / ``TEE`` が語彙から外れ、mutation ガードが
        # 不発になっていた（H-6）。scan は変更前の作業ツリーを読むので、
        # 取りこぼしはそのまま「未検査の内容が commit される」ことを意味する。
        name = normalize_executable_name(token)
        if name in _WORKTREE_MUTATING_EXECUTABLES:
            return True
        if name in _INPLACE_EDIT_EXECUTABLES:
            return any(is_inplace_edit_flag(arg) for arg in segment[i + 1 :])

    return False


def _compound_commit_risk(command: str) -> str | None:
    """1 回の Bash 呼び出しの中で commit 内容を検査できなくする構造を検出します。

    PreToolUse フックが観測できるのは「コマンド実行前」の index/worktree だけ
    です。したがって次の 2 つは原理的に検査不能であり、ADR-0001（検査対象確定後
    の検査不能は fail-closed）と ADR-0002（誤検出 > 誤通過）に従って deny します。

    1. 複数の `git commit` — 2 つ目以降がコミットする内容は実行前状態に現れない。
    2. commit より前のセグメントによる作業ツリー / index の変更 — 例えば
       ``printf 'password=...' > secret.py && git add secret.py && git commit -m x``
       は検査時点の index が空なので素通りする。

    代償として ``git add . && git commit -m x`` という一般的なイディオムも塞ぎます。
    それでも塞ぐのは、通した場合に「品質フックが commit 内容を検査している」という
    契約自体が成立せず、フックの存在が誤った安心感になるためです。

    Args:
        command: 検査対象のコマンド文字列です。

    Returns:
        deny すべき場合はその理由文字列、問題なければ None を返します。

    Raises:
        例外は発生しません。
    """
    segments = split_segments(tokenize(command))
    commit_indices = [i for i, segment in enumerate(segments) if _find_git_commit_args_in_segment(segment) is not None]
    if not commit_indices:
        return None
    if len(commit_indices) > 1:
        return _MULTIPLE_COMMITS_MESSAGE
    if any(_segment_mutates_worktree_or_index(segment) for segment in segments[: commit_indices[0]]):
        return _MUTATION_BEFORE_COMMIT_MESSAGE
    return None


def _is_commit_all_flag(commit_args: list[str]) -> bool:
    """commit 引数トークンに `-a`/`--all`（結合短形式含む）が含まれるかを判定します。

    `-am` のような結合短形式は、`-` 始まり・`--` ではない・`=` を含まない
    短形式トークンを文字単位に展開して `a` を探す最小実装です。

    Args:
        commit_args: `git commit` 呼び出しの引数トークン列です。

    Returns:
        `-a` 相当のフラグが含まれるなら True を返します。

    Raises:
        例外は発生しません。
    """
    for tok in commit_args:
        if tok == "--all":
            return True
        if tok.startswith("-") and not tok.startswith("--") and "=" not in tok and "a" in tok[1:]:
            return True
    return False


def _message_issue(kind: str, message: str, suggestion: str) -> dict:
    """コミットメッセージの問題 1 件を表す辞書を作る。

    Args:
        kind: 問題種別（format / length / capitalization / punctuation）。
        message: 問題の説明。
        suggestion: 修正提案。

    Returns:
        type / message / suggestion を持つ辞書。

    Raises:
        例外は発生しません。
    """
    return {"type": kind, "message": message, "suggestion": suggestion}


def _commit_message_issues(message: str) -> list[dict]:
    """コミットメッセージ本文の形式問題を列挙する。

    Args:
        message: 抽出済みのコミットメッセージ本文。

    Returns:
        問題辞書のリスト。問題がなければ空リスト。

    Raises:
        例外は発生しません。
    """
    issues: list[dict] = []
    if not _CONVENTIONAL_COMMIT.match(message):
        issues.append(
            _message_issue(
                "format",
                "Commit message does not follow conventional commit format",
                'Use format: type(scope): description (e.g., "feat(auth): add login flow")',
            )
        )
    if len(message) > 72:
        issues.append(
            _message_issue(
                "length",
                f"Commit message too long ({len(message)} chars, max 72)",
                "Keep the first line under 72 characters",
            )
        )
    if _CONVENTIONAL_COMMIT.match(message):
        after_colon = message.split(":", 1)[1] if ":" in message else ""
        if after_colon and re.match(r"^[A-Z]", after_colon.strip()):
            issues.append(
                _message_issue(
                    "capitalization",
                    "Subject should start with lowercase after type",
                    "Use lowercase for the first letter of the subject",
                )
            )
    if message.endswith("."):
        issues.append(
            _message_issue(
                "punctuation",
                "Commit message should not end with a period",
                "Remove the trailing period",
            )
        )
    return issues


def validate_commit_message(command: str) -> dict | None:
    """コミットメッセージの形式を検証します。

    Args:
        command: `git commit` コマンド文字列です。

    Returns:
        メッセージと問題一覧を含む辞書、またはメッセージがない場合は None を返します。

    Raises:
        例外は発生しません。
    """
    message_match = re.search(r"(?:-m|--message)[=\s]+[\"']?([^\"']+)[\"']?", command)
    if not message_match:
        return None
    message = message_match.group(1)
    return {"message": message, "issues": _commit_message_issues(message)}


def _scan_targets(files: list[str]) -> list[str]:
    """lint または secret スキャン対象のファイルだけを残す。

    Args:
        files: 候補ファイルパス。

    Returns:
        スキャン対象のファイルパス。

    Raises:
        例外は発生しません。
    """
    return [f for f in files if should_lint_file(f) or should_scan_secrets(f)]


def _partition_commit_all_files(
    staged_files: list[str], commit_args: list[str]
) -> tuple[list[str], list[str]] | None:
    """`-a`/`--all` 指定時に、INDEX から読む対象と作業ツリーから読む対象に分割します。

    `git commit -a` は作業ツリーの現在の内容をコミットするため、未ステージ
    変更ファイル（`get_unstaged_modified_files` 由来）は作業ツリー優先で
    読みます。ステージ済みかつ未ステージ変更もある（ステージ後にさらに
    作業ツリーで変更された）ファイルも、`-a` の場合は作業ツリー優先とします
    （`git commit -a` の実際の挙動と一致させるためです）。

    Args:
        staged_files: `get_staged_files()` によるステージ済みファイル一覧です。
        commit_args: `git commit` 呼び出しの引数トークン列です。

    Returns:
        (index_files, worktree_files) のタプルです。`-a`/`--all` が無ければ
        `worktree_files` は空リストです。`-a` 指定時に作業ツリーの列挙自体が
        失敗した場合は None を返します（呼び出し元で fail-closed にする）。

    Raises:
        例外は発生しません。
    """
    if not _is_commit_all_flag(commit_args):
        return list(staged_files), []

    unstaged = get_unstaged_modified_files()
    if unstaged is None:
        return None

    worktree_files = sorted(set(unstaged))
    index_files = sorted(set(staged_files) - set(worktree_files))
    return index_files, worktree_files


def _count_file_issues(
    files_to_check: list[str], repo_root: Path | None = None, *, deadline: float
) -> tuple[int, int, int, int]:
    """チェック対象ファイルの問題数を集計します。

    各ファイルに対して find_file_issues を呼び出し、severity 別に問題数を返します。
    ログ出力も行います。

    Args:
        files_to_check: チェック対象のファイルパスリストです。
        repo_root: 指定すると各ファイルを作業ツリーから読みます
            （`git commit -a` の未ステージ変更用）。None なら INDEX から
            読みます。
        deadline: secret scan の実時間予算を表す `_monotonic()` 基準の時刻です。
            本関数はフック 1 回につき INDEX 用・作業ツリー用の 2 回呼ばれるため、
            ここで deadline を作ると予算が 2 本になります。必ず
            `_evaluate_confirmed_commit` が起動ごとに 1 度だけ作った値を渡します。

    Returns:
        (total_issues, error_count, warning_count, info_count) のタプルを返します。

    Raises:
        例外は発生しません。
    """
    total_issues = 0
    error_count = 0
    warning_count = 0
    info_count = 0
    severity_label = {"error": "ERROR", "warning": "WARNING", "info": "INFO"}

    for file_path in files_to_check:
        file_issues = find_file_issues(file_path, repo_root=repo_root, deadline=deadline)
        if not file_issues:
            continue
        log(f"\n[FILE] {file_path}")
        for issue in file_issues:
            # issue は find_file_issues の内部契約（dict にキー欠落なし）に
            # 依存せず .get() で防御する。未知 severity は「検査したが
            # 分類できない」ことを示すため、見逃す（黙って info/合計のみ加算）
            # のではなく安全側の error 扱いにする。
            severity = issue.get("severity")
            label = severity_label.get(severity, "INFO")
            log(f"  {label} Line {issue.get('line', 0)}: {issue.get('message', '')}")
            total_issues += 1
            if severity == "error":
                error_count += 1
            elif severity == "warning":
                warning_count += 1
            elif severity == "info":
                info_count += 1
            else:
                error_count += 1

    return total_issues, error_count, warning_count, info_count


def _apply_commit_message_issues(
    command: str,
    total_issues: int,
    warning_count: int,
) -> tuple[int, int]:
    """コミットメッセージの問題を検証してカウントに加算します。

    validate_commit_message を呼び出し、問題があればログ出力して
    更新後の (total_issues, warning_count) を返します。

    Args:
        command: `git commit` コマンド文字列です。
        total_issues: 現在の問題総数です。
        warning_count: 現在の警告数です。

    Returns:
        (total_issues, warning_count) の更新後タプルを返します。

    Raises:
        例外は発生しません。
    """
    message_validation = validate_commit_message(command)
    if not (message_validation and message_validation["issues"]):
        return total_issues, warning_count

    log("\nCommit Message Issues:")
    for issue in message_validation["issues"]:
        log(f"  WARNING {issue['message']}")
        if issue.get("suggestion"):
            log(f"     TIP {issue['suggestion']}")
        total_issues += 1
        warning_count += 1

    return total_issues, warning_count


def _finalize_result(
    total_issues: int,
    error_count: int,
    warning_count: int,
    info_count: int,
    raw_input: str,
) -> dict:
    """問題集計結果をログに記録し、終了コードを含む結果辞書を返します。

    error_count > 0 の場合は exitCode=2（コミットブロック）＋ reason（
    emit_block_output に渡すブロック理由）、それ以外は exitCode=0 を返します。

    Args:
        total_issues: 検出された問題の総数です。
        error_count: エラー severity の問題数です。
        warning_count: 警告 severity の問題数です。
        info_count: info severity の問題数です。
        raw_input: そのまま output に返す生の入力文字列です。

    Returns:
        output と exitCode（exitCode=2 の場合は reason も）を含む辞書を返します。

    Raises:
        例外は発生しません。
    """
    if total_issues > 0:
        log(
            f"\nSummary: {total_issues} issue(s) found "
            f"({error_count} error(s), {warning_count} warning(s), {info_count} info)"
        )
        if error_count > 0:
            log("\n[Hook] ERROR: Commit blocked due to critical issues. Fix them before committing.")
            reason = (
                f"[Hook] BLOCKED: {error_count} error(s) found in staged files. "
                "Fix them before committing."
            )
            return {"output": raw_input, "exitCode": 2, "reason": reason}
        log("\n[Hook] WARNING: Warnings found. Consider fixing them, but commit is allowed.")
    else:
        log("\n[Hook] PASS: All checks passed!")

    return {"output": raw_input, "exitCode": 0}


def _collect_worktree_issues(
    worktree_targets: list[str],
    total_issues: int,
    error_count: int,
    warning_count: int,
    info_count: int,
    *,
    deadline: float,
) -> tuple[int, int, int, int] | None:
    """`git commit -a` の作業ツリー対象ファイルの問題数を集計に加算します。

    リポジトリルートを解決し、作業ツリー（`repo_root` 経由）から各ファイルを
    読んで `_count_file_issues` で集計し、既存のカウントに加算した結果を
    返します。作業ツリー対象が無ければ非ブロッキングで入力のカウントを
    そのまま返します。

    作業ツリー対象があるのに repo root が解決できない場合（git 自体の
    失敗・timeout）は None を返し、呼び出し側で fail-closed として扱わせ
    ます。`-a`/`--all` で実際にコミットされる未ステージ変更ファイルの
    内容を一切検査できないまま「問題なし」を返すのは、`get_staged_files`
    が git 失敗時に None を返すのと同じ理由で非ブロッキングにできません
    （R-01 で修正した staged 側の fail-open と対称にするため）。

    Args:
        worktree_targets: 作業ツリーから読むチェック対象ファイルパスです。
        total_issues: 現在の問題総数です。
        error_count: 現在のエラー数です。
        warning_count: 現在の警告数です。
        info_count: 現在の info 数です。
        deadline: secret scan の実時間予算を表す `_monotonic()` 基準の時刻です。
            INDEX 側の集計と同じ 1 本の予算を引き継ぐため、呼び出し元から
            そのまま受け取って `_count_file_issues` へ渡します。

    Returns:
        (total_issues, error_count, warning_count, info_count) の更新後
        タプル。repo root が必要なのに解決できなければ None。

    Raises:
        例外は発生しません。
    """
    if not worktree_targets:
        return total_issues, error_count, warning_count, info_count

    repo_root = resolve_repo_root()
    if repo_root is None:
        return None

    wt_total, wt_error, wt_warning, wt_info = _count_file_issues(
        worktree_targets, repo_root=repo_root, deadline=deadline
    )
    return (
        total_issues + wt_total,
        error_count + wt_error,
        warning_count + wt_warning,
        info_count + wt_info,
    )


def _evaluate_confirmed_commit(raw_input: str, command: str, commit_args: list[str]) -> dict:
    """`git commit` と判定済みの入力を検査し、結果を返します。

    ここに到達した時点で「これは commit である」ことは確定しているため、
    処理中に想定外の例外が起きても exitCode=0（非ブロッキング）へは
    倒さない。検査未完了のまま通すのは「検査したが問題なし」と区別が
    付かなくなり、品質・secret 検査を無言で迂回できてしまうため。

    Args:
        raw_input: フックに渡された生の入力文字列です。
        command: `git commit` を含む bash コマンド文字列です。
        commit_args: commit 呼び出しの引数トークン列です。

    Returns:
        output と exitCode を含む辞書を返します。

    Raises:
        例外は発生しません。
    """
    try:
        # ステージングされたファイルを取得（-a/--all の場合は未ステージの変更も加える。
        # -a の未ステージ分は作業ツリーの内容がコミットされるため作業ツリーから読む）
        staged_files = get_staged_files()
        if staged_files is None:
            log("[Hook] ERROR: could not determine staged files (git itself failed or timed out).")
            return {"output": raw_input, "exitCode": 2, "reason": _STAGED_FILES_UNAVAILABLE_MESSAGE}

        partitioned = _partition_commit_all_files(staged_files, commit_args)
        if partitioned is None:
            log("[Hook] ERROR: could not enumerate worktree changes for `git commit -a`.")
            return {"output": raw_input, "exitCode": 2, "reason": _WORKTREE_FILES_UNAVAILABLE_MESSAGE}

        index_files, worktree_files = partitioned
        all_files = sorted(set(index_files) | set(worktree_files))
        if not all_files:
            log('[Hook] No staged files found. Use "git add" to stage files first.')
            return {"output": raw_input, "exitCode": 0}

        log(f"[Hook] Checking {len(all_files)} staged file(s)...")

        index_targets = _scan_targets(index_files)
        worktree_targets = _scan_targets(worktree_files)

        # secret scan の実時間予算はフック 1 回の起動につき 1 本。ここで 1 度だけ
        # 作り、INDEX 側・作業ツリー側の双方の集計へ同じ値を渡す。ファイル単位や
        # _count_file_issues 単位で作ると、ファイル数（あるいは 2）倍の実時間を
        # 許してしまい hook timeout（30秒）に達しうる。
        secret_scan_deadline = new_secret_scan_deadline()

        total_issues, error_count, warning_count, info_count = _count_file_issues(
            index_targets, deadline=secret_scan_deadline
        )
        worktree_result = _collect_worktree_issues(
            worktree_targets,
            total_issues,
            error_count,
            warning_count,
            info_count,
            deadline=secret_scan_deadline,
        )
        if worktree_result is None:
            log("[Hook] ERROR: could not resolve repo root for `git commit -a` worktree scan.")
            return {"output": raw_input, "exitCode": 2, "reason": _REPO_ROOT_UNAVAILABLE_MESSAGE}
        total_issues, error_count, warning_count, info_count = worktree_result

        total_issues, warning_count = _apply_commit_message_issues(command, total_issues, warning_count)

        return _finalize_result(total_issues, error_count, warning_count, info_count, raw_input)

    except Exception as err:
        log(f"[Hook] Error: {err}")
        log("[Hook] BLOCKED: quality scan failed for a confirmed git commit; refusing an unverifiable commit.")
        return {"output": raw_input, "exitCode": 2, "reason": _SCAN_FAILURE_MESSAGE}


def evaluate(raw_input: str) -> dict:
    """入力を評価し、出力内容と終了コードを返します。

    JSON が壊れている・commit と無関係な Bash 呼び出しは非ブロッキング
    （exitCode=0）。JSON が壊れていても raw 文字列上に `git commit` が
    見える場合は deny する（malformed JSON を理由に品質・secret 検査を
    迂回させないため）。`git commit` と確定した入力は
    `_evaluate_confirmed_commit` に委譲し、以降は fail-closed で扱う。

    Args:
        raw_input: フックに渡された生の入力文字列です。

    Returns:
        output と exitCode を含む辞書を返します。

    Raises:
        例外は発生しません。
    """
    try:
        input_data = parse_json_object(raw_input)
        if input_data is None:
            # 空/空白入力、または JSON として壊れている。matcher が Bash 全体
            # （commit と無関係な呼び出しも含む）のため、malformed というだけで
            # 一律 deny すると無関係な Bash 呼び出しまで巻き込む。raw 文字列上に
            # `git commit` が見える場合のみ deny する。
            if raw_input.strip() and re.search(r"\bgit\s+commit\b", raw_input):
                log("[Hook] ERROR: malformed JSON input, but raw command text matches `git commit`.")
                return {"output": raw_input, "exitCode": 2, "reason": _MALFORMED_COMMIT_MESSAGE}
            if raw_input.strip():
                log("[Hook] WARNING: could not parse hook input as JSON; not a git commit, passing through.")
            return {"output": raw_input, "exitCode": 0}

        if not input_data:
            return {"output": raw_input, "exitCode": 0}

        # コンテナキーは 1 つも取りこぼさず走査し、commit を**全て**集める
        # （iter_bash_commands）。先に判定できたコンテナの結論を返す形にすると、
        # 先頭が「通る commit」のときに後続キーの commit が一切検査されない
        # （実測: 単独なら exit 2 の payload が、先頭へ通る commit を足すだけで
        # exit 0 になった）。走査層だけ全キー化しても評価層が先勝ちなら穴は残る。
        commits = []
        for raw_command in iter_bash_commands(input_data):
            # heredoc のデータ本文はコマンドの語彙に入らないため、判定へ渡す前に
            # 落とす。ここで 1 回だけ正規化することで、下流の 3 消費者
            # （_is_git_commit_command / _compound_commit_risk /
            # _evaluate_confirmed_commit）が必ず同じ文字列を見る。
            command = strip_data_heredoc_bodies(raw_command)
            # git commit コマンドの場合のみ実行（トークン化して堅牢に判定）
            try:
                detected = _detect_git_commit(command)
            except Exception as err:  # noqa: BLE001 - 1 コンテナの失敗で他を落とさない
                log(f"[Hook] Error: {err}")
                continue
            if detected is not None:
                commits.append(detected)

        if not commits:
            return {"output": raw_input, "exitCode": 0}

        # 複数コンテナに commit が散っている場合、どれが実際に実行されるかも
        # 順序も実行前には確定できない。1 コマンド内に複数 commit がある場合
        # （`_compound_commit_risk`）と同じ理由で検査不能として deny する。
        if len(commits) > 1:
            log("[Hook] ERROR: commit content cannot be inspected before execution.")
            return {"output": raw_input, "exitCode": 2, "reason": _MULTIPLE_COMMITS_MESSAGE}

        command, commit_args = commits[0]
        compound_risk = _compound_commit_risk(command)
        if compound_risk is not None:
            log("[Hook] ERROR: commit content cannot be inspected before execution.")
            return {"output": raw_input, "exitCode": 2, "reason": compound_risk}

        return _evaluate_confirmed_commit(raw_input, command, commit_args)

    except Exception as err:
        log(f"[Hook] Error: {err}")
        # commit と確定する前の例外（JSON 抽出・コマンド判定段階）は
        # 非ブロッキング。判定済みの例外は _evaluate_confirmed_commit 側で
        # fail-closed にする。

    return {"output": raw_input, "exitCode": 0}


def run(raw_input: str) -> dict:
    """フックを実行し、output と exitCode を含む結果を返します。

    Args:
        raw_input: フックに渡された生の入力文字列です。

    Returns:
        output と exitCode を含む辞書を返します。

    Raises:
        例外は発生しません。
    """
    return evaluate(raw_input)


def main() -> int:
    """スクリプト実行時に入力を読み取り、品質チェックを行います。

    ブロック時（exitCode == 2）は emit_block_output で host 非依存の合併出力
    （stderr の理由 + stdout の permissionDecision: deny JSON、exit 2）に変換する。

    fail-open/fail-closed の境界は「commit と確定したか」で分けます
    （A-05 相当対応）:

    - `read_raw_stdin_with_truncation` が `StdinUnavailableError` を送出した
      場合（payload はあるはずなのに読めなかった）は fail-closed。commit か
      どうかを判定する材料そのものが得られていないため、他の 3 保護 hook と
      同じ deny に倒す（ADR-0019）。読む対象が無い場合（tty 起動・stdin 未
      接続・即 EOF）は例外にならず空文字列として届き、従来どおり素通りする。
      1 MiB 超の truncation は commit かどうか判定不能なため fail-closed
      （block_no_verify / config_protection と同じ 4 段構成に揃える）。
    - `evaluate()` 自体は例外を投げない契約だが、防御的に例外時は
      commit 確定前として fail-open のまま扱う。
    - `evaluate()` が exitCode=2（commit と確定しブロック判定済み）を返した
      後、`emit_block_output` 自体が失敗した場合は fail-closed（exit 2）。
      commit であることは既に確定しているため、出力層の失敗を理由に
      検査未完了のコミットを通さない。

    Returns:
        コミットを許可する場合は 0、ブロックする場合は 2 を返します。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    from ple4.hooks.hook_common import (
        StdinUnavailableError,
        emit_block_output,
        read_raw_stdin_with_truncation,
        stdin_unreadable_message,
    )

    try:
        raw, truncated = read_raw_stdin_with_truncation()
    except StdinUnavailableError as exc:
        try:
            return emit_block_output(stdin_unreadable_message("pre:bash-commit-quality", exc))
        except Exception as err:
            log(f"[Hook] Error: {err}")
            return 2

    if truncated:
        try:
            return emit_block_output(_TRUNCATED_INPUT_MESSAGE)
        except Exception as err:
            log(f"[Hook] Error: {err}")
            return 2

    try:
        result = evaluate(raw)
    except Exception as err:
        # evaluate() は例外を投げない契約だが、防御的に commit 確定前の
        # 例外と同様に fail-open で扱う。
        log(f"[Hook] Error: {err}")
        return 0

    if result["exitCode"] == 2:
        try:
            return emit_block_output(result["reason"])
        except Exception as err:
            # commit と確定した後の出力層失敗は fail-closed。
            log(f"[Hook] Error: {err}")
            return 2
    return result["exitCode"]


if __name__ == "__main__":
    import sys

    sys.exit(main())
