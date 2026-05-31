#!/usr/bin/env python3
"""スキル説明のトリガー評価を実行する。

スキルの説明が、与えられたクエリ群に対して Claude をトリガーするか
（スキルを読むか）を検証し、結果を JSON で出力する。
"""

import argparse
import json
import os
import select
import subprocess
import sys
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from .cli_runner import detect_cli_binary
from .utils import parse_skill_md


def find_project_root() -> Path:
    """cwd から .claude/ を探しながらプロジェクトルートを見つける。

    プロジェクトルート検出に合わせることで、作成した
    コマンドファイルが参照される場所に置かれる。
    """
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / ".claude").is_dir():
            return parent
    return current


def _write_command_file(
    command_file: Path,
    skill_name: str,
    skill_description: str,
) -> None:
    """LLM の available_skills に見えるコマンドファイルを作成する。"""
    command_file.parent.mkdir(parents=True, exist_ok=True)
    indented_desc = "\n  ".join(skill_description.split("\n"))
    command_content = (
        f"---\n"
        f"description: |\n"
        f"  {indented_desc}\n"
        f"---\n\n"
        f"# {skill_name}\n\n"
        f"This skill handles: {skill_description}\n"
    )
    command_file.write_text(command_content)


def _build_query_cmd(binary: str, query: str, model: str | None) -> list[str]:
    """クエリ実行用 LLM CLI コマンドリストを構築して返す。"""
    if binary == "claude":
        cmd = [binary, "-p", query, "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    else:
        cmd = [binary, "-p", query, "--output-format", "json"]
    if model:
        cmd.extend(["--model", model])
    return cmd


def _process_stream_event(
    se: dict,
    clean_name: str,
    pending_tool_name_ref: list,
    accumulated_json_ref: list,
) -> bool | None:
    """stream_event を処理し、トリガー判定結果（True/False）または継続（None）を返す。"""
    se_type = se.get("type", "")

    if se_type == "content_block_start":
        cb = se.get("content_block", {})
        if cb.get("type") == "tool_use":
            tool_name = cb.get("name", "")
            if tool_name in ("Skill", "Read"):
                pending_tool_name_ref[0] = tool_name
                accumulated_json_ref[0] = ""
            else:
                pending_tool_name_ref[0] = None
        return None

    if se_type == "content_block_delta" and pending_tool_name_ref[0]:
        delta = se.get("delta", {})
        if delta.get("type") == "input_json_delta":
            accumulated_json_ref[0] += delta.get("partial_json", "")
            if clean_name in accumulated_json_ref[0]:
                return True
        return None

    if se_type in ("content_block_stop", "message_stop"):
        if pending_tool_name_ref[0]:
            return clean_name in accumulated_json_ref[0]
        if se_type == "message_stop":
            return False
        return None

    return None


def _scan_output_for_trigger(
    process: subprocess.Popen,
    clean_name: str,
    timeout: int,
) -> bool:
    """プロセスの stdout をストリームで読み、スキルトリガーを検出して bool を返す。"""
    triggered = False
    start_time = time.time()
    buffer = ""
    pending_tool_name_ref = [None]
    accumulated_json_ref = [""]

    try:
        while time.time() - start_time < timeout:
            if process.poll() is not None:
                remaining = process.stdout.read()
                if remaining:
                    buffer += remaining.decode("utf-8", errors="replace")
                break

            ready, _, _ = select.select([process.stdout], [], [], 1.0)
            if not ready:
                continue

            chunk = os.read(process.stdout.fileno(), 8192)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="replace")

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if event.get("type") == "stream_event":
                    result = _process_stream_event(
                        event.get("event", {}), clean_name,
                        pending_tool_name_ref, accumulated_json_ref,
                    )
                    if result is not None:
                        return result

                elif event.get("type") == "assistant":
                    message = event.get("message", {})
                    for content_item in message.get("content", []):
                        if content_item.get("type") != "tool_use":
                            continue
                        tool_name = content_item.get("name", "")
                        tool_input = content_item.get("input", {})
                        if tool_name == "Skill" and clean_name in tool_input.get("skill", ""):
                            triggered = True
                        elif tool_name == "Read" and clean_name in tool_input.get("file_path", ""):
                            triggered = True
                    return triggered

                elif event.get("type") == "result":
                    return triggered
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    return triggered


def run_single_query(
    query: str,
    skill_name: str,
    skill_description: str,
    timeout: int,
    project_root: str,
    model: str | None = None,
) -> bool:
    """単一クエリを実行し、スキルがトリガーされたかを返す。

    .claude/commands/ にコマンドファイルを作成して LLM の
    available_skills に見えるようにし、そのうえで生のクエリを使って
    LLM CLI を実行する。
    claude 環境では stream-json + --include-partial-messages で早期トリガー判定、
    copilot 環境では json 出力で完了後に判定する。
    """
    binary = detect_cli_binary()
    unique_id = uuid.uuid4().hex[:8]
    clean_name = f"{skill_name}-skill-{unique_id}"
    project_commands_dir = Path(project_root) / ".claude" / "commands"
    command_file = project_commands_dir / f"{clean_name}.md"

    try:
        _write_command_file(command_file, skill_name, skill_description)
        cmd = _build_query_cmd(binary, query, model)
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=project_root,
            env=env,
        )
        return _scan_output_for_trigger(process, clean_name, timeout)
    finally:
        if command_file.exists():
            command_file.unlink()


