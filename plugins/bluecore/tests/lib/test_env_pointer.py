"""bluecore.lib.env_pointer のテスト。

``write_env_pointer`` の Python 側ロジック（roots/ の生成・古いエントリの刈り取り・
失敗時の握り潰し）に加え、生成された ``env.sh`` を実際に shell で source し、
PATH 由来 root / PPID 別 root / latest フォールバック / 全滅時の 127 を
実挙動として検証する（``env-template.sh`` は shell ファイルのため `--cov` の対象外
であり、テキスト assert だけでは分岐が検証されない）。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from bluecore.lib import env_pointer as mod
from bluecore.lib.constants import BASE_DIR_NAME

_ENV_TEMPLATE = (
    Path(__file__).resolve().parents[2] / "runtime" / "env-template.sh"
)


def _make_plugin_root(root: Path) -> Path:
    """``runtime/bluecore-helpers.sh`` と ``runtime/env-template.sh`` を持つ最小 plugin root を作る。"""
    runtime_dir = root / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "bluecore-helpers.sh").write_text(
        'bluecore_run() { printf "ran:%s\\n" "$*"; }\n', encoding="utf-8"
    )
    (runtime_dir / "env-template.sh").write_text(_ENV_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    return root


class TestWriteEnvPointer:
    """write_env_pointer の Python 側ロジックのテスト。"""

    def test_writes_roots_and_env_sh(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """roots/<ppid>.sh・roots/latest.sh・env.sh が生成されること。"""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("BLUECORE_HOME", str(home))
        plugin_root = _make_plugin_root(tmp_path / "plugin")

        mod.write_env_pointer(plugin_root)

        bluecore_dir = home / BASE_DIR_NAME
        roots_dir = bluecore_dir / "roots"
        pid_file = roots_dir / f"{os.getppid()}.sh"
        assert pid_file.read_text(encoding="utf-8") == f'BLUECORE_ROOT="{plugin_root}"\n'
        assert (roots_dir / "latest.sh").read_text(encoding="utf-8") == f'BLUECORE_ROOT="{plugin_root}"\n'
        assert (bluecore_dir / "env.sh").read_text(encoding="utf-8") == _ENV_TEMPLATE.read_text(encoding="utf-8")
        assert oct(bluecore_dir.stat().st_mode)[-3:] == "700"

    def test_overwrites_on_repeated_call(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """同一 PID から再度呼んでも古い root を上書きする（PID 再利用対策の前提）。"""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("BLUECORE_HOME", str(home))
        first_root = _make_plugin_root(tmp_path / "first")
        second_root = _make_plugin_root(tmp_path / "second")

        mod.write_env_pointer(first_root)
        mod.write_env_pointer(second_root)

        pid_file = home / BASE_DIR_NAME / "roots" / f"{os.getppid()}.sh"
        assert pid_file.read_text(encoding="utf-8") == f'BLUECORE_ROOT="{second_root}"\n'

    def test_prunes_stale_root_entries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """_MAX_ROOT_AGE_SECONDS より古い roots/<pid>.sh は削除される。"""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("BLUECORE_HOME", str(home))
        plugin_root = _make_plugin_root(tmp_path / "plugin")
        roots_dir = home / BASE_DIR_NAME / "roots"
        roots_dir.mkdir(parents=True)
        stale = roots_dir / "999999.sh"
        stale.write_text('BLUECORE_ROOT="/old"\n', encoding="utf-8")
        old_mtime = time.time() - mod._MAX_ROOT_AGE_SECONDS - 3600
        os.utime(stale, (old_mtime, old_mtime))

        mod.write_env_pointer(plugin_root)

        assert not stale.exists()

    def test_keeps_fresh_root_entries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """新しい roots/<pid>.sh は刈られない（他プロセスの root を消さない）。"""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("BLUECORE_HOME", str(home))
        plugin_root = _make_plugin_root(tmp_path / "plugin")
        roots_dir = home / BASE_DIR_NAME / "roots"
        roots_dir.mkdir(parents=True)
        fresh = roots_dir / "424242.sh"
        fresh.write_text('BLUECORE_ROOT="/other-host"\n', encoding="utf-8")

        mod.write_env_pointer(plugin_root)

        assert fresh.exists()

    def test_keeps_latest_regardless_of_age(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """latest.sh は古くても刈り取り対象から除外される。"""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("BLUECORE_HOME", str(home))
        plugin_root = _make_plugin_root(tmp_path / "plugin")
        roots_dir = home / BASE_DIR_NAME / "roots"
        roots_dir.mkdir(parents=True)
        latest = roots_dir / "latest.sh"
        latest.write_text('BLUECORE_ROOT="/prev"\n', encoding="utf-8")
        old_mtime = time.time() - mod._MAX_ROOT_AGE_SECONDS - 3600
        os.utime(latest, (old_mtime, old_mtime))

        mod.write_env_pointer(plugin_root)

        assert latest.read_text(encoding="utf-8") == f'BLUECORE_ROOT="{plugin_root}"\n'

    def test_swallows_missing_template(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """env-template.sh が無くても例外を送出しない（hook を壊さない）。"""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("BLUECORE_HOME", str(home))
        plugin_root = tmp_path / "plugin-without-template"
        plugin_root.mkdir()

        mod.write_env_pointer(plugin_root)  # 例外を送出しないことを確認

        assert not (home / BASE_DIR_NAME / "env.sh").exists()

    def test_prune_ignores_unlistable_roots_dir(self, tmp_path: Path) -> None:
        """roots_dir を列挙できない場合は何もせず戻る。"""
        mod._prune_stale_roots(tmp_path / "does-not-exist")

    def test_prune_ignores_unlink_failure(self, tmp_path: Path) -> None:
        """stale エントリの削除に失敗しても例外を伝播しない。"""
        roots_dir = tmp_path / "roots"
        roots_dir.mkdir()
        # ディレクトリを stale エントリとして置くと unlink() が IsADirectoryError
        # （OSError のサブクラス）で失敗するため、モックなしで分岐を再現できる。
        stale_dir = roots_dir / "999999.sh"
        stale_dir.mkdir()
        old_mtime = time.time() - mod._MAX_ROOT_AGE_SECONDS - 3600
        os.utime(stale_dir, (old_mtime, old_mtime))

        mod._prune_stale_roots(roots_dir)  # 例外を送出しないことを確認

        assert stale_dir.exists()

    def test_swallows_unwritable_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """~/.bluecore を作成できない場合も例外を送出しない。"""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("BLUECORE_HOME", str(home))
        plugin_root = _make_plugin_root(tmp_path / "plugin")

        def _boom(_dir_path: object) -> Path:
            raise OSError("permission denied")

        monkeypatch.setattr(mod, "ensure_private_dir", _boom)

        mod.write_env_pointer(plugin_root)  # 例外を送出しないことを確認


def _run_env_sh(home: Path, *, path: str, ppid: int | None = None) -> subprocess.CompletedProcess[str]:
    """clean shell で ``. "$HOME/.bluecore/env.sh"; bluecore_run x`` を実行する。"""
    script = '. "$HOME/.bluecore/env.sh" || exit 127\nbluecore_run marker\n'
    env = {"HOME": str(home), "PATH": path}
    return subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )


class TestEnvShRealExecution:
    """生成された env.sh を実際に source し、3 段解決の各分岐を実行検証する。"""

    def test_path_resolution_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """PATH 上の bin/ の親に helpers.sh があればそれを使う。"""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("BLUECORE_HOME", str(home))
        path_root = _make_plugin_root(tmp_path / "path-root")
        (path_root / "bin").mkdir()
        pointer_root = _make_plugin_root(tmp_path / "pointer-root")
        mod.write_env_pointer(pointer_root)

        result = _run_env_sh(home, path=f"{path_root / 'bin'}:/usr/bin:/bin")

        assert result.returncode == 0, result.stderr
        assert "ran:marker" in result.stdout

    def test_ppid_pointer_used_when_path_misses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """PATH に一致が無ければ roots/$PPID.sh を使う。"""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("BLUECORE_HOME", str(home))
        pointer_root = _make_plugin_root(tmp_path / "pointer-root")
        mod.write_env_pointer(pointer_root)

        # sh -c で起動した子プロセスの $PPID はこのテストプロセスの PID になる。
        result = _run_env_sh(home, path="/usr/bin:/bin")

        assert result.returncode == 0, result.stderr
        assert "ran:marker" in result.stdout

    def test_latest_fallback_when_ppid_unmatched(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """roots/$PPID.sh が無くても roots/latest.sh を使う。"""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("BLUECORE_HOME", str(home))
        pointer_root = _make_plugin_root(tmp_path / "pointer-root")
        mod.write_env_pointer(pointer_root)
        pid_file = home / BASE_DIR_NAME / "roots" / f"{os.getppid()}.sh"
        pid_file.unlink()

        result = _run_env_sh(home, path="/usr/bin:/bin")

        assert result.returncode == 0, result.stderr
        assert "ran:marker" in result.stdout

    def test_exits_127_when_nothing_resolves(self, tmp_path: Path) -> None:
        """PATH・PID・latest のいずれも解決できなければ 127 で終了する。"""
        home = tmp_path / "home"
        home.mkdir()
        (home / BASE_DIR_NAME).mkdir()
        (home / BASE_DIR_NAME / "env.sh").write_text(
            _ENV_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
        )

        result = _run_env_sh(home, path="/usr/bin:/bin")

        assert result.returncode == 127
        assert "could not resolve the plugin root" in result.stderr
