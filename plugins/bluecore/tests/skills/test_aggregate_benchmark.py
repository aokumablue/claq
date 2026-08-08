"""aggregate_benchmark のベンチマーク集約ロジックを検証するテスト。

対象:
  - calculate_stats() — 空/単一/複数の統計計算
  - _resolve_eval_id() — メタデータ・ディレクトリ名からの eval_id 解決
  - _parse_run_number() — run-<正の整数> 形式の厳密な検証（fail-loud）
  - _load_timing() — timing.json の必須フィールド検証（fail-loud、フォールバックなし）
  - _extract_run_result() — grading.json + timing.json からの結果構築
  - _load_config_results() — config 配下 run 走査と異常スキップ／fail-loud
  - load_run_results() — benchmark ディレクトリ全体の読み込み
  - aggregate_results() — config 別統計と delta 計算
  - generate_benchmark() — benchmark.json 生成
  - _append_summary_table_rows() — サマリーテーブル行生成
  - generate_markdown() — benchmark.md 生成
  - main() — CLI エントリポイント（存在チェック・出力先決定・fail-loud な入力検証）

ファイル I/O は tmp_path、argv は monkeypatch、出力は capsys で検証する。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from bluecore.skills import aggregate_benchmark as mod


def _write_json(path: Path, payload: dict) -> None:
    """親ディレクトリを作成し payload を JSON として書き出す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# --- calculate_stats ---------------------------------------------------------


def test_calculate_stats_empty() -> None:
    """空リストは全 0.0 の統計を返す。"""
    assert mod.calculate_stats([]) == {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0}


def test_calculate_stats_single() -> None:
    """単一要素は stddev=0.0 を返す。"""
    stats = mod.calculate_stats([4.0])
    assert stats["mean"] == 4.0
    assert stats["stddev"] == 0.0
    assert stats["min"] == 4.0
    assert stats["max"] == 4.0


def test_calculate_stats_multiple() -> None:
    """複数要素は分散経路で stddev を計算する。"""
    stats = mod.calculate_stats([2.0, 4.0, 6.0])
    assert stats["mean"] == 4.0
    assert stats["stddev"] == round(math.sqrt(4.0), 4)
    assert stats["min"] == 2.0
    assert stats["max"] == 6.0


# --- _resolve_eval_id --------------------------------------------------------


def test_resolve_eval_id_from_metadata(tmp_path: Path) -> None:
    """eval_metadata.json があればその eval_id を返す。"""
    eval_dir = tmp_path / "eval-0"
    _write_json(eval_dir / "eval_metadata.json", {"eval_id": 99})
    assert mod._resolve_eval_id(eval_dir, 5) == 99


def test_resolve_eval_id_metadata_corrupt(tmp_path: Path) -> None:
    """eval_metadata.json が壊れていれば eval_idx を返す。"""
    eval_dir = tmp_path / "eval-0"
    eval_dir.mkdir()
    (eval_dir / "eval_metadata.json").write_text("{ broken", encoding="utf-8")
    assert mod._resolve_eval_id(eval_dir, 7) == 7


def test_resolve_eval_id_from_dirname(tmp_path: Path) -> None:
    """メタデータが無ければディレクトリ名から番号を取る。"""
    eval_dir = tmp_path / "eval-3"
    eval_dir.mkdir()
    assert mod._resolve_eval_id(eval_dir, 5) == 3


def test_resolve_eval_id_dirname_value_error(tmp_path: Path) -> None:
    """ディレクトリ名が数値でなければ eval_idx を返す。"""
    eval_dir = tmp_path / "eval-abc"
    eval_dir.mkdir()
    assert mod._resolve_eval_id(eval_dir, 8) == 8


# --- _extract_run_result -----------------------------------------------------


def test_extract_run_result_uses_timing_json_only(tmp_path: Path) -> None:
    """time_seconds/tokens は timing.json 由来の値のみを使い、grading.json の timing は無視する。"""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    _write_json(run_dir / "timing.json", {"total_duration_seconds": 8.0, "total_tokens": 555})
    grading = {
        "summary": {"pass_rate": 0.5, "passed": 1, "failed": 1, "total": 2},
        "timing": {"total_duration_seconds": 12.5},  # 無視されるべき（黙示的フォールバック禁止）
        "execution_metrics": {"total_tool_calls": 3, "output_chars": 100, "errors_encountered": 0},
        "expectations": [{"text": "x", "passed": True}],
    }
    result = mod._extract_run_result(run_dir, 1, grading)
    assert result["time_seconds"] == 8.0
    assert result["tokens"] == 555
    assert result["tool_calls"] == 3
    assert result["run_number"] == 1


