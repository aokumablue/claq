"""scripts/install-dev.sh の「旧 symlink 回収案内」のテスト。

旧版の install-dev.sh は ``/usr/local/bin/{ruff,vulture}`` へ symlink を張って
いた。張り先は checkout 固有の ``<repo>/.venv/bin/*`` で、**ユーザーが書ける
パスを system-wide の名前へ露出させたまま**になる。作成を止めただけでは既に
張られた分が古い開発機に残り続け、しかも生きている symlink は ``command -v``
を成功させるので「venv の中だけにある」という新設の案内まで抑止される。
開発者は別 checkout の ruff を無自覚に実行し続けることになる。

ここで検査するのは回収案内だけ。venv 作成・pip install は副作用を持つので
``--skip-python`` で必ず止める（回収案内はその早期 exit より**前**に置いて
あり、そこが検査可能性の要）。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT = _REPO_ROOT / "scripts" / "install-dev.sh"
_BASH = "/bin/bash"

_WARNING_MARKER = "is a leftover symlink into a checkout venv"


def _run_installer(tmp_path: Path, *, path: str) -> subprocess.CompletedProcess[str]:
    """``install-dev.sh --skip-python`` を合成 PATH で実行する。

    Args:
        tmp_path: ``--repo-root`` に渡す合成リポジトリルート。
        path: 子プロセスへ渡す PATH の値。

    Returns:
        完了したプロセス。
    """
    env = {**os.environ, "PATH": path}
    # bash は絶対パスで呼ぶ。PATH を退化させる表が自分自身の起動に失敗するのを
    # 避けるためと、macOS の /bin/bash が 3.2（配列の落とし穴を持つ版）だから。
    return subprocess.run(
        [_BASH, str(_SCRIPT), "--skip-python", "--repo-root", str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def _make_bin_dir(tmp_path: Path) -> Path:
    """PATH に載せる合成 bin ディレクトリを作る。

    Args:
        tmp_path: 作成先の一時ディレクトリ。

    Returns:
        作成した bin ディレクトリ。
    """
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    return bin_dir


def _link_into_checkout_venv(bin_dir: Path, tmp_path: Path, tool: str, *, dangling: bool) -> Path:
    """checkout 内 venv を指す symlink を張る。

    Args:
        bin_dir: symlink を置くディレクトリ。
        tmp_path: 張り先の checkout を作る一時ディレクトリ。
        tool: ツール名（``ruff`` / ``vulture``）。
        dangling: 真なら張り先の実体を作らない（checkout 削除後を再現）。

    Returns:
        作成した symlink のパス。
    """
    target = tmp_path / "other-checkout" / ".venv" / "bin" / tool
    target.parent.mkdir(parents=True, exist_ok=True)
    if not dangling:
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o755)
    link = bin_dir / tool
    link.symlink_to(target)
    return link


def test_warns_about_a_live_symlink_into_a_checkout_venv(tmp_path: Path) -> None:
    """生きた旧 symlink を検出し、削除コマンドを提示すること。

    ここが H の本体。旧 symlink が**生きている**と ``command -v ruff`` は成功
    するので、`command -v` に紐づいた案内だけでは何も出ない。回収案内が
    その成否と独立していることを、実体のある symlink で固定する。
    """
    bin_dir = _make_bin_dir(tmp_path)
    link = _link_into_checkout_venv(bin_dir, tmp_path, "ruff", dangling=False)

    result = _run_installer(tmp_path, path=f"{bin_dir}:{os.environ['PATH']}")

    assert result.returncode == 0, result.stderr
    assert _WARNING_MARKER in result.stdout
    assert str(link) in result.stdout
    assert f"sudo rm -- '{link}'" in result.stdout


def test_warns_about_a_dangling_symlink(tmp_path: Path) -> None:
    """張り先の checkout が消えていても検出すること。

    `command -v` は dangling symlink を「見つからない」と扱うので、実体の
    有無で分岐すると、いちばん壊れている機械だけ警告が出ないことになる。
    """
    bin_dir = _make_bin_dir(tmp_path)
    link = _link_into_checkout_venv(bin_dir, tmp_path, "vulture", dangling=True)

    result = _run_installer(tmp_path, path=f"{bin_dir}:{os.environ['PATH']}")

    assert result.returncode == 0, result.stderr
    assert str(link) in result.stdout


def test_warns_for_every_path_entry_not_just_the_first(tmp_path: Path) -> None:
    """PATH の先頭に正規の ruff があっても、後方の旧 symlink を見落とさないこと。

    `command -v` は先頭 1 件しか返さない。venv を有効化した状態がまさにこれで、
    /usr/local/bin に残った旧 symlink が venv 側の実体に隠れる。全エントリを
    走査する実装であることをここで固定する。
    """
    good_bin = tmp_path / "venv-bin"
    good_bin.mkdir()
    real_ruff = good_bin / "ruff"
    real_ruff.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real_ruff.chmod(0o755)
    stale_bin = _make_bin_dir(tmp_path)
    link = _link_into_checkout_venv(stale_bin, tmp_path, "ruff", dangling=False)

    result = _run_installer(tmp_path, path=f"{good_bin}:{stale_bin}:{os.environ['PATH']}")

    assert result.returncode == 0, result.stderr
    assert str(link) in result.stdout


@pytest.mark.parametrize(
    ("label", "target_parts"),
    [
        ("Homebrew の実体", ("opt", "homebrew", "bin", "ruff")),
        ("venv 以外の共有ディレクトリ", ("srv", "tools", "bin", "ruff")),
    ],
)
def test_does_not_warn_about_unrelated_symlinks(
    tmp_path: Path, label: str, target_parts: tuple[str, ...]
) -> None:
    """checkout 内 venv を指していない symlink には触れないこと。

    Args:
        tmp_path: 合成ツリーを作る一時ディレクトリ。
        label: 失敗時に対象を読めるようにする表のラベル。
        target_parts: 張り先パスの構成要素。

    Returns:
        なし。
    """
    bin_dir = _make_bin_dir(tmp_path)
    target = tmp_path.joinpath(*target_parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o755)
    (bin_dir / "ruff").symlink_to(target)

    result = _run_installer(tmp_path, path=f"{bin_dir}:{os.environ['PATH']}")

    assert result.returncode == 0, result.stderr
    assert _WARNING_MARKER not in result.stdout, label


def test_does_not_warn_about_a_real_binary(tmp_path: Path) -> None:
    """symlink でない実体には触れないこと（venv 内の ruff そのもの）。"""
    bin_dir = _make_bin_dir(tmp_path)
    real = bin_dir / "ruff"
    real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real.chmod(0o755)

    result = _run_installer(tmp_path, path=f"{bin_dir}:{os.environ['PATH']}")

    assert result.returncode == 0, result.stderr
    assert _WARNING_MARKER not in result.stdout


@pytest.mark.parametrize(
    ("label", "path_template"),
    [
        ("先頭の空要素", ":{bin}"),
        ("末尾の空要素", "{bin}:"),
        ("連続する空要素", "{bin}::{bin}"),
        ("PATH が空", ""),
    ],
)
def test_survives_degenerate_path_values(tmp_path: Path, label: str, path_template: str) -> None:
    """空要素だけの PATH でもインストーラが落ちないこと。

    macOS の system bash は 3.2 で、`set -u` 下の `"${arr[@]}"` は**空配列で
    unbound variable** になる（実測）。配列で PATH を割る実装だと PATH が空の
    環境でインストーラごと落ちるため、その形に戻っていないことを固定する。

    Args:
        tmp_path: 合成ツリーを作る一時ディレクトリ。
        label: 失敗時に PATH の形を読めるようにする表のラベル。
        path_template: `{bin}` に合成 bin を埋める PATH のひな形。

    Returns:
        なし。
    """
    bin_dir = _make_bin_dir(tmp_path)

    result = _run_installer(tmp_path, path=path_template.format(bin=bin_dir))

    assert result.returncode == 0, f"{label}: {result.stderr}"
    assert "[claq] OK" in result.stdout, label


def test_reclamation_runs_before_the_skip_python_early_exit() -> None:
    """回収案内が `--skip-python` の早期 exit より前に置かれていること。

    後ろに置くと、副作用の無い唯一の実行モードから到達できず、テストが
    「ソースを grep する」形に退化する。上のテスト群が実際にこの経路を
    通っていることの構造的な裏づけとして、位置そのものを固定する。
    """
    lines = _SCRIPT.read_text(encoding="utf-8").splitlines()
    call = next(i for i, line in enumerate(lines) if line.strip() == "warn_stale_tool_symlinks")
    skip_exit = next(i for i, line in enumerate(lines) if 'if [[ "${SKIP_PYTHON}" == "1" ]]' in line)

    assert call < skip_exit
