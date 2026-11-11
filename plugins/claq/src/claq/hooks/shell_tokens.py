"""シェルコマンド文字列の解析（トークン化・区切り・ラッパー抽出・走査上限）。

保護フックが共有する、シェル文字列の解析と実行トークン名の正規化だけを持つ葉モジュール。
出力や保護対象の判定は ``hook_common`` が持ち、このモジュールは ``hook_common`` を import しない。
"""

from __future__ import annotations

import re
import shlex
from typing import NamedTuple

# 1 コマンドに許すバイト数の上限。
# `shlex` の走査コストは単一の巨大トークンでバイト数の二次になり、1MB で hooks.json の timeout（15 秒）に
# 迫る。host が kill した hook は exit code を返さず silent fail-open になるため、超過は走査せず BLOCKED で
# 返す（docs/adr/hook-failure-direction.md）。64KB の 1 トークンは起動から終了まで 0.2 秒（実測）。
# この上限の内側では、エージェントが作れる形（単一のコマンド・argv 配列）で 3 つの保護フックとも
# 起動から終了まで 1.1 秒以下（bash_config_protection の書込み先を変えた繰り返しが最も遅い。macOS・Python 3.14 で実測）。
MAX_COMMAND_BYTES = 64 * 1024

# コマンド全体をセグメントに割るシェル区切りトークン。区切り文字に密着した
# トークン（``status;echo``）を全フックが同じ規則で割るため 1 箇所に持つ。
SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")"})

# ``sh -c`` 再帰と heredoc 本文判定で共有する既知シェル実行ファイル（`normalize_executable_name` で畳んで判定）。
# 別集合を持つと「本文が実行されるか」の判定軸が 2 つに割れる。
SHELL_WRAPPER_EXECUTABLES = frozenset({"sh", "bash", "zsh", "dash"})


# ``#`` を行コメントの開始として扱ってよい直前の文字。POSIX シェルは語の先頭に
# ある ``#`` だけをコメント開始とみなす（``echo a#b`` の ``#`` は語の一部）。
# 構文として働いたときだけ語を区切る（``echo \;#x`` の ``;`` は語の一部）ので、`QuoteState` が見る。
COMMENT_PRECEDING_CHARS = frozenset(" \t\n;|&()<>")

# ``-c`` を含む結合短フラグ（``bash -lc 'cmd'``）。単独の ``-c`` だけを見ると
# ``bash -lc`` でラッパー再帰が不発になり、保護フックが素通りする。
_SHELL_COMMAND_FLAG_RE = re.compile(r"-[A-Za-z]*c[A-Za-z]*")


def basename_any_separator(path: str) -> str:
    """`/` と `\\` の**両方**を区切りとみなして basename を取り出します。

    `hook_common.basename` は POSIX ホストでは `\\` を区切りとみなさない。クォートで守られた
    Windows 絶対パス（``"C:\\bin\\rm.exe" ruff.toml``）は方言展開でも `\\` を保った
    まま 1 トークンで残るため、ここで両方の区切りを扱う。

    Args:
        path: パスまたは実行トークンの文字列です。

    Returns:
        両方の区切りで切り出した末尾要素を返します。

    Raises:
        例外は発生しません。
    """
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def normalize_executable_name(token: str) -> str:
    """実行トークンを比較用の名前（basename 化・大小無視・`.exe` 除去）へ正規化します。

    `/usr/bin/git`・`git.exe`・`C:\\Program Files\\Git\\bin\\git.exe`・`GIT` を
    いずれも `git` へ畳む。大小を無視するのは、APFS / NTFS が既定で大小を区別せず、
    PowerShell が cmdlet 名を大小無視で解決するため（Linux での誤検出は
    docs/adr/shell-analysis-boundary.md の範囲内）。保護 hook の実行名照合は
    この 1 関数だけを通し、兄弟フック間で正規化がずれないようにする。

    Args:
        token: `shlex` 等でトークン化された 1 トークンです。

    Returns:
        basename を大小無視へ畳み、末尾の `.exe` を除いた名前を返します。

    Raises:
        例外は発生しません。
    """
    name = basename_any_separator(token).casefold()
    return name[: -len(".exe")] if name.endswith(".exe") else name


def is_shell_wrapper_token(token: str) -> bool:
    """トークンが既知シェル実行ファイルかを `normalize_executable_name` で畳んだ名前で判定する。

    Args:
        token: 判定対象のトークン。

    Returns:
        畳んだ名前が `SHELL_WRAPPER_EXECUTABLES` に属するなら True。

    Raises:
        例外は発生しません。
    """
    return normalize_executable_name(token) in SHELL_WRAPPER_EXECUTABLES


