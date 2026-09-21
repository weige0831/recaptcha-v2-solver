"""第2轮修复：把批量测试暴露的问题一次性打进 recaptcha_solver.py

每个替换都断言必须命中，避免静默失配。

问题依据（stability/r1_image run_01~03）：
  失败全部是"30 轮重试耗尽"（268~313s），不是超时。
  离线基准：每轮计算仅约 1.0s（预处理 301ms + YOLO11x@640 688ms），
  但实测 8.9s/轮 —— 差在每轮固定 5.0s 的 time.sleep（0.6+0.6+1+1+0.8+1.0）。
  => 同样预算内轮数只有一半，长题链必然撞上限。

  另一类错误来自误检：日志里大量 0.26~0.39 的检测框，且相互重叠
  （同一物体被格子边界切成两半各检一次）。在 "if there are none, click skip"
  这类题上，误检会直接导致误点而答错。
"""
import re
import sys

PATH = "recaptcha_solver.py"
src = open(PATH, encoding="utf-8").read()
orig = src
edits = []


def sub(old, new, count=1, label=""):
    global src
    n = src.count(old)
    if n != count:
        print(f"  ❌ [{label}] 期望 {count} 处，实际 {n} 处")
        print(f"     片段: {old[:90]!r}")
        return False
    src = src.replace(old, new, count)
    edits.append(label)
    print(f"  ✅ [{label}]")
    return True


ok = True

# ---------- 1. 常量：置信度分档 + 新参数 ----------
ok &= sub(
    """GROUNDING_DINO_CONFIDENCE = 0.25
# 被 Google 提示"请选择更多"后，下一轮降阈值重检一次
# （实测首轮常漏掉 0.27~0.31 置信度的格子，白费一轮）
RELAXED_DINO_CONFIDENCE = 0.15
YOLO_CONFIDENCE = 0.25
RELAXED_YOLO_CONFIDENCE = 0.12""",
    """# 首轮检测的置信度门槛。实测 0.25 会收进大量"目标被格子边界切成两半"产生的
# 边缘框（同一物体在同一道边界上被检出多次），在 "if there are none, click skip"
# 这类题上直接变成误点。首轮取高门槛保精度，漏掉的部分由"选更多"后的降阈值重检兜回。
GROUNDING_DINO_CONFIDENCE = 0.35
RELAXED_DINO_CONFIDENCE = 0.15
YOLO_CONFIDENCE = 0.45
RELAXED_YOLO_CONFIDENCE = 0.15
# 检测框 → 格子的映射门槛：框覆盖某格面积达到此比例即算命中该格。
# reCAPTCHA 的规则是"勾选所有包含目标一部分的方格"，只点中心格会漏。
BOX_TILE_MIN_COVER = 0.15
# YOLO 推理输入尺寸。crop 本身约 390px，默认 640 是白白放大、白花时间
YOLO_IMGSZ = int(os.environ.get("V2_YOLO_IMGSZ", "448"))
# 是否落盘调试图（全页 PNG + crop + 增强图）。批量测试关掉可省每轮约 0.3~1s
SAVE_DEBUG_SHOTS = os.environ.get("V2_SAVE_SHOTS", "1") != "0\"""",
    1, "常量分档与新参数")

