"""第2轮修复（续2）：挑战区离屏时的成因无关恢复

现象（stability/r2_image/run_01，55 次）：
    ⚠️ 挑战区不在视口内 y=-9875，回滚页面重测   <- 反复出现且 scrollTo(0,0) 无效
    => 截图空白、VERIFY 点不动，一路空转到 60 轮上限

探测未复现（正常时 bframe 在 [700,108,400,580]、scrollY=0），说明是特定条件下的
停靠/错位状态。与其继续猜成因，改为成因无关的恢复：连续两轮离屏就整页重载，
重建组件并重置本题状态。
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

# 1) 常量
ok &= sub(
    """# 连续多少轮"零检出"就主动换题（一道题不可能一直一个目标都没有）
BLANK_ROUNDS_BEFORE_RELOAD = 4""",
    """# 连续多少轮"零检出"就主动换题（一道题不可能一直一个目标都没有）
BLANK_ROUNDS_BEFORE_RELOAD = 4
# 连续多少轮挑战区不在视口内就整页重载。实测会一直卡在 y=-9875，
# scrollTo 无效，不重载就会空转到轮数上限
OFFSCREEN_ROUNDS_BEFORE_RELOAD = 2""",
    "离屏阈值常量")

# 2) 计数器初始化
ok &= sub(
    """        blank_rounds = 0         # 连续"零检出"的轮数，用来在空转时主动换题""",
    """        blank_rounds = 0         # 连续"零检出"的轮数，用来在空转时主动换题
        offscreen_rounds = 0     # 连续"挑战区不在视口内"的轮数，用于触发整页重载""",
    "离屏计数器初始化")

# 3) 离屏处理：改为计数 + 整页重载恢复
ok &= sub(
    """            if not self._box_in_view(page, ele_box):
                print(f"⚠️ 挑战区不在视口内 y={ele_box['y']:.0f}，回滚页面重测")
                try:
                    page.evaluate("window.scrollTo(0, 0)")
                    time.sleep(0.3)
                    ele_box = target_ele.bounding_box()
                except Exception:
                    ele_box = None
                if not ele_box or not self._box_in_view(page, ele_box):
                    print("   ⚠️ 仍在视口外，跳过本轮")
                    continue
                print(f"   ✅ 已恢复 y={ele_box['y']:.0f}")""",
    """            if not self._box_in_view(page, ele_box):
                offscreen_rounds += 1
                print(f"⚠️ 挑战区不在视口内 y={ele_box.get('y', 0):.0f} "
                      f"({offscreen_rounds}/{OFFSCREEN_ROUNDS_BEFORE_RELOAD})")
                # 先试温和手段：滚回顶部并重测
                try:
                    page.evaluate("window.scrollTo(0, 0)")
                    time.sleep(0.3)
                    ele_box = target_ele.bounding_box()
                except Exception:
                    ele_box = None
                if ele_box and self._box_in_view(page, ele_box):
                    print(f"   ✅ 滚回顶部后已恢复 y={ele_box['y']:.0f}")
                    offscreen_rounds = 0
                elif offscreen_rounds >= OFFSCREEN_ROUNDS_BEFORE_RELOAD:
                    # 温和手段无效，整页重载重建组件（成因无关的恢复）
                    print("   🔄 整页重载以重建组件")
                    try:
                        page.reload(wait_until="networkidle", timeout=30000)
                    except Exception as e:
                        print(f"   ⚠️ 重载异常 {type(e).__name__}")
                    time.sleep(2)
                    try:
                        page.wait_for_selector("iframe[title='reCAPTCHA']", timeout=30000)
                    except Exception:
                        pass
                    # 重置本题状态，避免沿用旧会话的记账
                    anchor_clicked = False
                    clicked_indices_history.clear()
                    last_category = None
                    dynamic_category = None
                    relaxed_retry = False
                    blank_rounds = 0
                    offscreen_rounds = 0
                    payload_hits[0] = 0
                    continue
                else:
                    print("   ⚠️ 仍在视口外，跳过本轮")
                    continue
            else:
                offscreen_rounds = 0""",
    "离屏整页重载恢复")

if not ok:
    print("\n有未命中，未写入")
    sys.exit(1)

with open(PATH, "w", encoding="utf-8") as f:
    f.write(s)
print(f"\n已应用 {len(applied)} 处修改")
