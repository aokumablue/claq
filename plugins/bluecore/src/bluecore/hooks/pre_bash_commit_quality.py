#!/usr/bin/env python3
"""
コミット前にステージ済みファイルの品質を確認します。

pre:bash で `git commit` を検出したときだけ、lint や簡易静的チェックを実行します。
問題が見つかった場合はコミットを止め、それ以外は入力をそのまま通過させます。

commit 検出は `shlex` によるトークン化を用い、`git` グローバルオプション
（値の有無・既知/未知を問わずすべて読み飛ばします）や連続空白・改行・
`&&`/`;`/`|` 区切りの複合コマンドを考慮します。`git commit -a`/`--all`/
結合短形式（例: `-am`）を検出した場合は、未ステージ変更ファイルを
**作業ツリーから**読んでスキャン対象へ加えます（`git commit -a` は
作業ツリーの内容をコミットするため、INDEX ではなく作業ツリーを読む
必要があります）。ステージ済みファイルは従来どおり INDEX
（`git show :path`）から読みます。

シークレット検出はバイナリ判定（lint 抑制のみに使用）や nosec、
ファイルサイズに関わらず可能な限り実行します（大容量ファイルは
先頭 `_SECRET_SCAN_MAX_BYTES` バイトに切り詰めて継続します）。

非目標: ラッパースクリプトやシェルエイリアス経由の `git commit` 呼び出し検出、
`git commit <pathspec>` で明示指定された未ステージファイルの取り込み
（`-a`/`--all` を伴わない場合は対象外）、およびシェル展開・変数分割経由
（`git $(echo commit)` / `git${IFS}commit` 等）で `git` と `commit` が
生文字列上で隣接しない形の検出（POSIX シェル展開の模倣は原理的に不能で
あり、フェイルセーフ・ヒューリスティックの追加は過剰ブロックを招くため
行いません）。
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from bluecore.hooks.hook_common import parse_json_object
from bluecore.lib.core_utils import log

_SHELL_SEPARATORS = {"&&", "||", ";", "|"}

_BINARY_SNIFF_SIZE = 8192  # 8KB
_SECRET_SCAN_MAX_BYTES = 1024 * 1024  # 1MB
_SECRET_SCAN_EXCLUDED_FILENAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
}


def get_staged_files() -> list[str]:
    """ステージング済みファイルの一覧を取得します。

    Returns:
        ステージングされたファイルパスのリストを返します。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return []
        return [f for f in result.stdout.strip().split("\n") if f]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def get_unstaged_modified_files() -> list[str]:
    """`git commit -a` 相当で追加取り込む作業ツリーの変更ファイル一覧を取得します。

    `git diff HEAD --name-only --diff-filter=ACMR` の結果を返します。

    Returns:
        変更されている作業ツリーファイルパスのリストを返します。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD", "--name-only", "--diff-filter=ACMR"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            # HEAD が存在しない（初回コミット）等の場合は非ブロッキングで空リスト
            return []
        return [f for f in result.stdout.strip().split("\n") if f]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def _resolve_repo_root() -> Path | None:
    """`git rev-parse --show-toplevel` でリポジトリルートの絶対パスを解決します。

    `git commit -a` の未ステージ変更を作業ツリーから読むために使います。
    タイムアウト・失敗時は None を返し、呼び出し側で非ブロッキングに
    フォールバック（従来どおり INDEX のみ・作業ツリー分はスキップ）
    できるようにします。

    Returns:
        リポジトリルートの絶対パス。解決できなければ None を返します。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            return None
        top = result.stdout.strip()
        return Path(top) if top else None
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _is_binary_content(content: str) -> bool:
    """先頭 `_BINARY_SNIFF_SIZE` 文字に NUL 文字（`\\x00`）が含まれるかでバイナリ判定します。

    UTF-8 の NUL バイト（0x00）はデコード後も `\\x00` 文字として保持される
    ため、デコード済みテキストに対して判定できます。この判定は lint
    チェックの抑制にのみ使用し、シークレット検出には使いません
    （バイナリ判定を悪用して secret 検査を回避できないようにするためです）。

    Args:
        content: 判定対象のデコード済み文字列です。

    Returns:
        バイナリファイルとみなすなら True を返します。

    Raises:
        例外は発生しません。
    """
    return "\0" in content[:_BINARY_SNIFF_SIZE]