def test_extract_run_result_reads_timing_file(tmp_path: Path) -> None:
    """timing.json があれば time_seconds/tokens をそこから読む。"""
    run_dir = tmp_path / "run-2"
    run_dir.mkdir()
    _write_json(run_dir / "timing.json", {"total_duration_seconds": 8.0, "total_tokens": 555})
    grading = {"summary": {}, "execution_metrics": {}, "expectations": []}
    result = mod._extract_run_result(run_dir, 1, grading)
    assert result["time_seconds"] == 8.0
    assert result["tokens"] == 555


def test_extract_run_result_timing_file_corrupt_raises(tmp_path: Path) -> None:
    """timing.json が壊れていれば ValueError を送出する（フォールバックしない）。"""
    run_dir = tmp_path / "run-3"
    run_dir.mkdir()
    (run_dir / "timing.json").write_text("{ broken", encoding="utf-8")
    grading = {"summary": {}, "execution_metrics": {"output_chars": 42}, "expectations": []}
    with pytest.raises(ValueError, match="JSON が不正です"):
        mod._extract_run_result(run_dir, 1, grading)


def test_extract_run_result_no_timing_file_raises(tmp_path: Path) -> None:
    """timing.json が無ければ ValueError を送出する（metrics へのフォールバックはしない）。"""
    run_dir = tmp_path / "run-4"
    run_dir.mkdir()
    grading = {"summary": {}, "execution_metrics": {"output_chars": 7}, "expectations": []}
    with pytest.raises(ValueError, match="timing.json が見つかりません"):
        mod._extract_run_result(run_dir, 1, grading)


def test_extract_run_result_timing_missing_required_fields_raises(tmp_path: Path) -> None:
    """timing.json に total_duration_seconds/total_tokens が無ければ ValueError を送出する。"""
    run_dir = tmp_path / "run-4"
    run_dir.mkdir()
    _write_json(run_dir / "timing.json", {"total_duration_seconds": 1.0})
    grading = {"summary": {}, "execution_metrics": {}, "expectations": []}
    with pytest.raises(ValueError, match="必須フィールドがありません"):
        mod._extract_run_result(run_dir, 1, grading)


def test_extract_run_result_warns_on_bad_expectation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """expectation に必須フィールドが無ければ警告を出す。"""
    run_dir = tmp_path / "run-5"
    run_dir.mkdir()
    _write_json(run_dir / "timing.json", {"total_duration_seconds": 1.0, "total_tokens": 1})
    grading = {"summary": {}, "execution_metrics": {}, "expectations": [{"text": "only"}]}
    mod._extract_run_result(run_dir, 1, grading)
    assert "必須フィールドがありません" in capsys.readouterr().out


def test_extract_run_result_invalid_run_name_raises(tmp_path: Path) -> None:
    """run ディレクトリ名が run-<正の整数> 形式でなければ ValueError を送出する。"""
    run_dir = tmp_path / "run-1abc"
    run_dir.mkdir()
    _write_json(run_dir / "timing.json", {"total_duration_seconds": 1.0, "total_tokens": 1})
    grading = {"summary": {}, "execution_metrics": {}, "expectations": []}
    with pytest.raises(ValueError, match="run-N 形式"):
        mod._extract_run_result(run_dir, 1, grading)


# --- _parse_run_number --------------------------------------------------------


@pytest.mark.parametrize("name", ["run-abc", "run-1abc", "run-", "run-01", "run-0", "run-1.5", "run-1-2"])
def test_parse_run_number_rejects_invalid_names(tmp_path: Path, name: str) -> None:
    """run-<正の整数> 形式でない名前は ValueError を送出する（先頭ゼロ・非数値サフィックス等）。"""
    run_dir = tmp_path / name
    run_dir.mkdir()
    with pytest.raises(ValueError, match="run-N 形式"):
        mod._parse_run_number(run_dir)