def _collect_futures(
    eval_set: list[dict],
    skill_name: str,
    description: str,
    timeout: int,
    project_root: Path,
    runs_per_query: int,
    model: str | None,
    num_workers: int,
) -> tuple[dict[str, list[bool]], dict[str, dict]]:
    """全クエリを並列実行し、クエリごとのトリガー結果と item マップを返す。"""
    query_triggers: dict[str, list[bool]] = {}
    query_items: dict[str, dict] = {}

    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        future_to_info = {}
        for item in eval_set:
            for run_idx in range(runs_per_query):
                future = executor.submit(
                    run_single_query,
                    item["query"],
                    skill_name,
                    description,
                    timeout,
                    str(project_root),
                    model,
                )
                future_to_info[future] = (item, run_idx)

        for future in as_completed(future_to_info):
            item, _ = future_to_info[future]
            query = item["query"]
            query_items[query] = item
            if query not in query_triggers:
                query_triggers[query] = []
            try:
                query_triggers[query].append(future.result())
            except Exception as e:
                print(f"警告: クエリに失敗しました: {e}", file=sys.stderr)
                query_triggers[query].append(False)

    return query_triggers, query_items


def run_eval(
    eval_set: list[dict],
    skill_name: str,
    description: str,
    num_workers: int,
    timeout: int,
    project_root: Path,
    runs_per_query: int = 1,
    trigger_threshold: float = 0.5,
    model: str | None = None,
) -> dict:
    """全 eval セットを並列実行し、結果を返す。"""
    query_triggers, query_items = _collect_futures(
        eval_set, skill_name, description, timeout, project_root,
        runs_per_query, model, num_workers,
    )

    results = []
    for query, triggers in query_triggers.items():
        item = query_items[query]
        trigger_rate = sum(triggers) / len(triggers)
        should_trigger = item["should_trigger"]
        did_pass = trigger_rate >= trigger_threshold if should_trigger else trigger_rate < trigger_threshold
        results.append(
            {
                "query": query,
                "should_trigger": should_trigger,
                "trigger_rate": trigger_rate,
                "triggers": sum(triggers),
                "runs": len(triggers),
                "pass": did_pass,
            }
        )

    passed = sum(1 for r in results if r["pass"])
    total = len(results)

    return {
        "skill_name": skill_name,
        "description": description,
        "results": results,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
        },
    }


def main():
    """eval CLI のエントリポイント。引数を解析してトリガー評価を実行する。"""
    parser = argparse.ArgumentParser(description="スキル説明のトリガー評価を実行する")
    parser.add_argument("--eval-set", required=True, help="eval セット JSON へのパス")
    parser.add_argument("--skill-path", required=True, help="スキルディレクトリへのパス")
    parser.add_argument("--description", default=None, help="テスト対象の説明を上書きする")
    parser.add_argument("--num-workers", type=int, default=10, help="並列ワーカー数")
    parser.add_argument("--timeout", type=int, default=30, help="クエリごとのタイムアウト秒数")
    parser.add_argument("--runs-per-query", type=int, default=3, help="クエリごとの実行回数")
    parser.add_argument("--trigger-threshold", type=float, default=0.5, help="トリガー率のしきい値")
    parser.add_argument("--model", default=None, help="LLM CLI に使うモデル（既定: 現在の設定モデル）")
    parser.add_argument("--verbose", action="store_true", help="進捗を stderr に表示する")
    args = parser.parse_args()

    eval_set = json.loads(Path(args.eval_set).read_text())
    skill_path = Path(args.skill_path)

    if not (skill_path / "SKILL.md").exists():
        print(f"エラー: {skill_path} に SKILL.md が見つかりません", file=sys.stderr)
        sys.exit(1)

    name, original_description, content = parse_skill_md(skill_path)
    description = args.description or original_description
    project_root = find_project_root()

    if args.verbose:
        print(f"評価中: {description}", file=sys.stderr)

    output = run_eval(
        eval_set=eval_set,
        skill_name=name,
        description=description,
        num_workers=args.num_workers,
        timeout=args.timeout,
        project_root=project_root,
        runs_per_query=args.runs_per_query,
        trigger_threshold=args.trigger_threshold,
        model=args.model,
    )

    if args.verbose:
        summary = output["summary"]
        print(f"結果: {summary['passed']}/{summary['total']} 件が合格", file=sys.stderr)
        for r in output["results"]:
            status = "合格" if r["pass"] else "不合格"
            rate_str = f"{r['triggers']}/{r['runs']}"
            print(f"  [{status}] rate={rate_str} expected={r['should_trigger']}: {r['query'][:70]}", file=sys.stderr)

    print(json.dumps(output, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
