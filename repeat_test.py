"""重复多次运行，统计成功率与耗时"""
import subprocess
import sys
import re
import time

RUNS = 2

print(f"将连续运行 {RUNS} 次完整求解流程\n")
results = []

for i in range(1, RUNS + 1):
    print("=" * 70)
    print(f"第 {i}/{RUNS} 次运行")
    print("=" * 70)
    t0 = time.time()
    p = subprocess.run(
        [sys.executable, "recaptcha_solver.py"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600,
    )
    elapsed = time.time() - t0
    out = p.stdout + p.stderr

    tok = re.search(r"🎉 Token: (\S+)", out)
    err = re.search(r"❌ Error: (.+)", out)
    loops = re.findall(r"--- 第 (\d+) 次循环检测 ---", out)
    models = re.findall(r"使用 (YOLO11x|GroundingDINO) 检测", out)
    modes = re.findall(r"检测到(动态|静态)验证模式", out)
    clicks = re.findall(r"🖱️ 点击 (\d+) 个图块", out)

    if tok:
        print(f"  ✅ 成功  |  耗时 {elapsed:.0f}s  |  循环 {len(loops)} 轮  |  token 长度 {len(tok.group(1))}")
        print(f"     验证模式: {sorted(set(modes))}  模型: {sorted(set(models))}  点击轮次: {clicks}")
        results.append((True, elapsed, len(tok.group(1))))
    else:
        print(f"  ❌ 失败  |  耗时 {elapsed:.0f}s")
        print(f"     错误: {err.group(1) if err else '(无明确错误信息)'}")
        print(f"     循环 {len(loops)} 轮, 模式 {sorted(set(modes))}, 模型 {sorted(set(models))}")
        tail = [l for l in out.splitlines() if l.strip()][-12:]
        print("     日志尾部:")
        for l in tail:
            print(f"       {l}")
        results.append((False, elapsed, 0))
    print()

print("=" * 70)
print("汇总")
print("=" * 70)
ok = sum(1 for r in results if r[0])
print(f"成功率: {ok}/{RUNS}")
for i, (succ, el, tl) in enumerate(results, 1):
    print(f"  第{i}次: {'成功' if succ else '失败'}  耗时 {el:.0f}s  token {tl} 字符")
