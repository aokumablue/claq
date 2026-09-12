"""保護フックを**別プロセスとして起動**し、exit code で block / allow を固定する。

`test_protection_bypass_regressions.py` は `main()` をインプロセスで呼ぶ。本ファイルは
`python3 -m claq.hooks.<module>` を実際に起動し、stdin へツール入力 JSON を流して
終了コードを観測する。ハーネスがフックを起動する経路そのものを通すため、
インプロセス呼び出しでは再現しない差（stdin 読み取り、モジュール実行時の初期化、
終了コードの伝播）まで含めて固定できる。

2026-09-05 の全体レビューで、`fail_under=100` を満たしスイートが全緑のまま
10 経路のバイパスが生きていた。保護語彙は複数行の set リテラル＝1 statement なので、
要素を消しても実行行が変わらずカバレッジでは検出できない。そのとき採取した
block / allow 表をここへ恒久化する。

`allow` 側を同じ表に置くのは、fail-closed へ寄せた判定が通常操作を巻き込んでいない
ことを同時に固定するため。片側だけ書くと「全部ブロックする」実装でも緑になる。

PreToolUse の終了コード契約: 2=deny / 0=allow / 1=non-blocking error（ツールは実行される）。
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

DENY = 2
"""PreToolUse がツール呼び出しを拒否する終了コード。"""

ALLOW = 0
"""PreToolUse がツール呼び出しを通す終了コード。"""


def _bash(module: str, label: str, command: str, expect: int) -> tuple[str, str, dict, int]:
    """Bash ツール入力 1 件を表の行にする。"""
    return (module, label, {"tool_name": "Bash", "tool_input": {"command": command}}, expect)


def _write(module: str, label: str, path: str, expect: int) -> tuple[str, str, dict, int]:
    """Write ツール入力 1 件を表の行にする。"""
    payload = {"tool_name": "Write", "tool_input": {"file_path": path, "content": "x"}}
    return (module, label, payload, expect)


_BLOCK_ALLOW_TABLE: tuple[tuple[str, str, dict, int], ...] = (
    # --- heredoc を開始しない << を演算子と誤認する穴（C-1） ---
    # コメント内・クォート内の << を演算子として採ると、後続の「実行される行」が
    # 本文として削除され、block_no_verify / bash_config_protection /
    # pre_bash_commit_quality の 3 種が揃って素通りしていた。
    _bash("claq.hooks.block_no_verify", "C-1 裸の --no-verify", "git commit --no-verify -m x", DENY),
    _bash("claq.hooks.block_no_verify", "C-1 コメント内 <<EOF", "# <<EOF\ngit commit --no-verify -m x\nEOF", DENY),
    _bash("claq.hooks.block_no_verify", "C-1 クォート内 <<EOF", 'echo "<<EOF"\ngit commit --no-verify -m x\nEOF', DENY),
    _bash("claq.hooks.block_no_verify", "C-1 コメント内 <<EOF + push", "# <<EOF\ngit push --no-verify\nEOF", DENY),
    # 陰性対照: 本物の heredoc 本文の中身は実行されないので保護対象ではない。
    _bash("claq.hooks.block_no_verify", "C-1 対 本物の heredoc", "cat <<EOF\ngit commit --no-verify\nEOF", ALLOW),
    # --- 大小文字の非対称（C-2） ---
    # APFS / NTFS は既定で大小を区別しないため、Ruff.toml への書込は実体 ruff.toml に当たる。
    _write("claq.hooks.config_protection", "C-2 Write 小文字", "ruff.toml", DENY),
    _write("claq.hooks.config_protection", "C-2 Write 先頭大文字", "Ruff.toml", DENY),
    _write("claq.hooks.config_protection", "C-2 Write 全大文字", "RUFF.TOML", DENY),
    _write("claq.hooks.config_protection", "C-2 対 無関係な Write", "app.py", ALLOW),
    _bash("claq.hooks.bash_config_protection", "C-2 rm 小文字", "rm ruff.toml", DENY),
    _bash("claq.hooks.bash_config_protection", "C-2 rm 先頭大文字", "rm Ruff.toml", DENY),
    _bash("claq.hooks.bash_config_protection", "C-2 対 無関係な rm", "rm app.py", ALLOW),
    # --- sed の長形式と実行 wrapper（H-5） ---
    _bash("claq.hooks.bash_config_protection", "H-5 sed -i", "sed -i s/x/y/ ruff.toml", DENY),
    _bash("claq.hooks.bash_config_protection", "H-5 sed --in-place", "sed --in-place s/x/y/ ruff.toml", DENY),
    _bash("claq.hooks.bash_config_protection", "H-5 対 sed --expression", "sed --expression s/x/y/ app.py", ALLOW),
    _bash("claq.hooks.bash_config_protection", "H-5 timeout 越し", "timeout 5 rm ruff.toml", DENY),
    _bash("claq.hooks.bash_config_protection", "H-5 nohup 越し", "nohup rm ruff.toml", DENY),
    _bash("claq.hooks.bash_config_protection", "H-5 nice 越し", "nice rm ruff.toml", DENY),
    _bash("claq.hooks.bash_config_protection", "H-5 env 越し", "env rm ruff.toml", DENY),
    # 陰性対照: 非実行位置の言及は allow のまま（M-01）。
    _bash("claq.hooks.bash_config_protection", "H-5 対 echo での言及", "echo tee pyproject.toml", ALLOW),
    # --- 保護語彙がサンプリングでしか検査されていなかった件（H-11） ---
    # 全 36 件の網羅は test_protection_bypass_regressions.py の parametrize が担う。
    # ここはプロセス起動経路での代表確認に絞る。
    _write("claq.hooks.config_protection", "H-11 .shellcheckrc", ".shellcheckrc", DENY),
    _write("claq.hooks.config_protection", "H-11 .markdownlintrc", ".markdownlintrc", DENY),
    _write("claq.hooks.config_protection", "H-11 .stylelintrc", ".stylelintrc", DENY),
    _write("claq.hooks.config_protection", "H-11 prettier.config.js", "prettier.config.js", DENY),
    _write("claq.hooks.config_protection", "H-11 eslint.config.mjs", "eslint.config.mjs", DENY),
    _bash("claq.hooks.bash_config_protection", "H-11 pushd 越し", "pushd sub && printf x > ../ruff.toml", DENY),
    _bash("claq.hooks.bash_config_protection", "H-11 popd 越し", "popd && printf x > ruff.toml", DENY),
    # --- フラグ無しで書き換えるエディタ（M-8） ---
    _bash("claq.hooks.pre_bash_commit_quality", "M-8 ed の commit 前編集", "ed app.py && git commit -am x", DENY),
    _bash("claq.hooks.bash_config_protection", "M-8 ed で保護 config", "ed ruff.toml", DENY),
    _bash("claq.hooks.pre_bash_commit_quality", "M-8 対 commit 無しの ed", "ed app.py", ALLOW),
    _bash("claq.hooks.bash_config_protection", "M-8 対 保護対象でない ed", "ed app.py", ALLOW),
    # --- touch による保護 config の空作成（M-9） ---
    # 内容は変えられないが、空の .eslintrc は ESLint の上位カスケード探索を止める。
    _bash("claq.hooks.bash_config_protection", "M-9 touch 保護 config", "touch ruff.toml", DENY),
    _bash("claq.hooks.bash_config_protection", "M-9 touch .eslintrc", "touch .eslintrc", DENY),
    _bash("claq.hooks.bash_config_protection", "M-9 対 touch app.py", "touch app.py", ALLOW),
    _bash("claq.hooks.bash_config_protection", "M-9 対 touch README.md", "touch README.md", ALLOW),
)


@pytest.mark.parametrize(
    ("module", "label", "payload", "expect"),
    _BLOCK_ALLOW_TABLE,
    ids=[row[1] for row in _BLOCK_ALLOW_TABLE],
)
def test_hook_process_exit_code(module: str, label: str, payload: dict, expect: int) -> None:
    """フックを別プロセスで起動し、表どおりの終了コードを返すこと。"""
    proc = subprocess.run(
        [sys.executable, "-m", module],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == expect, (
        f"{label}: {module} が exit {expect} を返さず {proc.returncode} を返した。"
        f" stdout={proc.stdout[:200]!r} stderr={proc.stderr[:200]!r}"
    )


def test_table_holds_both_directions() -> None:
    """表が block 側と allow 側の両方を持つこと。

    片側だけになると「全部ブロックする」実装でも「全部通す」実装でも緑になり、
    表が実効性の証拠でなくなる。
    """
    denies = [row for row in _BLOCK_ALLOW_TABLE if row[3] == DENY]
    allows = [row for row in _BLOCK_ALLOW_TABLE if row[3] == ALLOW]
    assert denies, "deny 側の行が 1 つも無い"
    assert allows, "allow 側の行が 1 つも無い"


def test_every_hook_in_table_has_an_allow_row() -> None:
    """deny 行を持つフックは allow 行も持つこと。

    あるフックについて deny だけを並べると、そのフックが全入力を拒否するよう
    退行しても表が緑のままになる。
    """
    deny_modules = {row[0] for row in _BLOCK_ALLOW_TABLE if row[3] == DENY}
    allow_modules = {row[0] for row in _BLOCK_ALLOW_TABLE if row[3] == ALLOW}
    missing = sorted(deny_modules - allow_modules)
    assert not missing, f"deny 行だけで allow 行を持たないフック: {missing}"
