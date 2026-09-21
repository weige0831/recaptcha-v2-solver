"""给 stability_test.py 增加 --retries：失败后换节点重试

配合"坏 IP 快速失败"：抽到信誉差的出口节点时题链会病态变长，
与其耗满时间预算，不如判失败后换一个节点重跑。

结果里同时记录 attempts（实际尝试次数），便于区分"一次过"和"重试后过"。
"""
import sys

PATH = "stability_test.py"
with open(PATH, encoding="utf-8") as f:
    s = f.read()

applied = []


def sub(old, new, label, count=1):
    global s
    n = s.count(old)
    if n != count:
        print(f"  x [{label}] 命中 {n} 次（期望 {count}）")
        return False
    s = s.replace(old, new, count)
    applied.append(label)
    print(f"  ok [{label}]")
    return True


ok = True

# 1) run_once 返回记录里带上尝试次数
ok &= sub(
    """    return {
        "idx": idx, "mode": mode, "ok": ok, "exit_code": rc, "timed_out": timed_out,""",
    """    return {
        "idx": idx, "mode": mode, "ok": ok, "exit_code": rc, "timed_out": timed_out,
        "attempts": 1,""",
    "记录 attempts 字段")

# 2) 主循环支持重试（每次重试换一个节点）
old_loop = """        r = run_once(args.mode, i, solve_timeout, args.tag, outdir,
                     headed=args.headed, url=args.url, sitekey=args.sitekey)
        results.append(r)"""
new_loop = """        r = run_once(args.mode, i, solve_timeout, args.tag, outdir,
                     headed=args.headed, url=args.url, sitekey=args.sitekey)
        # 抽到信誉差的出口 IP 时题链会病态变长。失败后换一个节点再试，
        # 而不是直接记失败。attempts 会如实记录用了几次。
        attempt = 1
        while (not r["ok"]) and attempt <= args.retries:
            attempt += 1
            print(f"  ↻ 失败，换节点重试（第 {attempt} 次尝试）", flush=True)
            if nodes:
                rotate_node(nodes, i + attempt * 97)  # 错开，确保换到不同节点
                time.sleep(args.node_wait)
            r = run_once(args.mode, i, solve_timeout, args.tag, outdir,
                         headed=args.headed, url=args.url, sitekey=args.sitekey)
            r["attempts"] = attempt
        results.append(r)"""
ok &= sub(old_loop, new_loop, "主循环支持重试")

# 3) 参数
ok &= sub(
    """    ap.add_argument("--node-wait", type=float, default=4.0, help="切换节点后等待秒数")""",
    """    ap.add_argument("--node-wait", type=float, default=4.0, help="切换节点后等待秒数")
    ap.add_argument("--retries", type=int, default=0,
                    help="失败后换节点重试的次数（0=不重试，如实记录单次成功率）")""",
    "重试参数")

# 4) 汇总里按 attempts 拆分
ok &= sub(
    """    ok = [r for r in results if r["ok"]]""",
    """    ok = [r for r in results if r["ok"]]
    first_try = [r for r in ok if r.get("attempts", 1) == 1]""",
    "统计一次过")

ok &= sub(
    """    lines.append(f"- **成功率: {rate:.0f}% ({len(ok)}/{n})**")""",
    """    lines.append(f"- **成功率: {rate:.0f}% ({len(ok)}/{n})**")
    if any(r.get("attempts", 1) > 1 for r in results):
        lines.append(f"- 其中一次通过: {len(first_try)}/{n}"
                     f"（首次成功率 {len(first_try)/n*100:.0f}%）")""",
    "汇总显示一次过")

if not ok:
    print("\n有未命中，未写入")
    sys.exit(1)

with open(PATH, "w", encoding="utf-8") as f:
    f.write(s)
print(f"\n已应用 {len(applied)} 处修改")
