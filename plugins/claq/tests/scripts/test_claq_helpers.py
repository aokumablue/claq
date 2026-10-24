"""claq-helpers.sh のテスト。

デシジョンテーブル（claq_plugin_root）:
  - 相対パスで source → 別ディレクトリへ cd        → plugin root（bash / zsh）
  - ambient な CLAUDE_PLUGIN_ROOT が別の場所を指す → plugin root（上書きしない）
  - CLAUDE_PLUGIN_ROOT を親環境から除く / シェル内で unset → plugin root（bash / zsh は外部 cwd）
  - _CLAQ_SOURCED_ROOT を受け取る                  → その root（bash / dash）
  - _CLAQ_SOURCED_ROOT が消えた stale root         → 非 0 + stderr に理由（claq_run も同じ）
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_HELPER = _REPO_ROOT / "plugins" / "claq" / "runtime" / "claq-helpers.sh"
_PLUGIN_ROOT = _REPO_ROOT / "plugins" / "claq"
_FREQUENCY_HEADER = "# ファイルごとのコミット頻度"

_NEEDS_ZSH = pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh が PATH 上に無い")
_ZSH = pytest.param("zsh", marks=_NEEDS_ZSH)
_DASH = pytest.param("dash", marks=pytest.mark.skipif(shutil.which("dash") is None, reason="dash が PATH 上に無い"))


def _run(
    shell: str, *lines: str, env: dict[str, str] | None = None, cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """`lines` を改行で連結したスクリプトを `shell -c` で実行する（zsh も rc を読まずマシン非依存）。"""
    return subprocess.run(
        [shell, "-c", "\n".join(lines)], capture_output=True, text=True, env=env, cwd=cwd, check=check, timeout=60
    )


@pytest.mark.parametrize("shell", ["bash", _ZSH])
def test_claq_plugin_root_relative_source_then_cd_survives(shell: str, tmp_path: Path) -> None:
    """相対パスで source した後に別ディレクトリへ cd しても正しい plugin root を返す。

    BASH_SOURCE[0] を呼び出し時に相対パスのまま dirname 解決すると、source 後の cd で
    cwd 起点の解決が壊れる。helper は source 時点で `_CLAQ_HELPERS_DIR` を絶対パスへ解決済み。
    """
    result = _run(
        shell,
        "set -euo pipefail",
        f'cd "{_REPO_ROOT}"',
        "unset CLAUDE_PLUGIN_ROOT",
        'source "plugins/claq/runtime/claq-helpers.sh"',
        f'cd "{tmp_path}"',
        "claq_plugin_root",
    )

    assert result.stdout.strip() == str(_PLUGIN_ROOT)


@pytest.mark.parametrize(
    ("shell", "env_mode", "cwd"),
    [
        ("bash", "ambient", "inherited"),
        ("bash", "removed_from_parent", "inherited"),
        ("bash", "unset_in_shell", "inherited"),
        pytest.param("zsh", "removed_from_parent", "outside_repo", marks=_NEEDS_ZSH),
        pytest.param("zsh", "unset_in_shell", "outside_repo", marks=_NEEDS_ZSH),
    ],
)
def test_claq_plugin_root_resolves_from_file_location(shell: str, env_mode: str, cwd: str, tmp_path: Path) -> None:
    """CLAUDE_PLUGIN_ROOT の有無・値に関わらず、source したファイルの位置から root を返すこと。

    ambient な値で自己申告を上書きすると「実行しているコードは A なのに自称は B」という自己不整合になり、
    docs/adr/plugin-root-resolution.md の verified root という主張が崩れる。zsh はリポジトリ外の cwd で見る。
    """
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    if env_mode == "ambient":
        env["CLAUDE_PLUGIN_ROOT"] = str(tmp_path / "elsewhere")
    result = _run(
        shell,
        "set -euo pipefail",
        f'cd "{tmp_path}"' if cwd == "outside_repo" else ":",
        "unset CLAUDE_PLUGIN_ROOT" if env_mode == "unset_in_shell" else ":",
        f'source "{_HELPER}"',
        "claq_plugin_root",
        env=None if env_mode == "unset_in_shell" else env,
    )

    assert result.stdout.strip() == str(_PLUGIN_ROOT)


@pytest.mark.parametrize("shell", ["bash", _DASH])
def test_claq_plugin_root_uses_sourced_root_handed_over_by_env_sh(shell: str, tmp_path: Path) -> None:
    """env-template.sh が渡す _CLAQ_SOURCED_ROOT を root として使い、dash でも Bad substitution にならないこと。

    POSIX sh は source されたファイルの位置を自力で解決できない（$0 はシェル名のまま）ため、env.sh 経由では
    この受け渡しが唯一の解決手段になる。env-template.sh は `#!/usr/bin/env sh` なので、bash 専用構文が
    残ると /bin/sh が dash の Linux で契約が壊れる。
    """
    handed_over = tmp_path / "handed-over"
    (handed_over / "runtime").mkdir(parents=True)

    result = _run(shell, "set -eu", f'_CLAQ_SOURCED_ROOT="{handed_over}"', f'. "{_HELPER}"', "claq_plugin_root")

    assert result.stderr == ""
    assert result.stdout.strip() == str(handed_over)


def test_helpers_expose_no_unwatchdogged_background_runner() -> None:
    """ハードタイムアウトを持たない background runner を helper に置かないこと。

    `nohup ... &` だけの runner は、`--bg` 経由の `detach.detach_process` が使う watchdog を通らず、
    mem.db のロック待ちに入ると無期限に残留する（CLAUDE.md のハードタイムアウト義務に反する）。
    """
    text = _HELPER.read_text(encoding="utf-8")

    assert "claq_run_bg" not in text
    assert "nohup" not in text


@pytest.mark.parametrize("call", ["claq_plugin_root", "claq_run claq.mem.cli list"])
@pytest.mark.parametrize("shell", ["bash", _DASH, _ZSH])
def test_stale_root_fails_loudly(shell: str, call: str, tmp_path: Path) -> None:
    """プラグイン更新で source 元の root が消えたら、空文字を返さず理由を stderr へ出して非 0 で返すこと。

    `cd` の失敗を検査せず空文字と 0 を返すと、`claq_run` が ``/runtime/claq-hook``（空の root を連結した
    絶対パス）を実行しようとし、更新後のシェルで以後すべての `claq_run` が原因と無関係なエラーになる。
    """
    gone = tmp_path / "gone"

    result = _run(
        shell, "set -eu", f'_CLAQ_SOURCED_ROOT="{gone}"', f'. "{_HELPER}"', call, "printf 'UNREACHABLE\\n'", check=False
    )

    assert result.returncode != 0
    assert result.stdout.strip() == ""
    assert str(gone / "runtime") in result.stderr
    assert "/runtime/claq-hook" not in result.stderr


@pytest.mark.parametrize("shell", ["bash", _DASH])
def test_claq_mem_learn_records_a_pending_agent_card(tmp_path: Path, shell: str) -> None:
    """`claq_mem_learn` が隔離 DB へ pending / agent のカードを 1 件書くこと。

    marshal は `claq_run claq.mem.learn_payload` が担い、helper 内の環境変数付き関数呼び出しと wrapper 2 回の
    パイプを通る。引用符・改行・非 ASCII をエスケープなしで通せることが marshal を挟む理由なので、それらを
    含む値で見る。値はシングルクォートで渡し、シェル側の展開を通さない。
    """
    from claq.mem.database import Database

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    title = '引用符 "quoted" と非 ASCII ✓ を含むタイトル'
    body = "パイプ | と & と ; と改行を含む本文"

    result = _run(
        shell,
        # POSIX sh には BASH_SOURCE 相当が無いので、env-template.sh が本番で渡す値を明示する。
        f'_CLAQ_SOURCED_ROOT="{_PLUGIN_ROOT}"',
        "export _CLAQ_SOURCED_ROOT",
        f'. "{_HELPER}"',
        f"claq_mem_learn --kind fact --scope global --title '{title}' --body '{body}'",
        env={**os.environ, "HOME": str(tmp_path), "CLAQ_DATA_PATH": str(data_dir)},
    )

    assert result.stderr == ""
    with Database(data_dir / "mem.db") as db:
        rows = db.list_knowledge(scope="global", status="pending")
    assert [(card.title, card.body) for card in rows] == [(title, body)]
    # docs/adr/untrusted-input-prompt-boundary.md: helper 経由のカードは例外なく agent / pending。
    assert (rows[0].source, rows[0].status) == ("agent", "pending")


@pytest.mark.parametrize("bad", ["abc", "0", "-1", "3.5"])
def test_collect_skill_create_inputs_rejects_non_positive_commits(bad: str) -> None:
    """commits 引数が正整数でなければ exit 2 で拒否すること。"""
    result = _run("bash", f'. "{_HELPER}"', f'collect_skill_create_inputs "{bad}"', check=False)

    assert result.returncode == 2


def test_collect_skill_create_inputs_applies_commits_to_both_git_log_calls(tmp_path: Path) -> None:
    """commits 引数が 2 回の git log 双方へ渡ること（片方だけ `-n 200` 固定だと 2 節が別範囲を説明する）。"""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls.txt"
    git = fake_bin / "git"
    git.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{calls}"\n', encoding="utf-8")
    git.chmod(0o755)

    _run("bash", f'PATH="{fake_bin}:$PATH"', f'. "{_HELPER}"', "collect_skill_create_inputs 7 >/dev/null")

    recorded = calls.read_text(encoding="utf-8").splitlines()
    assert len(recorded) == 2
    assert all("-n 7" in line for line in recorded), recorded


def test_collect_skill_create_inputs_frequency_table_lists_every_path_and_no_commit_header(tmp_path: Path) -> None:
    """頻度表が 16 進数字始まりのパスを落とさず、コミット見出し行も混ぜないこと。

    短縮 SHA 行を落とす `grep -v "^[a-f0-9]"` は `agents/` `commands/` `docs/` `conftest.py` も落とし、
    `plugins/` は残るので欠落に気づけない。空 `--pretty=format:` で見出しを最初から出さない方式の対として、
    見出し（件名 seed）が混ざらないことも見る。先頭節（1 回目の git log）にも同じパスが出るので、判定は
    頻度節だけに限る。外側のリポジトリ・ユーザ設定を引き込まないよう identity はコマンド単位で与える。
    """
    paths = ["agents/reviewer.md", "commands/review.md", "docs/guide.md", "conftest.py", "plugins/keep.py"]
    repo = tmp_path / "repo"
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q", str(repo)], check=True, capture_output=True)
    for rel in paths:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "seed"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    result = _run("bash", f'. "{_HELPER}"', "collect_skill_create_inputs 5", cwd=repo)

    assert _FREQUENCY_HEADER in result.stdout
    frequency_section = result.stdout.split(_FREQUENCY_HEADER, 1)[1]
    assert "seed" not in frequency_section
    counted = [line.split(maxsplit=1)[1] for line in frequency_section.splitlines() if line.strip()]
    assert sorted(counted) == sorted(paths)
