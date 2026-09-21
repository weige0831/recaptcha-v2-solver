"""分析稳定性批次的日志，定位失败原因与轮次浪费点

用法:
    python analyze_batch.py stability/r1_image
    python analyze_batch.py stability/r1_image stability/r1_audio
"""
import os
import re
import sys
from collections import Counter


def analyze(path):
    files = sorted(f for f in os.listdir(path) if f.startswith("run_") and f.endswith(".log"))
    print("=" * 78)
    print(f"批次: {path}   共 {len(files)} 次运行")
    print("=" * 78)

    fails = []
    total_loops = 0
    agg = Counter()
    per_run = []

    for fn in files:
        with open(os.path.join(path, fn), encoding="utf-8", errors="replace") as f:
            t = f.read()

        ok = "🎉 成功" in t
        dur = re.search(r"耗时 ([\d.]+)s", t)
        loops = len(re.findall(r"---\s*(?:\[音频\]\s*)?第 \d+ 次循环", t))

        # 轮次浪费点统计
        select_more = len(re.findall(r"请选择更多|请选择所有|select all matching|Select all matching", t, re.I))
        deadlock = len(re.findall(r"陷入死局", t))
        relaxed = len(re.findall(r"降阈值重检", t))
        dyn_upgrade = len(re.findall(r"升级为动态模式", t))
        no_target = len(re.findall(r"本轮未发现目标", t))
        unsupported = len(re.findall(r"遇到不支持的类别", t))
        no_tile = len(re.findall(r"组件未下发挑战图块", t))
        blocked = len(re.findall(r"检测到 Google 端限制", t))
        dyn_wait = len(re.findall(r"动态模式：智能等待", t))
        wait_vals = [float(x) for x in re.findall(r"等待了 ([\d.]+)s", t)]
        wait_to = len(re.findall(r"等待超时", t))
        notready = len(re.findall(r"音频挑战未就绪|图片元素未加载|验证窗口未找到", t))
        fetch_fail = len(re.findall(r"未取到有效音频|未取到音频", t))
        trans_fail = len(re.findall(r"转写失败", t))
        play = len(re.findall(r"点击 PLAY", t))
        reloads = len(re.findall(r"执行刷新|强制刷新", t))

        # 题目类别分布
        cats = re.findall(r"类别 '([^']+)'", t)

        per_run.append({
            "file": fn, "ok": ok, "dur": float(dur.group(1)) if dur else None,
            "loops": loops, "select_more": select_more, "no_target": no_target,
            "reloads": reloads,
        })
        total_loops += loops
        agg.update({
            "选更多": select_more, "死局强刷": deadlock, "降阈值重检": relaxed,
            "升级为动态": dyn_upgrade, "本轮无目标": no_target, "不支持类别": unsupported,
            "无图块": no_tile, "限流": blocked, "动态等待": dyn_wait,
            "等待超时": wait_to, "未就绪": notready, "取音频失败": fetch_fail,
            "转写失败": trans_fail, "PLAY": play, "刷新换题": reloads,
        })
        agg["刷新换题"] += 0
        for c in cats:
            agg[f"  类别 {c}"] += 1

        if not ok:
            mr = re.search(r"❌ 失败 \([\d.]+s\): (.+)", t)
            fails.append((fn, mr.group(1).strip()[:90] if mr else "未知"))

    okruns = [r for r in per_run if r["ok"]]
    print(f"\n成功 {len(okruns)}/{len(per_run)} = {len(okruns)/len(per_run)*100:.0f}%")
    print(f"总循环轮数 {total_loops}，成功run平均 {total_loops/max(1,len(per_run)):.1f} 轮/次")
    if okruns:
        d = sorted(r["dur"] for r in okruns if r["dur"])
        if d:
            print(f"成功耗时 最短{d[0]:.0f}s 中位{d[len(d)//2]:.0f}s 最长{d[-1]:.0f}s")

    print("\n逐次:")
    print(f"  {'#':<5}{'结果':<6}{'耗时':<8}{'轮数':<6}{'选更多':<7}{'无目标':<7}{'刷新':<6}")
    for r in per_run:
        dur = f"{r['dur']:.0f}s" if r["dur"] else "-"
        print(f"  {r['file'][4:6]:<5}{'✅' if r['ok'] else '❌':<6}"
              f"{dur:<8}{r['loops']:<6}{r['select_more']:<7}{r['no_target']:<7}{r['reloads']:<6}")

    print("\n轮次浪费点汇总（全批次）:")
    for k, v in agg.most_common():
        if not k.startswith("  类别") and v:
            print(f"  {k:14} {v}")

    print("\n题目类别出现次数:")
    for k, v in agg.most_common():
        if k.startswith("  类别"):
            print(f"  {k}  {v}")

    if fails:
        print("\n失败明细:")
        for fn, reason in fails:
            print(f"  {fn}: {reason}")
    return per_run


if __name__ == "__main__":
    for p in sys.argv[1:]:
        if os.path.isdir(p):
            analyze(p)
            print()
        else:
            print(f"跳过（不是目录）: {p}")
