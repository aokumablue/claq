"""採点 JSON を集計し、事前に決めた規則で判定する。

使い方:
  python3 tally.py l6  <dir> <mapping.json> <grade.json>...   L6 の採用の規則（protocol.md）
  python3 tally.py gen <dir> <mapping.json> <grade.json>...   実装後の検証の生成側の合格基準（verify_protocol.md）
"""
import json, sys, statistics as st
from pathlib import Path

L6_TASKS = ["T1","T2","T3","T4","T5","T6","T7","T8","C1","C2"]; L6_CTRL = ["T4","T5","T8","C1","C2"]
GEN_TASKS = ["T1","T2","T3","T4","T5","T6","T7","T8"]; GEN_CTRL = ["T4","T5","T8"]


def l6(m, grade):
    """条件 A/B の UD と統制を集計し、採用の規則 1〜3 を判定する。"""
    per = {"A": [], "B": []}; task = {c: {t: [] for t in L6_TASKS} for c in "AB"}; ctrl = {"A": [], "B": []}
    for rid, row in grade.items():
        c = m[rid][0]
        per[c].append(sum(row[t]["ud"] for t in L6_TASKS))
        for t in L6_TASKS: task[c][t].append(row[t]["ud"])
        ctrl[c] += [row[t]["control"] for t in L6_CTRL]
    for c in "AB":
        print(f"  {c}: UD/回 {per[c]} 平均 {st.mean(per[c]):.1f} ±{st.stdev(per[c]):.1f}  統制 {sum(ctrl[c])}/{len(ctrl[c])}")
    print("  課題別 UD 平均 A→B: " + "  ".join(f"{t} {st.mean(task['A'][t]):.1f}→{st.mean(task['B'][t]):.1f}" for t in L6_TASKS))
    r1 = st.mean(per["A"]) - st.mean(per["B"]) >= 1
    r2 = sum(ctrl["B"]) == len(ctrl["B"])
    r3 = all(st.mean(task["B"][t]) - st.mean(task["A"][t]) < 0.5 for t in L6_TASKS)
    print(f"  規則1 {r1} 規則2 {r2} 規則3 {r3} → {'採用' if r1 and r2 and r3 else '不採用'}")


def gen(m, grade):
    """回答ごとに UD の課題平均が 1 以下かつ統制 100% かを判定する。"""
    ok_all = True
    for rid in sorted(grade, key=lambda r: m[r]):
        row = grade[rid]
        avg = sum(row[t]["ud"] for t in GEN_TASKS) / len(GEN_TASKS)
        ctrl = [row[t]["control"] for t in GEN_CTRL]
        ok = avg <= 1 and all(c == 1 for c in ctrl)
        ok_all &= ok
        print(f"  {m[rid]:10} UD 課題平均 {avg:.2f}  統制 {sum(ctrl)}/{len(ctrl)}  {'合格' if ok else '不合格'}")
    print(f"  → {'合格' if ok_all else '不合格'}")


mode, d, mapping, *grades = sys.argv[1:]
d = Path(d); m = json.loads((d/mapping).read_text())
for g in grades:
    print(f"== {g}")
    {"l6": l6, "gen": gen}[mode](m, json.loads((d/g).read_text()))
