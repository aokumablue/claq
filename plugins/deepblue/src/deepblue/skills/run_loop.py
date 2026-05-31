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
from pathlib import Path

from .improve_description import improve_description
from .run_eval import find_project_root, run_eval
from .utils import parse_skill_md


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
    name: str, current_description: str, all_queries: list[dict],
    num_workers: int, timeout: int, project_root: Path,
    runs_per_query: int, trigger_threshold: float, model: str,
) -> tuple[dict, float]:
    """eval を実行し (all_results, elapsed_seconds) を返す。"""
    t0 = time.time()
    results = run_eval(
        eval_set=all_queries, skill_name=name, description=current_description,
        num_workers=num_workers, timeout=timeout, project_root=project_root,
        runs_per_query=runs_per_query, trigger_threshold=trigger_threshold, model=model,
    )
    return results, time.time() - t0


def _append_history(
    history: list[dict], iteration: int, description: str,
    train_summary: dict, test_summary: dict | None,
    train_results: dict, test_results: dict | None,
) -> None:
    """反復結果を history リストに追記する。"""
    history.append({
        "iteration": iteration, "description": description,
        "train_passed": train_summary["passed"], "train_failed": train_summary["failed"],
        "train_total": train_summary["total"], "train_results": train_results["results"],
        "test_passed": test_summary["passed"] if test_summary else None,
        "test_failed": test_summary["failed"] if test_summary else None,
        "test_total": test_summary["total"] if test_summary else None,
        "test_results": test_results["results"] if test_results else None,
    })


def _run_improve(
    name: str, content: str, current_description: str,
    train_results: dict, blinded_history: list[dict],
    model: str, log_dir: Path | None, iteration: int, verbose: bool,
) -> str:
    """説明文改善を実行し新しい説明文を返す。"""
    if verbose:
        print("\n説明を改善しています...", file=sys.stderr)
    t0 = time.time()
    new_desc = improve_description(
        skill_name=name, skill_content=content, current_description=current_description,
        eval_results=train_results, history=blinded_history,
        model=model, log_dir=log_dir, iteration=iteration,
    )
    if verbose:
        print(f"提案結果（{time.time() - t0:.1f}s）: {new_desc}", file=sys.stderr)
    return new_desc


def _run_single_iteration(
    iteration: int, max_iterations: int, current_description: str,
    name: str, content: str, train_set: list[dict], test_set: list[dict],
    num_workers: int, timeout: int, project_root: Path, runs_per_query: int,
    trigger_threshold: float, model: str, verbose: bool,
    history: list[dict], log_dir: Path | None,
) -> tuple[str, str | None]:
    """1反復分の eval・採点・改善を実行し、(新しい説明, 終了理由|None) を返す。"""
    _print_iteration_header(iteration, max_iterations, current_description, verbose)
    all_results, eval_elapsed = _eval_queries(
        name, current_description, train_set + test_set,
        num_workers, timeout, project_root, runs_per_query, trigger_threshold, model,
    )
    train_results, test_results, test_summary = _split_eval_results(all_results, train_set, test_set)
    train_summary = train_results["summary"]
    _append_history(history, iteration, current_description, train_summary, test_summary, train_results, test_results)
    if verbose:
        _print_eval_stats("学習用", train_results["results"], eval_elapsed)
        if test_summary:
            _print_eval_stats("検証用", test_results["results"], 0)  # type: ignore[index]
    if train_summary["failed"] == 0:
        if verbose:
            print(f"\nAll train queries passed on iteration {iteration}!", file=sys.stderr)
        return current_description, f"all_passed (iteration {iteration})"
    if iteration == max_iterations:
        if verbose:
            print(f"\nMax iterations reached ({max_iterations}).", file=sys.stderr)
        return current_description, f"max_iterations ({max_iterations})"
    blinded = [{k: v for k, v in h.items() if not k.startswith("test_")} for h in history]
    return _run_improve(name, content, current_description, train_results, blinded, model, log_dir, iteration, verbose), None


def _find_best(history: list[dict], test_set: list[dict]) -> tuple[dict, str]:
    """最良の反復結果と得点文字列を返す。"""
    if test_set:
        best = max(history, key=lambda h: h["test_passed"] or 0)
        return best, f"{best['test_passed']}/{best['test_total']}"
    best = max(history, key=lambda h: h["train_passed"])
    return best, f"{best['train_passed']}/{best['train_total']}"


def _build_loop_result(
    exit_reason: str, original_description: str, best: dict, best_score: str,
    final_description: str, history: list[dict], holdout: float,
    train_set: list[dict], test_set: list[dict],
) -> dict:
    """ループ実行結果辞書を組み立てて返す。"""
    return {
        "exit_reason": exit_reason,
        "original_description": original_description,
        "best_description": best["description"],
        "best_score": best_score,
        "best_train_score": f"{best['train_passed']}/{best['train_total']}",
        "best_test_score": f"{best['test_passed']}/{best['test_total']}" if test_set else None,
        "final_description": final_description,
        "iterations_run": len(history),
        "holdout": holdout,
        "train_size": len(train_set),
        "test_size": len(test_set),
        "history": history,
    }


def run_loop(
    eval_set: list[dict],
    skill_path: Path,
    description_override: str | None,
    num_workers: int,
    timeout: int,
    max_iterations: int,
    runs_per_query: int,
    trigger_threshold: float,
    holdout: float,
    model: str,
    verbose: bool,
    log_dir: Path | None = None,
) -> dict:
    """eval + 改善ループを実行する。"""
    project_root = find_project_root()
    name, original_description, content = parse_skill_md(skill_path)
    current_description = description_override or original_description
    if holdout > 0:
        train_set, test_set = split_eval_set(eval_set, holdout)
        if verbose:
            print(f"分割: train {len(train_set)} / test {len(test_set)}（holdout={holdout}）", file=sys.stderr)
    else:
        train_set, test_set = eval_set, []
    history: list[dict] = []
    exit_reason = "unknown"
    for iteration in range(1, max_iterations + 1):
        current_description, reason = _run_single_iteration(
            iteration, max_iterations, current_description,
            name, content, train_set, test_set,
            num_workers, timeout, project_root, runs_per_query,
            trigger_threshold, model, verbose, history, log_dir,
        )
        if reason is not None:
            exit_reason = reason
            break
    best, best_score = _find_best(history, test_set)
    if verbose:
        print(f"\n終了理由: {exit_reason}", file=sys.stderr)
        print(f"最良スコア: {best_score}（反復 {best['iteration']}）", file=sys.stderr)
    return _build_loop_result(
        exit_reason, original_description, best, best_score,
        current_description, history, holdout, train_set, test_set,
    )


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

    output = run_loop(
        eval_set=eval_set,
        skill_path=skill_path,
        description_override=args.description,
        num_workers=args.num_workers,
        timeout=args.timeout,
        max_iterations=args.max_iterations,
        runs_per_query=args.runs_per_query,
        trigger_threshold=args.trigger_threshold,
        holdout=args.holdout,
        model=args.model,
        verbose=args.verbose,
        log_dir=log_dir,
    )

    json_output = json.dumps(output, indent=2)
    print(json_output)
    if results_dir:
        (results_dir / "results.json").write_text(json_output)
        print(f"結果を保存しました: {results_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
