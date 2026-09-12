"""claq-helpers.sh のテスト。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_HELPER = _REPO_ROOT / "plugins" / "claq" / "runtime" / "claq-helpers.sh"
_PLUGIN_ROOT = _REPO_ROOT / "plugins" / "claq"

_ZSH_AVAILABLE = shutil.which("zsh") is not None
_skip_without_zsh = pytest.mark.skipif(
    not _ZSH_AVAILABLE, reason="zsh が PATH 上に見つからないため zsh 依存テストをスキップする。"
)


def _run_bash(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """bash -lc でスクリプトを実行する。"""
    return subprocess.run(
        ["bash", "-lc", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _run_zsh(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """zsh -c でスクリプトを実行する（~/.zshrc を読み込まずマシン非依存にする）。"""
    return subprocess.run(
        ["zsh", "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _print_plugin_root_script(*, unset_env: bool = False, cd_to: Path | None = None) -> str:
    """helper を source して claq_plugin_root を 1 行出す。

    cd_to を渡すと、source する前にそのディレクトリへ cd してから実行する。
    リポジトリ外の cwd でも結果が変わらないことを検証するために使う。
    """
    unset = "unset CLAUDE_PLUGIN_ROOT\n" if unset_env else ""
    cd = f'cd "{cd_to}"\n' if cd_to is not None else ""
    return f'''
set -euo pipefail
{cd}{unset}source "{_HELPER}"
printf '%s\\n' "$(claq_plugin_root)"
'''


def _print_plugin_root_script_relative_source_then_cd(cd_to: Path) -> str:
    """相対パスで helper を source した後に別ディレクトリへ cd してから呼び出す。

    _REPO_ROOT で相対パス "plugins/claq/runtime/claq-helpers.sh" を
    source し、CLAUDE_PLUGIN_ROOT を unset した状態で cd_to へ cd してから
    claq_plugin_root を呼ぶ。fix 前のコードは BASH_SOURCE[0] を
    claq_plugin_root 呼び出し時に相対パスのまま dirname 解決していたため、
    source 後に cd すると cwd 起点で解決が壊れる（bash でも zsh でも）。
    fix 後のコードは source 時点でファイル先頭スコープの
    `_CLAQ_HELPERS_DIR` を絶対パスへ解決済みのため、この cd の影響を受けない。
    """
    return f'''
set -euo pipefail
cd "{_REPO_ROOT}"
unset CLAUDE_PLUGIN_ROOT
source "plugins/claq/runtime/claq-helpers.sh"
cd "{cd_to}"
printf '%s\\n' "$(claq_plugin_root)"
'''


def test_claq_plugin_root_relative_source_then_cd_survives_under_bash(tmp_path: Path) -> None:
    """bash: 相対パスで source した後に別ディレクトリへ cd しても正しい plugin root を返す。"""
    result = _run_bash(_print_plugin_root_script_relative_source_then_cd(tmp_path))

    assert result.stdout.strip() == str(_PLUGIN_ROOT)


@_skip_without_zsh
def test_claq_plugin_root_relative_source_then_cd_survives_under_zsh(tmp_path: Path) -> None:
    """zsh: 相対パスで source した後に別ディレクトリへ cd しても正しい plugin root を返す。"""
    result = _run_zsh(_print_plugin_root_script_relative_source_then_cd(tmp_path))

    assert result.stdout.strip() == str(_PLUGIN_ROOT)


def test_helpers_expose_no_unwatchdogged_background_runner() -> None:
    """ハードタイムアウトを持たない background runner を再導入しないこと（H-17）。

    `claq_run_bg` は `nohup ... &` だけで、CLAUDE.md が新規の外部呼び出しに
    義務付けるハードタイムアウトを持たなかった（`--bg` 経由の
    `hook_common.detach_process` が使う watchdog を通らないため、mem.db の
    ロック待ちに入ると無期限に残留する）。本番の呼び出し元はゼロだったので、
    「古いコードは必ず削除する」に従い関数ごと落とした。
    """
    text = _HELPER.read_text(encoding="utf-8")

    assert "claq_run_bg" not in text
    assert "nohup" not in text


def _print_root_with_handover(root: Path) -> str:
    """env.sh が渡す _CLAQ_SOURCED_ROOT 経由で root を出力するスクリプトを返す。"""
    return "\n".join(
        [
            "set -eu",
            f'_CLAQ_SOURCED_ROOT="{root}"',
            f'. "{_HELPER}"',
            "claq_plugin_root",
        ]
    )


def test_claq_plugin_root_ignores_ambient_claude_plugin_root(tmp_path: Path) -> None:
    """ambient な CLAUDE_PLUGIN_ROOT では source 済み helper の自己申告を上書きしない。

    helper 本体は env.sh が pointer から選んだ root から source されている。
    返す root だけ環境変数で差し替えると「実行しているコードは A なのに自称は B」
    という自己不整合になり、ADR-0008 の verified root という主張が崩れる。
    どの root を source するかを環境から決めるのは構わないが、source 後の
    自己申告を上書きしてはならない。
    """
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(tmp_path / "elsewhere")}
    result = _run_bash(_print_plugin_root_script(), env=env)

    assert result.stdout.strip() == str(_PLUGIN_ROOT)


def test_claq_plugin_root_uses_sourced_root_handed_over_by_env_sh(tmp_path: Path) -> None:
    """env-template.sh が渡す _CLAQ_SOURCED_ROOT を root として使うこと。

    POSIX sh は source されたファイルの位置を自力で解決できない（$0 はシェル名の
    ままになる）ため、env.sh 経由ではこの受け渡しが唯一の解決手段になる。
    """
    handed_over = tmp_path / "handed-over"
    (handed_over / "runtime").mkdir(parents=True)
    result = _run_bash(_print_root_with_handover(handed_over))

    assert result.stdout.strip() == str(handed_over)


@pytest.mark.skipif(shutil.which("dash") is None, reason="dash が無い環境")
def test_helpers_load_under_posix_dash(tmp_path: Path) -> None:
    """dash（POSIX sh）で source しても Bad substitution にならないこと。

    env-template.sh は `#!/usr/bin/env sh` を宣言しつつ本 helper を source する。
    bash 専用構文が残っていると、/bin/sh が dash である Linux で契約が壊れる。
    """
    handed_over = tmp_path / "posix-root"
    (handed_over / "runtime").mkdir(parents=True)
    result = subprocess.run(
        ["dash", "-c", _print_root_with_handover(handed_over)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )

    assert result.stderr == ""
    assert result.stdout.strip() == str(handed_over)


_STALE_ROOT_SHELLS = ["bash", *(["dash"] if shutil.which("dash") else []), *(["zsh"] if _ZSH_AVAILABLE else [])]


def _stale_root_script(root: Path, call: str) -> str:
    """存在しない root を handover して `call` を 1 回呼ぶスクリプトを返す。

    プラグイン更新でバージョン付きキャッシュディレクトリが差し替えられた後、
    既に env.sh を source 済みのシェルが残っている状況を再現する。

    Args:
        root: 実在しない plugin root。
        call: source 後に呼ぶコマンド行。

    Returns:
        シェルへ渡すスクリプト文字列。
    """
    return "\n".join(
        [
            "set -eu",
            f'_CLAQ_SOURCED_ROOT="{root}"',
            f'. "{_HELPER}"',
            call,
            "printf 'UNREACHABLE\\n'",
        ]
    )


@pytest.mark.parametrize("shell", _STALE_ROOT_SHELLS)
def test_claq_plugin_root_fails_loudly_when_the_sourced_root_is_gone(shell: str, tmp_path: Path) -> None:
    """stale root では空文字を返さず、理由を stderr へ出して非ゼロで返すこと（M-5）。

    旧実装は `cd` の失敗を検査せず空文字を返し、終了ステータスも 0 だった。
    その結果 `claq_run` が ``"/runtime/claq-hook"``（root が空のまま連結された
    絶対パス）を実行しようとし、プラグイン更新後のシェルでは以後すべての
    `claq_run` が原因と無関係なエラーになっていた。

    Args:
        shell: 実行するシェル名。
        tmp_path: pytest の一時ディレクトリ。
    """
    gone = tmp_path / "gone"
    result = subprocess.run(
        [shell, "-c", _stale_root_script(gone, "claq_plugin_root")],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert result.stdout.strip() == ""
    assert str(gone / "runtime") in result.stderr
    assert "UNREACHABLE" not in result.stdout


@pytest.mark.parametrize("shell", _STALE_ROOT_SHELLS)
def test_claq_run_does_not_execute_a_root_relative_path_when_root_is_gone(
    shell: str, tmp_path: Path
) -> None:
    """stale root では `claq_run` が root 相対パスの実行へ進まないこと（M-5）。

    「解決できなかった」ことを stale root の名前で伝えるのが目的なので、
    ``/runtime/claq-hook`` という誤ったパスに言及するエラーが出ていないことを
    合わせて固定する。これが出るなら空文字の連結が復活している。

    Args:
        shell: 実行するシェル名。
        tmp_path: pytest の一時ディレクトリ。
    """
    gone = tmp_path / "gone"
    result = subprocess.run(
        [shell, "-c", _stale_root_script(gone, "claq_run claq.mem.cli list")],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "/runtime/claq-hook" not in result.stderr
    assert str(gone / "runtime") in result.stderr
    assert "UNREACHABLE" not in result.stdout


def _mem_learn_script(title: str, body: str) -> str:
    """helper を source して `claq_mem_learn` を 1 回呼ぶスクリプトを組み立てる。

    値はシングルクォートで囲んで渡す（シェル側の展開を通さず、helper が
    JSON へ marshal する経路だけを見るため）。``_CLAQ_SOURCED_ROOT`` は
    env-template.sh が本番で渡すのと同じ値を明示する（POSIX sh には
    ``BASH_SOURCE`` 相当が無く、これが無いと helper 自身が自己位置を
    解決できずに読み込みを中止する契約になっている）。

    Args:
        title: 記録するカードのタイトル。
        body: 記録するカードの本文。

    Returns:
        シェルへ渡すスクリプト文字列。
    """
    return "\n".join(
        [
            f'_CLAQ_SOURCED_ROOT="{_PLUGIN_ROOT}"',
            "export _CLAQ_SOURCED_ROOT",
            f'. "{_HELPER}"',
            f"claq_mem_learn --kind fact --scope global --title '{title}' --body '{body}'",
        ]
    )


def _learned_card(data_dir: Path) -> object:
    """隔離 DB から最初の知識カードを 1 件返す。

    Args:
        data_dir: `CLAQ_DATA_PATH` に渡したディレクトリ。

    Returns:
        取得した Knowledge 行。無ければ None。
    """
    from claq.mem.database import Database

    with Database(data_dir / "mem.db") as db:
        rows = db.list_knowledge(scope="global", status="pending")
    return rows[0] if rows else None


@pytest.mark.parametrize("shell", ["bash", *(["dash"] if shutil.which("dash") else [])])
def test_claq_mem_learn_records_a_pending_agent_card(
    tmp_path: Path, shell: str
) -> None:
    """`claq_mem_learn` が隔離 DB へ pending / agent のカードを 1 件書くこと。

    marshal を `python3 -c` から `claq_run claq.mem.learn_payload` へ移したため、
    この経路は「helper 内の環境変数付きシェル関数呼び出し」と「wrapper 2 回の
    パイプ」の 2 つが同時に変わっている。値の引用符・改行・非 ASCII を
    エスケープなしで通せることが marshal を挟む理由そのものなので、
    それらを含む本文で検証する。dash が入っていればそちらでも同じ契約を見る。
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["CLAQ_DATA_PATH"] = str(data_dir)
    title = '引用符 "quoted" と非 ASCII ✓ を含むタイトル'
    body = "パイプ | と & と ; と改行を含む本文"

    result = subprocess.run(
        [shell, "-c", _mem_learn_script(title, body)],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        timeout=60,
    )

    assert result.stderr == ""
    card = _learned_card(data_dir)
    assert card is not None
    assert card.title == title
    assert card.body == body
    # ADR-0007 / H-01: helper 経由のカードは例外なく agent / pending。
    assert (card.source, card.status) == ("agent", "pending")


