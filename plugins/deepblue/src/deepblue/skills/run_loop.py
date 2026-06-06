#!/usr/bin/env python3
"""全て合格するか最大反復回数に達するまで eval + 改善ループを回す。

run_eval.py と improve_description.py をループで組み合わせ、履歴を追跡し、
見つかった最良の説明を返す。過学習を防ぐため train/test 分割にも対応する。
"""

import argparse
import json
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ._eval_config import EvalConfig
from .improve_description import improve_description
from .run_eval import find_project_root, run_eval
from .utils import parse_skill_md


@dataclass(frozen=True)
class SkillContext:
    """スキル情報をまとめたパラメータオブジェクト。

    Attributes:
        name: スキル名。
        content: SKILL.md のテキスト内容。
    """

    name: str
    content: str


@dataclass(frozen=True)
class LoopConfig:
    """ループ実行設定をまとめたパラメータオブジェクト。

    Attributes:
        max_iterations: 改善の最大反復回数。
        holdout: テスト用に取り分ける eval セットの割合（0 で無効）。
        verbose: 進捗を stderr に表示するかどうか。
        log_dir: ログ出力先ディレクトリ（None で無効）。
        eval_config: eval 実行設定。
    """

    max_iterations: int
    holdout: float
    verbose: bool
    log_dir: Path | None
    eval_config: EvalConfig


@dataclass(frozen=True)
class IterationResult:
    """1反復分の評価結果をまとめたパラメータオブジェクト。

    Attributes:
        iteration: 反復番号。
        description: この反復で使用した説明文。
        train_results: 学習用クエリの評価結果辞書。
        test_results: 検証用クエリの評価結果辞書（なければ None）。
        train_summary: 学習用サマリー辞書。
        test_summary: 検証用サマリー辞書（なければ None）。
    """

    iteration: int
    description: str
    train_results: dict
    test_results: dict | None
    train_summary: dict
    test_summary: dict | None


@dataclass(frozen=True)
class EvalSets:
    """train / test 分割後の eval セットをまとめたパラメータオブジェクト。

    Attributes:
        train: 学習用クエリのリスト。
        test: 検証用クエリのリスト。
    """

    train: list[dict]
    test: list[dict]


@dataclass(frozen=True)
class IterationState:
    """1反復の進行状態をまとめたパラメータオブジェクト。

    Attributes:
        iteration: 現在の反復番号（1始まり）。
        current_description: この反復で評価する説明文。
    """

    iteration: int
    current_description: str


@dataclass(frozen=True)
class ImproveParams:
    """説明文改善に必要なパラメータをまとめたオブジェクト。

    Attributes:
        current_description: 現在の説明文。
        train_results: 学習用クエリの評価結果辞書。
        blinded_history: test_ キーを除去した履歴リスト。
        iteration: 現在の反復番号。
    """

    current_description: str
    train_results: dict
    blinded_history: list[dict]
    iteration: int


@dataclass(frozen=True)
class LoopOutcome:
    """ループ実行結果の骨格をまとめたパラメータオブジェクト。

    Attributes:
        exit_reason: ループ終了理由文字列。
        original_description: ループ開始時の元説明文。
        best: 最良反復の履歴エントリ辞書。
        best_score: 最良スコア文字列。
        final_description: ループ終了時点の最新説明文。
        history: 全反復の履歴リスト。
    """

    exit_reason: str
    original_description: str
    best: dict
    best_score: str
    final_description: str
    history: list[dict]


def split_eval_set(eval_set: list[dict], holdout: float, seed: int = 42) -> tuple[list[dict], list[dict]]:
    """eval セットを should_trigger で層化して train / test に分割する。"""
    random.seed(seed)

    # should_trigger で分ける
    trigger = [e for e in eval_set if e["should_trigger"]]
    no_trigger = [e for e in eval_set if not e["should_trigger"]]

    # 各グループをシャッフルする
    random.shuffle(trigger)
    random.shuffle(no_trigger)

    # 分割点を計算する
    n_trigger_test = max(1, int(len(trigger) * holdout))
    n_no_trigger_test = max(1, int(len(no_trigger) * holdout))

    # 分割する
    test_set = trigger[:n_trigger_test] + no_trigger[:n_no_trigger_test]
    train_set = trigger[n_trigger_test:] + no_trigger[n_no_trigger_test:]

    return train_set, test_set