class ShellWrapperCall(NamedTuple):
    """1 セグメント内の既知シェル ``-c`` 呼び出し。

    Attributes:
        prefix: 最初のシェル起動トークンより前のトークン列。``HOME=/tmp/h sh -c '...'`` や
            ``env HOME=/tmp/h sh -c '...'`` の ``HOME=/tmp/h`` は内側のコマンドにも環境として渡る。
            ``-c`` の文字列より後ろの位置引数（``sh -c '...' HOME=/tmp/h``）は環境ではないため含めない。
        command: ``-c`` に続く、ラップされた文字列コマンド。
    """

    prefix: tuple[str, ...]
    command: str


def extract_shell_wrapper_call(segment: list[str]) -> ShellWrapperCall | None:
    """セグメント内の既知シェル ``-c`` 呼び出しを取り出す。

    既知シェルのトークンに続く ``-c`` を含む短フラグを探し、その次のトークンをコマンドとする。
    `block_no_verify` と `bash_config_protection` が共有する。片方だけがラッパー再帰を
    持つと、同じコマンドが片方のフックだけを素通りする。再帰の段数は呼び出し側が
    決める（docs/adr/shell-analysis-boundary.md: 2 段以上のネストは非目標）。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        シェル起動より前のトークン列とラップされた文字列コマンド。見つからなければ None。

    Raises:
        例外は発生しません。
    """
    # 後ろのシェルトークンから見る範囲は最初のシェルトークンから見る範囲に含まれるので、最初の 1 つだけ見る。
    start = next((i for i, token in enumerate(segment) if is_shell_wrapper_token(token)), None)
    if start is None:
        return None
    for position in range(start + 1, len(segment) - 1):
        if _SHELL_COMMAND_FLAG_RE.fullmatch(segment[position]):
            return ShellWrapperCall(tuple(segment[:start]), segment[position + 1])
    return None


class QuoteState:
    """シェルのクォートと ``\\`` エスケープの状態を 1 文字ずつ進める。

    コメント除去・行継続の連結・改行の置換・heredoc 演算子の検出は、どれも同じ規則で
    「クォート内か」を決める。規則が走査ごとにずれると、片方の走査だけがクォートを読み違えて
    保護フックの判定が割れるため、遷移をここ 1 か所に持つ。

    Attributes:
        quote: 未閉鎖のクォート文字。クォート外なら None。
        escaped: 直前の文字が ``\\`` エスケープで、次の文字がその対象になるなら True。
        at_word_start: 次の文字が語の先頭にあるなら True。構文として働いた区切り文字の直後だけが
            語の先頭で、エスケープ・クォートで消費された文字は語の一部になる。行継続は改行ごと
            消えるので、その前の値を保つ。
    """

    __slots__ = ("at_word_start", "escaped", "quote")

    def __init__(self, quote: str | None = None) -> None:
        """状態を作る。

        Args:
            quote: 開始時点の未閉鎖クォート文字。無ければ None。
        """
        self.quote = quote
        self.escaped = False
        self.at_word_start = quote is None

    def advance(self, char: str) -> bool:
        """1 文字読んで状態を進め、その文字がシェルの構文として働くかを返す。

        単一クォート内の ``\\`` はエスケープにならない。

        Args:
            char: 次の 1 文字。

        Returns:
            クォート外の、エスケープされていない文字なら True。``\\`` とクォート文字自体は
            状態を変えるだけなので False。

        Raises:
            例外は発生しません。
        """
        if self.escaped:
            self.escaped = False
            self.at_word_start = self.at_word_start and char == "\n"
            return False
        if char == "\\" and self.quote != "'":
            self.escaped = True
            return False
        if self.quote is not None:
            if char == self.quote:
                self.quote = None
            return False
        if char in ("'", '"'):
            self.quote = char
            self.at_word_start = False
            return False
        self.at_word_start = char in COMMENT_PRECEDING_CHARS
        return True