@pytest.mark.parametrize("bad", ["abc", "0", "-1", "3.5"])
def test_collect_skill_create_inputs_rejects_non_positive_commits(bad: str) -> None:
    """commits 引数が正整数でなければ exit 2 で拒否すること。"""
    script = "\n".join([f'. "{_HELPER}"', f'collect_skill_create_inputs "{bad}"'])
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False, timeout=30
    )

    assert result.returncode == 2


def test_collect_skill_create_inputs_applies_commits_to_both_git_log_calls(tmp_path: Path) -> None:
    """commits 引数が 2 回の git log 双方へ渡ること。

    2 回目だけ `-n 200` がハードコードされていたため引数が半分しか効かず、
    2 つのセクションが別々の範囲を説明していた。
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls.txt"
    git = fake_bin / "git"
    git.write_text(
        "\n".join(["#!/usr/bin/env bash", f'printf "%s\\n" "$*" >> "{calls}"', ""]),
        encoding="utf-8",
    )
    git.chmod(0o755)

    script = "\n".join(
        [
            f'PATH="{fake_bin}:$PATH"',
            f'. "{_HELPER}"',
            "collect_skill_create_inputs 7 >/dev/null",
        ]
    )
    _run_bash(script)

    recorded = calls.read_text(encoding="utf-8").splitlines()
    assert len(recorded) == 2
    assert all("-n 7" in line for line in recorded), recorded


_FREQUENCY_HEADER = "# ファイルごとのコミット頻度"


def _synthetic_repo(tmp_path: Path, paths: list[str]) -> Path:
    """指定パスを 1 コミットだけ含む合成 git リポジトリを作る。

    外側のリポジトリ設定・ユーザ設定を引き込まないよう、identity は
    コマンド単位の `-c` で与える。

    Args:
        tmp_path: 作成先の親ディレクトリ。
        paths: リポジトリ内に作るファイルの相対パス。

    Returns:
        作成したリポジトリのルート。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    identity = ["-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", "-q", str(repo)],
        check=True,
        capture_output=True,
    )
    for rel in paths:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", *identity, "commit", "-q", "-m", "seed"], cwd=repo, check=True, capture_output=True
    )
    return repo


