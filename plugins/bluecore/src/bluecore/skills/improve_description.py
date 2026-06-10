#!/usr/bin/env python3
"""
eval 結果に基づいてスキル説明を改善する。

run_eval.py の結果を受け取り、LLM CLI を subprocess で呼び出して改善版の説明を生成する。
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .cli_runner import run_cli
from .utils import parse_skill_md


@dataclass(frozen=True)
class ImproveContext:
    """説明文改善に必要なコンテキスト（スキル名・内容・現在の説明・eval結果・履歴）。"""

    skill_name: str
    skill_content: str
    current_description: str
    eval_results: dict
    history: list
    test_results: dict | None = None


def _call_claude(prompt: str, model: str | None, timeout: int = 300) -> str:
    """stdin に prompt を流して LLM CLI を実行し、テキスト応答を返す。

    prompt には SKILL.md 全文が入るため argv に載せると長くなりすぎる。
    そのため stdin 経由で渡す。
    """
    args = ["-p", "--output-format", "text"]
    if model:
        args.extend(["--model", model])

    # CLAUDECODE を除去してネスト実行を許可する（対話端末衝突回避）。
    result = run_cli(args, stdin_input=prompt, timeout=timeout, strip_claudecode_env=True)
    if result.returncode != 0:
        raise RuntimeError(f"llm-cli exited {result.returncode}\nstderr: {result.stderr}")
    return result.stdout


def _build_prompt_suffix(skill_content: str) -> str:
    """プロンプトの後半（スキル内容・ガイドライン）を返す。"""
    return f"""</scores_summary>

スキル内容（スキルが何をするかの参考）:
<skill_content>
{skill_content}
</skill_content>

失敗結果を踏まえて、より正しくトリガーしやすい新しい説明文を書いてください。「失敗結果を踏まえて」と言っても、見えている具体例に過剰適合したくはありません。ですので、このスキルがトリガーすべきかどうかの具体的なクエリを延々と列挙するのではなく、失敗からユーザー意図や、このスキルが有用な状況・不要な状況のより広いカテゴリに一般化してください。そうする理由は 2 つあります。

1. 過剰適合を避けるため
2. 列挙が長くなると全クエリに注入される文量が増え、他のスキルも多いので、1 つの説明文に使える文字数を無駄にしたくないため

具体的には、正確さが少し落ちても構わないので、説明文は 100〜200 語程度に収めてください。1024 文字のハード制限があり、それを超えると切り詰められるので、余裕を持ってその下に収めてください。

このような説明文を書くときに有効だったポイントをいくつか示します:
- スキルは命令形で書くこと。「このスキルは〜する」より「〜するときにこのスキルを使う」
- スキル説明では、実装の詳細よりもユーザーが何を達成したいかという意図に焦点を当てること
- この説明は他のスキルとも競合するので、Claude の注意を引けるように、独自性と即時性のある表現にすること
- 何度も失敗しているなら、書きぶりを変えてみること。文の構造や言い回しを変えてみてください

いくつか違うスタイルを試す機会があるので、創造的に書き換えて構いません。最後に最もスコアが高かったものを採用します。

