#!/usr/bin/env python3
"""コミット対象ファイルの内容を取得し、品質・シークレット問題を検出します。

`pre_bash_commit_quality` フックのスキャナ層です。ファイル内容の取得（INDEX /
作業ツリー）、lint 対象・シークレットスキャン対象の判定、バイナリ判定、問題検出
（ログ出力呼び出し / デバッガ文 / TODO / シークレット）を担います。`git commit` の
検出やコミットメッセージ検証はエントリ側の `pre_bash_commit_quality` に残ります。

シークレット検出は nosec・ファイルサイズに関わらず全体を走査します。バイナリ判定
されたファイルも、`_extract_printable_runs` が抽出した印字可能文字列へ同じパターンを
当てます（docs/adr/shell-analysis-boundary.md。外すと先頭に NUL を 1 バイト混ぜるだけで
検査を回避できる）。

走査量に上限が無いと `pre_bash_commit_quality` の hook timeout（30秒）に達し、host が
フックをキャンセルして続行する（fail-open）。そのため実時間バジェット
（`_SECRET_SCAN_TIME_BUDGET_SECONDS`）を設け、超過は `scan_error`（severity `error`、
fail-closed）として扱います。このバジェットは**フック 1 回の起動全体で共有する
1 本の予算**で（`new_secret_scan_deadline`）、secret より先に走る lint scan も同じ予算に
載せます。ファイル単位や secret 単独の予算では、累積や lint 側で hook timeout に
達する経路が残るためです。予算の確認間隔は行数ではなく走査済みバイト数で数えます
（改行の無い巨大 1 行のファイルでは、行数基準だと走査開始前の 1 回しか確認されない）。
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from claq.lib.core_utils import log
from claq.mem.tag_stripping import strip_tags

# deny 出力へ載せる不信データ（ステージされたファイルの行内容）の最大文字数。
# stderr はモデルへ提示されるチャネルなので、長さ無制限の抜粋はコンテキストを
# 丸ごと押し出せる。
_UNTRUSTED_EXCERPT_MAX_CHARS = 200

_BINARY_SNIFF_SIZE = 8192  # 8KB

# バイナリ判定された内容から抽出する印字可能文字列の最短長。`strings(1)` と
# 同じ発想で、制御文字（NUL を含む）で区切られた連続を擬似的な行として扱う。
_BINARY_STRINGS_MIN_LENGTH = 4
_BINARY_STRINGS_RE = re.compile(
    rf"[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f]{{{_BINARY_STRINGS_MIN_LENGTH},}}"
)

# フック 1 回の起動で走査全体に許す実時間予算（秒）。hooks.json の timeout（30秒）より
# 十分小さく取り、ホスト側 timeout に達する前に scan_error（fail-closed）として検知する。
# 予算が支配するのはパターン走査の実時間だけで、ファイル内容の読み取り（`git show` は
# 1 ファイルにつき timeout=5 秒）は課金対象外。
_SECRET_SCAN_TIME_BUDGET_SECONDS = 10.0

# 予算チェックの間隔（走査済みバイト数）。毎回時刻を読むコストを避けつつ、予算超過を
# 実用上十分な精度で検知する。走査の実コストはバイト数に比例するため、行数では数えない。
_SCAN_BUDGET_CHECK_BYTES = 256 * 1024

# 1 行あたりの上限長は設けない。打ち切ると、上限より後ろに secret を置いた 1 行で
# 検査を回避できる。代わりに予算確認を「行 × パターン」の粒度で行い、超過検知から
# 停止までの超過量を「1 パターン × 1 行の線形走査 1 回」に抑える。全パターンが線形で
# あること（JWT の lookbehind と所有量指定子）がこの上界の前提。

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
    # ベンダ prefix の集合は `claq.mem.redaction._PATTERNS` と揃える（記録時はマスクするが
    # commit 時は検出しない秘密を作らないため）。
    #
    # `sk-` の文字クラスに `-` を含めるのは、OpenAI の現行形式がハイフンを含むため。
    # `(?!ant-)` は Anthropic 形と 1 行から 2 件の issue が出る二重計上を避けるため。
    (r"sk-(?!ant-)[a-zA-Z0-9_-]{20,}", "OpenAI API key"),
    (r"sk-ant-[a-zA-Z0-9_-]{20,}", "Anthropic API key"),
    (r"gh[pors]_[a-zA-Z0-9]{36,}", "GitHub PAT"),
    (r"github_pat_[a-zA-Z0-9_]{59,}", "GitHub fine-grained PAT"),
    (r"xox[bpoa]-[a-zA-Z0-9-]{10,}", "Slack token"),
    (r"AIza[a-zA-Z0-9_-]{35}", "Google API key"),
    (r"(?:AKIA|ASIA|AIDA|AROA)[A-Z0-9]{16}", "AWS Access Key"),
    # `eyJ` の直前に base64url 文字が無いことを要求する（lookbehind）。無いと `eyJeyJeyJ…`
    # で 3 バイトごとに開始位置が立ち、各位置が残り全体を貪欲に取って後退するため走査が
    # 二次オーダーへ落ち、hook timeout を桁で超える。量指定子も所有的にして後退を禁じる
    # （直後の `\.` は文字クラスと素なので一致集合は変わらない）。代償として base64url
    # 文字へ続く `eyJ` は一致しないが、JWT の実物は区切りの直後に現れる。
    (r"(?<![a-zA-Z0-9_-])eyJ[a-zA-Z0-9_-]++\.[a-zA-Z0-9_-]++\.[a-zA-Z0-9_-]*+", "JWT"),
    # 認証情報の代入。クォートを任意にすると `token = segment[index]` のような通常の
    # コード代入まで拾い、commit をブロックするフックの可用性を壊す。そのため
    # 「クォート付き」「区切りの前後に空白が無い env/ini 形」「行頭キーの YAML 形」の
    # 3 形に限定し、値の文字集合も ASCII の秘密様文字へ絞る。
    (
        r"(?:password|passwd|secret|token|api[-_]?key)\s*[=:]\s*['\"][^'\"\s]+['\"]",
        "credential assignment",
    ),
    (
        r"(?:password|passwd|secret|token|api[-_]?key)[=:][A-Za-z0-9_\-./+]{8,}",
        "credential assignment",
    ),
    (
        r"^\s*[a-z0-9_]*(?:password|passwd|secret|token|api[-_]?key)\s*:\s+[A-Za-z0-9_\-./+]{8,}\s*$",
        "credential assignment",
    ),
    # PEM / OpenSSH / PGP 秘密鍵ブロックのヘッダ行。鍵種別は列挙で固定し、走査を二次
    # オーダーへ落とす `[A-Z ]+` のような曖昧な繰り返しを使わない。公開物のヘッダは
    # "PRIVATE KEY" を含まないので一致しない。
    #
    # 鍵種別はこの 1 本の交替へ語を足す。完成形のヘッダを literal で書き並べると、その
    # 行自身がパターンへ一致し、本モジュールの自己走査テストが落ちる。同じ理由で、この
    # 付近のコメントにも完成形のヘッダを書かない。
    (
        r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |ENCRYPTED |PGP |SSH2 |SSH2 ENCRYPTED )?"
        r"PRIVATE KEY(?: BLOCK)?[-]{5}",
        "private key",
    ),
    # ssh.com / Tectia 形式。デリミタが 4 ハイフンで、ヘッダ語との間に空白が入る
    # ため上の交替では拾えない。
    (r"[-]{4} BEGIN SSH2 (?:ENCRYPTED )?PRIVATE KEY [-]{4}", "SSH2 private key"),
    # PuTTY の .ppk。版番号を文字クラスにするのは v3 を拾うためと、この行自身が
    # 自分のパターンに一致しないようにするため（上のコメントと同じ理由）。
    (r"PuTTY-User-Key-File-[0-9]", "PuTTY private key"),
)


def _monotonic() -> float:
    """`time.monotonic()` を返す間接呼び出し。

    実時間バジェット判定の時刻取得を、テストがスリープに依存せず決定的に差し替える
    ための間接層です。

    Returns:
        単調増加する時刻（秒）。

    Raises:
        例外は発生しません。
    """
    return time.monotonic()


def new_secret_scan_deadline() -> float:
    """走査の実時間予算 1 本分の deadline を作ります。

    フック 1 回の起動につき 1 度だけ呼び、返り値を全ファイルの `find_file_issues` へ
    渡してください。ファイルごとに呼ぶと予算がファイル数分だけ増えます。

    Returns:
        `_monotonic()` 基準で走査を打ち切るべき時刻（秒）。

    Raises:
        例外は発生しません。
    """
    return _monotonic() + _SECRET_SCAN_TIME_BUDGET_SECONDS


class SecretScanBudgetExceeded(Exception):
    """走査が実時間予算を超過したことを表す例外。

    `find_file_issues` の例外ガード（`scan_error`、severity `error`、fail-closed）に
    乗せるための専用例外です。予算は lint scan とも共有しますが、名前は
    `new_secret_scan_deadline` と対にしています。
    """


class _ScanBudget:
    """走査の実時間予算を「走査済みバイト数」間隔で確認するガードです。

    `check()` は走査済みバイト数を積み、`_SCAN_BUDGET_CHECK_BYTES` を跨いだときだけ
    時刻を読みます。初期残量を 0 にしてあるため、最初の `check()` は必ず時刻を読みます。

    共有されるのは `deadline` だけで、バイト数の積算はインスタンス（ファイルごとの
    lint 用・secret 用）単位です。実時間の上界を与えるのは共有 `deadline` の方で、各
    スキャナが開始時に必ず 1 回 deadline を見るため、予算が尽きていれば次のスキャナが
    fail-closed で止まります。積算間隔が受け持つのは 1 ファイルの中を走り抜ける間だけです。
    """

    def __init__(self, deadline: float) -> None:
        """予算ガードを生成します。

        Args:
            deadline: 走査を打ち切る `_monotonic()` 基準の時刻です。
        """
        self._deadline = deadline
        self._remaining = 0

    def check(self, scanned_bytes: int, where: str) -> None:
        """走査済みバイト数を積み、間隔を跨いだら予算を確認します。

        Args:
            scanned_bytes: 直前の走査で消費したバイト数です。
            where: 超過時のメッセージに載せる位置情報です。

        Raises:
            SecretScanBudgetExceeded: 実時間バジェットを超過した場合。
        """
        self._remaining -= scanned_bytes
        if self._remaining > 0:
            return
        self._remaining = _SCAN_BUDGET_CHECK_BYTES
        if _monotonic() > self._deadline:
            raise SecretScanBudgetExceeded(
                f"scan exceeded {_SECRET_SCAN_TIME_BUDGET_SECONDS}s time budget at {where}"
            )


def _is_binary_content(content: str) -> bool:
    """先頭 `_BINARY_SNIFF_SIZE` 文字に NUL 文字（`\\x00`）が含まれるかでバイナリ判定します。

    この判定は lint チェックの抑制にのみ使い、シークレット検出の実施可否には使いません
    （バイナリ判定を悪用して secret 検査を回避できないようにするため）。

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

    `strings(1)` と同じ発想で、制御文字で区切られた `_BINARY_STRINGS_MIN_LENGTH` 文字
    以上の連続を返します。真のバイナリ（画像等）は secret パターンに一致する印字可能
    文字列を通常含まないため、一律ブロックにはなりません。

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

    `git show :path` の出力を bytes で取得し、不正なバイト列で `UnicodeDecodeError` に
    ならないよう UTF-8 / `errors="replace"` でデコードします。

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
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def get_worktree_file_content(repo_root: Path, file_path: str) -> str | None:
    """作業ツリーからファイル内容をテキストとして取得します。

    `git commit -a` は作業ツリーの現在の内容をコミットするため、未ステージの変更
    ファイルはここから読みます。読み取り前に、親ディレクトリの解決後の絶対パスが
    `repo_root` 配下に収まることを確認し、中間ディレクトリの symlink・絶対パス・`..`
    で repo 外の秘密鍵等を読んでログへ一部露出させる経路を塞ぎます。違反は
    「読めなければ None」と同じ扱いにします。

    末尾要素が symlink のときは先を辿らず、link 先文字列を返します。git が symlink
    の blob として保存するのがその文字列であり（INDEX 側の `git show :path` も同じ
    文字列を返す）、先を辿ると通常ファイルから symlink（ディレクトリ・壊れた先・
    repo 外）へ typechange しただけの正当な commit が読み取り失敗で止まるうえ、
    repo 外の内容を読む経路にもなるため。

    Args:
        repo_root: リポジトリルートの絶対パスです。
        file_path: `repo_root` からの相対ファイルパスです。

    Returns:
        ファイル内容の文字列（symlink は link 先文字列）。読み取れない・repo_root 外の
        場合は None を返します。

    Raises:
        例外は発生しません。
    """
    try:
        base = repo_root.resolve()
        requested = repo_root / file_path
        parent = requested.parent.resolve()
        if not parent.is_relative_to(base):
            return None
        target = parent / requested.name
        if target.is_symlink():
            return os.readlink(target)
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

    拡張子で絞り込まず原則として全ファイル（`tests/` 配下も）を対象とし、パッケージ
    マネージャのロックファイルと `*.min.js` / `*.min.css` だけを除外します。サイズや
    バイナリ判定による除外は行いません。

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


