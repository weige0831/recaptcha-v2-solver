"""最终修复：页面加载重试 + 坏 IP 快速失败

r7（图片，节点轮换）8/10，两次失败都可修：
  #3 45s 轮数=0  TimeoutError: Page.goto: Timeout 30000ms exceeded
     -> 切换节点后首次加载偶发超时，加大重试即可
  #5 526s 轮数=120 Exception: failed to solve recaptcha
     -> 抽到信誉差的 IP 时题链病态地长（120 轮仍未完），
        应快速失败并在新节点上重试，而不是耗满时间预算

配套：stability_test.py 增加 --retries，失败后换节点重试。
"""
import sys

PATH = "recaptcha_solver.py"
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

# 1) 轮数上限回调：坏 IP 要快速失败（配合换节点重试），好 IP 的题链通常 <30 轮
ok &= sub(
    """        # 轮数上限。实测长题链能到 60+ 轮（r5 有 3 次精确卡死在 60），
        # 放宽到 120，让时间预算（默认 420s，批量用 720s）成为唯一约束。
        max_retries = int(os.environ.get("V2_MAX_RETRIES", "120"))""",
    """        # 轮数上限。配合"每次求解换出口 IP"使用：IP 新鲜时题链通常 2~12 轮，
        # 抽到信誉差的 IP 时题链会病态地长（实测到 120 轮仍未完）。
        # 因此定在 45：好 IP 绰绰有余，坏 IP 快速失败、交给上层换节点重试，
        # 而不是耗满整个时间预算。
        max_retries = int(os.environ.get("V2_MAX_RETRIES", "45"))""",
    "轮数上限改为 45")

# 2) page.goto 重试
ok &= sub(
    """            print(f"🌐 正在打开页面: {url}  (模式: {solve_mode})")
            page.goto(url, wait_until="networkidle")
            time.sleep(2)""",
    """            print(f"🌐 正在打开页面: {url}  (模式: {solve_mode})")
            # 切换出口节点后首次加载偶发超时（实测 TimeoutError），重试几次
            last_err = None
            for attempt in range(1, 4):
                try:
                    page.goto(url, wait_until="networkidle", timeout=45000)
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    print(f"   ⚠️ 页面加载失败({type(e).__name__})，第 {attempt}/3 次重试...")
                    time.sleep(2)
            if last_err is not None:
                raise last_err
            time.sleep(2)""",
    "page.goto 重试")

if not ok:
    print("\n有未命中，未写入")
    sys.exit(1)

with open(PATH, "w", encoding="utf-8") as f:
    f.write(s)
print(f"\n已应用 {len(applied)} 处修改")
