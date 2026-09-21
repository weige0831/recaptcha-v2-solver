"""批量稳定性测试

每次运行都是独立子进程 + 独立日志 + 硬超时（超时杀整棵进程树），
结果落盘为 results.jsonl，便于跨批次对比。

用法:
    python stability_test.py --mode image --runs 10
    python stability_test.py --mode audio --runs 10
    python stability_test.py --mode image --runs 10 --tag round2

产物:
    stability/<tag>_<mode>/run_XX.log    每次运行的完整日志
    stability/<tag>_<mode>/results.jsonl 逐次结果
    stability/<tag>_<mode>/summary.md    汇总
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

SOLVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recaptcha_solver.py")
STABILITY_DIR = "stability"

# 图片/音频在官方 demo 上的默认单次超时
DEFAULT_TIMEOUT = {"image": 420, "audio": 240}
# 默认测量目标：2captcha 公共 demo。
# 官方 Google demo (google.com/recaptcha/api2/demo) 对本机出口 IP 会无限出题
# （实测单次会话要清 31~38 道），那衡量的是 IP 信誉而不是求解器能力。
DEMO_URL = "https://2captcha.com/demo/recaptcha-v2"
DEMO_SITEKEY = "6LfD3PIbAAAAAJs_eEHvoOl75_83eXSqpPSRFJ_u"
# 每轮求解后额外留给"进程启动 + 模型加载 + 浏览器启动"的余量
STARTUP_MARGIN = 180


def kill_tree(pid):
    """超时时杀掉整棵进程树（Firefox 子进程会残留）"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=30)
        else:
            os.killpg(os.getpgid(pid), 9)
    except Exception:
        pass