# ---------- 2. 检测框 → 格子 的重叠映射 ----------
ok &= sub(
    '''def find_marker(blob: str, markers: dict) -> Optional[str]:''',
    '''def box_to_tiles(bx1, by1, bx2, by2, tile_w, tile_h, grid_side,
                 min_cover=BOX_TILE_MIN_COVER) -> Set[int]:
    """检测框 → 需要点击的格子集合

    reCAPTCHA 的判定规则是"勾选所有包含目标一部分的方格"，所以跨格的物体要把
    覆盖到的格子都点上。只点中心所在那一格，正是"请选择更多"的主要来源。
    min_cover: 框覆盖该格的面积占该格面积的比例达到此值即算命中。
    """
    out: Set[int] = set()
    if bx2 <= bx1 or by2 <= by1:
        return out
    c0 = max(0, int(bx1 // tile_w))
    c1 = min(grid_side - 1, int((bx2 - 1e-6) // tile_w))
    r0 = max(0, int(by1 // tile_h))
    r1 = min(grid_side - 1, int((by2 - 1e-6) // tile_h))
    tile_area = tile_w * tile_h
    for row in range(r0, r1 + 1):
        for col in range(c0, c1 + 1):
            ox = max(0.0, min(bx2, (col + 1) * tile_w) - max(bx1, col * tile_w))
            oy = max(0.0, min(by2, (row + 1) * tile_h) - max(by1, row * tile_h))
            cover = (ox * oy) / tile_area if tile_area else 0.0
            if cover >= min_cover:
                out.add(row * grid_side + col)
    if not out:
        # 兜底：框太小、哪一格都没到阈值时，至少取中心所在格
        col = min(grid_side - 1, max(0, int(((bx1 + bx2) / 2) // tile_w)))
        row = min(grid_side - 1, max(0, int(((by1 + by2) / 2) // tile_h)))
        out.add(row * grid_side + col)
    return out


def find_marker(blob: str, markers: dict) -> Optional[str]:''',
    1, "box_to_tiles 工具函数")

# ---------- 3. GroundingDINO 分支改用重叠映射 ----------
ok &= sub(
    """                for box, score, label in detections:
                    bx1, by1, bx2, by2 = box
                    # 使用检测框中心点确定所在格子
                    center_x = (bx1 + bx2) / 2
                    center_y = (by1 + by2) / 2
                    col = int(center_x / tile_w)
                    row = int(center_y / tile_h)
                    if 0 <= row < grid_side and 0 <= col < grid_side:
                        idx = row * grid_side + col
                        if idx not in click_indices:
                            click_indices.add(idx)
                            print(f"      格子 [{row},{col}] 置信度: {score:.3f} ✓")""",
    """                for box, score, label in detections:
                    bx1, by1, bx2, by2 = box
                    hits = box_to_tiles(bx1, by1, bx2, by2, tile_w, tile_h, grid_side)
                    new = sorted(hits - click_indices)
                    click_indices |= hits
                    if new:
                        print(f"      格子 {new} 置信度: {score:.3f} ✓")""",
    1, "DINO 重叠映射")

# ---------- 4. YOLO 分支改用重叠映射 ----------
ok &= sub(
    """                            # 使用检测框中心点确定所在格子
                            center_x = (bx1 + bx2) / 2
                            center_y = (by1 + by2) / 2
                            col_idx = int(center_x / tile_w)
                            row_idx = int(center_y / tile_h)
                            if 0 <= row_idx < grid_side and 0 <= col_idx < grid_side:
                                click_indices.add(row_idx * grid_side + col_idx)""",
    """                            click_indices |= box_to_tiles(
                                bx1, by1, bx2, by2, tile_w, tile_h, grid_side)""",
    1, "YOLO 重叠映射")

# ---------- 5. YOLO 推理尺寸 ----------
ok &= sub(
    """                results = self._yolo_model(
                    img_enhanced, verbose=False,
                    conf=RELAXED_YOLO_CONFIDENCE if relaxed_retry else YOLO_CONFIDENCE)""",
    """                results = self._yolo_model(
                    img_enhanced, verbose=False, imgsz=YOLO_IMGSZ,
                    conf=RELAXED_YOLO_CONFIDENCE if relaxed_retry else YOLO_CONFIDENCE)""",
    1, "YOLO imgsz")

# ---------- 6. 去噪搜索窗 21 → 11（273ms → 101ms） ----------
n_nlm = src.count(", 7, 21)")
src = src.replace(", 7, 21)", ", 7, 11)")
print(f"  ✅ [NLM 搜索窗] 替换 {n_nlm} 处")
edits.append("NLM 搜索窗 21→11")

# ---------- 7. 削减每轮固定等待（5.0s → 约 1.9s） ----------
ok &= sub(
    """            # 截图和识别
            time.sleep(0.6)""",
    """            # 截图和识别
            time.sleep(0.2)""", 1, "等待:截图前 0.6→0.2")