新しい説明文以外は出力しないでください。<new_description> タグの中だけに入れて返してください。"""


def _build_improve_prompt(ctx: ImproveContext) -> str:
    """説明文改善用プロンプトを組み立てて返す。"""
    failed_triggers = [r for r in ctx.eval_results["results"] if r["should_trigger"] and not r["pass"]]
    false_triggers = [r for r in ctx.eval_results["results"] if not r["should_trigger"] and not r["pass"]]
    train_score = f"{ctx.eval_results['summary']['passed']}/{ctx.eval_results['summary']['total']}"
    if ctx.test_results:
        test_score = f"{ctx.test_results['summary']['passed']}/{ctx.test_results['summary']['total']}"
        scores_summary = f"学習用: {train_score}, 検証用: {test_score}"
    else:
        scores_summary = f"学習用: {train_score}"
    prompt = (
        f'あなたは "{ctx.skill_name}" というスキルの説明文を最適化しています。'
        "スキルはプロンプトに少し似ていますが、段階的に情報を開示する仕組みです。"
        'この説明は "available_skills" 一覧に表示されます。\n\n'
        f'現在の説明:\n<current_description>\n"{ctx.current_description}"\n</current_description>\n\n'
        f"現在のスコア ({scores_summary}):\n<scores_summary>\n"
    )
    if failed_triggers:
        prompt += "トリガー漏れ（本来トリガーすべきだった）:\n"
        for r in failed_triggers:
            prompt += f'  - "{r["query"]}"（{r["triggers"]}/{r["runs"]} 回トリガー）\n'
        prompt += "\n"
    if false_triggers:
        prompt += "誤トリガー（トリガーすべきでなかった）:\n"
        for r in false_triggers:
            prompt += f'  - "{r["query"]}"（{r["triggers"]}/{r["runs"]} 回トリガー）\n'
        prompt += "\n"
    if ctx.history:
        prompt += _format_history_section(ctx.history)
    return prompt + _build_prompt_suffix(ctx.skill_content)


def _format_history_section(history: list[dict]) -> str:
    """過去の試行履歴をプロンプト用テキストとしてフォーマットして返す。"""
    text = "過去の試行（これらは繰り返さず、構造を変えてください）:\n\n"
    for h in history:
        train_s = f"{h.get('train_passed', h.get('passed', 0))}/{h.get('train_total', h.get('total', 0))}"
        test_s = (
            f"{h.get('test_passed', '?')}/{h.get('test_total', '?')}" if h.get("test_passed") is not None else None
        )
        score_str = f"train={train_s}" + (f", test={test_s}" if test_s else "")
        text += f"<attempt {score_str}>\n"
        text += f'説明: "{h["description"]}"\n'
        if "results" in h:
            text += "学習結果:\n"
            for r in h["results"]:
                status = "合格" if r["pass"] else "不合格"
                text += f'  [{status}] "{r["query"][:80]}"（{r["triggers"]}/{r["runs"]} 回トリガー）\n'
        if h.get("note"):
            text += f"備考: {h['note']}\n"
        text += "</attempt>\n\n"
    return text


def _shorten_description_if_needed(
    description: str,
    prompt: str,
    model: str | None,
    transcript: dict,
) -> str:
    """1024 文字超の説明を再度 LLM に短縮依頼し、短縮版を返す。"""
    if len(description) <= 1024:
        return description

    shorten_prompt = (
        f"{prompt}\n\n"
        "---\n\n"
        f"A previous attempt produced this description, which at "
        f"{len(description)} characters is over the 1024-character hard limit:\n\n"
        f'"{description}"\n\n'
        "Rewrite it to be under 1024 characters while keeping the most "
        "important trigger words and intent coverage. Respond with only "
        "the new description in <new_description> tags."
    )
    shorten_text = _call_claude(shorten_prompt, model)
    match = re.search(r"<new_description>(.*?)</new_description>", shorten_text, re.DOTALL)
    shortened = match.group(1).strip().strip('"') if match else shorten_text.strip().strip('"')

    transcript["rewrite_prompt"] = shorten_prompt
    transcript["rewrite_response"] = shorten_text
    transcript["rewrite_description"] = shortened
    transcript["rewrite_char_count"] = len(shortened)
    return shortened


def improve_description(
    ctx: ImproveContext,
    model: str | None,
    log_dir: Path | None = None,
    iteration: int | None = None,
) -> str:
    """eval 結果に基づいて Claude に説明文の改善を依頼する。"""
    prompt = _build_improve_prompt(ctx)
    text = _call_claude(prompt, model)

    match = re.search(r"<new_description>(.*?)</new_description>", text, re.DOTALL)
    description = match.group(1).strip().strip('"') if match else text.strip().strip('"')

    transcript: dict = {
        "iteration": iteration,
        "prompt": prompt,
        "response": text,
        "parsed_description": description,
        "char_count": len(description),
        "over_limit": len(description) > 1024,
    }

    description = _shorten_description_if_needed(description, prompt, model, transcript)
    transcript["final_description"] = description

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"improve_iter_{iteration or 'unknown'}.json"
        log_file.write_text(json.dumps(transcript, indent=2), encoding="utf-8")

    return description


def _build_improve_output(
    new_description: str,
    current_description: str,
    eval_results: dict,
    history: list[dict],
) -> dict:
    """improve_description の JSON 出力用辞書を構築して返す。"""
    return {
        "description": new_description,
        "history": history
        + [
            {
                "description": current_description,
                "passed": eval_results["summary"]["passed"],
                "failed": eval_results["summary"]["failed"],
                "total": eval_results["summary"]["total"],
                "results": eval_results["results"],
            }
        ],
    }


def main():
    """improve_description CLI のエントリポイント。引数を解析してスキル説明の改善を実行する。"""
    parser = argparse.ArgumentParser(description="eval 結果に基づいてスキル説明を改善する")
    parser.add_argument("--eval-results", required=True, help="eval 結果 JSON へのパス（run_eval.py の出力）")
    parser.add_argument("--skill-path", required=True, help="スキルディレクトリへのパス")
    parser.add_argument("--history", default=None, help="history JSON へのパス（過去の試行）")
    parser.add_argument("--model", required=True, help="改善に使うモデル（LLM CLI に渡す）")
    parser.add_argument("--verbose", action="store_true", help="思考内容を stderr に表示する")
    args = parser.parse_args()

    skill_path = Path(args.skill_path)
    if not (skill_path / "SKILL.md").exists():
        print(f"エラー: {skill_path} に SKILL.md が見つかりません", file=sys.stderr)
        sys.exit(1)

    eval_results = json.loads(Path(args.eval_results).read_text(encoding="utf-8"))
    history = []
    if args.history:
        history = json.loads(Path(args.history).read_text(encoding="utf-8"))

    name, _, content = parse_skill_md(skill_path)
    current_description = eval_results["description"]

    if args.verbose:
        print(f"現在の説明: {current_description}", file=sys.stderr)
        print(f"スコア: {eval_results['summary']['passed']}/{eval_results['summary']['total']}", file=sys.stderr)

    new_description = improve_description(
        ImproveContext(
            skill_name=name,
            skill_content=content,
            current_description=current_description,
            eval_results=eval_results,
            history=history,
        ),
        model=args.model,
    )

    if args.verbose:
        print(f"改善後: {new_description}", file=sys.stderr)

    output = _build_improve_output(new_description, current_description, eval_results, history)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