def _print_eval_stats(label: str, results: list[dict], elapsed: float) -> None:
    """eval 結果から精度・再現率などの統計を stderr に表示する。"""
    pos = [r for r in results if r["should_trigger"]]
    neg = [r for r in results if not r["should_trigger"]]
    tp = sum(r["triggers"] for r in pos)
    pos_runs = sum(r["runs"] for r in pos)
    fn = pos_runs - tp
    fp = sum(r["triggers"] for r in neg)
    neg_runs = sum(r["runs"] for r in neg)
    tn = neg_runs - fp
    total = tp + tn + fp + fn
    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    accuracy = (tp + tn) / total if total > 0 else 0.0
    print(
        f"{label}: {tp + tn}/{total} correct, precision={precision:.0%} recall={recall:.0%} accuracy={accuracy:.0%} ({elapsed:.1f}s)",
        file=sys.stderr,
    )
    for r in results:
        status = "合格" if r["pass"] else "不合格"
        rate_str = f"{r['triggers']}/{r['runs']}"
        print(
            f"  [{status}] rate={rate_str} expected={r['should_trigger']}: {r['query'][:60]}",
            file=sys.stderr,
        )


def _split_eval_results(
    all_results: dict,
    train_set: list[dict],
    test_set: list[dict],
) -> tuple[dict, dict | None, dict | None]:
    """全評価結果を train / test に分割し、それぞれのサマリーとともに返す。"""
    train_queries_set = {q["query"] for q in train_set}
    train_result_list = [r for r in all_results["results"] if r["query"] in train_queries_set]
    test_result_list = [r for r in all_results["results"] if r["query"] not in train_queries_set]

    train_passed = sum(1 for r in train_result_list if r["pass"])
    train_total = len(train_result_list)
    train_summary = {"passed": train_passed, "failed": train_total - train_passed, "total": train_total}
    train_results = {"results": train_result_list, "summary": train_summary}

    if test_set:
        test_passed = sum(1 for r in test_result_list if r["pass"])
        test_total = len(test_result_list)
        test_summary: dict | None = {"passed": test_passed, "failed": test_total - test_passed, "total": test_total}
        test_results: dict | None = {"results": test_result_list, "summary": test_summary}
    else:
        test_results = None
        test_summary = None

    return train_results, test_results, test_summary


def _print_iteration_header(iteration: int, max_iterations: int, current_description: str, verbose: bool) -> None:
    """反復開始ヘッダーを stderr に出力する。verbose=False の場合は何もしない。"""
    if not verbose:
        return
    print(f"\n{'=' * 60}", file=sys.stderr)
    print(f"反復 {iteration}/{max_iterations}", file=sys.stderr)
    print(f"説明: {current_description}", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)


def _eval_queries(
    skill_ctx: SkillContext,
    current_description: str,
    all_queries: list[dict],
    eval_cfg: EvalConfig,
) -> tuple[dict, float]:
    """eval を実行し (all_results, elapsed_seconds) を返す。"""
    t0 = time.time()
    results = run_eval(
        eval_set=all_queries,
        skill_name=skill_ctx.name,
        description=current_description,
        eval_cfg=eval_cfg,
    )
    return results, time.time() - t0


def _append_history(history: list[dict], iter_result: IterationResult) -> None:
    """反復結果を history リストに追記する。"""
    ts = iter_result.test_summary
    tr = iter_result.test_results
    history.append({
        "iteration": iter_result.iteration,
        "description": iter_result.description,
        "train_passed": iter_result.train_summary["passed"],
        "train_failed": iter_result.train_summary["failed"],
        "train_total": iter_result.train_summary["total"],
        "train_results": iter_result.train_results["results"],
        "test_passed": ts["passed"] if ts else None,
        "test_failed": ts["failed"] if ts else None,
        "test_total": ts["total"] if ts else None,
        "test_results": tr["results"] if tr else None,
    })


