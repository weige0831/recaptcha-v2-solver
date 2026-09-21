"""验证新增逻辑：跨进程冷却间隔 + Google 端限制识别"""
import os
import time
import recaptcha_solver as rs

Solver = rs.RecaptchaSolver
fails = []


def check(name, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {name}{('  ' + detail) if detail else ''}")
    if not cond:
        fails.append(name)


# ---------- 1. 冷却间隔 ----------
class Dummy:
    """只为测试 _respect_cooldown，避免加载模型"""
    min_interval = 3.0

    def _read_last_finished(self):
        return Solver._read_last_finished()

    _respect_cooldown = Solver._respect_cooldown


print("=" * 60)
print("[1/2] 冷却间隔 _respect_cooldown")
print("=" * 60)
d = Dummy()

# 情况 A：无记录 -> 不应等待
Solver._last_solve_finished = None
if os.path.exists(rs.COOLDOWN_STATE_FILE):
    os.remove(rs.COOLDOWN_STATE_FILE)
t0 = time.time()
d._respect_cooldown()
check("无历史记录时不等待", time.time() - t0 < 0.5, f"耗时 {time.time()-t0:.2f}s")

# 情况 B：刚结束（落盘时间戳） -> 应等待接近 min_interval
Solver._last_solve_finished = None
with open(rs.COOLDOWN_STATE_FILE, "w", encoding="utf-8") as f:
    f.write(str(time.time()))
t0 = time.time()
d._respect_cooldown()
el = time.time() - t0
check("跨进程（读落盘时间戳）会等待", 2.5 <= el < 4.5, f"耗时 {el:.2f}s，期望 ≈3s")

# 情况 C：只记在内存里也应生效
os.remove(rs.COOLDOWN_STATE_FILE)
Solver._last_solve_finished = time.time()
t0 = time.time()
d._respect_cooldown()
el = time.time() - t0
check("同进程（读类属性）会等待", 2.5 <= el < 4.5, f"耗时 {el:.2f}s，期望 ≈3s")

# 情况 D：已超间隔 -> 不应等待
with open(rs.COOLDOWN_STATE_FILE, "w", encoding="utf-8") as f:
    f.write(str(time.time() - 100))
Solver._last_solve_finished = None
t0 = time.time()
d._respect_cooldown()
check("超过间隔后不等待", time.time() - t0 < 0.5, f"耗时 {time.time()-t0:.2f}s")

# 情况 E：min_interval=0 时禁用限流
class DummyOff(Dummy):
    min_interval = 0

with open(rs.COOLDOWN_STATE_FILE, "w", encoding="utf-8") as f:
    f.write(str(time.time()))
t0 = time.time()
DummyOff()._respect_cooldown()
check("min_interval=0 时禁用限流", time.time() - t0 < 0.5, f"耗时 {time.time()-t0:.2f}s")

Solver._last_solve_finished = None
if os.path.exists(rs.COOLDOWN_STATE_FILE):
    os.remove(rs.COOLDOWN_STATE_FILE)


# ---------- 2. Google 端限制识别 ----------
print()
print("=" * 60)
print("[2/2] 限制识别 _detect_block")
print("=" * 60)


class FakeLocator:
    def __init__(self, text):
        self._text = text

    def inner_text(self, timeout=None):
        return self._text


class FakeMainFrame:
    def __init__(self, text):
        self._text = text

    def locator(self, sel):
        return FakeLocator(self._text)


class FakePage:
    """query_selector 恒返回 None：只测 anchor iframe 这条路径"""
    def query_selector(self, sel):
        return None


cases = [
    # (页面文字, 期望硬限制命中, 期望仅警告命中, 说明)
    ("This site is exceeding reCAPTCHA Enterprise free quota.", False, True,
     "配额提示(实测此时仍能出题，只警告)"),
    ("Please try again later", True, False, "稍后重试"),
    ("Your computer or network may be sending automated queries.", True, False, "自动化查询"),
    ("I'm not a robot reCAPTCHA Privacy - Terms", False, False, "正常组件"),
    ("Select all images with a bus Click verify once there are none left.", False, False, "正常图片挑战"),
]

for text, want_block, want_warn, label in cases:
    mf, pg = FakeMainFrame(text), FakePage()
    block = rs.find_marker(Solver._page_text(None, pg, mf), rs.BLOCK_MARKERS)
    warn = rs.find_marker(Solver._page_text(None, pg, mf), rs.WARN_MARKERS)
    check(f"{label}: 硬限制{'命中' if want_block else '不命中'}",
          (block is not None) == want_block, f"-> {block}" if block else "")
    check(f"{label}: 警告{'命中' if want_warn else '不命中'}",
          (warn is not None) == want_warn, f"-> {warn}" if warn else "")

print()
print("=" * 60)
print(f"结果: {'全部通过 ✅' if not fails else '失败 ' + str(fails) + ' ❌'}")
print("=" * 60)
