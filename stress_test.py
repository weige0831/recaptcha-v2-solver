"""多线程压力测试：图片模式 / 音频模式

用法:
    python stress_test.py --mode image --threads 4 --runs 2
    python stress_test.py --mode audio --threads 4 --runs 2

注意：
- Playwright 同步 API 要求浏览器只能由创建它的线程使用，因此每个工作线程
  各自创建一个 RecaptchaSolver（各自一个 Camoufox）；模型是类级单例，带锁共享。
- 压力测试会显式关掉调用间隔限流（min_interval=0），否则全局冷却会把所有
  线程串行化，测不出并发能力。代价是更容易触发 Google 端限流，这正是要看的东西。
- 并发会争抢 CPU，单次耗时会明显变长，所以超时默认放宽到 900s。
"""
import argparse
import os
import statistics
import sys
import threading
import time

import recaptcha_solver as rs

SITEKEY = "6LfD3PIbAAAAAJs_eEHvoOl75_83eXSqpPSRFJ_u"
URL = "https://2captcha.com/demo/recaptcha-v2"


def worker(tid, mode, runs, timeout, cooldown, barrier, results, lock, t_start_box):
    """单个工作线程：自己建浏览器，跑 runs 次求解"""
    tag = f"[T{tid}]"
    try:
        solver = rs.RecaptchaSolver(headless=True, min_interval=cooldown, mode=mode)
        with solver as s:
            try:
                barrier.wait(timeout=300)
            except threading.BrokenBarrierError:
                with lock:
                    results.append({"tid": tid, "run": 0, "ok": False,
                                    "sec": 0, "reason": "启动同步失败(barrier broken)"})
                return
            # 记下第一个线程真正开跑的时刻，用于统计吞吐
            with lock:
                if t_start_box[0] is None:
                    t_start_box[0] = time.time()

            for i in range(1, runs + 1):
                t0 = time.time()
                try:
                    token = s.solve(SITEKEY, URL, timeout)
                    rec = {"tid": tid, "run": i, "ok": True,
                           "sec": time.time() - t0, "reason": None, "tlen": len(token)}
                except rs.RecaptchaBlockedError as e:
                    rec = {"tid": tid, "run": i, "ok": False,
                           "sec": time.time() - t0, "reason": f"限流: {str(e)[:60]}"}
                except Exception as e:
                    rec = {"tid": tid, "run": i, "ok": False,
                           "sec": time.time() - t0, "reason": f"{type(e).__name__}: {str(e)[:60]}"}
                with lock:
                    results.append(rec)
                mark = "✅" if rec["ok"] else "❌"
                print(f"{tag} 第{i}/{runs}次 {mark} {rec['sec']:.0f}s "
                      f"{'' if rec['ok'] else rec['reason']}", flush=True)
    except Exception as e:
        with lock:
            results.append({"tid": tid, "run": 0, "ok": False, "sec": 0,
                            "reason": f"线程异常 {type(e).__name__}: {str(e)[:80]}"})
        print(f"{tag} 线程异常: {type(e).__name__}: {e}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="image", choices=["image", "audio"])
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--runs", type=int, default=2, help="每个线程的求解次数")
    ap.add_argument("--timeout", type=int, default=900, help="单次求解超时(秒)")
    ap.add_argument("--torch-threads", type=int, default=0,
                    help="每个 torch 算子用的线程数，0=按并发数自动分摊")
    ap.add_argument("--cooldown", type=float, default=0,
                    help="调用间隔限流(秒)，0=关闭。开启后同进程内所有线程共享冷却")
    args = ap.parse_args()

    ncpu = os.cpu_count() or 4
    torch_threads = args.torch_threads or max(1, ncpu // max(1, args.threads))
    import torch
    torch.set_num_threads(torch_threads)

    total = args.threads * args.runs
    print("=" * 72)
    print(f"多线程压力测试   模式={args.mode}   线程={args.threads}   "
          f"每线程={args.runs} 次   总尝试={total}")
    print(f"单次超时={args.timeout}s   torch 每算子线程={torch_threads}   "
          f"CPU={ncpu}  调用间隔限流="
          f"{'已关闭(压力模式)' if not args.cooldown else f'{args.cooldown:.0f}s'}")
    print("=" * 72, flush=True)

    # 主线程先把模型加载好，避免各线程同时首次加载
    print("预加载模型...", flush=True)
    t0 = time.time()
    pre = rs.RecaptchaSolver(headless=True, min_interval=0, mode=args.mode)
    if args.mode == "audio":
        pre._load_whisper()
    else:
        pre._load_models()
    print(f"模型就绪（{time.time()-t0:.0f}s）\n", flush=True)

    results, lock = [], threading.Lock()
    t_start_box = [None]
    barrier = threading.Barrier(args.threads)
    threads = [threading.Thread(target=worker,
                                args=(i + 1, args.mode, args.runs, args.timeout,
                                      args.cooldown, barrier, results, lock, t_start_box),
                                name=f"solver-{i+1}", daemon=True)
               for i in range(args.threads)]

    wall0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.time() - wall0

    # ---------------- 汇总 ----------------
    print()
    print("=" * 72)
    print(f"结果汇总   模式={args.mode}   线程={args.threads}   每线程={args.runs} 次")
    print("=" * 72)

    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    done = len(results)
    print(f"完成尝试 : {done}/{total}")
    print(f"成功     : {len(ok)}")
    print(f"成功率   : {len(ok)/done*100:.0f}%  ({len(ok)}/{done})" if done else "成功率   : n/a")

    if ok:
        secs = sorted(r["sec"] for r in ok)
        print(f"成功耗时 : 最短 {secs[0]:.0f}s  中位 {statistics.median(secs):.0f}s  "
              f"最长 {secs[-1]:.0f}s  平均 {statistics.mean(secs):.0f}s")
        if len(ok) > 1:
            print(f"token 长度: {sorted(set(r.get('tlen', 0) for r in ok))}")

    print(f"总墙钟   : {wall:.0f}s ({wall/60:.1f} 分钟)")
    if ok:
        print(f"吞吐     : {len(ok)/(wall/60):.2f} 成功/分钟    "
              f"折算 {len(ok)/(wall/3600):.1f} 成功/小时")

    if bad:
        print()
        print("失败明细:")
        from collections import Counter
        cnt = Counter(r["reason"] for r in bad)
        for reason, n in cnt.most_common():
            print(f"  {n} 次  {reason}")
        print()
        print("逐条:")
        for r in sorted(bad, key=lambda x: x["tid"]):
            print(f"  T{r['tid']} 第{r['run']}次  {r['sec']:.0f}s  {r['reason']}")

    if done >= args.threads:
        print()
        print("按线程:")
        for tid in sorted(set(r["tid"] for r in results)):
            rs_ = [r for r in results if r["tid"] == tid]
            o = sum(1 for r in rs_ if r["ok"])
            avg = statistics.mean([r["sec"] for r in rs_]) if rs_ else 0
            print(f"  T{tid}: {o}/{len(rs_)} 成功   平均 {avg:.0f}s")

    return 0


if __name__ == "__main__":
    sys.exit(main())
