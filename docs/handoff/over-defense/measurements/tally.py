"""採点 JSON を条件ごとに集計し、事前に決めた採用の規則で判定する。"""
import json, sys, statistics as st
from pathlib import Path
d = Path(sys.argv[1]); m = json.loads((d/"mapping.json").read_text())
TASKS = ["T1","T2","T3","T4","T5","T6","T7","T8","C1","C2"]; CTRL = ["T4","T5","T8","C1","C2"]
for g in sys.argv[2:]:
    grade = json.loads((d/g).read_text())
    per = {"A": [], "B": []}; task = {c: {t: [] for t in TASKS} for c in "AB"}; ctrl = {"A": [], "B": []}
    for rid, row in grade.items():
        c = m[rid][0]
        per[c].append(sum(row[t]["ud"] for t in TASKS))
        for t in TASKS: task[c][t].append(row[t]["ud"])
        ctrl[c] += [row[t]["control"] for t in CTRL]
    print(f"== {g}")
    for c in "AB":
        print(f"  {c}: UD/回 {per[c]} 平均 {st.mean(per[c]):.1f} ±{st.stdev(per[c]):.1f}  統制 {sum(ctrl[c])}/{len(ctrl[c])}")
    print("  課題別 UD 平均 A→B: " + "  ".join(f"{t} {st.mean(task['A'][t]):.1f}→{st.mean(task['B'][t]):.1f}" for t in TASKS))
    r1 = st.mean(per["A"]) - st.mean(per["B"]) >= 1
    r2 = sum(ctrl["B"]) == len(ctrl["B"])
    r3 = all(st.mean(task["B"][t]) - st.mean(task["A"][t]) < 0.5 for t in TASKS)
    print(f"  規則1 {r1} 規則2 {r2} 規則3 {r3} → {'採用' if r1 and r2 and r3 else '不採用'}")
