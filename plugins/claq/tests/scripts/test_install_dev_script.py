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
    """``install-dev.sh --skip-python --repo-root <tmp_path>`` を合成 PATH で実行する。"""
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


def _write_exe(path: Path) -> Path:
    """`path` に何もしない実行可能スクリプトを置いて返す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.parametrize(
    ("tool", "dangling", "shadowed"),
    [("ruff", False, False), ("vulture", True, False), ("ruff", False, True)],
    ids=["live-symlink", "dangling-symlink", "behind-a-real-ruff-earlier-on-path"],
)
def test_warns_about_a_leftover_symlink_into_a_checkout_venv(
    tmp_path: Path, tool: str, dangling: bool, shadowed: bool
) -> None:
    """旧 symlink を生死・PATH 上の位置に関わらず検出し、削除コマンドを提示すること。

    - live: 生きた旧 symlink は ``command -v ruff`` を成功させるので、その成否に紐づいた案内では何も出ない。
    - dangling: `command -v` は dangling symlink を「見つからない」と扱うので、実体の有無で分岐すると
      いちばん壊れている機械だけ警告が出ない。
    - shadowed: `command -v` は先頭 1 件しか返さず、venv を有効化すると /usr/local/bin の旧 symlink が
      venv 側の実体に隠れる。全エントリを走査することを固定する。
    """
    stale_bin = tmp_path / "fake-bin"
    stale_bin.mkdir()
    target = tmp_path / "other-checkout" / ".venv" / "bin" / tool
    if dangling:
        target.parent.mkdir(parents=True)
    else:
        _write_exe(target)
    link = stale_bin / tool
    link.symlink_to(target)
    path = f"{stale_bin}:{os.environ['PATH']}"
    if shadowed:
        path = f"{_write_exe(tmp_path / 'venv-bin' / 'ruff').parent}:{path}"

    result = _run_installer(tmp_path, path=path)

    assert result.returncode == 0, result.stderr
    assert _WARNING_MARKER in result.stdout
    assert str(link) in result.stdout
    assert f"sudo rm -- '{link}'" in result.stdout


@pytest.mark.parametrize(
    "target_parts",
    [("opt", "homebrew", "bin", "ruff"), ("srv", "tools", "bin", "ruff"), None],
    ids=["homebrew-symlink", "non-venv-shared-dir-symlink", "real-binary"],
)
def test_does_not_warn_about_unrelated_ruff(tmp_path: Path, target_parts: tuple[str, ...] | None) -> None:
    """checkout 内 venv を指していない symlink や、symlink でない実体（venv 内の ruff そのもの）には触れないこと。"""
    ruff = tmp_path / "fake-bin" / "ruff"
    if target_parts is None:
        _write_exe(ruff)
    else:
        ruff.parent.mkdir()
        ruff.symlink_to(_write_exe(tmp_path.joinpath(*target_parts)))

    result = _run_installer(tmp_path, path=f"{ruff.parent}:{os.environ['PATH']}")

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
    """
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()

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
