"""修复 add_timing.py 因 `\\1` 被当成八进制转义而写坏的 3 处

`\\1` 写在非 raw 字符串里会被 Python 解释成 U+0001，导致 re.subn 的捕获组
没有被插回，原文丢失。这里按已知的原始内容逐处还原。
"""
import sys

PATH = "recaptcha_solver.py"
with open(PATH, encoding="utf-8") as f:
    s = f.read()

BAD = "\x01"
print(f"修复前 U+0001 个数: {s.count(BAD)}")

repairs = [
    # 1) img_w/img_h 与 tile_w 两行丢失
    ("            \x01            tile_h = img_h / grid_side",
     "            img_w, img_h = img_obj.size\n"
     "            tile_w = img_w / grid_side\n"
     "            tile_h = img_h / grid_side",
     "还原 img_w/img_h + tile_w"),
    # 2) sorted_indices 赋值行丢失
    ("            \x01            model_name = \"GroundingDINO\" if use_yolo_world",
     "            sorted_indices = sorted(list(click_indices))\n"
     "            model_name = \"GroundingDINO\" if use_yolo_world",
     "还原 sorted_indices"),
    # 3) new_payloads 赋值行丢失
    ("                \x01                if new_payloads > 0:",
     "                new_payloads = payload_hits[0] - payload_before\n"
     "                if new_payloads > 0:",
     "还原 new_payloads"),
]

ok = True
for old, new, label in repairs:
    n = s.count(old)
    if n != 1:
        print(f"  x [{label}] 命中 {n} 次")
        ok = False
        continue
    s = s.replace(old, new)
    print(f"  ok [{label}]")

if not ok:
    print("有未命中，未写入")
    sys.exit(1)

print(f"修复后 U+0001 个数: {s.count(BAD)}")
if BAD in s:
    print("仍有残留，未写入")
    sys.exit(1)

with open(PATH, "w", encoding="utf-8") as f:
    f.write(s)
print("已写入")