def strip_line_comments(command: str) -> str:
    """クォート外の ``#`` 行コメントを行末まで取り除く。

    ``shlex`` にコメント処理を任せると、改行を ``;`` へ正規化した後の文字列では
    ``#`` 以降が**入力末尾まで**捨てられ、2 行目以降が未検査になる。``shlex`` は
    語中の ``#`` もコメント開始として扱うため ``echo a#b && git commit --no-verify``
    も素通りする。そこで正規化の前段で POSIX 規則（語の先頭の ``#`` だけ）に従って
    自分で落とし、``shlex`` 側のコメント処理は無効化する。

    クォートが閉じていない入力ではクォート内と判定したまま末尾に達するため、
    コメントは剥がされず検査対象に残る（fail-closed 側）。

    Args:
        command: 元のコマンド文字列。

    Returns:
        行コメントを除去した文字列。改行は保持する。

    Raises:
        例外は発生しません。
    """
    if "#" not in command:
        return command

    result: list[str] = []
    state = QuoteState()
    in_comment = False
    for char in command:
        if in_comment:
            # コメント中。区切りとして働く行末の改行だけを残す。
            if char != "\n":
                continue
            in_comment = False
            state.at_word_start = True
        else:
            at_word_start = state.at_word_start
            if state.advance(char) and char == "#" and at_word_start:
                in_comment = True
                continue
        result.append(char)
    return "".join(result)


def _strip_line_continuations(command: str) -> str:
    """クォート外・二重クォート内の行継続（``\\`` + 改行）を除去する。

    除去しないと ``shlex(posix=True)`` が改行を次トークンの先頭文字として吸収し、
    継続行が 0 桁目から始まるときにその先頭語が別トークンへ化ける（``git commit \\``
    + 改行 + ``--no-verify`` が素通りする）。単一クォート内では行継続は成立しない
    ので触らない。

    `strip_line_comments` の**後**に適用すること。前に置くと ``# foo \\`` + 改行 +
    ``git commit --no-verify`` が 1 行のコメントへ畳まれ、2 行目が消える（シェルは
    コメント中の ``\\`` を行継続として扱わない）。

    Args:
        command: 検査対象のコマンド文字列。

    Returns:
        行継続を連結した文字列。

    Raises:
        例外は発生しません。
    """
    if "\\\n" not in command:
        return command

    result: list[str] = []
    state = QuoteState()
    for char in command:
        if state.escaped and char == "\n":
            # 行継続。直前に積んだ ``\\`` ごと落とす。
            result.pop()
        else:
            result.append(char)
        state.advance(char)
    return "".join(result)


def _replace_unquoted_newlines(command: str) -> str:
    """クォート外の改行をシェル区切り ``;`` へ置き換える。

    改行はシェルにとって ``;`` と等価だが、``shlex`` は whitespace として消費する
    ため、複数行コマンドが 1 セグメントへ融合してセグメント境界に依存する判定が
    すべて不発になる。クォート内の改行と ``\\`` でエスケープされた改行は置き換えない。

    Args:
        command: 元のコマンド文字列。

    Returns:
        クォート外の改行を ``;`` へ置き換えた文字列。

    Raises:
        例外は発生しません。
    """
    if "\n" not in command:
        return command

    state = QuoteState()
    result = []
    for char in command:
        result.append(";" if state.advance(char) and char == "\n" else char)
    return "".join(result)


# Windows 読みでパス区切り ``/`` へ変換するバックスラッシュ（直後が空白と ``/`` 以外）。
_WINDOWS_SEPARATOR_BACKSLASH_RE = re.compile(r"\\(?=[^\s/])")

# Windows 読みで除去するバックスラッシュ（直後が空白）。エスケープを持たない
# PowerShell / cmd では ``rm my\\ ruff.toml`` は 2 引数で ``ruff.toml`` が実際に
# 削除されるため、検出側へ倒す。``/`` へ置換しないのは実在しない ``my/`` を作らないため。
_WINDOWS_ESCAPED_SPACE_RE = re.compile(r"\\(?=\s)")


def command_dialect_variants(command: str) -> tuple[str, ...]:
    """1 つのコマンド文字列を、POSIX 読みと Windows 読みの 2 方言へ展開する。

    ``shlex(posix=True)`` はクォート外の ``\\`` をエスケープとして消費するが、
    Windows の実行シェルでは ``\\`` はパス区切りである。どちらの方言で実行されるかは
    hook の入力から決められないため、両方の読み方で検査し、どちらかが引っ掛かれば
    deny する（docs/adr/shell-analysis-boundary.md）。

    Windows 読みは ``\\`` の直後の文字で分ける: パス構成文字なら ``/`` へ置換、
    空白 / タブなら ``\\`` を除去、``/`` なら変換しない（``\\/`` は sed 等の POSIX
    エスケープで、Windows のパス区切りには現れない）。

    `block_no_verify` と `bash_config_protection` が共有する。「何をパス区切りと
    みなすか」を 2 箇所に持つと、片方だけ強化される非対称が生じる。

    Args:
        command: 検査対象のコマンド文字列。

    Returns:
        検査すべき読み方のタプル。Windows 読みが元と同じなら 1 つだけ。

    Raises:
        例外は発生しません。
    """
    windows_reading = _WINDOWS_ESCAPED_SPACE_RE.sub("", _WINDOWS_SEPARATOR_BACKSLASH_RE.sub("/", command))
    if windows_reading == command:
        return (command,)
    return (command, windows_reading)