def get_staged_file_content(file_path: str) -> str | None:
    """INDEX（ステージング領域）からファイル内容をテキストとして取得します。

    `git show :path` の出力を bytes で取得し、UTF-8 として
    `errors="replace"` でデコードします（不正なバイト列による
    `UnicodeDecodeError` を避けるためです）。バイナリ判定はここでは行わず
    `find_file_issues` 側で `_is_binary_content` を用いて lint 抑制のみに
    適用します（シークレット検出はバイナリでも常に実行するためです）。

    Args:
        file_path: 対象ファイルのパスです。

    Returns:
        ファイル内容の文字列。取得できない場合は None を返します。

    Raises:
        例外は発生しません。
    """
    try:
        result = subprocess.run(
            ["git", "show", f":{file_path}"],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        raw: bytes = result.stdout
        return raw.decode("utf-8", errors="replace")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def get_worktree_file_content(repo_root: Path, file_path: str) -> str | None:
    """作業ツリーからファイル内容をテキストとして取得します。

    `git commit -a` は作業ツリーの現在の内容をコミットするため、未ステージ
    の変更ファイル（INDEX には反映されていない）はここから読む必要が
    あります。UTF-8 として `errors="replace"` でデコードします。

    読み取り前に realpath 包含チェックを行い、解決後の絶対パスが
    `repo_root` 配下に収まっていることを確認します。これにより以下を
    まとめて封鎖します:

    - `file_path` 自身、または中間ディレクトリがシンボリックリンクで
      `repo_root` 外の実体を指している場合（OS レベルでリンクを追跡すると
      repo 外の秘密鍵等を読み込み、secret 検出結果としてログに一部露出
      しうるため）
    - `file_path` が絶対パスの場合（pathlib の仕様上 `repo_root` が無視される）
    - `file_path` に `..` が含まれ `repo_root` 外へ traversal する場合

    包含チェックに違反した場合は無言で None を返します（既存の「読めなければ
    None」契約と一致させ、fail-open のノイズを増やさないためです）。
    repo 内に留まる正当なシンボリックリンクは通過し従来どおり読みます
    （最小修正の方針。過検知は避けつつ実体は別経路でも検出されます）。

    Args:
        repo_root: リポジトリルートの絶対パスです。
        file_path: `repo_root` からの相対ファイルパスです。

    Returns:
        ファイル内容の文字列。読み取れない・repo_root 外の場合は None を
        返します。

    Raises:
        例外は発生しません。
    """
    try:
        base = repo_root.resolve()
        target = (repo_root / file_path).resolve()
        if not target.is_relative_to(base):
            return None
        raw = target.read_bytes()
    except OSError:
        return None
    return raw.decode("utf-8", errors="replace")


def should_lint_file(file_path: str) -> bool:
    """console.log / デバッガ文 / TODO の lint チェック対象かどうかを判定します。

    Args:
        file_path: 判定対象のファイルパスです。

    Returns:
        lint チェック対象なら True を返します。

    Raises:
        例外は発生しません。
    """
    checkable_extensions = {".js", ".jsx", ".ts", ".tsx", ".py", ".go", ".rs"}
    return Path(file_path).suffix in checkable_extensions


def should_scan_secrets(file_path: str) -> bool:
    """ハードコードされたシークレットのスキャン対象かどうかを判定します。

    lint チェックとは異なり拡張子で絞り込まず、原則として全ファイルを対象と
    します（`tests/` 配下も除外しません）。以下のみ除外します:

    - パッケージマネージャのロックファイル（内容が長大かつ生成物のため）
    - 圧縮・生成物（`*.min.js` / `*.min.css`）

    ファイルサイズ（1MB 超）による扱いは除外ではなく、実際に取得した内容を
    `_SECRET_SCAN_MAX_BYTES` まで切り詰めてスキャンを継続します
    （呼び出し側 `find_file_issues` が行います）。

    Args:
        file_path: 判定対象のファイルパスです。

    Returns:
        シークレットスキャン対象なら True を返します。

    Raises:
        例外は発生しません。
    """
    name = Path(file_path).name
    if name in _SECRET_SCAN_EXCLUDED_FILENAMES:
        return False
    if name.endswith(".min.js") or name.endswith(".min.css"):
        return False
    return True


def _find_git_commit_args(tokens: list[str]) -> list[str] | None:
    """トークン列内の `git commit` 呼び出しを探し、commit 直後の引数トークンを返します。

    `git` トークンの後は、既知/未知を問わずグローバルオプション・その値
    トークンを区別せず単純に読み飛ばし、`commit` サブコマンドに到達するかを
    判定します（allowlist に無い `--exec-path <path>` / `--super-prefix <path>`
    等の値トークンで走査が打ち切られ検出漏れになる問題を避けるため、過剰
    検出側に倒しています）。`&&`/`;`/`|` 等のシェル区切りトークンに達した
    場合はその `git` 呼び出しは commit ではないとみなし、次の `git` トークン
    を探します。見つかった場合、`commit` 以降シェル区切りトークンが現れる
    までを引数リストとして返します。

    Args:
        tokens: `shlex` 等でトークン化されたコマンド列です。

    Returns:
        commit 呼び出しの引数トークンリスト。見つからなければ None を返します。

    Raises:
        例外は発生しません。
    """
    for i, token in enumerate(tokens):
        if token != "git":
            continue
        j = i + 1
        while j < len(tokens):
            tok = tokens[j]
            if tok == "commit":
                args: list[str] = []
                k = j + 1
                while k < len(tokens) and tokens[k] not in _SHELL_SEPARATORS:
                    args.append(tokens[k])
                    k += 1
                return args
            if tok in _SHELL_SEPARATORS:
                break
            j += 1
    return None


def _is_git_commit_command(command: str) -> tuple[bool, list[str]]:
    """コマンド文字列が `git commit` 呼び出しかを判定し、commit 引数トークンを返します。

    `shlex.split` でトークン化し、`git` → グローバルオプション → `commit`
    の並びを検出します。連続空白・改行・`&&`/`;`/`|` 区切りの複合コマンドは
    トークン走査で自然に扱えます。

    `shlex.split` がクォート不整合（heredoc 等）で `ValueError` を送出した
    場合は、空白による簡易分割へフォールバックしてトークン走査を継続し、
    それでも判定できなければ `re.search(r"\\bgit\\s+commit\\b", command)` で
    最終判定します。過剰検出側に倒すフェイルセーフ設計です。

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
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    commit_args = _find_git_commit_args(tokens)
    if commit_args is not None:
        return True, commit_args

    if re.search(r"\bgit\s+commit\b", command):
        return True, []
    return False, []


def _is_amend_commit(commit_args: list[str]) -> bool:
    """commit 引数トークンに `--amend` が含まれるかを判定します。

    Args:
        commit_args: `git commit` 呼び出しの引数トークン列です。

    Returns:
        `--amend` が含まれるなら True を返します。

    Raises:
        例外は発生しません。
    """
    return "--amend" in commit_args


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


def find_file_issues(file_path: str, *, repo_root: Path | None = None) -> list[dict]:
    """ファイル内容から代表的な問題を検出します。

    `repo_root` が None（既定）なら INDEX（`git show :path`、
    `get_staged_file_content`）から読みます。`repo_root` を渡すと作業ツリー
    （`get_worktree_file_content`）から読みます。`git commit -a` で
    コミットされる未ステージ変更ファイルは作業ツリーの内容がコミット対象と
    なるため、`repo_root` 経由で読む必要があります。

    lint チェック（console.log / debugger / TODO）は `should_lint_file` が
    True、かつバイナリでない（先頭 `_BINARY_SNIFF_SIZE` 文字に NUL を含まない）
    ファイルのみ対象です。

    シークレット検出は `should_scan_secrets` が True のファイルであれば、
    バイナリ判定・`# nosec`・ファイルサイズに関わらず常に実行します
    （NUL バイトを1つ混ぜるだけで検査を回避できるバイパスや、大容量化に
    よる全面スキップを防ぐためです）。`_SECRET_SCAN_MAX_BYTES` を超える
    場合は全体を放棄せず、先頭 `_SECRET_SCAN_MAX_BYTES` バイトに切り詰めて
    スキャンを継続します（末尾側のみに存在するシークレットは検出できません
    が、水増しによる全面回避は防げます）。

    `# nosec` を含む行は console.log / debugger / TODO チェックを抑制します
    （検出器自身のテストフィクスチャ等、意図的にパターンを含む行のため）。
    シークレット検出は `# nosec` の対象外です。

    Args:
        file_path: 調査対象のファイルパスです。
        repo_root: 指定すると作業ツリーから読みます（`git commit -a` の
            未ステージ変更用）。None なら INDEX から読みます。

    Returns:
        検出した問題の辞書リストを返します。

    Raises:
        例外は発生しません。
    """
    issues = []

    secret_patterns = [
        (r"sk-[a-zA-Z0-9]{20,}", "OpenAI API key"),
        (r"ghp_[a-zA-Z0-9]{36}", "GitHub PAT"),
        (r"AKIA[A-Z0-9]{16}", "AWS Access Key"),
        (r"api[_-]?key\s*[=:]\s*['\"][^'\"]+['\"]", "API key"),
    ]

    try:
        content = (
            get_worktree_file_content(repo_root, file_path)
            if repo_root is not None
            else get_staged_file_content(file_path)
        )
        if content is None:
            return issues

        is_binary = _is_binary_content(content)
        do_lint = should_lint_file(file_path) and not is_binary
        do_secrets = should_scan_secrets(file_path)

        if not do_lint and not do_secrets:
            return issues

        if do_lint:
            for index, line in enumerate(content.split("\n")):
                line_num = index + 1

                # 抑制マーカー付き行（検出器自身のテストフィクスチャ等、意図的に
                # パターンを含む行）は console.log/debugger/todo をスキップする。
                if "# nosec" in line:
                    continue

                # ログ出力呼び出しをチェック
                if "console.log" in line and not line.strip().startswith(("//", "*")):  # nosec
                    issues.append(
                        {
                            "type": "console.log",  # nosec
                            "message": f"console.log found at line {line_num}",  # nosec
                            "line": line_num,
                            "severity": "warning",
                        }
                    )

                # デバッガ文をチェック
                if re.search(r"\bdebugger\b", line) and not line.strip().startswith("//"):
                    issues.append(
                        {
                            "type": "debugger",  # nosec
                            "message": f"debugger statement at line {line_num}",  # nosec
                            "line": line_num,
                            "severity": "error",
                        }
                    )

                # Issue 参照のない TODO/FIXME をチェック
                todo_match = re.search(r"(?://|#)\s*(TODO|FIXME):?\s*(.+)", line)
                if todo_match and not re.search(r"#\d+|issue", todo_match.group(2), re.IGNORECASE):
                    issues.append(
                        {
                            "type": "todo",
                            "message": f'TODO/FIXME without issue reference at line {line_num}: "{todo_match.group(2).strip()}"',
                            "line": line_num,
                            "severity": "info",
                        }
                    )

        if do_secrets:
            # 大容量ファイルは全面放棄せず、先頭 _SECRET_SCAN_MAX_BYTES バイトに
            # 切り詰めてスキャンを継続する（水増しによる回避を防ぐ）。
            raw_bytes = content.encode("utf-8")
            if len(raw_bytes) > _SECRET_SCAN_MAX_BYTES:
                secret_scan_text = raw_bytes[:_SECRET_SCAN_MAX_BYTES].decode("utf-8", errors="ignore")
            else:
                secret_scan_text = content

            for index, line in enumerate(secret_scan_text.split("\n")):
                line_num = index + 1
                for pattern, name in secret_patterns:
                    if re.search(pattern, line, re.IGNORECASE):
                        issues.append(
                            {
                                "type": "secret",
                                "message": f"Potential {name} exposed at line {line_num}",
                                "line": line_num,
                                "severity": "error",
                            }
                        )

    except Exception:
        # ファイルが読めない場合はスキップ
        pass

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
    # コマンドからコミットメッセージを抽出
    message_match = re.search(r"(?:-m|--message)[=\s]+[\"']?([^\"']+)[\"']?", command)
    if not message_match:
        return None

    message = message_match.group(1)
    issues = []

    # コンベンショナルコミット形式をチェック
    conventional_commit = re.compile(r"^(feat|fix|docs|style|refactor|test|chore|build|ci|perf|revert)(\(.+\))?:\s*.+")
    if not conventional_commit.match(message):
        issues.append(
            {
                "type": "format",
                "message": "Commit message does not follow conventional commit format",
                "suggestion": 'Use format: type(scope): description (e.g., "feat(auth): add login flow")',
            }
        )

    # メッセージの長さをチェック
    if len(message) > 72:
        issues.append(
            {
                "type": "length",
                "message": f"Commit message too long ({len(message)} chars, max 72)",
                "suggestion": "Keep the first line under 72 characters",
            }
        )

    # 先頭の小文字をチェック（規約）
    if conventional_commit.match(message):
        after_colon = message.split(":", 1)[1] if ":" in message else ""
        if after_colon and re.match(r"^[A-Z]", after_colon.strip()):
            issues.append(
                {
                    "type": "capitalization",
                    "message": "Subject should start with lowercase after type",
                    "suggestion": "Use lowercase for the first letter of the subject",
                }
            )

    # 末尾のピリオドをチェック
    if message.endswith("."):
        issues.append(
            {
                "type": "punctuation",
                "message": "Commit message should not end with a period",
                "suggestion": "Remove the trailing period",
            }
        )

    return {"message": message, "issues": issues}


def _partition_commit_all_files(
    staged_files: list[str], commit_args: list[str]
) -> tuple[list[str], list[str]]:
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
        `worktree_files` は空リストです。

    Raises:
        例外は発生しません。
    """
    if not _is_commit_all_flag(commit_args):
        return list(staged_files), []

    worktree_files = sorted(set(get_unstaged_modified_files()))
    index_files = sorted(set(staged_files) - set(worktree_files))
    return index_files, worktree_files


def _count_file_issues(files_to_check: list[str], repo_root: Path | None = None) -> tuple[int, int, int, int]:
    """チェック対象ファイルの問題数を集計します。

    各ファイルに対して find_file_issues を呼び出し、severity 別に問題数を返します。
    ログ出力も行います。

    Args:
        files_to_check: チェック対象のファイルパスリストです。
        repo_root: 指定すると各ファイルを作業ツリーから読みます
            （`git commit -a` の未ステージ変更用）。None なら INDEX から
            読みます。

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
        file_issues = find_file_issues(file_path, repo_root=repo_root)
        if not file_issues:
            continue
        log(f"\n[FILE] {file_path}")
        for issue in file_issues:
            label = severity_label.get(issue["severity"], "INFO")
            log(f"  {label} Line {issue['line']}: {issue['message']}")
            total_issues += 1
            if issue["severity"] == "error":
                error_count += 1
            elif issue["severity"] == "warning":
                warning_count += 1
            elif issue["severity"] == "info":
                info_count += 1

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

    error_count > 0 の場合は exitCode=2（コミットブロック）、
    それ以外は exitCode=0 を返します。

    Args:
        total_issues: 検出された問題の総数です。
        error_count: エラー severity の問題数です。
        warning_count: 警告 severity の問題数です。
        info_count: info severity の問題数です。
        raw_input: そのまま output に返す生の入力文字列です。

    Returns:
        output と exitCode を含む辞書を返します。

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
            return {"output": raw_input, "exitCode": 2}
        log("\n[Hook] WARNING: Warnings found. Consider fixing them, but commit is allowed.")
        log("[Hook] To bypass these checks, use: git commit --no-verify")
    else:
        log("\n[Hook] PASS: All checks passed!")

    return {"output": raw_input, "exitCode": 0}


def evaluate(raw_input: str) -> dict:
    """入力を評価し、出力内容と終了コードを返します。

    Args:
        raw_input: フックに渡された生の入力文字列です。

    Returns:
        output と exitCode を含む辞書を返します。

    Raises:
        例外は発生しません。
    """
    try:
        input_data = parse_json_object(raw_input)
        if not input_data:
            return {"output": raw_input, "exitCode": 0}

        command = input_data.get("tool_input", {}).get("command", "")

        # git commit コマンドの場合のみ実行（トークン化して堅牢に判定）
        is_commit, commit_args = _is_git_commit_command(command)
        if not is_commit:
            return {"output": raw_input, "exitCode": 0}

        # --amend の場合はチェックをスキップ（ブロックを避けるため）
        if _is_amend_commit(commit_args):
            return {"output": raw_input, "exitCode": 0}

        # ステージングされたファイルを取得（-a/--all の場合は未ステージの変更も加える。
        # -a の未ステージ分は作業ツリーの内容がコミットされるため作業ツリーから読む）
        staged_files = get_staged_files()
        index_files, worktree_files = _partition_commit_all_files(staged_files, commit_args)
        all_files = sorted(set(index_files) | set(worktree_files))
        if not all_files:
            log('[Hook] No staged files found. Use "git add" to stage files first.')
            return {"output": raw_input, "exitCode": 0}

        log(f"[Hook] Checking {len(all_files)} staged file(s)...")

        index_targets = [f for f in index_files if should_lint_file(f) or should_scan_secrets(f)]
        worktree_targets = [f for f in worktree_files if should_lint_file(f) or should_scan_secrets(f)]

        total_issues, error_count, warning_count, info_count = _count_file_issues(index_targets)

        if worktree_targets:
            repo_root = _resolve_repo_root()
            if repo_root is not None:
                wt_total, wt_error, wt_warning, wt_info = _count_file_issues(
                    worktree_targets, repo_root=repo_root
                )
                total_issues += wt_total
                error_count += wt_error
                warning_count += wt_warning
                info_count += wt_info
            # repo root が解決できない場合は非ブロッキングで作業ツリー分をスキップする

        total_issues, warning_count = _apply_commit_message_issues(command, total_issues, warning_count)

        return _finalize_result(total_issues, error_count, warning_count, info_count, raw_input)

    except Exception as err:
        log(f"[Hook] Error: {err}")
        # エラー時はノンブロッキング

    return {"output": raw_input, "exitCode": 0}


def run(raw_input: str) -> dict:
    """フックを実行し、run_with_flags 用の結果を返します。

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

    Returns:
        コミットを許可する場合は 0、ブロックする場合は 2 を返します。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    from bluecore.hooks.hook_common import read_raw_stdin

    try:
        raw = read_raw_stdin()
        result = evaluate(raw)
        return result["exitCode"]
    except Exception:
        return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