def _run_improve(
    skill_ctx: SkillContext,
    loop_cfg: LoopConfig,
    params: ImproveParams,
) -> str:
    """説明文改善を実行し新しい説明文を返す。"""
    if loop_cfg.verbose:
        print("\n説明を改善しています...", file=sys.stderr)
    t0 = time.time()
    from .improve_description import ImproveContext
    new_desc = improve_description(
        ImproveContext(
            skill_name=skill_ctx.name,
            skill_content=skill_ctx.content,
            current_description=params.current_description,
            eval_results=params.train_results,
            history=params.blinded_history,
        ),
        model=loop_cfg.eval_config.model,
        log_dir=loop_cfg.log_dir,
        iteration=params.iteration,
    )
    if loop_cfg.verbose:
        print(f"提案結果（{time.time() - t0:.1f}s）: {new_desc}", file=sys.stderr)
    return new_desc


def _run_single_iteration(
    state: IterationState,
    skill_ctx: SkillContext,
    eval_sets: EvalSets,
    loop_cfg: LoopConfig,
    history: list[dict],
) -> tuple[str, str | None]:
    """1反復分の eval・採点・改善を実行し、(新しい説明, 終了理由|None) を返す。"""
    iteration = state.iteration
    current_description = state.current_description
    _print_iteration_header(iteration, loop_cfg.max_iterations, current_description, loop_cfg.verbose)
    all_results, eval_elapsed = _eval_queries(
        skill_ctx, current_description, eval_sets.train + eval_sets.test, loop_cfg.eval_config
    )
    train_results, test_results, test_summary = _split_eval_results(all_results, eval_sets.train, eval_sets.test)
    train_summary = train_results["summary"]
    iter_result = IterationResult(
        iteration=iteration,
        description=current_description,
        train_results=train_results,
        test_results=test_results,
        train_summary=train_summary,
        test_summary=test_summary,
    )
    _append_history(history, iter_result)
    if loop_cfg.verbose:
        _print_eval_stats("学習用", train_results["results"], eval_elapsed)
        if test_summary:
            _print_eval_stats("検証用", test_results["results"], 0)  # type: ignore[index]
    if train_summary["failed"] == 0:
        if loop_cfg.verbose:
            print(f"\nAll train queries passed on iteration {iteration}!", file=sys.stderr)
        return current_description, f"all_passed (iteration {iteration})"
    if iteration == loop_cfg.max_iterations:
        if loop_cfg.verbose:
            print(f"\nMax iterations reached ({loop_cfg.max_iterations}).", file=sys.stderr)
        return current_description, f"max_iterations ({loop_cfg.max_iterations})"
    blinded = [{k: v for k, v in h.items() if not k.startswith("test_")} for h in history]
    improve_params = ImproveParams(
        current_description=current_description,
        train_results=train_results,
        blinded_history=blinded,
        iteration=iteration,
    )
    return _run_improve(skill_ctx, loop_cfg, improve_params), None


def _find_best(history: list[dict], test_set: list[dict]) -> tuple[dict, str]:
    """最良の反復結果と得点文字列を返す。"""
    if test_set:
        best = max(history, key=lambda h: h["test_passed"] or 0)
        return best, f"{best['test_passed']}/{best['test_total']}"
    best = max(history, key=lambda h: h["train_passed"])
    return best, f"{best['train_passed']}/{best['train_total']}"


def _build_loop_result(
    outcome: LoopOutcome,
    loop_cfg: LoopConfig,
    eval_sets: EvalSets,
) -> dict:
    """ループ実行結果辞書を組み立てて返す。"""
    best = outcome.best
    return {
        "exit_reason": outcome.exit_reason,
        "original_description": outcome.original_description,
        "best_description": best["description"],
        "best_score": outcome.best_score,
        "best_train_score": f"{best['train_passed']}/{best['train_total']}",
        "best_test_score": f"{best['test_passed']}/{best['test_total']}" if eval_sets.test else None,
        "final_description": outcome.final_description,
        "iterations_run": len(outcome.history),
        "holdout": loop_cfg.holdout,
        "train_size": len(eval_sets.train),
        "test_size": len(eval_sets.test),
        "history": outcome.history,
    }