def tokenize(command: str) -> list[str]:
    """シェルコマンドを区切り記号込みのトークン列へ分割する。

    詳細は `tokenize_with_status` を参照。

    Args:
        command: 対象のシェルコマンド文字列。

    Returns:
        トークンのリスト。

    Raises:
        例外は発生しません。
    """
    return tokenize_with_status(command)[0]


def command_exceeds_scan_budget(command: str) -> bool:
    """コマンドが検査予算（バイト数）を超えるか判定する。

    呼び出し元は True を受けたら走査せず BLOCKED を返すこと（理由は `MAX_COMMAND_BYTES` の
    コメント）。``sh -c`` の中身は外側のトークンから引用符・エスケープを除いた文字だけでできて
    いて外側より長くならないので、入口で 1 回検査すれば再帰先も縛れる。

    Args:
        command: 検査対象のシェルコマンド文字列。

    Returns:
        UTF-8 のバイト数が上限を超えるなら True。

    Raises:
        例外は発生しません。
    """
    return len(command.encode("utf-8", "surrogatepass")) > MAX_COMMAND_BYTES


def scan_budget_message(hook_name: str, subject: str) -> str:
    """`command_exceeds_scan_budget` が True のときの deny 理由を組み立てる。

    検査予算で止める保護 hook が同じ文面を使うための単一情報源。

    Args:
        hook_name: 呼び出し元 hook の識別名。
        subject: 通してはならない対象の名詞（``command`` / ``commit``）。

    Returns:
        deny 理由の文字列。

    Raises:
        例外は発生しません。
    """
    return (
        f"[Hook] BLOCKED: command exceeds {MAX_COMMAND_BYTES} bytes for {hook_name}. "
        "A command this large cannot be inspected within the hook timeout, and an un-inspected "
        f"{subject} must not be allowed. Split it into smaller commands."
    )


# ファイルへ書き込むリダイレクト演算子。``1>``/``2>`` 等は数字が別トークンに分かれ ``>``/``>>``
# に一致するため不要だが、意図の記録として明示している。``&>>`` は stdout+stderr の追記、``>&`` は
# ``&>`` の別綴り（``>&file``）で、どちらも `tokenize` が 1 トークンにする。``>&2`` / ``2>&1`` の
# ような fd 複製も同じ ``>&`` で、書き込み先かどうかは後続の語で利用側が判断する。
# ``<>`` は読み書きオープンで、対象が無ければ作成する。
# `bash_config_protection` と `commit_risk` が共有する。判定軸が兄弟ごとに割れると、
# 片方だけずれた語彙を持って静かなバイパスになる。
WRITE_REDIRECT_OPERATORS = frozenset({">", ">>", "&>", "&>>", ">&", ">|", "<>", "1>", "2>", "1>>", "2>>"})

# `shlex` が解析できなかった入力で、空白分割の前に前後へ空白を入れる区切り演算子。
# 長い演算子を先に置いて最長一致にする。1 文字ずつ割ると ``>>ruff.toml`` が
# ``> > ruff.toml`` になり、リダイレクト先の判定がずれて新しい迂回口になる。
# ``1>`` / ``2>`` / ``1>>`` / ``2>>`` は語頭の fd 指定だけを 1 単位にする（``a2>x`` の ``a2``
# は割らない）。``>&`` / ``<&``（fd 複製）は 1 単位で、``2>&1`` の ``&`` を区切りとして読むと
# 後続の語が別セグメントへ切り離される。``<>``（読み書きオープン。無ければ作成する）も
# 1 単位にし、`WRITE_REDIRECT_OPERATORS` の ``<>`` と単位を揃える。
#
# 解析成功経路でも、記号だけからなるトークンをこの単位で割る（`_split_operator_run`）。
_FALLBACK_OPERATOR_RE = re.compile(
    r"(&>>|(?<!\S)[12]>>|&>|(?<!\S)[12]>(?!&)|>>|>\||>&|<&|<>|&&|\|\||[;|&()<>])"
)

# `shlex(punctuation_chars=True)` が 1 トークンにまとめる記号だけの語。
_OPERATOR_RUN_RE = re.compile(r"[;|&()<>]+")