def _sanitize_untrusted_excerpt(text: str) -> str:
    """ファイル由来の抜粋を deny 出力へ載せる前に無害化する。

    抜粋は攻撃者が内容を選べるファイル本文で、deny 時に stderr 経由でモデルの
    コンテキストへ入る。囲いを偽装するタグ（``</claq-memory>`` など）を `strip_tags` で
    倒し、長さにも上限を置く（`mem` の注入経路と同じ扱い）。

    Args:
        text: ファイル本文から切り出した抜粋。

    Returns:
        タグを無害化し上限で切り詰めた抜粋。

    Raises:
        例外は発生しません。
    """
    sanitized = strip_tags(text.strip()).strip()
    if len(sanitized) > _UNTRUSTED_EXCERPT_MAX_CHARS:
        return sanitized[:_UNTRUSTED_EXCERPT_MAX_CHARS] + "…"
    return sanitized


def _scan_lint_issues(lines: list[str], *, deadline: float) -> list[dict]:
    """ファイル内容からログ出力呼び出し / デバッガ文 / Issue 参照なし TODO を検出します。

    `# nosec` を含む行は検出器自身のテストフィクスチャ等、意図的にパターンを含む行と
    みなして抑制します。secret scan と同じ 1 本の実時間予算に載せます。

    Args:
        lines: 検査対象のデコード済みファイル内容を改行で分割した行リストです。
        deadline: 走査を打ち切る `_monotonic()` 基準の時刻です
            （`new_secret_scan_deadline` の 1 本の予算）。

    Returns:
        検出した lint 問題の辞書リストを返します。

    Raises:
        SecretScanBudgetExceeded: 実時間バジェットを超過した場合。
    """
    issues = []
    budget = _ScanBudget(deadline)
    for index, line in enumerate(lines):
        line_num = index + 1
        budget.check(len(line), f"line {line_num}")

        if "# nosec" in line:
            continue

        if "console.log" in line and not line.strip().startswith(("//", "*")):  # nosec
            issues.append(
                {
                    "type": "console.log",  # nosec
                    "message": f"console.log found at line {line_num}",  # nosec
                    "line": line_num,
                    "severity": "warning",
                }
            )

        if re.search(r"\bdebugger\b", line) and not line.strip().startswith("//"):
            issues.append(
                {
                    "type": "debugger",  # nosec
                    "message": f"debugger statement at line {line_num}",  # nosec
                    "line": line_num,
                    "severity": "error",
                }
            )

        todo_match = re.search(r"(?://|#)\s*(TODO|FIXME):?\s*(.+)", line)
        if todo_match and not re.search(r"#\d+|issue", todo_match.group(2), re.IGNORECASE):
            issues.append(
                {
                    "type": "todo",
                    # 行内容は不信データで、deny 時にモデルのコンテキストへ入るため無害化する。
                    "message": (
                        f"TODO/FIXME without issue reference at line {line_num}: "
                        f'"{_sanitize_untrusted_excerpt(todo_match.group(2))}"'
                    ),
                    "line": line_num,
                    "severity": "info",
                }
            )

    return issues


