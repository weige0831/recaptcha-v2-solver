"""成功率实测：单进程内连续求解 N 次

模型只加载一次；每次调用之间由 _respect_cooldown() 自动等待。
输出每次的结果、循环轮数、耗时、失败原因，以及总体成功率。
"""
import re
import sys
import time

import recaptcha_solver as rs

SITEKEY = "6LfD3PIbAAAAAJs_eEHvoOl75_83eXSqpPSRFJ_u"
URL = "https://2captcha.com/demo/recaptcha-v2"
RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 5

print(f"成功率实测：连续 {RUNS} 次   单次超时 {rs.DEFAULT_SOLVE_TIMEOUT}s   "
      f"调用间隔 {rs.MIN_SOLVE_INTERVAL:.0f}s\n", flush=True)

results = []

t_all = time.time()
with rs.RecaptchaSolver(headless=True) as solver:
    for i in range(1, RUNS + 1):
        print("=" * 70, flush=True)
        print(f"第 {i}/{RUNS} 次求解", flush=True)
        print("=" * 70, flush=True)
        t0 = time.time()
        try:
            token = solver.solve(SITEKEY, URL)
            el = time.time() - t0
            results.append(("成功", el, None))
            print(f"\n✅ 第 {i} 次成功：{el:.0f}s  token {len(token)} 字符\n", flush=True)
        except rs.RecaptchaBlockedError as e:
            el = time.time() - t0
            results.append(("失败", el, f"被 Google 限流: {e}"))
            print(f"\n🚫 第 {i} 次失败：{el:.0f}s  {e}\n", flush=True)
        except Exception as e:
            el = time.time() - t0
            results.append(("失败", el, f"{type(e).__name__}: {e}"))
            print(f"\n❌ 第 {i} 次失败：{el:.0f}s  {type(e).__name__}: {e}\n", flush=True)

total = time.time() - t_all
ok = sum(1 for r in results if r[0] == "成功")

print("=" * 70, flush=True)
print("结果", flush=True)
print("=" * 70, flush=True)
for i, (st, el, why) in enumerate(results, 1):
    line = f"  第{i}次: {st}  {el:.0f}s"
    if why:
        line += f"   原因: {why[:120]}"
    print(line, flush=True)

print(flush=True)
print(f"成功率: {ok}/{RUNS}  ({ok / RUNS * 100:.0f}%)", flush=True)
if ok:
    durs = [r[1] for r in results if r[0] == "成功"]
    print(f"成功耗时: 最短 {min(durs):.0f}s  最长 {max(durs):.0f}s  平均 {sum(durs)/len(durs):.0f}s", flush=True)
print(f"总耗时: {total/60:.1f} 分钟", flush=True)