ok &= sub(
    """            # 等待图片完全加载后截图
            time.sleep(1)  # 基础等待时间""",
    """            # 等待图片完全加载后截图
            time.sleep(0.4)  # 基础等待（后面还有图块就绪检查兜底）""",
    1, "等待:基础 1.0→0.4")

ok &= sub(
    """                        print("   ⏳ 等待图片加载...")
                        time.sleep(1.0)  # 额外等待""",
    """                        print("   ⏳ 等待图片加载...")
                        time.sleep(0.3)  # 额外等待""",
    1, "等待:加载后 1.0→0.3")

ok &= sub(
    """            time.sleep(1)
            full_screenshot = page.screenshot()
            
            # 保存截图
            if DEBUG:""",
    """            time.sleep(0.3)
            full_screenshot = page.screenshot()
            
            # 保存截图
            if DEBUG and SAVE_DEBUG_SHOTS:""",
    1, "等待:截图前 1.0→0.3 + 截图开关")

ok &= sub(
    """            # 保存裁剪截图
            if DEBUG:""",
    """            # 保存裁剪截图
            if DEBUG and SAVE_DEBUG_SHOTS:""",
    1, "裁剪截图开关")

ok &= sub(
    """                    img_enhanced = preprocess_image(img_obj)
                    if DEBUG:""",
    """                    img_enhanced = preprocess_image(img_obj)
                    if DEBUG and SAVE_DEBUG_SHOTS:""",
    1, "增强图开关")

ok &= sub(
    """            time.sleep(0.6)
            
            # Google 端状态检测""",
    """            time.sleep(0.3)
            
            # Google 端状态检测""",
    1, "等待:循环头 0.6→0.3")

ok &= sub(
    """                    verify_btn.click(timeout=CLICK_TIMEOUT_MS)
                    time.sleep(0.8)""",
    """                    verify_btn.click(timeout=CLICK_TIMEOUT_MS)
                    time.sleep(0.4)""",
    1, "等待:提交后 0.8→0.4")

ok &= sub(
    """                    else:
                        relaxed_retry = False
                    time.sleep(1.0)""",
    """                    else:
                        relaxed_retry = False
                    time.sleep(0.6)""",
    1, "等待:错误检查后 1.0→0.6")

# ---------- 8. 图片路径放宽轮数上限（时间上限本来就会先触发） ----------
ok &= sub(
    """        start_time = time.time()
        max_retries = 30
        current_try = 0
        clicked_indices_history: Set[int] = set()""",
    """        start_time = time.time()
        # 每轮已压到约 4~5s，30 轮的上限反而先于时间上限触发。
        # 放宽到 60，让 420s 的时间预算成为真正的约束。
        max_retries = 60
        current_try = 0
        clicked_indices_history: Set[int] = set()""",
    1, "图片 max_retries 30→60")

# ---------- 9. 错误信息读取加超时（r1 run_05 卡死 7 分钟的元凶） ----------
# inner_text() 默认 30s 自动等待，错误元素若已 detach 就会逐个卡满，
# 5 个元素就是 150s，表现为"轮数打满后长时间无输出"。
ok &= sub(
    """                    for msg in error_msgs:
                        try:
                            if msg.is_visible():
                                has_error = True
                                err_text += " " + (msg.inner_text() or "")
                        except Exception:
                            pass""",
    """                    for msg in error_msgs:
                        try:
                            if msg.is_visible():
                                has_error = True
                                err_text += " " + (msg.inner_text(timeout=1000) or "")
                        except Exception:
                            pass""",
    1, "错误信息 inner_text 加超时")

ok &= sub(
    """            el = bframe.query_selector(".rc-audiochallenge-error-message")
            if el:
                t = (el.inner_text() or "").strip()""",
    """            el = bframe.query_selector(".rc-audiochallenge-error-message")
            if el:
                t = (el.inner_text(timeout=1000) or "").strip()""",
    1, "音频错误信息加超时")

if not ok:
    print("\n有替换未命中，未写入文件。")
    sys.exit(1)

open(PATH, "w", encoding="utf-8").write(src)
print(f"\n✅ 已应用 {len(edits)} 组修改: {', '.join(edits)}")
print(f"   文件 {len(orig)} → {len(src)} 字节")
