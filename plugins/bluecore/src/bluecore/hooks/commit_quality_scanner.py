#!/usr/bin/env python3
"""コミット対象ファイルの内容を取得し、品質・シークレット問題を検出します。

`pre_bash_commit_quality` フックのスキャナ層です。ファイル内容の取得
（INDEX / 作業ツリー）、lint 対象・シークレットスキャン対象の判定、
バイナリ判定、そして実際の問題検出（console.log / debugger / TODO /
シークレット）を担います。`git commit` の検出やコミットメッセージ検証
といったエントリ側のロジックは `pre_bash_commit_quality` に残ります。

シークレット検出はバイナリ判定（lint 抑制のみに使用）や nosec、
ファイルサイズに関わらず可能な限り実行します（大容量ファイルは
先頭 `_SECRET_SCAN_MAX_BYTES` バイトに切り詰めて継続します）。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

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


def _scan_lint_issues(lines: list[str]) -> list[dict]:
    """ファイル内容から console.log / debugger / Issue 参照なし TODO を検出します。

    `# nosec` を含む行は検出器自身のテストフィクスチャ等、意図的に
    パターンを含む行とみなして console.log / debugger / TODO チェックを
    抑制します（シークレット検出は別関数で `# nosec` の対象外です）。

    Args:
        lines: 検査対象のデコード済みファイル内容を改行で分割した行リストです
            （呼び出し側 `find_file_issues` が一度だけ分割して渡します）。

    Returns:
        検出した lint 問題の辞書リストを返します。

    Raises:
        例外は発生しません。
    """
    issues = []
    for index, line in enumerate(lines):
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

    return issues


def _scan_secret_issues(content: str, lines: list[str]) -> list[dict]:
    """ファイル内容からハードコードされたシークレットを検出します。

    バイナリ判定・`# nosec`・ファイルサイズに関わらず常に実行します
    （NUL バイトを1つ混ぜるだけで検査を回避できるバイパスや、大容量化に
    よる全面スキップを防ぐためです）。`_SECRET_SCAN_MAX_BYTES` を超える
    場合は全体を放棄せず、先頭 `_SECRET_SCAN_MAX_BYTES` バイトに切り詰めて
    スキャンを継続します（末尾側のみに存在するシークレットは検出できません
    が、水増しによる全面回避は防げます）。

    Args:
        content: 検査対象のデコード済みファイル内容です（バイト長判定・
            切り詰めに使用します）。
        lines: `content` を改行で分割済みの行リストです。切り詰めが
            発生しない（大多数の）場合はこれを再利用し、`content.split`
            の再計算を避けます（呼び出し側 `find_file_issues` が
            lint スキャンと共有する分割結果です）。

    Returns:
        検出したシークレット問題の辞書リストを返します。

    Raises:
        例外は発生しません。
    """
    secret_patterns = [
        (r"sk-[a-zA-Z0-9]{20,}", "OpenAI API key"),
        (r"ghp_[a-zA-Z0-9]{36}", "GitHub PAT"),
        (r"AKIA[A-Z0-9]{16}", "AWS Access Key"),
        (r"api[_-]?key\s*[=:]\s*['\"][^'\"]+['\"]", "API key"),
    ]

    # 大容量ファイルは全面放棄せず、先頭 _SECRET_SCAN_MAX_BYTES バイトに
    # 切り詰めてスキャンを継続する（水増しによる回避を防ぐ）。切り詰めが
    # 発生しない場合は呼び出し側で分割済みの lines をそのまま使う。
    raw_bytes = content.encode("utf-8")
    if len(raw_bytes) > _SECRET_SCAN_MAX_BYTES:
        secret_scan_text = raw_bytes[:_SECRET_SCAN_MAX_BYTES].decode("utf-8", errors="ignore")
        scan_lines = secret_scan_text.split("\n")
    else:
        scan_lines = lines

    issues = []
    for index, line in enumerate(scan_lines):
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

    return issues


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
    issues: list[dict] = []
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

        # lint / secret 双方が対象の場合、content.split("\n") の重複計算を
        # 避けるため一度だけ分割して共有する。
        lines = content.split("\n")
        if do_lint:
            issues.extend(_scan_lint_issues(lines))
        if do_secrets:
            issues.extend(_scan_secret_issues(content, lines))

    except Exception:
        # ファイルが読めない場合はスキップ
        pass

    return issues
