"""第2轮修复（续）：解决"挑战区域被滚出视口导致整轮空转"

证据（timing.log 第 14~32 轮，24/32 轮空转）：
    📐 坐标: box=(1187,-9875,1573,-9487)   <- y 严重越界
    📊 图像质量: 噪点=0, 马赛克=0, 偏色=0   <- 裁出来是空白图
    ⚠️ 验证按钮操作异常: element is outside of the viewport
  => 截图是空的 → 检测必然为空 → 既不成功也不报错，一直空转到轮数上限。
     与 xbox-cn/recaptcha-v2-solver RUNLOG 记录的 "box y=-12341, 截图全黑" 是同一个问题。

三处修复：
  A. 页面模板禁止滚动（根因：min-height:100vh 的容器被挑战层撑高后可滚动）
  B. 坐标越界时回滚页面并重测；仍越界就跳过本轮，不要拿空白图去点提交
  C. 连续多轮零检出即换题，避免在一道题上空转
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

# ---------- A. 模板禁止滚动 ----------
ok &= sub(
    """        body { font-family: Arial; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; background: #f5f5f5; }""",
    """        /* 禁止页面滚动：挑战层比 100vh 高时页面一旦可滚动，
           Playwright 点击时的自动滚动会把挑战区推出视口，坐标变成大负数，
           截图为空白、按钮也点不动（整轮空转的根因） */
        html, body { height: 100%; overflow: hidden; }
        body { font-family: Arial; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #f5f5f5; }""",
    "模板禁止滚动")

# ---------- B. 坐标越界检测与恢复 ----------
ok &= sub(
    """            ele_box = target_ele.bounding_box()
            if not ele_box:
                print("⚠️ 无法获取元素位置")
                continue
            """,
    """            ele_box = target_ele.bounding_box()
            if not ele_box:
                print("⚠️ 无法获取元素位置")
                continue
            # 挑战区可能被滚出视口（点击带来的自动滚动等），此时坐标是大负数，
            # 截出来是空白图，检测必然为空、按钮也点不动 —— 表现为整轮空转。
            # 先滚回顶部重测，仍越界就跳过本轮，不要拿空白图去点提交。
            if not self._box_in_view(page, ele_box):
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
                print(f"   ✅ 已恢复 y={ele_box['y']:.0f}")
            """,
    "坐标越界检测与恢复")

# ---------- B2. _box_in_view 辅助方法 ----------
ok &= sub(
    """    def _click_anchor(self, page: Page, main_frame) -> bool:""",
    '''    @staticmethod
    def _box_in_view(page: Page, box: dict) -> bool:
        """判断元素框是否落在视口内（允许少量出界）

        挑战层被滚出视口时 y 会是很大的负数，此时截图是空白图。
        """
        try:
            vw = page.evaluate("window.innerWidth") or 0
            vh = page.evaluate("window.innerHeight") or 0
        except Exception:
            return True  # 取不到视口信息就不拦
        if not vw or not vh:
            return True
        x, y = box.get("x", 0), box.get("y", 0)
        w, h = box.get("width", 0), box.get("height", 0)
        if w < 20 or h < 20:
            return False
        # 允许上下各 20px 的溢出余量
        return (y > -20) and (y + h < vh + 20) and (x > -20) and (x + w < vw + 20)

    def _click_anchor(self, page: Page, main_frame) -> bool:''',
    "_box_in_view 辅助方法")

# ---------- C. 空转守卫 ----------
ok &= sub(
    """        no_tile_hits = 0
        relaxed_retry = False    # 上一轮被提示"请选择更多" → 本轮降阈值重检一次""",
    """        no_tile_hits = 0
        blank_rounds = 0         # 连续"零检出"的轮数，用来在空转时主动换题
        relaxed_retry = False    # 上一轮被提示"请选择更多" → 本轮降阈值重检一次""",
    "空转计数器初始化")

ok &= sub(
    """            # 静态模式过滤已点击
            if not is_dynamic_mode and clicked_indices_history:""",
    """            # 空转守卫：连续多轮一个目标都检不出，说明这道题已经没法推进
            # （图不对、类别判错、或题目本身有 none），主动换一道题，别耗到轮数上限
            if not click_indices:
                blank_rounds += 1
                if blank_rounds >= BLANK_ROUNDS_BEFORE_RELOAD:
                    print(f"   🔄 连续 {blank_rounds} 轮零检出，换成新题")
                    blank_rounds = 0
                    relaxed_retry = False
                    try:
                        reload_btn = recaptcha_frame.locator("#recaptcha-reload-button").first
                        reload_btn.click(timeout=CLICK_TIMEOUT_MS)
                        time.sleep(1.0)
                    except Exception:
                        pass
                    continue
            else:
                blank_rounds = 0

            # 静态模式过滤已点击
            if not is_dynamic_mode and clicked_indices_history:""",
    "空转守卫")

# 常量
ok &= sub(
    """# 连续多少轮拿不到挑战图块就判定为被限流：与其耗光重试次数，不如及早报错
MAX_NO_TILE_STREAK = 8""",
    """# 连续多少轮拿不到挑战图块就判定为被限流：与其耗光重试次数，不如及早报错
MAX_NO_TILE_STREAK = 8
# 连续多少轮"零检出"就主动换题（一道题不可能一直一个目标都没有）
BLANK_ROUNDS_BEFORE_RELOAD = 4""",
    "空转阈值常量")

if not ok:
    print("\n有未命中，未写入")
    sys.exit(1)

with open(PATH, "w", encoding="utf-8") as f:
    f.write(s)
print(f"\n已应用 {len(applied)} 处修改")