def run_loop(
    eval_set: list[dict],
    skill_path: Path,
    description_override: str | None,
    loop_cfg: LoopConfig,
) -> dict:
    """eval + 改善ループを実行する。"""
    name, original_description, content = parse_skill_md(skill_path)
    current_description = description_override or original_description
    skill_ctx = SkillContext(name=name, content=content)
    if loop_cfg.holdout > 0:
        train, test = split_eval_set(eval_set, loop_cfg.holdout)
        if loop_cfg.verbose:
            print(f"分割: train {len(train)} / test {len(test)}（holdout={loop_cfg.holdout}）", file=sys.stderr)
    else:
        train, test = eval_set, []
    eval_sets = EvalSets(train=train, test=test)
    history: list[dict] = []
    exit_reason = "unknown"
    for iteration in range(1, loop_cfg.max_iterations + 1):  # pragma: no branch  # 最終反復は必ず reason を返し break する
        state = IterationState(iteration=iteration, current_description=current_description)
        current_description, reason = _run_single_iteration(state, skill_ctx, eval_sets, loop_cfg, history)
        if reason is not None:
            exit_reason = reason
            break
    best, best_score = _find_best(history, eval_sets.test)
    if loop_cfg.verbose:
        print(f"\n終了理由: {exit_reason}", file=sys.stderr)
        print(f"最良スコア: {best_score}（反復 {best['iteration']}）", file=sys.stderr)
    outcome = LoopOutcome(
        exit_reason=exit_reason,
        original_description=original_description,
        best=best,
        best_score=best_score,
        final_description=current_description,
        history=history,
    )
    return _build_loop_result(outcome, loop_cfg, eval_sets)


def _build_loop_parser() -> argparse.ArgumentParser:
    """run_loop CLI 用の ArgumentParser を構築して返す。"""
    parser = argparse.ArgumentParser(description="eval + 改善ループを実行する")
    parser.add_argument("--eval-set", required=True, help="eval セット JSON へのパス")
    parser.add_argument("--skill-path", required=True, help="スキルディレクトリへのパス")
    parser.add_argument("--description", default=None, help="開始時の説明を上書きする")
    parser.add_argument("--num-workers", type=int, default=10, help="並列ワーカー数")
    parser.add_argument("--timeout", type=int, default=30, help="クエリごとのタイムアウト秒数")
    parser.add_argument("--max-iterations", type=int, default=5, help="改善の最大反復回数")
    parser.add_argument("--runs-per-query", type=int, default=3, help="クエリごとの実行回数")
    parser.add_argument("--trigger-threshold", type=float, default=0.5, help="トリガー率のしきい値")
    parser.add_argument("--holdout", type=float, default=0.4, help="テスト用に取り分ける eval セットの割合（0 で無効）")
    parser.add_argument("--model", required=True, help="改善に使うモデル")
    parser.add_argument("--verbose", action="store_true", help="進捗を stderr に表示する")
    parser.add_argument(
        "--results-dir",
        default=None,
        help="結果（results.json / log.txt）をこの日時付きサブディレクトリに保存する",
    )
    return parser


def main():
    """eval + 改善ループ CLI のエントリポイント。引数を解析してループを実行する。"""
    parser = _build_loop_parser()
    args = parser.parse_args()

    eval_set = json.loads(Path(args.eval_set).read_text())
    skill_path = Path(args.skill_path)

    if not (skill_path / "SKILL.md").exists():
        print(f"Error: No SKILL.md found at {skill_path}", file=sys.stderr)
        sys.exit(1)

    if args.results_dir:
        timestamp = time.strftime("%Y-%m-%d_%H%M%S")
        results_dir = Path(args.results_dir) / timestamp
        results_dir.mkdir(parents=True, exist_ok=True)
    else:
        results_dir = None

    log_dir = results_dir / "logs" if results_dir else None
    project_root = find_project_root()

    eval_cfg = EvalConfig(
        num_workers=args.num_workers,
        timeout=args.timeout,
        project_root=project_root,
        runs_per_query=args.runs_per_query,
        trigger_threshold=args.trigger_threshold,
        model=args.model,
    )
    loop_cfg = LoopConfig(
        max_iterations=args.max_iterations,
        holdout=args.holdout,
        verbose=args.verbose,
        log_dir=log_dir,
        eval_config=eval_cfg,
    )

    output = run_loop(
        eval_set=eval_set,
        skill_path=skill_path,
        description_override=args.description,
        loop_cfg=loop_cfg,
    )

    json_output = json.dumps(output, indent=2)
    print(json_output)
    if results_dir:
        (results_dir / "results.json").write_text(json_output)
        print(f"結果を保存しました: {results_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
