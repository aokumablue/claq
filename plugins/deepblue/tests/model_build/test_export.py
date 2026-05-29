"""export モジュールのユニットテスト（ネットワーク・GPU 不要）。"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from model_build.export import _OPSET14_COMPAT_NAMES, _patch_torch_onnx_symbolic_opset14


def _make_fake_torch_modules(
    missing: list[str],
) -> tuple[types.ModuleType, types.ModuleType, dict[str, types.ModuleType]]:
    """公開 opset14 モジュール（一部欠落）と内部モジュール（全名前あり）を返す。

    戻り値: (pub_mod, internal_mod, sys_modules_patch_dict)
    """
    pub = types.ModuleType("torch.onnx.symbolic_opset14")
    internal = types.ModuleType("torch.onnx._internal.torchscript_exporter.symbolic_opset14")
    for name in _OPSET14_COMPAT_NAMES:
        sentinel = object()
        setattr(internal, name, sentinel)
        if name not in missing:
            setattr(pub, name, sentinel)

    torch_mod = types.ModuleType("torch")
    torch_onnx = types.ModuleType("torch.onnx")
    torch_internal = types.ModuleType("torch.onnx._internal")
    exporter_pkg = types.ModuleType("torch.onnx._internal.torchscript_exporter")
    exporter_pkg.symbolic_opset14 = internal  # type: ignore[attr-defined]

    modules = {
        "torch": torch_mod,
        "torch.onnx": torch_onnx,
        "torch.onnx.symbolic_opset14": pub,
        "torch.onnx._internal": torch_internal,
        "torch.onnx._internal.torchscript_exporter": exporter_pkg,
        "torch.onnx._internal.torchscript_exporter.symbolic_opset14": internal,
    }
    return pub, internal, modules


class TestPatchTorchOnnxSymbolicOpset14:
    """_patch_torch_onnx_symbolic_opset14 の互換パッチ適用テスト。"""

    def test_injects_all_missing_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """欠落している全名前が内部モジュールから注入される。"""
        pub, internal, modules = _make_fake_torch_modules(missing=_OPSET14_COMPAT_NAMES)
        for key, mod in modules.items():
            monkeypatch.setitem(sys.modules, key, mod)

        _patch_torch_onnx_symbolic_opset14()

        for name in _OPSET14_COMPAT_NAMES:
            assert hasattr(pub, name), f"{name} が注入されていない"
            assert getattr(pub, name) is getattr(internal, name)

    def test_skips_already_present_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """すでに存在する名前は上書きしない。"""
        pub, internal, modules = _make_fake_torch_modules(missing=[])
        original = {name: getattr(pub, name) for name in _OPSET14_COMPAT_NAMES}
        for key, mod in modules.items():
            monkeypatch.setitem(sys.modules, key, mod)

        _patch_torch_onnx_symbolic_opset14()

        for name in _OPSET14_COMPAT_NAMES:
            assert getattr(pub, name) is original[name], f"{name} が予期せず上書きされた"

    def test_partial_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """一部欠落時は欠落分だけ注入し、既存分は保持する。"""
        missing = _OPSET14_COMPAT_NAMES[:2]
        present = _OPSET14_COMPAT_NAMES[2:]
        pub, internal, modules = _make_fake_torch_modules(missing=missing)
        existing_vals = {name: getattr(pub, name) for name in present}
        for key, mod in modules.items():
            monkeypatch.setitem(sys.modules, key, mod)

        _patch_torch_onnx_symbolic_opset14()

        for name in missing:
            assert hasattr(pub, name)
            assert getattr(pub, name) is getattr(internal, name)
        for name, val in existing_vals.items():
            assert getattr(pub, name) is val, f"{name} が予期せず変更された"


class TestExportToOnnxCallsPatch:
    """export_to_onnx が _patch_torch_onnx_symbolic_opset14 を呼ぶことを確認。"""

    def test_patch_is_called(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """export_to_onnx 実行時にパッチ関数が呼ばれる。"""
        import model_build.export as export_mod

        patch_called = []

        def fake_patch() -> None:
            patch_called.append(True)

        fake_main_export = MagicMock()

        onnx_out = tmp_path / "onnx_export"
        onnx_out.mkdir()
        (onnx_out / "model.onnx").write_bytes(b"fake")

        fake_optimum_onnx = types.ModuleType("optimum.exporters.onnx")
        fake_optimum_onnx.main_export = fake_main_export  # type: ignore[attr-defined]
        fake_optimum = types.ModuleType("optimum")
        fake_optimum_exporters = types.ModuleType("optimum.exporters")

        monkeypatch.setattr(export_mod, "_patch_torch_onnx_symbolic_opset14", fake_patch)
        monkeypatch.setitem(sys.modules, "optimum", fake_optimum)
        monkeypatch.setitem(sys.modules, "optimum.exporters", fake_optimum_exporters)
        monkeypatch.setitem(sys.modules, "optimum.exporters.onnx", fake_optimum_onnx)

        export_mod.export_to_onnx(
            model_name="test/model",
            revision="abc1234",
            output_dir=tmp_path,
            opset=17,
        )

        assert patch_called, "_patch_torch_onnx_symbolic_opset14 が呼ばれなかった"
        fake_main_export.assert_called_once()

    def test_raises_when_no_onnx_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """main_export 後に ONNX ファイルがなければ FileNotFoundError を送出する。"""
        import model_build.export as export_mod

        fake_main_export = MagicMock()
        (tmp_path / "onnx_export").mkdir()  # ONNX ファイルを作らない

        fake_optimum_onnx = types.ModuleType("optimum.exporters.onnx")
        fake_optimum_onnx.main_export = fake_main_export  # type: ignore[attr-defined]
        monkeypatch.setattr(export_mod, "_patch_torch_onnx_symbolic_opset14", lambda: None)
        monkeypatch.setitem(sys.modules, "optimum", types.ModuleType("optimum"))
        monkeypatch.setitem(sys.modules, "optimum.exporters", types.ModuleType("optimum.exporters"))
        monkeypatch.setitem(sys.modules, "optimum.exporters.onnx", fake_optimum_onnx)

        with pytest.raises(FileNotFoundError, match="ONNX ファイルが"):
            export_mod.export_to_onnx(
                model_name="test/model",
                revision="abc1234",
                output_dir=tmp_path,
                opset=17,
            )

    def test_handles_optimum_external_data_cleanup_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main_export が FileNotFoundError を送出しても model.onnx が存在すれば正常終了する。

        optimum バグ: dynamo エクスポーターが model.onnx.data をインライン化後に削除するが、
        optimum のクリーンアップが同ファイルを再度削除しようとして FileNotFoundError が発生する。
        """
        import model_build.export as export_mod

        onnx_out = tmp_path / "onnx_export"
        onnx_out.mkdir()
        (onnx_out / "model.onnx").write_bytes(b"fake onnx content")

        fake_optimum_onnx = types.ModuleType("optimum.exporters.onnx")
        fake_optimum_onnx.main_export = MagicMock(  # type: ignore[attr-defined]
            side_effect=FileNotFoundError("model.onnx.data")
        )
        monkeypatch.setattr(export_mod, "_patch_torch_onnx_symbolic_opset14", lambda: None)
        monkeypatch.setitem(sys.modules, "optimum", types.ModuleType("optimum"))
        monkeypatch.setitem(sys.modules, "optimum.exporters", types.ModuleType("optimum.exporters"))
        monkeypatch.setitem(sys.modules, "optimum.exporters.onnx", fake_optimum_onnx)

        result = export_mod.export_to_onnx(
            model_name="test/model",
            revision="abc1234",
            output_dir=tmp_path,
        )
        assert result == onnx_out / "model.onnx"

    def test_reraises_file_not_found_when_no_onnx(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main_export が FileNotFoundError を送出し model.onnx も存在しない場合は再送出する。"""
        import model_build.export as export_mod

        (tmp_path / "onnx_export").mkdir()  # model.onnx を作らない

        fake_optimum_onnx = types.ModuleType("optimum.exporters.onnx")
        fake_optimum_onnx.main_export = MagicMock(  # type: ignore[attr-defined]
            side_effect=FileNotFoundError("some other file")
        )
        monkeypatch.setattr(export_mod, "_patch_torch_onnx_symbolic_opset14", lambda: None)
        monkeypatch.setitem(sys.modules, "optimum", types.ModuleType("optimum"))
        monkeypatch.setitem(sys.modules, "optimum.exporters", types.ModuleType("optimum.exporters"))
        monkeypatch.setitem(sys.modules, "optimum.exporters.onnx", fake_optimum_onnx)

        with pytest.raises(FileNotFoundError, match="some other file"):
            export_mod.export_to_onnx(
                model_name="test/model",
                revision="abc1234",
                output_dir=tmp_path,
            )