# 空白分割へ倒れたトークンから取り除く引用符文字。`shlex` が解析できない入力では引用符が
# トークンに残り、保護名の照合（basename 比較）やコマンド名・フラグの照合が
# ``"ruff.toml"`` / ``"git"`` / ``"--no-verify"`` に一致しない。
_FALLBACK_QUOTE_CHARS_RE = re.compile(r"[\"']")


def _split_operator_run(token: str) -> list[str]:
    """記号だけからなるトークンを演算子の最長一致単位へ割る。

    ``punctuation_chars=True`` の `shlex` は隣接する記号を 1 トークンにまとめるため、
    ``true;(rm ruff.toml)`` の ``;(``・``(printf x)>f`` の ``)>``・``echo x|&tee f`` の ``|&``
    が区切り・リダイレクトの完全一致をすり抜ける。フォールバック経路と同じ
    `_FALLBACK_OPERATOR_RE` の単位で割り、``>>`` ``>&`` ``&>>`` 等はそのまま 1 単位に保つ。

    `shlex` の出力からは引用符の有無が分からないため、``';('`` のような引用された
    記号だけの語も割る。区切りとして読むと後ろの引数が別セグメントへ落ちるので、見逃し側に
    倒れる。これは引用された 1 文字の区切り（``';'``）を `shlex` が区切りとして返す既存の
    境界と同じもので、ここで新しく開いた口ではない。

    Args:
        token: `shlex` が返した 1 トークン。

    Returns:
        記号だけの語なら演算子単位のリスト、それ以外は ``[token]``。

    Raises:
        例外は発生しません。
    """
    if not _OPERATOR_RUN_RE.fullmatch(token):
        return [token]
    return _FALLBACK_OPERATOR_RE.findall(token)


def tokenize_with_status(command: str) -> tuple[list[str], bool]:
    """シェルコマンドをトークン化し、``shlex`` で解析し切れたかどうかも返す。

    ``punctuation_chars=True`` で、空白なしで連結された区切り記号
    （``git add -A&&git commit``）も独立トークンにする。`shlex` がまとめた記号だけの語
    （``;(`` 等）は `_split_operator_run` で演算子単位へ割る。コメント除去・行継続の連結・
    クォート外改行の ``;`` 化を先に行う。

    クォート不整合で ``ValueError`` になる入力（bash では有効な ``$'\\''`` を含む）は、
    区切り演算子（`_FALLBACK_OPERATOR_RE`）の前後に空白を入れてから空白分割する。
    これで ``echo $'\\'';git commit`` の ``;`` や ``>ruff.toml`` の ``>`` が語に埋もれず、
    クォート内容もフラグとして走査される。ただしクォートの対応は取らないため、本物の
    クォート内にある区切り記号まで割る。割るのは区切りの前後だけで、危険語が見えた
    こと自体では deny しない（読み取りコマンドを巻き込むため）。分割後は各トークンから
    ``"`` と ``'`` の文字を取り除く（トークンの個数と位置は変えない。位置ベースの書き込み先
    判定がそのまま効く）。解析できた入力のトークン列はこのフォールバックの影響を受けない。

    解析できたかを返すのは、生文字列への正規表現フォールバックを「解析できなかった
    ときだけ」に限定するため。解析できた入力にまで当てると ``echo "git commit"``
    のような引用文まで実行命令と誤認する。

    Args:
        command: 対象のシェルコマンド文字列。

    Returns:
        (トークンのリスト, ``shlex`` が最後まで解析できたか) のタプル。

    Raises:
        例外は発生しません。
    """
    normalized = _replace_unquoted_newlines(_strip_line_continuations(strip_line_comments(command)))
    lexer = shlex.shlex(normalized, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    # コメントは `strip_line_comments` が落とし済み。既定の ``#`` のままだと、
    # 改行を失った文字列で ``#`` 以降が入力末尾まで捨てられる。
    lexer.commenters = ""
    try:
        return [unit for token in lexer for unit in _split_operator_run(token)], True
    except ValueError:
        spaced = _FALLBACK_OPERATOR_RE.sub(r" \1 ", normalized).split()
        return [_FALLBACK_QUOTE_CHARS_RE.sub("", token) for token in spaced], False


def split_segments(tokens: list[str]) -> list[list[str]]:
    """トークン列をシェル区切りごとのセグメントへ分割する。

    Args:
        tokens: `tokenize` が返したトークン列。

    Returns:
        区切りトークンを含まないセグメント（トークンリスト）のリスト。
        空セグメントは除外します。

    Raises:
        例外は発生しません。
    """
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token not in SHELL_SEPARATORS:
            current.append(token)
            continue
        if current:
            segments.append(current)
        current = []
    if current:
        segments.append(current)
    return segments
