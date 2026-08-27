#!/usr/bin/env python3
"""コミット対象ファイルの内容を取得し、品質・シークレット問題を検出します。

`pre_bash_commit_quality` フックのスキャナ層です。ファイル内容の取得
（INDEX / 作業ツリー）、lint 対象・シークレットスキャン対象の判定、
バイナリ判定、そして実際の問題検出（ログ出力呼び出し / デバッガ文 / TODO /
シークレット）を担います。`git commit` の検出やコミットメッセージ検証
といったエントリ側のロジックは `pre_bash_commit_quality` に残ります。

シークレット検出はテキストファイルであれば nosec・ファイルサイズに関わらず
全体を走査します（サイズによる打ち切りは行いません。A-02 対応）。バイナリ
判定されたファイルは secret scan 自体をスキップし、severity `warning` の
痕跡 issue を残します（severity `error` にはしません。deny は画像 commit を
ブロックし、ユーザーの明示要件に反するため）。走査量に上限を設けないと
`pre_bash_commit_quality` の hook timeout（30秒）に達し、host がフックを
キャンセルして続行する（fail-open）ため commit 全体の secret scan が無検査に
なりうる、という 1MiB cap より悪いリスクがあります。そのため実時間バジェット
（`_SECRET_SCAN_TIME_BUDGET_SECONDS`）を設け、超過したテキストファイルは
`scan_error`（severity `error`、fail-closed）として扱います。
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from bluecore.lib.core_utils import log

_BINARY_SNIFF_SIZE = 8192  # 8KB

# バイナリ判定された内容から抽出する印字可能文字列の最短長。`strings(1)` と
# 同じ発想で、制御文字（NUL を含む）で区切られた連続を擬似的な行として扱う。
_BINARY_STRINGS_MIN_LENGTH = 4
_BINARY_STRINGS_RE = re.compile(
    rf"[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f]{{{_BINARY_STRINGS_MIN_LENGTH},}}"
)

# secret scan 1 回あたりに許す実時間予算（秒）。pre_bash_commit_quality の
# hooks.json timeout（30秒）より十分小さく取り、予算超過を scan_error
# （fail-closed）として検知してからホスト側 timeout に達しないようにする。
_SECRET_SCAN_TIME_BUDGET_SECONDS = 10.0

# 予算チェックの頻度（行数）。毎行 time.monotonic() を呼ぶコストを避けつつ、
# 予算超過を実用上十分な精度で検知する。
_SECRET_SCAN_BUDGET_CHECK_INTERVAL = 2000

_LINTABLE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".py", ".go", ".rs"}
_MINIFIED_SUFFIXES = (".min.js", ".min.css")
_SECRET_SCAN_EXCLUDED_FILENAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
}
_SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"sk-[a-zA-Z0-9]{20,}", "OpenAI API key"),
    (r"sk-ant-[a-zA-Z0-9_-]{20,}", "Anthropic API key"),
    (r"ghp_[a-zA-Z0-9]{36}", "GitHub PAT"),
    (r"AKIA[A-Z0-9]{16}", "AWS Access Key"),
    (r"api[_-]?key\s*[=:]\s*['\"][^'\"]+['\"]", "API key"),
)


def _monotonic() -> float:
    """`time.monotonic()` を返す間接呼び出し。

    secret scan の実時間バジェット判定に使う時刻取得をテストから
    決定的に差し替え可能にするための間接層です（`time.monotonic()` を
    直接呼ぶとテストがスリープに依存し flaky になるため）。

    Args:
        なし

    Returns:
        単調増加する時刻（秒）。

    Raises:
        例外は発生しません。
    """
    return time.monotonic()


class SecretScanBudgetExceeded(Exception):
    """secret scan が実時間予算を超過したことを表す例外。

    `find_file_issues` の既存の例外ガード（`scan_error`、severity `error`、
    fail-closed）にそのまま乗せるための専用例外です。
    """


def _is_binary_content(content: str) -> bool:
    """先頭 `_BINARY_SNIFF_SIZE` 文字に NUL 文字（`\\x00`）が含まれるかでバイナリ判定します。

    UTF-8 の NUL バイト（0x00）はデコード後も `\\x00` 文字として保持される
    ため、デコード済みテキストに対して判定できます。この判定は lint
    チェックの抑制にのみ使用し、シークレット検出の実施可否には使いません
    （バイナリ判定を悪用して secret 検査を回避できないようにするためです）。
    バイナリ判定された内容に対しては、行分割の代わりに
    `_extract_printable_runs` で印字可能文字列を抽出して同じ secret パターンを
    適用します（ADR-0013）。

    Args:
        content: 判定対象のデコード済み文字列です。

    Returns:
        バイナリファイルとみなすなら True を返します。

    Raises:
        例外は発生しません。
    """
    return "\0" in content[:_BINARY_SNIFF_SIZE]


def _extract_printable_runs(content: str) -> list[str]:
    """バイナリ判定された内容から、印字可能文字の連続を擬似的な行として抽出します。

    `strings(1)` と同じ発想です。NUL を含む制御文字で区切られた
    `_BINARY_STRINGS_MIN_LENGTH` 文字以上の連続だけを返し、それぞれを 1 行と
    みなして通常の secret パターンを適用します。

    これにより「先頭に NUL を 1 バイト混ぜてバイナリ判定させ、secret 検査を
    まるごと回避する」経路が塞がれます。真のバイナリ（画像等）は secret
    パターンに一致する印字可能文字列を通常含まないため、一律ブロックには
    なりません。

    Args:
        content: バイナリ判定されたデコード済み文字列です。

    Returns:
        抽出した印字可能文字列のリストを返します。

    Raises:
        例外は発生しません。
    """
    return _BINARY_STRINGS_RE.findall(content)


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
            timeout=5,
        )
        if result.returncode != 0:
            return None
        return result.stdout.decode("utf-8", errors="replace")
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
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
    """ログ出力呼び出し / デバッガ文 / TODO の lint チェック対象かどうかを判定します。

    Args:
        file_path: 判定対象のファイルパスです。

    Returns:
        lint チェック対象なら True を返します。

    Raises:
        例外は発生しません。
    """
    return Path(file_path).suffix in _LINTABLE_SUFFIXES


def should_scan_secrets(file_path: str) -> bool:
    """ハードコードされたシークレットのスキャン対象かどうかを判定します。

    lint チェックとは異なり拡張子で絞り込まず、原則として全ファイルを対象と
    します（`tests/` 配下も除外しません）。以下のみ除外します:

    - パッケージマネージャのロックファイル（内容が長大かつ生成物のため）
    - 圧縮・生成物（`*.min.js` / `*.min.css`）

    ファイルサイズによる除外は行いません。テキストファイルは全体を走査し、
    バイナリ判定されたファイルのみ `find_file_issues` 側で secret scan 自体を
    スキップします（severity `warning` の痕跡を残す。A-02 対応）。

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
    return not name.endswith(_MINIFIED_SUFFIXES)