@pytest.mark.parametrize(("name", "expected"), [("run-1", 1), ("run-12", 12), ("run-999", 999)])
def test_parse_run_number_accepts_canonical_names(tmp_path: Path, name: str, expected: int) -> None:
    """run-<正の整数> 形式は正しい番号を返す。"""
    run_dir = tmp_path / name
    run_dir.mkdir()
    assert mod._parse_run_number(run_dir) == expected


# --- _load_timing --------------------------------------------------------------


def test_load_timing_missing_file(tmp_path: Path) -> None:
    """timing.json が無ければ ValueError を送出する（フォールバックしない）。"""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    with pytest.raises(ValueError, match="timing.json が見つかりません"):
        mod._load_timing(run_dir)


def test_load_timing_corrupt_json(tmp_path: Path) -> None:
    """timing.json が壊れていれば ValueError を送出する（フォールバックしない）。"""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    (run_dir / "timing.json").write_text("{ broken", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON が不正です"):
        mod._load_timing(run_dir)


def test_load_timing_missing_required_fields(tmp_path: Path) -> None:
    """total_duration_seconds/total_tokens が無ければ ValueError を送出する。"""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    _write_json(run_dir / "timing.json", {"total_duration_seconds": 1.0})
    with pytest.raises(ValueError, match="必須フィールドがありません"):
        mod._load_timing(run_dir)


def test_load_timing_valid(tmp_path: Path) -> None:
    """timing.json に必須フィールドがあればそのまま返す。"""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    _write_json(run_dir / "timing.json", {"total_duration_seconds": 8.0, "total_tokens": 555})
    timing = mod._load_timing(run_dir)
    assert timing["total_duration_seconds"] == 8.0
    assert timing["total_tokens"] == 555


# --- _load_config_results ----------------------------------------------------


def test_load_config_results_new_and_skips(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """新規 config キーを作り、grading 欠落・破損 run はスキップする。"""
    config_dir = tmp_path / "with_skill"
    good = config_dir / "run-1"
    _write_json(good / "grading.json", {"summary": {"pass_rate": 1.0}, "execution_metrics": {}, "expectations": []})
    _write_json(good / "timing.json", {"total_duration_seconds": 1.0, "total_tokens": 10})
    (config_dir / "run-2").mkdir(parents=True)  # grading.json 無し
    bad = config_dir / "run-3"
    bad.mkdir()
    (bad / "grading.json").write_text("{ broken", encoding="utf-8")

    results: dict[str, list] = {}
    mod._load_config_results(config_dir, 1, results)

    assert "with_skill" in results
    assert len(results["with_skill"]) == 1
    out = capsys.readouterr().out
    assert "grading.json が見つかりません" in out
    assert "JSON が不正です" in out


def test_load_config_results_existing_key(tmp_path: Path) -> None:
    """既存の config キーには追記する。"""
    config_dir = tmp_path / "with_skill"
    _write_json(
        config_dir / "run-1" / "grading.json",
        {"summary": {}, "execution_metrics": {}, "expectations": []},
    )
    _write_json(
        config_dir / "run-1" / "timing.json",
        {"total_duration_seconds": 1.0, "total_tokens": 10},
    )
    results: dict[str, list] = {"with_skill": [{"eval_id": 0}]}
    mod._load_config_results(config_dir, 1, results)
    assert len(results["with_skill"]) == 2


def test_load_config_results_invalid_run_name_raises(tmp_path: Path) -> None:
    """run-N 形式でないディレクトリ名は ValueError を送出する（fail-loud）。"""
    config_dir = tmp_path / "with_skill"
    (config_dir / "run-1abc").mkdir(parents=True)
    results: dict[str, list] = {}
    with pytest.raises(ValueError, match="run-N 形式"):
        mod._load_config_results(config_dir, 1, results)


# --- load_run_results --------------------------------------------------------


def test_load_run_results_no_eval(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """eval ディレクトリが無ければ空 dict を返し警告する。"""
    assert mod.load_run_results(tmp_path) == {}
    assert "eval ディレクトリが" in capsys.readouterr().out


def test_load_run_results_skips_file_and_empty_config(tmp_path: Path) -> None:
    """config がファイル/run 無しの場合はスキップし、正常 config のみ集約する。"""
    eval_dir = tmp_path / "eval-0"
    eval_dir.mkdir()
    (eval_dir / "stray.txt").write_text("x", encoding="utf-8")  # is_dir False
    (eval_dir / "empty_cfg").mkdir()  # run-* 無し
    _write_json(
        eval_dir / "with_skill" / "run-1" / "grading.json",
        {"summary": {"pass_rate": 1.0}, "execution_metrics": {}, "expectations": []},
    )
    _write_json(
        eval_dir / "with_skill" / "run-1" / "timing.json",
        {"total_duration_seconds": 1.0, "total_tokens": 10},
    )
    results = mod.load_run_results(tmp_path)
    assert set(results.keys()) == {"with_skill"}
    assert len(results["with_skill"]) == 1


# --- aggregate_results -------------------------------------------------------


def test_aggregate_results_empty_runs() -> None:
    """runs が空の config はゼロ初期化統計になる。"""
    summary = mod.aggregate_results({"with_skill": []})
    assert summary["with_skill"]["pass_rate"]["mean"] == 0.0
    assert summary["with_skill"]["tokens"]["mean"] == 0


def test_aggregate_results_two_configs() -> None:
    """config が 2 つ以上なら primary/baseline 間の delta を計算する。"""
    results = {
        "with_skill": [{"pass_rate": 1.0, "time_seconds": 2.0, "tokens": 100}],
        "without_skill": [{"pass_rate": 0.5, "time_seconds": 4.0, "tokens": 200}],
    }
    summary = mod.aggregate_results(results)
    assert summary["delta"]["pass_rate"] == "+0.50"
    assert summary["delta"]["time_seconds"] == "-2.0"
    assert summary["delta"]["tokens"] == "-100"


def test_aggregate_results_single_config() -> None:
    """config が 1 つなら baseline は空で delta は primary 基準。"""
    results = {"with_skill": [{"pass_rate": 1.0, "time_seconds": 2.0, "tokens": 100}]}
    summary = mod.aggregate_results(results)
    assert summary["delta"]["pass_rate"] == "+1.00"


def test_aggregate_results_no_configs() -> None:
    """config が無ければ primary も空で delta はゼロ。"""
    summary = mod.aggregate_results({})
    assert summary["delta"]["pass_rate"] == "+0.00"


# --- generate_benchmark ------------------------------------------------------


def test_generate_benchmark_defaults_empty(tmp_path: Path) -> None:
    """eval 無し + skill 未指定なら placeholder メタデータを返す。"""
    benchmark = mod.generate_benchmark(tmp_path)
    assert benchmark["metadata"]["skill_name"] == "<skill-name>"
    assert benchmark["metadata"]["skill_path"] == "<path/to/skill>"
    assert benchmark["runs"] == []


def test_generate_benchmark_with_data(tmp_path: Path) -> None:
    """実データと skill 指定で runs 配列とメタデータを構築する。"""
    _write_json(
        tmp_path / "eval-0" / "with_skill" / "run-1" / "grading.json",
        {
            "summary": {"pass_rate": 1.0, "passed": 2, "failed": 0, "total": 2},
            "timing": {"total_duration_seconds": 3.0},
            "execution_metrics": {"total_tool_calls": 1, "output_chars": 50},
            "expectations": [{"text": "x", "passed": True}],
        },
    )
    _write_json(
        tmp_path / "eval-0" / "with_skill" / "run-1" / "timing.json",
        {"total_duration_seconds": 3.0, "total_tokens": 50},
    )
    benchmark = mod.generate_benchmark(tmp_path, "myskill", "path/to/myskill")
    assert benchmark["metadata"]["skill_name"] == "myskill"
    assert benchmark["metadata"]["skill_path"] == "path/to/myskill"
    assert len(benchmark["runs"]) == 1
    assert benchmark["metadata"]["evals_run"] == [0]


def test_generate_benchmark_missing_timing_raises(tmp_path: Path) -> None:
    """timing.json 欠落時は集計全体を止めるため ValueError を送出する（fail-loud）。"""
    _write_json(
        tmp_path / "eval-0" / "with_skill" / "run-1" / "grading.json",
        {"summary": {"pass_rate": 1.0}, "execution_metrics": {}, "expectations": []},
    )
    with pytest.raises(ValueError, match="timing.json"):
        mod.generate_benchmark(tmp_path)


# --- _append_summary_table_rows / generate_markdown --------------------------


def test_generate_markdown_full() -> None:
    """2 config + notes ありで全テーブル行と備考を生成する。"""
    benchmark = {
        "metadata": {
            "skill_name": "s",
            "executor_model": "m",
            "timestamp": "2026-01-01T00:00:00Z",
            "evals_run": [0, 1],
            "runs_per_configuration": 3,
        },
        "run_summary": {
            "with_skill": {
                "pass_rate": {"mean": 1.0, "stddev": 0.0},
                "time_seconds": {"mean": 2.0, "stddev": 0.1},
                "tokens": {"mean": 100, "stddev": 5},
            },
            "without_skill": {
                "pass_rate": {"mean": 0.5, "stddev": 0.0},
                "time_seconds": {"mean": 4.0, "stddev": 0.2},
                "tokens": {"mean": 200, "stddev": 10},
            },
            "delta": {"pass_rate": "+0.50", "time_seconds": "-2.0", "tokens": "-100"},
        },
        "notes": ["気になる点1"],
    }
    md = mod.generate_markdown(benchmark)
    assert "# スキルベンチマーク: s" in md
    assert "合格率" in md and "トークン" in md
    assert "## 備考" in md and "気になる点1" in md


def test_generate_markdown_defaults_no_notes() -> None:
    """config 無し + notes 無しでデフォルトラベルかつ備考なし。"""
    benchmark = {
        "metadata": {
            "skill_name": "s",
            "executor_model": "m",
            "timestamp": "t",
            "evals_run": [],
            "runs_per_configuration": 1,
        },
        "run_summary": {"delta": {}},
        "notes": [],
    }
    md = mod.generate_markdown(benchmark)
    assert "Config A" in md and "Config B" in md
    assert "## 備考" not in md


# --- main --------------------------------------------------------------------


def test_main_missing_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """存在しないディレクトリ指定で sys.exit(1)。"""
    missing = tmp_path / "nope"
    monkeypatch.setattr("sys.argv", ["prog", str(missing)])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1
    assert "ディレクトリが見つかりません" in capsys.readouterr().out


def test_main_default_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--output 未指定なら benchmark_dir 配下に出力する。"""
    _write_json(
        tmp_path / "eval-0" / "with_skill" / "run-1" / "grading.json",
        {"summary": {"pass_rate": 1.0}, "execution_metrics": {}, "expectations": []},
    )
    _write_json(
        tmp_path / "eval-0" / "with_skill" / "run-1" / "timing.json",
        {"total_duration_seconds": 1.0, "total_tokens": 10},
    )
    monkeypatch.setattr("sys.argv", ["prog", str(tmp_path)])
    mod.main()
    assert (tmp_path / "benchmark.json").exists()
    assert (tmp_path / "benchmark.md").exists()


def test_main_explicit_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--output 指定なら そのパスに出力する。"""
    _write_json(
        tmp_path / "eval-0" / "with_skill" / "run-1" / "grading.json",
        {"summary": {"pass_rate": 1.0}, "execution_metrics": {}, "expectations": []},
    )
    _write_json(
        tmp_path / "eval-0" / "with_skill" / "run-1" / "timing.json",
        {"total_duration_seconds": 1.0, "total_tokens": 10},
    )
    out = tmp_path / "custom" / "result.json"
    out.parent.mkdir()
    monkeypatch.setattr("sys.argv", ["prog", str(tmp_path), "-o", str(out)])
    mod.main()
    assert out.exists()
    assert out.with_suffix(".md").exists()


def test_main_aggregation_error_exits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """timing.json 欠落など入力不正時は集計全体を止め sys.exit(1) する（fail-loud）。"""
    _write_json(
        tmp_path / "eval-0" / "with_skill" / "run-1" / "grading.json",
        {"summary": {"pass_rate": 1.0}, "execution_metrics": {}, "expectations": []},
    )
    monkeypatch.setattr("sys.argv", ["prog", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1
    assert "timing.json" in capsys.readouterr().out
