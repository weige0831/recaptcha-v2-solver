"""加入计时埋点（V2_TIMING=1 开启），用于定位每轮 9s 到底花在哪

用 Write 写入而不是 heredoc，避免多层转义把反斜杠搞乱。
"""
import re
import sys

PATH = "recaptcha_solver.py"
with open(PATH, encoding="utf-8") as f:
    s = f.read()

applied = []


def rsub(pat, rep, label, count=1):
    global s
    new, k = re.subn(pat, rep, s, count=count)
    if k != count:
        print(f"  x [{label}] 命中 {k} 次（期望 {count}）")
        return False
    s = new
    applied.append(label)
    print(f"  ok [{label}]")
    return True


ok = True

# 1) TIMING 常量
ok &= rsub(
    r'SAVE_DEBUG_SHOTS = os\.environ\.get\("V2_SAVE_SHOTS", "1"\) != "0"',
    'SAVE_DEBUG_SHOTS = os.environ.get("V2_SAVE_SHOTS", "1") != "0"\n'
    '# 打印每轮及各阶段耗时（V2_TIMING=1），用于定位时间去向\n'
    'TIMING = os.environ.get("V2_TIMING", "0") != "0"',
    "TIMING 常量")

# 2) 循环前初始化计时变量
ok &= rsub(
    r'(main_frame = page\.frame_locator\("iframe\[title=.reCAPTCHA.\]"\)\n)',
    r't_prev_round = time.time()\n        t_stage = time.time()\n        \1',
    "初始化计时变量")

# 3) 循环头打印上一轮耗时
ok &= rsub(
    r'(print\(f"\\n🔄 --- 第 \{current_try\} 次循环检测 ---"\)\n)',
    r'\1'
    '            if TIMING and current_try > 1:\n'
    '                print(f"   [计时] 上一轮共 {time.time() - t_prev_round:.1f}s")\n'
    '            t_prev_round = time.time()\n'
    '            t_stage = time.time()\n',
    "循环头计时")

# 4) 截图计时
ok &= rsub(
    r'(time\.sleep\(0\.3\)\n            full_screenshot = page\.screenshot\(\)\n)',
    'time.sleep(0.3)\n'
    '            if TIMING:\n'
    '                print(f"   [计时] 截图前准备 {time.time()-t_stage:.1f}s")\n'
    '            t_shot = time.time()\n'
    '            full_screenshot = page.screenshot()\n'
    '            if TIMING:\n'
    '                print(f"   [计时] page.screenshot {time.time()-t_shot:.1f}s")\n'
    '            t_stage = time.time()\n',
    "截图计时")

# 5) 裁剪+预处理计时
ok &= rsub(
    r'(img_w, img_h = img_obj\.size\n            tile_w = img_w / grid_side\n)',
    'if TIMING:\n'
    '                print(f"   [计时] 裁剪+预处理 {time.time()-t_stage:.1f}s")\n'
    '            t_det = time.time()\n            \1',
    "后处理计时")

# 6) 推理计时
ok &= rsub(
    r'(sorted_indices = sorted\(list\(click_indices\)\)\n)',
    'if TIMING:\n'
    '                print(f"   [计时] 检测推理 {time.time()-t_det:.1f}s")\n            \1',
    "推理计时")

# 7) 等新图计时
ok &= rsub(
    r'(new_payloads = payload_hits\[0\] - payload_before\n)',
    'if TIMING:\n'
    '                    print(f"   [计时] 等新图拉取 {time.time()-t_repl:.1f}s")\n                \1',
    "等新图计时")

# 8) 等渲染计时
ok &= rsub(
    r'(\n                if not loaded:\n)',
    '\n                if TIMING:\n'
    '                    print(f"   [计时] 等渲染完成 {time.time()-t_wait_start:.1f}s")\n'
    r'\1',
    "等渲染计时")

if not ok:
    print("\n未全部命中，未写入文件")
    sys.exit(1)

with open(PATH, "w", encoding="utf-8") as f:
    f.write(s)
print(f"\n已加 {len(applied)} 处计时埋点")