def _scan_secret_issues(
    content: str, lines: list[str], *, deadline: float, report_lines: bool = True
) -> list[dict]:
    """ファイル内容からハードコードされたシークレットを検出します。

    `# nosec`・ファイルサイズに関わらず全体を走査します。呼び出し元から渡された
    `deadline` を「行 × パターン」の粒度で確認し、超過したら `SecretScanBudgetExceeded`
    を送出します（呼び出し元で `scan_error` として fail-closed になる）。deadline を
    ここで作らないのは、ファイル単位の予算になるためです。

    Args:
        content: 検査対象のデコード済みファイル内容です（未使用ですが、
            呼び出し側とのインターフェース共有のため引数として残します）。
        lines: 走査単位のリストです。テキストファイルでは改行で分割済みの行リスト、
            バイナリでは `_extract_printable_runs` の抽出結果です。
        deadline: 走査を打ち切る `_monotonic()` 基準の時刻です
            （`new_secret_scan_deadline` の 1 本の予算）。
        report_lines: 走査単位が実際の行に対応するなら True。バイナリの抽出
            文字列は行に対応しないため False を渡し、行番号を報告しません。

    Returns:
        検出したシークレット問題の辞書リストを返します。

    Raises:
        SecretScanBudgetExceeded: 実時間バジェットを超過した場合。
    """
    issues = []
    budget = _ScanBudget(deadline)
    for index, line in enumerate(lines):
        line_num = index + 1 if report_lines else 0
        location = f"at line {line_num}" if report_lines else "in extracted binary content"
        for pattern, name in _SECRET_PATTERNS:
            budget.check(len(line), f"line {index + 1}")
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


