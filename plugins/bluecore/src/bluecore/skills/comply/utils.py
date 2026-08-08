"""comply スクリプトで共有するユーティリティ。"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

_ALLOWED_SETUP_COMMANDS = frozenset({"mkdir", "touch", "echo", "printf"})
"""setup_commands で実行を許可するコマンド名。それ以外は ValueError で拒否する。"""

_DISALLOWED_TOKEN_CHARS = frozenset("|;&$`()<\n")
"""コマンド内の各トークンに含まれてはならないシェルメタ文字。

パイプ・セミコロン・変数展開・コマンド置換・サブシェル等、shlex.split では
解釈されずリテラルとして残る文字列を検出し、危険な組み合わせを拒否する。
"""


def extract_yaml(text: str) -> str:
    """LLM出力からYAMLを抽出し、Markdownフェンスがあれば除去する。"""
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)


def run_validated_setup_command(cmd: str, cwd: Path, timeout: float) -> None:
    """comply シナリオの setup_commands を1件だけ検証したうえで安全に実行する。

    LLMが生成した ``setup_commands`` はプロンプトインジェクションや誤生成を含みうるため、
    任意コマンドとして直接実行せず、安全な最小限の操作のみを許可する:
      - ``mkdir`` / ``touch``: サンドボックス配下のパスに対してのみサブプロセス実行する。
      - ``echo`` / ``printf``: ``>`` / ``>>`` によるファイル書き込みのみサポートする
        （リダイレクトはシェルの機能なので Python 側で解釈し、書き込みを直接行う）。

    ``shell=True`` は一切使用しない。危険なコマンド名・シェルメタ文字
    （``| ; & $ `` ` `` ( ) <`` 等）・サンドボックス外へのパストラバーサルを
    検出した場合は ``ValueError`` を送出する。

    Args:
        cmd: setup_commands の1コマンド文字列。
        cwd: コマンドを実行するサンドボックスディレクトリ（相対パスの基点）。
        timeout: mkdir/touch をサブプロセス実行する際のハードタイムアウト（秒）。

    Raises:
        ValueError: 許可外のコマンド・シェルメタ文字・パストラバーサルを検出した場合。
        subprocess.TimeoutExpired: mkdir/touch の実行がタイムアウトした場合。
    """
    tokens = shlex.split(cmd)
    if not tokens:
        raise ValueError("empty setup command")

    _reject_shell_metacharacters(tokens)

    command_name, args = tokens[0], tokens[1:]
    if command_name not in _ALLOWED_SETUP_COMMANDS:
        raise ValueError(f"disallowed setup command: {command_name!r}")

    if command_name in ("mkdir", "touch"):
        _run_path_command(command_name, args, cwd, timeout)
    else:
        _run_redirected_write(command_name, args, cwd)


def _reject_shell_metacharacters(tokens: list[str]) -> None:
    """トークン列にシェルメタ文字が含まれる場合は ValueError を送出する。"""
    for token in tokens:
        if any(ch in _DISALLOWED_TOKEN_CHARS for ch in token):
            raise ValueError(f"disallowed shell metacharacter in setup command token: {token!r}")


def _resolve_sandbox_path(path_str: str, cwd: Path) -> Path:
    """相対パス文字列を検証し、cwd配下に収まる絶対パスへ解決する。

    絶対パスや ``..`` によってサンドボックス外へ出るパスは ValueError とする。
    """
    if path_str.startswith("/") or path_str.startswith("~"):
        raise ValueError(f"absolute paths are not allowed in setup command: {path_str!r}")

    resolved_cwd = cwd.resolve()
    target = (resolved_cwd / path_str).resolve()
    try:
        target.relative_to(resolved_cwd)
    except ValueError as exc:
        raise ValueError(f"path escapes sandbox directory: {path_str!r}") from exc
    return target


def _run_path_command(command_name: str, args: list[str], cwd: Path, timeout: float) -> None:
    """mkdir/touch を検証済み引数でサブプロセス実行する（shell=Falseのまま個別引数で実行）。"""
    validated_args = []
    for arg in args:
        if arg.startswith("-"):
            validated_args.append(arg)
            continue
        _resolve_sandbox_path(arg, cwd)
        validated_args.append(arg)

    subprocess.run(
        [command_name, *validated_args],
        cwd=cwd,
        capture_output=True,
        timeout=timeout,
        shell=False,
    )


def _run_redirected_write(command_name: str, args: list[str], cwd: Path) -> None:
    """echo/printf の ``>``/``>>`` リダイレクトをPython側でファイル書き込みとして処理する。

    ``shell=True`` を使わずにリダイレクトを解釈するため、subprocessは呼ばない。
    リダイレクトを伴わない echo/printf は非対応として ValueError で拒否する。
    """
    if ">>" in args:
        redirect_index = args.index(">>")
        append = True
    elif ">" in args:
        redirect_index = args.index(">")
        append = False
    else:
        raise ValueError(f"{command_name} without redirection is not supported in setup commands")

    content_tokens = args[:redirect_index]
    target_tokens = args[redirect_index + 1 :]
    if len(target_tokens) != 1:
        raise ValueError("setup command redirection must target exactly one file")

    target_path = _resolve_sandbox_path(target_tokens[0], cwd)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    content = " ".join(content_tokens)
    if command_name == "echo":
        content += "\n"

    mode = "a" if append else "w"
    with target_path.open(mode, encoding="utf-8") as f:
        f.write(content)