def _scan_lint_issues(lines: list[str]) -> list[dict]:
    """ファイル内容からログ出力呼び出し / デバッガ文 / Issue 参照なし TODO を検出します。

    `# nosec` を含む行は検出器自身のテストフィクスチャ等、意図的に
    パターンを含む行とみなしてログ出力呼び出し / デバッガ文 / TODO チェックを
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
        # パターンを含む行）はログ出力呼び出し/デバッガ文/todo をスキップする。
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


def _scan_secret_issues(content: str, lines: list[str], *, report_lines: bool = True) -> list[dict]:
    """ファイル内容からハードコードされたシークレットを検出します。

    呼び出し元（`find_file_issues`）はバイナリ判定されたファイルではこの
    関数を呼びません。テキストファイルは `# nosec`・ファイルサイズに関わらず
    全体を走査します（サイズによる打ち切りは行いません。A-02 対応。1MiB 境界
    より後ろに置かれた secret も検出します）。

    走査量に上限を設けないと `pre_bash_commit_quality` の hook timeout に
    達しうるため、実時間バジェット（`_SECRET_SCAN_TIME_BUDGET_SECONDS`）を
    `_SECRET_SCAN_BUDGET_CHECK_INTERVAL` 行ごとに確認します。超過した場合は
    `SecretScanBudgetExceeded` を送出し、呼び出し元の既存の例外ガードで
    `scan_error`（severity `error`、fail-closed）として扱われます。

    Args:
        content: 検査対象のデコード済みファイル内容です（未使用ですが、
            呼び出し側とのインターフェース共有のため引数として残します）。
        lines: 走査単位のリストです。テキストファイルでは `content` を改行で
            分割済みの行リスト（呼び出し側 `find_file_issues` が lint スキャンと
            共有する分割結果）、バイナリでは `_extract_printable_runs` の抽出結果です。
        report_lines: 走査単位が実際の行に対応するなら True。バイナリの抽出
            文字列は行に対応しないため False を渡し、行番号を報告しません。

    Returns:
        検出したシークレット問題の辞書リストを返します。

    Raises:
        SecretScanBudgetExceeded: 実時間バジェットを超過した場合。
    """
    deadline = _monotonic() + _SECRET_SCAN_TIME_BUDGET_SECONDS

    issues = []
    for index, line in enumerate(lines):
        if index % _SECRET_SCAN_BUDGET_CHECK_INTERVAL == 0 and _monotonic() > deadline:
            raise SecretScanBudgetExceeded(
                f"secret scan exceeded {_SECRET_SCAN_TIME_BUDGET_SECONDS}s time budget "
                f"at line {index + 1}"
            )
        line_num = index + 1 if report_lines else 0
        location = f"at line {line_num}" if report_lines else "in extracted binary content"
        for pattern, name in _SECRET_PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                issues.append(
                    {
                        "type": "secret",
                        "message": f"Potential {name} exposed {location}",
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

    lint チェック（ログ出力呼び出し / デバッガ文 / TODO）は `should_lint_file` が
    True、かつバイナリでない（先頭 `_BINARY_SNIFF_SIZE` 文字に NUL を含まない）
    ファイルのみ対象です。

    シークレット検出は `should_scan_secrets` が True であれば `# nosec`・ファイル
    サイズ・バイナリ判定に関わらず必ず実施します（A-02 対応。サイズによる
    打ち切りはありません）。バイナリ判定されたファイルは行分割が意味を持たない
    ため、`_extract_printable_runs` で印字可能文字列を抽出し、それを擬似的な行と
    して同じパターンを当てます（ADR-0013。以前は secret scan 自体をスキップして
    おり、先頭に NUL を 1 バイト混ぜるだけで検査を回避できた）。真のバイナリ
    （画像等）は secret パターンに一致する印字可能文字列を通常含まないため、
    一律ブロックにはなりません。

    `# nosec` を含む行はログ出力呼び出し / デバッガ文 / TODO チェックを抑制します
    （検出器自身のテストフィクスチャ等、意図的にパターンを含む行のため）。
    シークレット検出は `# nosec` の対象外です。

    ファイル読み取り・走査対象判定・lint・secret の各段階はそれぞれ独立した
    例外ガードを持ちます。いずれかで想定外の例外が起きた場合は検査不能を
    示す `scan_error` issue を積んで呼び出し元へ返します（secret scanner
    とファイル読み取りの失敗は error severity。false negative のコストが
    lint より高いため、検査不能をブロック側へ倒します。lint scanner の
    失敗は warning に留めます）。黙って issue が消える（＝検査したのに
    問題なしと区別が付かない）ことはありません。

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
    except Exception as err:
        # get_staged_file_content / get_worktree_file_content は自前で
        # 例外を握り潰し読めなければ None を返す契約だが、その契約が
        # 破られた場合でも find_file_issues 自体の「例外を発生させない」
        # 契約を守るため、ここでもガードして scan_error を返す。
        issues.append(_scan_error_issue(file_path, "ファイル読み取り", err, severity="error"))
        return issues
    if content is None:
        # get_staged_file_content / get_worktree_file_content の正常系の
        # エラー経路（git 非0終了・OSError・repo_root 外への symlink/traversal
        # 拒否）はいずれも None を返す契約。ここで黙って空 issue を返すと
        # 「スキャンして問題なし」と「検査不能」が区別できず、対象ファイル
        # は無検査のままコミットが通ってしまう。scan_error（error）として
        # 積み、_finalize_result 側でブロック対象にする。
        log(f"[Hook] scan_error: {file_path} の内容取得に失敗しました（読み取り不能または取得拒否）")
        issues.append(
            {
                "type": "scan_error",
                "message": f"{file_path}: 内容を取得できなかったため検査できませんでした",
                "line": 0,
                "severity": "error",
            }
        )
        return issues

    try:
        is_binary = _is_binary_content(content)
        do_lint = should_lint_file(file_path) and not is_binary
        do_secrets = should_scan_secrets(file_path)
    except Exception as err:
        issues.append(_scan_error_issue(file_path, "scan target 判定", err, severity="error"))
        return issues

    if not do_lint and not do_secrets:
        return issues

    # lint / secret 双方が対象の場合、content.split("\n") の重複計算を
    # 避けるため一度だけ分割して共有する。
    lines = content.split("\n")
    if do_lint:
        try:
            issues.extend(_scan_lint_issues(lines))
        except Exception as err:
            issues.append(_scan_error_issue(file_path, "lint scan", err, severity="warning"))
    if do_secrets:
        # バイナリ判定でも secret scan は必ず実施する。行分割が意味を持たない
        # ため、印字可能文字列の抽出結果を擬似的な行として同じパターンを当てる
        # （ADR-0013: NUL 1 バイトで検査を回避できる状態を許容しない）。
        scan_lines = _extract_printable_runs(content) if is_binary else lines
        try:
            issues.extend(_scan_secret_issues(content, scan_lines, report_lines=not is_binary))
        except Exception as err:
            issues.append(_scan_error_issue(file_path, "secret scan", err, severity="error"))

    return issues


def _scan_error_issue(file_path: str, stage: str, err: Exception, *, severity: str) -> dict:
    """scanner 内部例外を issue 化する。

    例外メッセージ自体（ファイル内容の断片を含みうる）は出力に含めず、
    型名だけを記録する（秘密情報の二次漏洩を避けるため）。

    Args:
        file_path: 対象ファイルのパス。
        stage: 失敗した処理段階の説明（ログ用）。
        err: 捕捉した例外。
        severity: ``"error"``（ブロック対象）または ``"warning"``。

    Returns:
        issue 辞書。

    Raises:
        例外は発生しません。
    """
    error_type = type(err).__name__
    log(f"[Hook] scan_error: {file_path} の{stage}に失敗しました（{error_type}）")
    return {
        "type": "scan_error",
        "message": f"{file_path}: {stage}に失敗したため検査できませんでした（{error_type}）",
        "line": 0,
        "severity": severity,
    }