def find_file_issues(file_path: str, *, repo_root: Path | None = None, deadline: float) -> list[dict]:
    """ファイル内容から代表的な問題を検出します。

    `repo_root` が None なら INDEX から、渡すと作業ツリー（`git commit -a` の未ステージ
    変更用）から読みます。lint チェックは `should_lint_file` が True でバイナリでない
    ファイルだけ、シークレット検出は `should_scan_secrets` が True ならバイナリでも
    （印字可能文字列を抽出して）必ず実施します。

    各段階は独立した例外ガードを持ち、想定外の例外は検査不能を示す `scan_error` issue に
    します（読み取り・判定・secret は error、lint は warning）。黙って issue が消える
    （＝検査したのに問題なしと区別が付かない）ことはありません。lint 側で予算が尽きた
    場合は error を積んで secret scan を回さずに返します。

    `deadline` に既定値を持たせないのは、渡し忘れた呼び出し元がファイル単位予算へ静かに
    退行し、型・lint・テストのいずれでも検知できなくなるためです。

    Args:
        file_path: 調査対象のファイルパスです。
        repo_root: 指定すると作業ツリーから読みます（`git commit -a` の
            未ステージ変更用）。None なら INDEX から読みます。
        deadline: 走査を打ち切る `_monotonic()` 基準の時刻です
            （`new_secret_scan_deadline()` の返り値）。

    Returns:
        検出した問題の辞書リストを返します。

    Raises:
        例外は発生しません。
    """
    try:
        content = (
            get_worktree_file_content(repo_root, file_path)
            if repo_root is not None
            else get_staged_file_content(file_path)
        )
    except Exception as err:
        # 読み取り関数は読めなければ None を返す契約だが、それが破られても本関数の
        # 「例外を発生させない」契約を守る。
        return [_scan_error_issue(file_path, "ファイル読み取り", err, severity="error")]
    if content is None:
        # 黙って空 issue を返すと「問題なし」と「検査不能」が区別できず、対象ファイルは
        # 無検査のままコミットが通る。scan_error（error）としてブロック対象にする。
        log(f"[Hook] scan_error: {file_path} の内容取得に失敗しました（読み取り不能または取得拒否）")
        return [
            {
                "type": "scan_error",
                "message": f"{file_path}: 内容を取得できなかったため検査できませんでした",
                "line": 0,
                "severity": "error",
            }
        ]

    try:
        is_binary = _is_binary_content(content)
        do_lint = should_lint_file(file_path) and not is_binary
        do_secrets = should_scan_secrets(file_path)
    except Exception as err:
        return [_scan_error_issue(file_path, "scan target 判定", err, severity="error")]

    issues: list[dict] = []
    if not do_lint and not do_secrets:
        return issues

    lines = content.split("\n")
    if do_lint:
        try:
            issues.extend(_scan_lint_issues(lines, deadline=deadline))
        except SecretScanBudgetExceeded as err:
            # 予算超過だけは lint でも error。共有の予算が尽きている以上、secret scan を
            # 回してもホスト側 timeout を踏むだけなので、検査不能として返す。
            issues.append(_scan_error_issue(file_path, "lint scan", err, severity="error"))
            return issues
        except Exception as err:
            issues.append(_scan_error_issue(file_path, "lint scan", err, severity="warning"))
    if do_secrets:
        scan_lines = _extract_printable_runs(content) if is_binary else lines
        try:
            issues.extend(
                _scan_secret_issues(content, scan_lines, deadline=deadline, report_lines=not is_binary)
            )
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
