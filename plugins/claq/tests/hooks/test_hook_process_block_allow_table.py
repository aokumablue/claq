"""保護フックを**別プロセスとして起動**し、exit code で block / allow を固定する。

`test_protection_bypass_regressions.py` は `main()` をインプロセスで呼ぶ。本ファイルは
`python3 -m claq.hooks.<module>` を実際に起動し、stdin へツール入力 JSON を流して
終了コードを観測する。ハーネスがフックを起動する経路そのものを通すため、
インプロセス呼び出しでは再現しない差（stdin 読み取り、モジュール実行時の初期化、
終了コードの伝播）まで含めて固定できる。

`fail_under=100` を満たしスイートが全緑でも、保護フックのバイパスは残りうる。
保護語彙は複数行の set リテラル＝1 statement なので、要素を消しても実行行が
変わらずカバレッジでは検出できない。そのため block / allow 表をここで固定する。

`allow` 側を同じ表に置くのは、fail-closed へ寄せた判定が通常操作を巻き込んでいない
ことを同時に固定するため。片側だけ書くと「全部ブロックする」実装でも緑になる。

PreToolUse の終了コード契約: 2=deny / 0=allow / 1=non-blocking error（ツールは実行される）。
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from ._support import _bash as _bash_payload
from ._support import _write as _write_payload

DENY = 2
"""PreToolUse がツール呼び出しを拒否する終了コード。"""

ALLOW = 0
"""PreToolUse がツール呼び出しを通す終了コード。"""


def _bash(module: str, label: str, command: str, expect: int) -> tuple[str, str, dict, int]:
    """Bash ツール入力 1 件を表の行にする。"""
    return (module, label, _bash_payload(command), expect)


def _write(module: str, label: str, path: str, expect: int) -> tuple[str, str, dict, int]:
    """Write ツール入力（内容は "x"）1 件を表の行にする。"""
    return (module, label, _write_payload(path), expect)


_BLOCK_ALLOW_TABLE: tuple[tuple[str, str, dict, int], ...] = (
    # --- heredoc を開始しない << を演算子と誤認する穴 ---
    # コメント内・クォート内の << を演算子として採ると、後続の「実行される行」が
    # 本文として削除され、block_no_verify / bash_config_protection /
    # pre_bash_commit_quality の 3 種が揃って素通りする。
    _bash("claq.hooks.block_no_verify", "裸の --no-verify", "git commit --no-verify -m x", DENY),
    _bash("claq.hooks.block_no_verify", "コメント内 <<EOF", "# <<EOF\ngit commit --no-verify -m x\nEOF", DENY),
    _bash("claq.hooks.block_no_verify", "クォート内 <<EOF", 'echo "<<EOF"\ngit commit --no-verify -m x\nEOF', DENY),
    _bash("claq.hooks.block_no_verify", "コメント内 <<EOF + push", "# <<EOF\ngit push --no-verify\nEOF", DENY),
    # 陰性対照: 本物の heredoc 本文の中身は実行されないので保護対象ではない。
    _bash("claq.hooks.block_no_verify", "対 本物の heredoc", "cat <<EOF\ngit commit --no-verify\nEOF", ALLOW),
    # --- 大小文字の非対称 ---
    # APFS / NTFS は既定で大小を区別しないため、Ruff.toml への書込は実体 ruff.toml に当たる。
    _write("claq.hooks.config_protection", "Write 小文字", "ruff.toml", DENY),
    _write("claq.hooks.config_protection", "Write 先頭大文字", "Ruff.toml", DENY),
    _write("claq.hooks.config_protection", "Write 全大文字", "RUFF.TOML", DENY),
    _write("claq.hooks.config_protection", "対 無関係な Write", "app.py", ALLOW),
    _bash("claq.hooks.bash_config_protection", "rm 小文字", "rm ruff.toml", DENY),
    _bash("claq.hooks.bash_config_protection", "rm 先頭大文字", "rm Ruff.toml", DENY),
    _bash("claq.hooks.bash_config_protection", "対 無関係な rm", "rm app.py", ALLOW),
    # --- sed の長形式と実行 wrapper ---
    _bash("claq.hooks.bash_config_protection", "sed -i", "sed -i s/x/y/ ruff.toml", DENY),
    _bash("claq.hooks.bash_config_protection", "sed --in-place", "sed --in-place s/x/y/ ruff.toml", DENY),
    _bash("claq.hooks.bash_config_protection", "対 sed --expression", "sed --expression s/x/y/ app.py", ALLOW),
    _bash("claq.hooks.bash_config_protection", "timeout 越し", "timeout 5 rm ruff.toml", DENY),
    _bash("claq.hooks.bash_config_protection", "nohup 越し", "nohup rm ruff.toml", DENY),
    _bash("claq.hooks.bash_config_protection", "nice 越し", "nice rm ruff.toml", DENY),
    _bash("claq.hooks.bash_config_protection", "env 越し", "env rm ruff.toml", DENY),
    # 陰性対照: 非実行位置の言及は allow のまま。
    _bash("claq.hooks.bash_config_protection", "対 echo での言及", "echo tee pyproject.toml", ALLOW),
    # --- 保護語彙の代表確認 ---
    # 全 36 件の網羅は test_protection_bypass_regressions.py の parametrize が担う。
    # ここはプロセス起動経路での代表確認に絞る。
    _write("claq.hooks.config_protection", ".shellcheckrc", ".shellcheckrc", DENY),
    _write("claq.hooks.config_protection", ".markdownlintrc", ".markdownlintrc", DENY),
    _write("claq.hooks.config_protection", ".stylelintrc", ".stylelintrc", DENY),
    _write("claq.hooks.config_protection", "prettier.config.js", "prettier.config.js", DENY),
    _write("claq.hooks.config_protection", "eslint.config.mjs", "eslint.config.mjs", DENY),
    _bash("claq.hooks.bash_config_protection", "pushd 越し", "pushd sub && printf x > ../ruff.toml", DENY),
    _bash("claq.hooks.bash_config_protection", "popd 越し", "popd && printf x > ruff.toml", DENY),
    # --- フラグ無しで書き換えるエディタ ---
    _bash("claq.hooks.pre_bash_commit_quality", "ed の commit 前編集", "ed app.py && git commit -am x", DENY),
    _bash("claq.hooks.bash_config_protection", "ed で保護 config", "ed ruff.toml", DENY),
    _bash("claq.hooks.pre_bash_commit_quality", "対 commit 無しの ed", "ed app.py", ALLOW),
    _bash("claq.hooks.bash_config_protection", "対 保護対象でない ed", "ed app.py", ALLOW),
    # --- touch による保護 config の空作成 ---
    # 内容は変えられないが、空の .eslintrc は ESLint の上位カスケード探索を止める。
    _bash("claq.hooks.bash_config_protection", "touch 保護 config", "touch ruff.toml", DENY),
    _bash("claq.hooks.bash_config_protection", "touch .eslintrc", "touch .eslintrc", DENY),
    _bash("claq.hooks.bash_config_protection", "対 touch app.py", "touch app.py", ALLOW),
    _bash("claq.hooks.bash_config_protection", "対 touch README.md", "touch README.md", ALLOW),
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


def test_every_hook_in_table_has_an_allow_row() -> None:
    """表が deny 行を持ち、deny 行を持つフックはどれも allow 行も持つこと。

    片側だけだと「全部ブロックする」実装でも「全部通す」実装でも緑になり、表が実効性の証拠でなくなる。
    あるフックについて deny だけを並べると、そのフックが全入力を拒否するよう退行しても表が緑のままになる。
    """
    deny_modules = {row[0] for row in _BLOCK_ALLOW_TABLE if row[3] == DENY}
    allow_modules = {row[0] for row in _BLOCK_ALLOW_TABLE if row[3] == ALLOW}
    assert deny_modules, "deny 側の行が 1 つも無い"
    assert deny_modules <= allow_modules, f"deny 行だけで allow 行を持たないフック: {sorted(deny_modules - allow_modules)}"
