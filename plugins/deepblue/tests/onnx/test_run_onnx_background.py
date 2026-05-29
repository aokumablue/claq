"""_run_onnx_background.sh のテスト。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
ONNX_SCRIPT = ROOT / "plugins" / "devgear" / "onnx" / "_run_onnx_background.sh"


def _write_exec(path: Path, content: str) -> None:
    """実行可能なシェルスクリプトを書き込む。"""
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _run_script(
    script: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """指定スクリプトを実行して結果を返す。"""
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)
    return subprocess.run(
        ["bash", str(script)],
        env=proc_env,
        capture_output=True,
        text=True,
        check=False,
    )


def _prepare_fake_lib(onnx_dir: Path, log_path: Path) -> None:
    """_build_onnx_lib.sh を fake で差し替え、build_onnx_if_missing の呼び出しを記録する。"""
    _write_exec(
        onnx_dir / "_build_onnx_lib.sh",
        "#!/usr/bin/env bash\n"
        f"LOG={str(log_path)!r}\n"
        "build_onnx_if_missing() {\n"
        '  echo "build_onnx_if_missing:$*" >> "${LOG}"\n'
        "}\n",
    )


@pytest.fixture()
def fake_onnx_dir(tmp_path: Path) -> Path:
    """fake _build_onnx_lib.sh を持つ onnx ディレクトリを返す。"""
    onnx_dir = tmp_path / "onnx"
    onnx_dir.mkdir()
    # _run_onnx_background.sh 本体をコピー
    import shutil

    shutil.copy2(ONNX_SCRIPT, onnx_dir / "_run_onnx_background.sh")
    return onnx_dir


class TestRunOnnxBackground:
    def test_calls_build_onnx_if_missing(self, fake_onnx_dir: Path, tmp_path: Path) -> None:
        """スクリプトが build_onnx_if_missing を呼び出す。"""
        log_path = tmp_path / "call.log"
        _prepare_fake_lib(fake_onnx_dir, log_path)

        result = _run_script(
            fake_onnx_dir / "_run_onnx_background.sh",
            env={
                "HOME": str(tmp_path),
                "PATH": os.environ["PATH"],
            },
        )

        assert result.returncode == 0, result.stderr
        assert log_path.exists(), "build_onnx_if_missing が呼ばれなかった"
        log = log_path.read_text(encoding="utf-8")
        assert "build_onnx_if_missing" in log

    def test_appends_to_log_file(self, fake_onnx_dir: Path, tmp_path: Path) -> None:
        """ビルド結果が modelbuild.log に追記される。"""
        log_path = tmp_path / "call.log"
        _prepare_fake_lib(fake_onnx_dir, log_path)

        _run_script(
            fake_onnx_dir / "_run_onnx_background.sh",
            env={
                "HOME": str(tmp_path),
                "PATH": os.environ["PATH"],
            },
        )

        modelbuild_log = tmp_path / ".devgear" / "logs" / "modelbuild.log"
        assert modelbuild_log.exists(), "modelbuild.log が作成されなかった"

    def test_flock_prevents_duplicate_run(self, fake_onnx_dir: Path, tmp_path: Path) -> None:
        """flock によって 2 度目の起動は即終了する。"""
        log_path = tmp_path / "call.log"
        _prepare_fake_lib(fake_onnx_dir, log_path)

        lock_file = tmp_path / ".devgear" / "onnx_build.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)

        # flock を保持したまま 2 度目の起動を試みる
        # flock -n でロックが取れなければ即終了するため、外から flock を保持する
        with open(lock_file, "w") as lf:
            import fcntl

            fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = _run_script(
                fake_onnx_dir / "_run_onnx_background.sh",
                env={
                    "HOME": str(tmp_path),
                    "PATH": os.environ["PATH"],
                },
            )
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

        assert result.returncode == 0
        # ロック中は build_onnx_if_missing が呼ばれない
        assert not log_path.exists() or "build_onnx_if_missing" not in log_path.read_text(encoding="utf-8")
        # "another build is in progress" が modelbuild.log に記録される
        modelbuild_log = tmp_path / ".devgear" / "logs" / "modelbuild.log"
        assert modelbuild_log.exists()
        assert "another build is in progress" in modelbuild_log.read_text(encoding="utf-8")

    def test_aborts_when_lock_file_is_symlink(self, fake_onnx_dir: Path, tmp_path: Path) -> None:
        """H-3: ロックファイルがシンボリックリンクのとき即終了する。"""
        log_path = tmp_path / "call.log"
        _prepare_fake_lib(fake_onnx_dir, log_path)

        devgear_dir = tmp_path / ".devgear"
        devgear_dir.mkdir(parents=True)
        lock_file = devgear_dir / "onnx_build.lock"
        target = tmp_path / "innocent_file.txt"
        target.write_text("target")
        lock_file.symlink_to(target)  # symlink に差し替え

        result = _run_script(
            fake_onnx_dir / "_run_onnx_background.sh",
            env={
                "HOME": str(tmp_path),
                "PATH": os.environ["PATH"],
            },
        )

        assert result.returncode == 1, "symlink 検出時は exit 1 で終了すること"
        assert not log_path.exists() or "build_onnx_if_missing" not in log_path.read_text(encoding="utf-8")
        modelbuild_log = tmp_path / ".devgear" / "logs" / "modelbuild.log"
        assert modelbuild_log.read_text(encoding="utf-8").strip().endswith("aborting")

    def test_truncates_large_log_file(self, fake_onnx_dir: Path, tmp_path: Path) -> None:
        """L-1: 10MB 超の modelbuild.log はビルド前に truncate される。"""
        log_path = tmp_path / "call.log"
        _prepare_fake_lib(fake_onnx_dir, log_path)

        log_dir = tmp_path / ".devgear" / "logs"
        log_dir.mkdir(parents=True)
        modelbuild_log = log_dir / "modelbuild.log"
        # 11MB のダミーログを書き込む
        modelbuild_log.write_bytes(b"x" * (11 * 1024 * 1024))

        _run_script(
            fake_onnx_dir / "_run_onnx_background.sh",
            env={
                "HOME": str(tmp_path),
                "PATH": os.environ["PATH"],
            },
        )

        # truncate 後にビルドログが追記されているので 11MB より小さくなっているはず
        assert modelbuild_log.stat().st_size < 11 * 1024 * 1024, "10MB 超のログが truncate されていない"