def run_once(mode, idx, solve_timeout, tag, outdir, headed=False, url=None, sitekey=None):
    log_path = os.path.join(outdir, f"run_{idx:02d}.log")
    cmd = [sys.executable, "-u", SOLVER,
           "--mode", mode,
           "--timeout", str(solve_timeout),
           "--min-interval", "0"]
    if not headed:
        cmd.append("--headless")
    if url:
        cmd += ["--url", url]
    if sitekey:
        cmd += ["--sitekey", sitekey]
    wall_timeout = solve_timeout + STARTUP_MARGIN
    t0 = time.time()
    timed_out = False
    rc = None
    with open(log_path, "w", encoding="utf-8", errors="replace") as f:
        f.write(f"# cmd: {' '.join(cmd)}\n# started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.flush()
        p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT,
                             cwd=os.path.dirname(SOLVER))
        try:
            rc = p.wait(timeout=wall_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(p.pid)
            try:
                rc = p.wait(timeout=30)
            except Exception:
                rc = -999
    dur = time.time() - t0

    # 解析日志
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:
        text = ""

    # 成功判定以求解器打印的 RESULT_OK 标记为准：浏览器收尾可能挂住，
    # 只看退出码会把"已拿到 token"误判成失败
    ok = "RESULT_OK" in text
    m = re.search(r"耗时 ([\d.]+)s", text)
    solve_dur = float(m.group(1)) if m else None
    tl = re.search(r"RESULT_OK token_len=(\d+)", text) or re.search(r"Token 长度: (\d+)", text)
    loops = len(re.findall(r"---\s*(?:\[音频\]\s*)?第 \d+ 次循环", text))

    reason = ""
    if timed_out:
        reason = f"墙钟超时(>{wall_timeout}s)，已杀进程树"
    elif not ok:
        mr = re.search(r"❌ 失败 \([\d.]+s\): (.+)", text)
        if mr:
            reason = mr.group(1).strip()[:200]
        else:
            reason = f"退出码 {rc}（无明确错误行）"

    return {
        "idx": idx, "mode": mode, "ok": ok, "exit_code": rc, "timed_out": timed_out,
        "dur": round(dur, 1),
        "solve_dur": round(solve_dur, 1) if solve_dur else None,
        "token_len": int(tl.group(1)) if tl else None,
        "loops": loops,
        "reason": reason,
        "log": log_path,
    }


def summarize(results, mode, tag, outdir, wall):
    n = len(results)
    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    rate = len(ok) / n * 100 if n else 0
    lines = []
    lines.append(f"# 稳定性测试 {tag} / {mode}")
    lines.append("")
    lines.append(f"- 运行次数: {n}")
    lines.append(f"- 成功: {len(ok)}")
    lines.append(f"- **成功率: {rate:.0f}% ({len(ok)}/{n})**")
    if ok:
        dur = sorted(r["dur"] for r in ok)
        med = dur[len(dur) // 2]
        lines.append(f"- 成功耗时: 最短 {dur[0]:.0f}s / 中位 {med:.0f}s / 最长 {dur[-1]:.0f}s")
        loops = [r["loops"] for r in ok if r["loops"]]
        if loops:
            lines.append(f"- 循环轮数: 最少 {min(loops)} / 中位 {sorted(loops)[len(loops)//2]} / 最多 {max(loops)}")
    lines.append(f"- 总墙钟: {wall:.0f}s ({wall/60:.1f} 分钟)")
    lines.append("")
    lines.append("| # | 结果 | 耗时 | 求解耗时 | 轮数 | token | 失败原因 |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in results:
        lines.append("| {} | {} | {:.0f}s | {} | {} | {} | {} |".format(
            r["idx"], "✅" if r["ok"] else "❌", r["dur"],
            f'{r["solve_dur"]:.0f}s' if r["solve_dur"] else "-",
            r["loops"] or "-",
            r["token_len"] or "-",
            r["reason"] or ""))
    if bad:
        lines.append("")
        lines.append("## 失败原因分布")
        from collections import Counter
        for reason, c in Counter(r["reason"] for r in bad).most_common():
            lines.append(f"- {c} 次: {reason}")

    summary = "\n".join(lines) + "\n"
    with open(os.path.join(outdir, "summary.md"), "w", encoding="utf-8") as f:
        f.write(summary)
    return summary, rate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=["image", "audio"])
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--timeout", type=int, default=None, help="单次求解超时(秒)")
    ap.add_argument("--gap", type=float, default=5.0, help="每次之间的间隔(秒)")
    ap.add_argument("--tag", default="r1")
    ap.add_argument("--headed", action="store_true", help="有头模式（默认无头）")
    ap.add_argument("--url", default=DEMO_URL)
    ap.add_argument("--sitekey", default=DEMO_SITEKEY)
    args = ap.parse_args()

    solve_timeout = args.timeout or DEFAULT_TIMEOUT[args.mode]
    outdir = os.path.join(STABILITY_DIR, f"{args.tag}_{args.mode}")
    os.makedirs(outdir, exist_ok=True)

    print("=" * 72)
    print(f"稳定性测试  模式={args.mode}  次数={args.runs}  单次超时={solve_timeout}s  间隔={args.gap}s")
    print(f"输出目录: {outdir}")
    print("=" * 72, flush=True)

    results = []
    t_all = time.time()
    for i in range(1, args.runs + 1):
        print(f"\n--- [{i}/{args.runs}] {args.mode} ---", flush=True)
        r = run_once(args.mode, i, solve_timeout, args.tag, outdir,
                     headed=args.headed, url=args.url, sitekey=args.sitekey)
        results.append(r)
        mark = "✅" if r["ok"] else "❌"
        extra = "" if r["ok"] else f"  {r['reason']}"
        print(f"  {mark} {r['dur']:.0f}s  轮数={r['loops'] or '-'}  token={r['token_len'] or '-'}{extra}",
              flush=True)
        with open(os.path.join(outdir, "results.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        if i < args.runs:
            time.sleep(args.gap)

    wall = time.time() - t_all
    summary, rate = summarize(results, args.mode, args.tag, outdir, wall)

    print("\n" + "=" * 72)
    print(summary)
    print(f"汇总已写入 {outdir}/summary.md", flush=True)

    # 达标判定：本轮成功率是否 >= 95%
    print(f"\n本轮成功率 {rate:.0f}%  {'✅ 达标(>=95%)' if rate >= 95 else '❌ 未达标(<95%)'}", flush=True)
    return 0 if rate >= 95 else 1


if __name__ == "__main__":
    sys.exit(main())