def test_collect_skill_create_inputs_keeps_paths_that_start_with_hex_characters(
    tmp_path: Path,
) -> None:
    """頻度表が `a-f` / `0-9` 始まりのパスを落とさないこと（H-16）。

    短縮 SHA 行を落とす目的の `grep -v "^[a-f0-9]"` は、**16 進数字で始まる
    全パス**も落としていた。実測で `agents/` `commands/` `docs/` `conftest.py`
    が消え、`plugins/` `scripts/` は残るため欠落に気づけない。/skill-gen は
    「agents/commands はほとんど触られていない」という誤った頻度表を受け取る。

    先頭セクション（1 回目の git log）にも同じパスが出るため、判定は
    頻度セクション側だけに限定する。ここを全出力で見ると、バグを残したまま
    通ってしまう。
    """
    kept = "plugins/keep.py"
    dropped = ["agents/reviewer.md", "commands/review.md", "docs/guide.md", "conftest.py"]
    repo = _synthetic_repo(tmp_path, [*dropped, kept])

    result = subprocess.run(
        ["bash", "-c", "\n".join([f'. "{_HELPER}"', "collect_skill_create_inputs 5"])],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    assert _FREQUENCY_HEADER in result.stdout
    frequency_section = result.stdout.split(_FREQUENCY_HEADER, 1)[1]
    assert kept in frequency_section
    for path in dropped:
        assert path in frequency_section, f"頻度表から落ちている: {path}"


def test_collect_skill_create_inputs_omits_commit_headers_from_the_frequency_table(
    tmp_path: Path,
) -> None:
    """頻度表にコミット見出し行が混ざらないこと。

    SHA 行を grep で落とすのをやめ、空 `--pretty=format:` で最初から出力
    しない方式へ変えたため、「実データを削らない」と対になるこちら側も固定する。
    """
    repo = _synthetic_repo(tmp_path, ["plugins/keep.py"])

    result = subprocess.run(
        ["bash", "-c", "\n".join([f'. "{_HELPER}"', "collect_skill_create_inputs 5"])],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    frequency_section = result.stdout.split(_FREQUENCY_HEADER, 1)[1]
    assert "seed" not in frequency_section
    counted = [line.split(maxsplit=1)[1] for line in frequency_section.splitlines() if line.strip()]
    assert counted == ["plugins/keep.py"]


def test_claq_plugin_root_uses_file_location_fallback_with_env() -> None:
    """親環境から CLAUDE_PLUGIN_ROOT を除いた場合はファイル位置にフォールバックする。"""
    env = dict(os.environ)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    result = _run_bash(_print_plugin_root_script(), env=env)

    assert result.stdout.strip() == str(_PLUGIN_ROOT)


def test_claq_plugin_root_uses_file_location_fallback_without_env() -> None:
    """シェル内で CLAUDE_PLUGIN_ROOT を unset した場合もファイル位置にフォールバックする。"""
    result = _run_bash(_print_plugin_root_script(unset_env=True))

    assert result.stdout.strip() == str(_PLUGIN_ROOT)


@_skip_without_zsh
def test_claq_plugin_root_uses_file_location_fallback_with_env_under_zsh(tmp_path: Path) -> None:
    """zsh: 親環境から CLAUDE_PLUGIN_ROOT を除いても、リポジトリ外の cwd でファイル位置に解決する。"""
    env = dict(os.environ)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    result = _run_zsh(_print_plugin_root_script(cd_to=tmp_path), env=env)

    assert result.stdout.strip() == str(_PLUGIN_ROOT)


@_skip_without_zsh
def test_claq_plugin_root_uses_file_location_fallback_without_env_under_zsh(tmp_path: Path) -> None:
    """zsh: シェル内で CLAUDE_PLUGIN_ROOT を unset しても、リポジトリ外の cwd でファイル位置に解決する。"""
    result = _run_zsh(_print_plugin_root_script(unset_env=True, cd_to=tmp_path))

    assert result.stdout.strip() == str(_PLUGIN_ROOT)
