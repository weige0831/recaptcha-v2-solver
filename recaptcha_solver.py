"""
ReCAPTCHA v2 打码平台
通过 sitekey + url 本地复现挑战并获取 token
使用路由拦截伪装原始 URL

两种求解模式：
  image（默认）—— 图片挑战，YOLO / GroundingDINO 识别后点图块
  audio        —— 语音挑战，Whisper 转写音频后填答案
                   （思路参考 yfe404/recaptcha-audio-solver）

多线程：每个线程各自创建一个 RecaptchaSolver 实例（Playwright 同步 API 要求
浏览器只能由创建它的线程使用）；模型为类级单例、带锁加载，可跨线程共享。
"""

from ultralytics import YOLO
from camoufox.sync_api import Camoufox
from PIL import Image
import numpy as np
import cv2
import io
import re
import time
import os
import base64
import random
import tempfile
import threading
import torch
from transformers import AutoProcessor, AutoModelForZeroShotObjectDetection
from playwright.sync_api import Page, Route
from typing import Optional, List, Tuple, Set
from urllib.parse import urlparse
import warnings
warnings.filterwarnings("ignore", category=UserWarning)

# --- 配置选项 ---
ENABLE_IMAGE_PREPROCESSING = True
# 首轮检测的置信度门槛。实测 0.25 会收进大量"目标被格子边界切成两半"产生的
# 边缘框（同一物体在同一道边界上被检出多次），在 "if there are none, click skip"
# 这类题上直接变成误点。首轮取高门槛保精度，漏掉的部分由"选更多"后的降阈值重检兜回。
GROUNDING_DINO_CONFIDENCE = 0.28
RELAXED_DINO_CONFIDENCE = 0.15
YOLO_CONFIDENCE = 0.30
RELAXED_YOLO_CONFIDENCE = 0.15
# 检测框 → 格子的映射门槛：方框落在某格内的面积占方框总面积达到此比例即算命中。
# reCAPTCHA 的规则是"勾选所有包含目标一部分的方格"，只点中心格会漏。
BOX_TILE_MIN_BOX_FRAC = 0.25
# YOLO 推理输入尺寸。crop 本身约 390px，默认 640 是白白放大、白花时间
YOLO_IMGSZ = int(os.environ.get("V2_YOLO_IMGSZ", "448"))
# 是否落盘调试图（全页 PNG + crop + 增强图）。批量测试关掉可省每轮约 0.3~1s
SAVE_DEBUG_SHOTS = os.environ.get("V2_SAVE_SHOTS", "1") != "0"
# 打印每轮及各阶段耗时（V2_TIMING=1），用于定位时间去向
TIMING = os.environ.get("V2_TIMING", "0") != "0"
# 成功拿到 token 后先落盘的路径。浏览器收尾偶尔会无限期挂起（实测），
# 结果必须先落地，否则"已经解出来了"会被当成失败
TOKEN_STASH_FILE = os.path.join(tempfile.gettempdir(), "recaptcha_last_token.txt")
# 收尾（page.close / 浏览器关闭）看门狗：超时未关完就强制退出进程，
# 避免整个进程挂死。只建议在"一轮一进程"的批处理用法下开启
FORCE_EXIT_ON_CLOSE_HANG = os.environ.get("V2_FORCE_EXIT", "1") != "0"
BROWSER_CLOSE_TIMEOUT = float(os.environ.get("V2_CLOSE_TIMEOUT", "25"))
GROUNDING_DINO_DEBUG = False  # 调试模式
DEBUG = True  # 全局调试开关

# 本地代理。浏览器能直连 Google 时留空即可；直连不通再设（如 http://127.0.0.1:7890）
PROXY = os.environ.get("V2_PROXY", "")
# YOLO 权重。yolo26x 是 NMS-free 端到端版本，重叠框更少，可用环境变量切换
YOLO_WEIGHTS = os.environ.get("V2_YOLO_WEIGHTS", "yolo11x.pt")
# Whisper 后端: "faster"（faster-whisper，CTranslate2 int8，CPU 上明显更快，
#              且不需要 PATH 上有 ffmpeg）/ "openai"（openai-whisper，依赖 ffmpeg）
WHISPER_BACKEND = os.environ.get("V2_WHISPER_BACKEND", "faster")
# 模型规格。留空则用后端默认：faster -> base.en，openai -> tiny
WHISPER_MODEL = os.environ.get("V2_WHISPER_MODEL", "")
FASTER_WHISPER_MODEL = "base.en"
OPENAI_WHISPER_MODEL = "tiny"

# 单次 solve 的默认超时（秒）。CPU 推理慢，加上点击重试与动态模式的多轮
# 刷新，200s 容易被耗尽；实测 420s 可以稳定覆盖
DEFAULT_SOLVE_TIMEOUT = 420
# 连续调用之间的最小间隔（秒）。短时间内反复请求会触发 Google 端限制
# （"exceeding reCAPTCHA Enterprise free quota"），此时组件根本不下发挑战，
# 重试没有意义。0 表示不限制
MIN_SOLVE_INTERVAL = 45.0
# 单个元素点击的超时（毫秒）。避免一次点击卡住吃掉整个时间预算
CLICK_TIMEOUT_MS = 5000
# 主验证框（复选框）点击的超时（毫秒）。这是必须成功的一步，且组件刚加载
# 时可能还不稳定，所以给得比图块点击宽松；失败不致命，下一轮会重试
ANCHOR_CLICK_TIMEOUT_MS = 15000
# 限流时间戳落盘位置。放在临时目录，使间隔限制跨进程同样生效
# （反复用命令行运行本脚本是最容易踩到限流的用法）
COOLDOWN_STATE_FILE = os.path.join(tempfile.gettempdir(), "recaptcha_solver_cooldown")

# --- 音频模式（参考 yfe404/recaptcha-audio-solver 的实现思路）---
# 默认求解模式: "image"（图片挑战）| "audio"（语音挑战）
SOLVE_MODE = "image"
# 点击音频挑战的 PLAY 并等它播完再作答。不播放直接填答案是明显的机器行为。
AUDIO_PLAY = True
# 音频临时文件目录
AUDIO_TMP_DIR = os.path.join(tempfile.gettempdir(), "recaptcha_audio")


class RecaptchaBlockedError(Exception):
    """Google 端限制（配额超限 / 风控 / 要求稍后重试）

    这类状态下组件不会下发挑战，继续重试没有意义。单独抛出该异常是为了让
    调用方及早退避，而不是把重试次数耗光。
    """


# Google 端「确实无法继续」的页面特征文本 -> 可读原因（小写匹配）
# 注意：不要把 "exceeding ... free quota" 放进来。实测该提示出现时组件
# 仍可能正常下发挑战，当成致命错误会误判（它归在 WARN_MARKERS）
BLOCK_MARKERS = {
    "try again later": "Google 要求稍后重试 (try again later)",
    "automated queries": "Google 判定为自动化查询 (automated queries)",
    "网络错误": "Google 网络错误提示",
}

# 只提示、不中止流程的页面特征文本
WARN_MARKERS = {
    "exceeding": "该站点已超出 reCAPTCHA Enterprise 免费配额（Google 仍可能正常出题，继续尝试）",
}

# 连续多少轮拿不到挑战图块就判定为被限流：与其耗光重试次数，不如及早报错
MAX_NO_TILE_STREAK = 8
# 连续多少轮"零检出"就主动换题（一道题不可能一直一个目标都没有）
BLANK_ROUNDS_BEFORE_RELOAD = 4
# 连续多少轮挑战区不在视口内就整页重载。实测会一直卡在 y=-9875，
# scrollTo 无效，不重载就会空转到轮数上限
OFFSCREEN_ROUNDS_BEFORE_RELOAD = 2


def box_to_tiles(bx1, by1, bx2, by2, tile_w, tile_h, grid_side,
                 min_box_frac=BOX_TILE_MIN_BOX_FRAC) -> Set[int]:
    """检测框 → 需要点击的格子集合

    reCAPTCHA 的判定规则是"勾选所有包含目标一部分的方格"，所以跨格的物体要把
    覆盖到的格子都点上。只点中心所在那一格，正是"请选择更多"的主要来源。

    判据用「方框落在某格内的面积占方框总面积的比例」，而不是「占该格面积的比例」——
    后者对细长物体不成立：一根立柱跨在两格之间时，每格只占格子面积的一小部分，
    但两格确实都含有目标的一部分。
    """
    out: Set[int] = set()
    if bx2 <= bx1 or by2 <= by1:
        return out
    box_area = (bx2 - bx1) * (by2 - by1)
    c0 = max(0, int(bx1 // tile_w))
    c1 = min(grid_side - 1, int((bx2 - 1e-6) // tile_w))
    r0 = max(0, int(by1 // tile_h))
    r1 = min(grid_side - 1, int((by2 - 1e-6) // tile_h))
    for row in range(r0, r1 + 1):
        for col in range(c0, c1 + 1):
            ox = max(0.0, min(bx2, (col + 1) * tile_w) - max(bx1, col * tile_w))
            oy = max(0.0, min(by2, (row + 1) * tile_h) - max(by1, row * tile_h))
            if box_area and (ox * oy) / box_area >= min_box_frac:
                out.add(row * grid_side + col)
    if not out:
        # 兜底：方框大跨界、每格都不到比例时，至少取中心所在格
        col = min(grid_side - 1, max(0, int(((bx1 + bx2) / 2) // tile_w)))
        row = min(grid_side - 1, max(0, int(((by1 + by2) / 2) // tile_h)))
        out.add(row * grid_side + col)
    return out


def find_marker(blob: str, markers: dict) -> Optional[str]:
    """在页面文字里找出第一个命中的特征文本对应的可读原因"""
    for marker, reason in markers.items():
        if marker in blob:
            return reason
    return None

# 创建截图保存目录
SCREENSHOT_DIR = "debug_screenshots"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

# GroundingDINO 提示词映射
GROUNDING_PROMPTS = {
    "crosswalk": "zebra crossing. crosswalk.",
    "人行横道": "zebra crossing. pedestrian crossing. road stripes.",
    "stair": "staircase. stairs. steps.",
    "楼梯": "staircase. stairs. steps.",
    "bridge": "bridge. overpass.",
    "桥": "bridge. overpass.",
    "chimney": "chimney. smokestack.",
    "烟囱": "chimney. smokestack.",
    "intersection": "road intersection. crossroads. street corner.",
    "十字路口": "road intersection. crossroads. street corner.",
}

# 类别映射表
CATEGORY_MAPPING = {
    "摩托": ["motorcycle"], "motorcycle": ["motorcycle"],
    "公交": ["bus"], "巴士": ["bus"], "bus": ["bus"],
    "自行": ["bicycle"], "bicycle": ["bicycle"],
    "红绿灯": ["traffic light"], "traffic light": ["traffic light"],
    "消防": ["fire hydrant"], "hydrant": ["fire hydrant"],
    "汽车": ["car", "truck"], "轿车": ["car"], "car": ["car", "truck"],
    "boat": ["boat"], "船": ["boat"],
    "人行横道": ["zebra crossing", "crosswalk", "pedestrian crossing"],
    "crosswalk": ["zebra crossing", "crosswalk", "pedestrian crossing"],
    "楼梯": ["stairway", "stair", "steps"], "stair": ["stairway", "stair", "steps"],
    "桥": ["bridge", "overpass"], "bridge": ["bridge", "overpass"],
    "烟囱": ["chimney", "smokestack"], "chimney": ["chimney", "smokestack"],
    "山": ["mountain", "hill"], "mountain": ["mountain", "hill"],
    "棕榈树": ["palm tree", "palm"], "palm": ["palm tree", "palm"],
    "出租车": ["taxi", "yellow cab"], "taxi": ["taxi", "yellow cab"],
    "拖拉机": ["tractor", "farm vehicle"], "tractor": ["tractor", "farm vehicle"],
    "停车计时器": ["parking meter"], "parking meter": ["parking meter"],
    "十字路口": ["intersection", "crossroads"], "intersection": ["intersection", "crossroads"],
}

# 繁体 → 简体 归一化。港台出口的代理会下发繁体挑战文本，
# 而上面的关键词表是简体，不归一化会整题走 reload 白白浪费轮次。
# 先做整词替换（处理港式用词差异），再做逐字替换。
TC_PHRASE_MAP = {
    "電單車": "摩托", "機車": "摩托", "摩托車": "摩托",
    "腳踏車": "自行", "單車": "自行", "自行車": "自行",
    "交通燈": "红绿灯", "紅綠燈": "红绿灯", "交通信號燈": "红绿灯",
    "計程車": "出租车", "的士": "出租车", "出租車": "出租车",
    "消防栓": "消防", "消防水龍頭": "消防",
    "斑馬線": "人行横道", "行人穿越道": "人行横道", "人行橫道": "人行横道",
    "棕櫚樹": "棕榈树", "椰子樹": "棕榈树",
    "停車收費錶": "停车计时器", "停車計時器": "停车计时器", "咪錶": "停车计时器",
    "曳引機": "拖拉机", "拖拉機": "拖拉机",
    "煙囪": "烟囱", "樓梯": "楼梯", "階梯": "楼梯",
    "汽車": "汽车", "轎車": "轿车", "公車": "公交", "巴士": "公交",
    "橋": "桥", "船隻": "船",
}
TC_CHAR_MAP = str.maketrans("橋車電燈紅綠計樓煙囪機腳單棕櫚錶隻",
                            "桥车电灯红绿计楼烟囱机脚单棕榈表只")


def normalize_zh(text_str: str) -> str:
    """繁体/港台用词 → 简体关键词"""
    s = text_str or ""
    for tc, sc in TC_PHRASE_MAP.items():
        s = s.replace(tc, sc)
    return s.translate(TC_CHAR_MAP)

YOLO_NATIVE_CLASSES = {
    "motorcycle", "bus", "bicycle", "traffic light", "fire hydrant",
    "car", "truck", "boat", "person", "dog", "cat", "bird"
}

# HTML 模板
HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>reCAPTCHA Solver</title>
    <script src="https://www.google.com/recaptcha/api.js" async defer></script>
    <style>
        /* 禁止页面滚动：挑战层比 100vh 高时页面一旦可滚动，
           Playwright 点击时的自动滚动会把挑战区推出视口，坐标变成大负数，
           截图为空白、按钮也点不动（整轮空转的根因） */
        html, body { height: 100%; overflow: hidden; }
        body { font-family: Arial; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #f5f5f5; }
        .container { text-align: center; padding: 20px; }
    </style>
</head>
<body>
    <div class="container">
        <div id="g-recaptcha" class="g-recaptcha" data-sitekey="{{SITEKEY}}" data-callback="onSuccess"></div>
        <input type="hidden" id="g-recaptcha-response" name="g-recaptcha-response">
    </div>
    <script>
        function onSuccess(token) {
            document.getElementById('g-recaptcha-response').value = token;
        }
    </script>
</body>
</html>"""


def get_category_info(text_str):
    """根据题目文本获取类别信息（先做繁简归一化）"""
    text_lower = normalize_zh(text_str).lower()
    for keyword, classes in CATEGORY_MAPPING.items():
        if keyword in text_lower:
            use_yolo_world = not all(c in YOLO_NATIVE_CLASSES for c in classes)
            return classes, use_yolo_world, keyword
    return [], False, None


def assess_image_quality(img_bgr):
    """
    评估图像质量，检测噪点、马赛克和偏色问题
    返回: dict 包含各项质量指标和建议的处理策略
    """
    quality = {
        'noise_level': 0,      # 噪点程度 (0-100, 越高越差)
        'blockiness': 0,       # 马赛克/块效应程度 (0-100, 越高越差)
        'color_cast': 0,       # 偏色程度 (0-100, 越高越差)
        'strategy': 'full'     # 建议策略: 'full', 'gentle', 'denoise_only', 'none'
    }
    
    # 1. 噪点检测 (使用拉普拉斯算子方差)
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
    
    # 同时检测高频噪声
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    noise_diff = np.abs(gray.astype(float) - blur.astype(float))
    noise_level = np.mean(noise_diff)
    
    # 综合评估噪点 (高方差但高频差异也大说明有噪点)
    if laplacian_var > 500 and noise_level > 10:
        quality['noise_level'] = min(100, int(noise_level * 5))
    elif noise_level > 8:  # 降低阈值，单独检测高频噪声
        quality['noise_level'] = min(100, int(noise_level * 4))
    
    # 2. 马赛克/块效应检测 (分析边缘的规则性)
    # 使用 Sobel 算子检测水平和垂直边缘
    sobel_x = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    sobel_y = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    
    # 检测规则的水平/垂直线条 (马赛克特征)
    h, w = gray.shape
    # 计算每行/列的边缘强度
    row_edges = np.sum(np.abs(sobel_y), axis=1) / w
    col_edges = np.sum(np.abs(sobel_x), axis=0) / h
    
    # 检测周期性峰值 (马赛克的块边界)
    def detect_periodicity(signal, min_period=4, max_period=16):
        if len(signal) < max_period * 3:
            return 0
        # 简单的自相关检测
        for period in range(min_period, max_period):
            if len(signal) < period * 3:
                continue
            correlation = 0
            count = 0
            for i in range(period, len(signal) - period):
                correlation += abs(signal[i] - signal[i - period])
                count += 1
            if count > 0:
                avg_diff = correlation / count
                # 低差异说明有周期性
                if avg_diff < np.std(signal) * 0.5:
                    return min(100, int((1 - avg_diff / (np.std(signal) + 1)) * 100))
        return 0
    
    row_periodicity = detect_periodicity(row_edges)
    col_periodicity = detect_periodicity(col_edges)
    quality['blockiness'] = max(row_periodicity, col_periodicity)
    
    # 3. 偏色检测 (分析 LAB 色彩空间的 a 和 b 通道)
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    
    # a 通道: 负值偏绿，正值偏红；b 通道: 负值偏蓝，正值偏黄
    a_mean = np.mean(a) - 128  # 中心化
    b_mean = np.mean(b) - 128
    
    # 偏色程度 = 偏离中性的距离
    color_deviation = np.sqrt(a_mean**2 + b_mean**2)
    quality['color_cast'] = min(100, int(color_deviation * 3))
    
    # 4. 决定处理策略
    total_issues = quality['noise_level'] + quality['blockiness'] + quality['color_cast']
    
    if total_issues < 30:
        quality['strategy'] = 'full'       # 图片质量好，可以完整增强
    elif quality['noise_level'] > 50 or quality['blockiness'] > 50:
        quality['strategy'] = 'denoise_only'  # 噪点/马赛克严重，只做去噪
    elif quality['color_cast'] > 40:
        quality['strategy'] = 'color_correct'  # 偏色严重，做颜色校正
    elif total_issues < 80:
        quality['strategy'] = 'gentle'     # 中等问题，轻度增强
    else:
        quality['strategy'] = 'none'       # 问题太多，不做增强
    
    return quality


def preprocess_image(img):
    """智能图像预处理增强"""
    img_np = np.array(img)
    if len(img_np.shape) == 3 and img_np.shape[2] == 3:
        img_bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
    else:
        img_bgr = img_np
    quality = assess_image_quality(img_bgr)
    if DEBUG:
        print(f"   📊 图像质量: 噪点={quality['noise_level']}, 马赛克={quality['blockiness']}, 偏色={quality['color_cast']}, 策略={quality['strategy']}")
    strategy = quality['strategy']
    if strategy == 'none':
        img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        return Image.fromarray(img_rgb)
    if strategy == 'denoise_only':
        h_luminance = 10 if quality['noise_level'] > 70 else 6
        img_denoised = cv2.fastNlMeansDenoisingColored(img_bgr, None, h_luminance, h_luminance, 7, 11)
        if quality['blockiness'] > 40:
            img_denoised = cv2.bilateralFilter(img_denoised, 5, 50, 50)
        img_rgb = cv2.cvtColor(img_denoised, cv2.COLOR_BGR2RGB)
        return Image.fromarray(img_rgb)
    if strategy == 'color_correct':
        lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        a = cv2.add(a, 128 - int(np.mean(a)))
        b = cv2.add(b, 128 - int(np.mean(b)))
        lab_corrected = cv2.merge([l, a, b])
        img_corrected = cv2.cvtColor(lab_corrected, cv2.COLOR_LAB2BGR)
        img_denoised = cv2.fastNlMeansDenoisingColored(img_corrected, None, 3, 3, 7, 11)
        img_rgb = cv2.cvtColor(img_denoised, cv2.COLOR_BGR2RGB)
        return Image.fromarray(img_rgb)
    if strategy == 'gentle':
        img_denoised = cv2.fastNlMeansDenoisingColored(img_bgr, None, 4, 4, 7, 11)
        lab = cv2.cvtColor(img_denoised, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8))
        l_clahe = clahe.apply(l)
        lab_clahe = cv2.merge([l_clahe, a, b])
        img_result = cv2.cvtColor(lab_clahe, cv2.COLOR_LAB2BGR)
        img_rgb = cv2.cvtColor(img_result, cv2.COLOR_BGR2RGB)
        return Image.fromarray(img_rgb)
    img_denoised = cv2.fastNlMeansDenoisingColored(img_bgr, None, 3, 3, 7, 11)
    lab = cv2.cvtColor(img_denoised, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l_clahe = clahe.apply(l)
    lab_clahe = cv2.merge([l_clahe, a, b])
    img_clahe = cv2.cvtColor(lab_clahe, cv2.COLOR_LAB2BGR)
    kernel_sharpen = np.array([[0, -0.5, 0], [-0.5, 3, -0.5], [0, -0.5, 0]])
    img_sharpened = cv2.filter2D(img_clahe, -1, kernel_sharpen)
    alpha = 1.1
    beta = 5
    img_adjusted = cv2.convertScaleAbs(img_sharpened, alpha=alpha, beta=beta)
    img_rgb = cv2.cvtColor(img_adjusted, cv2.COLOR_BGR2RGB)
    return Image.fromarray(img_rgb)


def crop_image_from_bytes(image_bytes, crop_box):
    """裁剪图片"""
    try:
        image_stream = io.BytesIO(image_bytes)
        img = Image.open(image_stream)
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        cropped_img = img.crop(crop_box)
        output_stream = io.BytesIO()
        cropped_img.save(output_stream, format='JPEG')
        return output_stream.getvalue()
    except:
        return None


class RecaptchaSolver:
    """ReCAPTCHA v2 打码器
    
    多线程用法：每个线程各自创建一个实例（Playwright 同步 API 要求浏览器只能
    由创建它的线程使用）；模型是类级单例、带锁加载，可跨线程共享。
    """
    
    _yolo_model = None
    _grounding_processor = None
    _grounding_model = None
    _whisper_model = None
    _last_solve_finished: Optional[float] = None
    # 模型加载锁；Whisper 转写锁（whisper 实例不能多线程并发调用）
    _load_lock = threading.Lock()
    _whisper_lock = threading.Lock()
    
    def __init__(self, headless: bool = True, humanize: bool = False,
                 min_interval: Optional[float] = None,
                 mode: Optional[str] = None,
                 whisper_model: Optional[str] = None,
                 proxy: Optional[str] = None):
        self.headless = headless
        self.humanize = humanize
        self.min_interval = MIN_SOLVE_INTERVAL if min_interval is None else min_interval
        self.mode = mode or SOLVE_MODE
        self.whisper_model_name = whisper_model or WHISPER_MODEL
        self.proxy = PROXY if proxy is None else proxy
        self.browser = None
        # 图片模型惰性加载：音频模式用不到 YOLO/GroundingDINO，
        # 没必要多花约 45s 和 1.3GB 内存。
        # 但音频模式的 Whisper 必须在开浏览器之前加载好，否则首次下载/加载
        # 会吃掉挑战的有效期，导致挑战已过期
        if (self.mode or "").lower() == "audio":
            self._load_whisper()
    
    @classmethod
    def _load_models(cls):
        """加载图片模式所需的模型（单例、带锁，可多线程安全调用）"""
        with cls._load_lock:
            if cls._yolo_model is None:
                print(f"🚀 正在加载 YOLO 模型 ({YOLO_WEIGHTS})...")
                cls._yolo_model = YOLO(YOLO_WEIGHTS)
                print("✅ YOLO 加载完成")
            if cls._grounding_processor is None:
                print("🚀 正在加载 GroundingDINO 模型...")
                cls._grounding_processor = AutoProcessor.from_pretrained("IDEA-Research/grounding-dino-tiny")
                cls._grounding_model = AutoModelForZeroShotObjectDetection.from_pretrained("IDEA-Research/grounding-dino-tiny")
                cls._grounding_model.eval()
                print("✅ GroundingDINO 加载完成")
    
    @staticmethod
    def _ensure_ffmpeg_on_path() -> Optional[str]:
        """让 Whisper 能找到 ffmpeg
        
        Whisper 内部是按命令名 "ffmpeg" 调 subprocess 的，而 imageio-ffmpeg 自带的
        二进制名字带版本号（ffmpeg-win-x86_64-v7.1.exe），直接塞 PATH 找不到。
        这里在临时目录里放一个改名副本，再把该目录挂到 PATH 最前面。
        """
        try:
            import imageio_ffmpeg
            src = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None
        if not src or not os.path.exists(src):
            return None
        shim_dir = os.path.join(tempfile.gettempdir(), "recaptcha_ffmpeg_shim")
        try:
            os.makedirs(shim_dir, exist_ok=True)
            dst = os.path.join(shim_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
            if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
                import shutil
                shutil.copy2(src, dst)
            if shim_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = shim_dir + os.pathsep + os.environ.get("PATH", "")
            return dst
        except Exception:
            return None
    
    def _load_whisper(self):
        """加载 Whisper（惰性、单例、带锁）。只有音频模式会用到
        
        默认 faster-whisper：CTranslate2 + int8，CPU 上比 openai-whisper 快得多，
        且自带解码不需要外部 ffmpeg。可在不可用时自动回退 openai-whisper。
        """
        cls = RecaptchaSolver
        with cls._load_lock:
            if cls._whisper_model is None:
                backend = (WHISPER_BACKEND or "faster").lower()
                explicit = bool(self.whisper_model_name)
                if backend == "faster":
                    try:
                        from faster_whisper import WhisperModel
                        name = self.whisper_model_name or FASTER_WHISPER_MODEL
                        print(f"🚀 正在加载 faster-whisper ({name})...")
                        cls._whisper_model = WhisperModel(name, device="cpu", compute_type="int8")
                        cls._whisper_backend = "faster"
                        print("✅ faster-whisper 加载完成")
                        return cls._whisper_model
                    except Exception as e:
                        print(f"⚠️ faster-whisper 不可用({type(e).__name__}: {e})，回退 openai-whisper")
                ffmpeg = self._ensure_ffmpeg_on_path()
                if DEBUG:
                    print(f"   ffmpeg: {ffmpeg or '未找到（转写会失败）'}")
                import whisper
                name = self.whisper_model_name if (explicit and backend != "faster") else OPENAI_WHISPER_MODEL
                print(f"🚀 正在加载 openai-whisper ({name})...")
                cls._whisper_model = whisper.load_model(name)
                cls._whisper_backend = "openai"
                print("✅ openai-whisper 加载完成")
        return cls._whisper_model
    
    def __enter__(self):
        kwargs = dict(
            headless=self.headless,
            humanize=self.humanize,
            i_know_what_im_doing=True,
            config={'forceScopeAccess': True},
            disable_coop=True,
            # Windows 上被遮挡或处于后台的窗口会触发渲染节流，输入派发延迟可从
            # 6s 恶化到 120s，表现为 locator click 莫名超时。必须关掉。
            firefox_user_prefs={'widget.windows.window_occlusion_tracking.enabled': False},
        )
        if self.proxy:
            kwargs['proxy'] = {"server": self.proxy}
            # 走代理时把时区/语言/地理位置对齐到出口 IP，消除指纹泄漏
            kwargs['geoip'] = True
        self.browser = Camoufox(**kwargs).__enter__()
        return self
    
    def __exit__(self, *args):
        if not self.browser:
            return
        # 浏览器关闭实测会无限期挂起（表现为"已验证成功但进程不退出"）。
        # 加看门狗：关得掉就正常关，关不掉就在超时后强制退出。
        # 批处理是一轮一进程，强退没有副作用（V2_FORCE_EXIT=0 可关掉）。
        watchdog = None
        if FORCE_EXIT_ON_CLOSE_HANG:
            watchdog = threading.Timer(BROWSER_CLOSE_TIMEOUT, lambda: os._exit(0))
            watchdog.daemon = True
            watchdog.start()
        try:
            self.browser.__exit__(*args)
        finally:
            if watchdog:
                watchdog.cancel()
    
    def solve(self, sitekey: str, url: str, timeout: int = DEFAULT_SOLVE_TIMEOUT,
              mode: Optional[str] = None) -> str:
        """
        解决 ReCAPTCHA v2 挑战
        
        :param sitekey: Google reCAPTCHA sitekey
        :param url: 原始页面 URL（用于伪装）
        :param timeout: 超时时间（秒）
        :param mode: 求解模式 "image"（图片挑战）| "audio"（语音挑战），缺省用实例的 mode
        :return: token 字符串
        :raises RecaptchaBlockedError: 命中 Google 端限制，退避后重试即可
        :raises Exception: 如果获取失败
        """
        self._respect_cooldown()
        solve_mode = (mode or self.mode or "image").lower()
        page = self.browser.new_page()
        html_content = HTML_TEMPLATE.replace('{{SITEKEY}}', sitekey)
        
        def handle_route(route: Route):
            route.fulfill(status=200, content_type='text/html', body=html_content)
        
        page.route(url, handle_route)
        if url.endswith('/'):
            page.route(url.rstrip('/'), handle_route)
        else:
            page.route(url + '/', handle_route)
        
        try:
            print(f"🌐 正在打开页面: {url}  (模式: {solve_mode})")
            page.goto(url, wait_until="networkidle")
            time.sleep(2)
            if solve_mode == "audio":
                token = self._solve_audio_challenge(page, timeout)
            else:
                token = self._solve_challenge(page, timeout)
            # 先把结果落地再收尾：浏览器收尾实测会无限期挂起，
            # 不能让"已经解出来了"因为收尾卡死而被记成失败
            self._stash_token(token)
            return token
        finally:
            RecaptchaSolver._last_solve_finished = time.time()
            self._write_last_finished()
            try:
                page.close()
            except Exception:
                pass

    @staticmethod
    def _stash_token(token: str) -> None:
        """立刻把 token 写到临时文件，并打印机器可读的成功标记

        收尾阶段（page.close / 浏览器关闭）可能挂起，批处理应以这个标记判定成功，
        而不是只看进程退出码。
        """
        try:
            with open(TOKEN_STASH_FILE, "w", encoding="utf-8") as f:
                f.write(token or "")
        except Exception:
            pass
        print(f"RESULT_OK token_len={len(token or '')}", flush=True)
    
    @staticmethod
    def _read_last_finished() -> Optional[float]:
        try:
            with open(COOLDOWN_STATE_FILE, "r", encoding="utf-8") as f:
                return float(f.read().strip())
        except Exception:
            return None
    
    @staticmethod
    def _write_last_finished() -> None:
        try:
            with open(COOLDOWN_STATE_FILE, "w", encoding="utf-8") as f:
                f.write(str(time.time()))
        except Exception:
            pass
    
    def _respect_cooldown(self):
        """距上次求解结束不足 min_interval 时先等待，避免触发 Google 端限流
        
        时间戳同时记在内存和临时文件里，因此跨进程（例如反复用命令行运行
        本脚本）也同样生效。
        """
        if not self.min_interval:
            return
        stamps = [t for t in (self._read_last_finished(),
                              RecaptchaSolver._last_solve_finished) if t is not None]
        if not stamps:
            return
        remain = self.min_interval - (time.time() - max(stamps))
        if remain > 0:
            print(f"⏸  距上次求解不足 {self.min_interval:.0f}s，先等待 {remain:.1f}s（避免触发 Google 端限流）...")
            time.sleep(remain)
    
    def _page_text(self, page: Page, main_frame) -> str:
        """取 anchor + bframe 的可见文字（小写），用于状态判定"""
        texts = []
        try:
            texts.append(main_frame.locator("body").inner_text(timeout=1500))
        except Exception:
            pass
        try:
            bframe_selector = "iframe[src*='recaptcha/api2/bframe'], iframe[src*='recaptcha/enterprise/bframe']"
            bframe_element = page.query_selector(bframe_selector)
            bframe = bframe_element.content_frame() if bframe_element else None
            if bframe:
                texts.append(bframe.evaluate("document.body ? document.body.innerText : ''") or "")
        except Exception:
            pass
        return " ".join(t for t in texts if t).lower()
    
    def _detect_block(self, page: Page, main_frame) -> Optional[str]:
        """检测 Google 端「无法继续」状态，命中则返回原因文本，否则返回 None"""
        return find_marker(self._page_text(page, main_frame), BLOCK_MARKERS)
    
    def _detect_warning(self, page: Page, main_frame) -> Optional[str]:
        """检测只提示、不中止流程的异常状态"""
        return find_marker(self._page_text(page, main_frame), WARN_MARKERS)
    
    def _grounding_dino_detect(self, image, text_prompt, threshold=GROUNDING_DINO_CONFIDENCE):
        """使用 GroundingDINO 进行检测"""
        try:
            inputs = self._grounding_processor(images=image, text=text_prompt, return_tensors="pt")
            with torch.no_grad():
                outputs = self._grounding_model(**inputs)
            results = self._grounding_processor.post_process_grounded_object_detection(
                outputs, inputs.input_ids, threshold=threshold, text_threshold=threshold,
                target_sizes=[image.size[::-1]]
            )[0]
            detections = []
            boxes = results["boxes"].cpu().numpy()
            scores = results["scores"].cpu().numpy()
            labels = results.get("text_labels", results.get("labels", ["object"] * len(boxes)))
            for box, score, label in zip(boxes, scores, labels):
                detections.append((box.tolist(), float(score), str(label)))
            return detections
        except Exception as e:
            if DEBUG:
                print(f"   ⚠️ GroundingDINO 检测出错: {e}")
            return []
    
    @staticmethod
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

    def _click_anchor(self, page: Page, main_frame) -> bool:
        """点击主验证框，返回是否点成功
        
        Camoufox 上 locator click 可能协议级卡死（元素已 visible/stable 也照样
        超时），此时降级为坐标级原始鼠标输入 —— 仍然是 trusted 事件。
        """
        for sel in ("#recaptcha-anchor", "[class^='rc-anchor-center-item']"):
            anchor = main_frame.locator(sel).first
            try:
                if anchor.count() == 0:
                    continue
            except Exception:
                continue
            try:
                anchor.click(timeout=ANCHOR_CLICK_TIMEOUT_MS)
                return True
            except Exception as e:
                print(f"   ⚠️ locator click 失败({type(e).__name__})，降级 raw mouse")
            try:
                box = anchor.bounding_box(timeout=5000)
                if not box:
                    continue
                cx = box['x'] + box['width'] / 2
                cy = box['y'] + box['height'] / 2
                page.mouse.move(cx - 30, cy - 20)
                page.mouse.move(cx - 10, cy - 8, steps=5)
                page.mouse.click(cx, cy)
                return True
            except Exception as e:
                print(f"   ⚠️ raw mouse 点击也失败({type(e).__name__})")
        return False
    
    def _solve_challenge(self, page: Page, timeout: int) -> str:
        """内部方法：解决图片挑战"""
        self._load_models()
        start_time = time.time()
        # 每轮已压到约 4~5s，30 轮的上限反而先于时间上限触发。
        # 放宽到 60，让 420s 的时间预算成为真正的约束。
        max_retries = 60
        current_try = 0
        clicked_indices_history: Set[int] = set()
        last_category = None
        blocked_hits = 0
        anchor_clicked = False
        warned_quota = False
        no_tile_hits = 0
        blank_rounds = 0         # 连续"零检出"的轮数，用来在空转时主动换题
        offscreen_rounds = 0     # 连续"挑战区不在视口内"的轮数，用于触发整页重载
        relaxed_retry = False    # 上一轮被提示"请选择更多" → 本轮降阈值重检一次
        dynamic_category = None  # 经 DOM 行为确认为动态题的类别（题干措辞不可靠）
        
        # 等待 reCAPTCHA 加载
        print("⏳ 等待 reCAPTCHA 加载...")
        page.wait_for_selector("iframe[title='reCAPTCHA']", timeout=30000)
        
        # 统计图片 payload 的拉取次数。动态题点完图块后 Google 会重新拉取被替换格子
        # 的新图，计数增加才是"图片确实被替换过"的硬信号。
        # 只看"图块已加载且不透明"不够用：替换还没开始时，已被清掉的旧图同样满足
        # 条件，于是截到旧图、检测为空 → 直接提交 → Google 报"请同时勾选新的图片"。
        # 实测这是最主要的失败模式（31 次 vs 漏选 5 次）。
        payload_hits = [0]

        def _on_payload(resp):
            try:
                u = resp.url
                if "/recaptcha/api2/payload" in u and "/payload/audio" not in u:
                    payload_hits[0] += 1
            except Exception:
                pass

        page.on("response", _on_payload)
        
        t_prev_round = time.time()
        t_stage = time.time()
        main_frame = page.frame_locator("iframe[title='reCAPTCHA']")
        
        time.sleep(1)
        
        while current_try < max_retries and time.time() - start_time < timeout:
            current_try += 1
            print(f"\n🔄 --- 第 {current_try} 次循环检测 ---")
            if TIMING and current_try > 1:
                print(f"   [计时] 上一轮共 {time.time() - t_prev_round:.1f}s")
            t_prev_round = time.time()
            t_stage = time.time()
            
            # 检查是否成功
            try:
                checked = main_frame.locator('[aria-checked="true"]').count() > 0
                if checked:
                    print("✅ 验证成功！(复选框已打钩)")
                    return self._get_token(page)
            except:
                pass
            
            time.sleep(0.3)
            
            # Google 端状态检测：硬限制连续两次命中才中止，避免把瞬时状态误判；
            # 配额提示只警告一次，不中止（实测该提示存在时仍能正常出题）
            blob = self._page_text(page, main_frame)
            block_reason = find_marker(blob, BLOCK_MARKERS)
            if block_reason:
                blocked_hits += 1
                print(f"⚠️ 检测到 Google 端限制: {block_reason} ({blocked_hits}/2)")
                if blocked_hits >= 2:
                    raise RecaptchaBlockedError(f"{block_reason}；建议退避后重试")
                time.sleep(2)
                continue
            blocked_hits = 0
            if not warned_quota:
                warn_reason = find_marker(blob, WARN_MARKERS)
                if warn_reason:
                    print(f"ℹ️  {warn_reason}")
                    warned_quota = True
            
            # 点击主验证框：必须成功，但失败不致命。组件刚加载或处于限制
            # 状态时可能点不动，本轮跳过、下一轮重试
            if not anchor_clicked:
                print("🖱️ 点击验证框...")
                anchor_clicked = self._click_anchor(page, main_frame)
                if not anchor_clicked:
                    print("   ⚠️ 验证框点击失败，本轮跳过")
            
            # 获取弹出层 iframe
            recaptcha_frame = None
            try:
                bframe_selector = "iframe[src*='recaptcha/api2/bframe']"
                if page.query_selector(bframe_selector):
                    recaptcha_frame = page.frame_locator(bframe_selector)
            except:
                pass
            
            if not recaptcha_frame:
                try:
                    bframe_selector = "iframe[src*='recaptcha/enterprise/bframe']"
                    if page.query_selector(bframe_selector):
                        recaptcha_frame = page.frame_locator(bframe_selector)
                except:
                    pass
            
            if not recaptcha_frame:
                print("❓ 验证窗口未找到，等待...")
                time.sleep(1)
                continue
            
            # 等待图片容器
            try:
                target_ele = recaptcha_frame.locator(".rc-imageselect-challenge").first
                target_ele.wait_for(state="visible", timeout=2500)
            except:
                print("⏳ 图片元素未加载或正在刷新...")
                time.sleep(1)
                continue
            
            # 获取题目文本
            text_str = ""
            try:
                desc_el = recaptcha_frame.locator(".rc-imageselect-desc-no-canonical").first
                text_str = desc_el.inner_text(timeout=2000).lower()
            except:
                try:
                    desc_el = recaptcha_frame.locator(".rc-imageselect-desc").first
                    text_str = desc_el.inner_text(timeout=2000).lower()
                except:
                    pass
            
            print(f"📝 题目要求: {text_str}")
            text_str = normalize_zh(text_str)   # 繁体/港台用词 → 简体关键词
            
            target_classes, use_yolo_world, category_name = get_category_info(text_str)
            
            if category_name:
                if use_yolo_world:
                    print(f"   🌐 类别 '{category_name}' 将使用 GroundingDINO 检测")
                else:
                    print(f"   🎯 类别 '{category_name}' 将使用 YOLO 检测")
            
            if category_name != last_category:
                clicked_indices_history.clear()
                last_category = category_name
                relaxed_retry = False
            if relaxed_retry:
                print(f"   🔍 降阈值重检 (DINO {RELAXED_DINO_CONFIDENCE} / YOLO {RELAXED_YOLO_CONFIDENCE})")
            
            # 检测动态模式
            tiles_elements = recaptcha_frame.locator(".rc-image-tile-target").all()
            grid_side = 4 if len(tiles_elements) == 16 else 3
            
            # 一张图块都没有说明组件没在正常出题（例如被限流）。连续多轮如此，
            # 与其把重试次数耗光，不如及早报错并说明原因
            if tiles_elements:
                no_tile_hits = 0
            else:
                no_tile_hits += 1
                print(f"   ⚠️ 组件未下发挑战图块 ({no_tile_hits}/{MAX_NO_TILE_STREAK})")
                if no_tile_hits >= MAX_NO_TILE_STREAK:
                    raise RecaptchaBlockedError(
                        "组件连续多轮未下发挑战图块，疑似被 Google 限流/风控；建议退避后重试")
            dynamic_keywords = ["没有新图片", "沒有新圖片", "新圖片", "新图片", "new images",
                                "none left", "once there are none left", "直到", "until",
                                "click verify once", "点按", "點按"]
            is_dynamic_mode = (any(kw in text_str for kw in dynamic_keywords)
                               or (category_name is not None and category_name == dynamic_category))
            
            if is_dynamic_mode:
                print(f"   🔄 检测到动态验证模式")
            else:
                print(f"   📋 检测到静态验证模式 ({grid_side}x{grid_side})")
            
            if not target_classes:
                print(f"⚠️ 遇到不支持的类别，执行刷新!")
                try:
                    reload_btn = recaptcha_frame.locator("#recaptcha-reload-button").first
                    reload_btn.click(timeout=CLICK_TIMEOUT_MS)
                except:
                    pass
                continue
            
            # 截图和识别
            time.sleep(0.2)
            dpr = page.evaluate("window.devicePixelRatio")
            ele_box = target_ele.bounding_box()
            if not ele_box:
                print("⚠️ 无法获取元素位置")
                continue
            # 挑战区可能被滚出视口（点击带来的自动滚动等），此时坐标是大负数，
            # 截出来是空白图，检测必然为空、按钮也点不动 —— 表现为整轮空转。
            # 先滚回顶部重测，仍越界就跳过本轮，不要拿空白图去点提交。
            if not self._box_in_view(page, ele_box):
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
                offscreen_rounds = 0
            
            x1 = int(ele_box['x'] * dpr)
            y1 = int(ele_box['y'] * dpr)
            x2 = int((ele_box['x'] + ele_box['width']) * dpr)
            y2 = int((ele_box['y'] + ele_box['height']) * dpr)
            
            print(f"📐 坐标: dpr={dpr}, box=({x1},{y1},{x2},{y2})")
            
            # 等待图片完全加载后截图
            time.sleep(0.4)  # 基础等待（后面还有图块就绪检查兜底）
            
            # 尝试检测图片是否加载完成
            try:
                bframe_selector = "iframe[src*='recaptcha/api2/bframe'], iframe[src*='recaptcha/enterprise/bframe']"
                bframe_element = page.query_selector(bframe_selector)
                bframe = bframe_element.content_frame() if bframe_element else None
                
                if bframe:
                    all_loaded = bframe.evaluate("""
                        () => {
                            const container = document.querySelector('.rc-imageselect-target');
                            if (!container) return true;
                            
                            const imgs = container.querySelectorAll('img');
                            if (imgs.length === 0) return true;
                            
                            for (const img of imgs) {
                                if (!img.complete) return false;
                            }
                            return true;
                        }
                    """)
                    if not all_loaded:
                        print("   ⏳ 等待图片加载...")
                        time.sleep(0.3)  # 额外等待
            except:
                pass  # 忽略错误
            
            time.sleep(0.3)
            if TIMING:
                print(f"   [计时] 截图前准备 {time.time()-t_stage:.1f}s")
            t_shot = time.time()
            full_screenshot = page.screenshot()
            if TIMING:
                print(f"   [计时] page.screenshot {time.time()-t_shot:.1f}s")
            t_stage = time.time()
            
            # 保存截图
            if DEBUG and SAVE_DEBUG_SHOTS:
                full_path = os.path.join(SCREENSHOT_DIR, f"full_{current_try}.png")
                with open(full_path, "wb") as f:
                    f.write(full_screenshot)
                print(f"📸 保存完整截图: {full_path}")
            
            image_cp = crop_image_from_bytes(full_screenshot, (x1, y1, x2, y2))
            
            if not image_cp:
                print("⚠️ 裁剪失败")
                continue
            
            # 保存裁剪截图
            if DEBUG and SAVE_DEBUG_SHOTS:
                crop_path = os.path.join(SCREENSHOT_DIR, f"crop_{current_try}.jpg")
                with open(crop_path, "wb") as f:
                    f.write(image_cp)
                print(f"📸 保存裁剪截图: {crop_path}")
            
            img_obj = Image.open(io.BytesIO(image_cp))
            
            if ENABLE_IMAGE_PREPROCESSING:
                try:
                    img_enhanced = preprocess_image(img_obj)
                    if DEBUG and SAVE_DEBUG_SHOTS:
                        enhanced_path = os.path.join(SCREENSHOT_DIR, f"enhanced_{current_try}.jpg")
                        img_enhanced.save(enhanced_path, "JPEG")
                        print(f"🔧 保存增强截图: {enhanced_path}")
                except Exception as e:
                    print(f"⚠️ 预处理失败: {e}")
                    img_enhanced = img_obj
            else:
                img_enhanced = img_obj
            
            if TIMING:
                print(f"   [计时] 裁剪+预处理 {time.time()-t_stage:.1f}s")
            t_det = time.time()
            img_w, img_h = img_obj.size
            tile_w = img_w / grid_side
            tile_h = img_h / grid_side
            
            click_indices: Set[int] = set()
            
            if use_yolo_world:
                # GroundingDINO 检测
                grounding_prompt = None
                for keyword in GROUNDING_PROMPTS:
                    if keyword in text_str.lower() or (category_name and keyword in category_name.lower()):
                        grounding_prompt = GROUNDING_PROMPTS[keyword]
                        break
                if not grounding_prompt:
                    grounding_prompt = ". ".join(target_classes) + "."
                
                print(f"   🦖 使用 GroundingDINO 检测: {grounding_prompt}")
                
                detections = self._grounding_dino_detect(
                    img_obj, grounding_prompt,
                    threshold=RELAXED_DINO_CONFIDENCE if relaxed_retry else GROUNDING_DINO_CONFIDENCE)
                
                if GROUNDING_DINO_DEBUG:
                    print(f"      [DEBUG] 总共检测到 {len(detections)} 个目标")
                    for i, (box, score, label) in enumerate(detections):
                        print(f"      [DEBUG] #{i}: {label} conf={score:.4f} box={[int(c) for c in box]}")
                
                for box, score, label in detections:
                    bx1, by1, bx2, by2 = box
                    hits = box_to_tiles(bx1, by1, bx2, by2, tile_w, tile_h, grid_side)
                    new = sorted(hits - click_indices)
                    click_indices |= hits
                    if new:
                        print(f"      格子 {new} 置信度: {score:.3f} ✓")
                
                print(f"   🦖 GroundingDINO 识别到目标格子: {sorted(list(click_indices))}")
            else:
                # YOLO 检测
                print(f"   🎯 使用 YOLO 检测: {target_classes}")
                results = self._yolo_model(
                    img_enhanced, verbose=False, imgsz=YOLO_IMGSZ,
                    conf=RELAXED_YOLO_CONFIDENCE if relaxed_retry else YOLO_CONFIDENCE)
                for r in results:
                    for box in r.boxes:
                        cls_name = self._yolo_model.names[int(box.cls[0])]
                        conf = float(box.conf[0])
                        if cls_name in target_classes:
                            bx1, by1, bx2, by2 = box.xyxy[0].tolist()
                            if DEBUG:
                                print(f"      检测到 {cls_name} conf={conf:.3f} box=({int(bx1)},{int(by1)},{int(bx2)},{int(by2)})")
                            click_indices |= box_to_tiles(
                                bx1, by1, bx2, by2, tile_w, tile_h, grid_side)
                
                print(f"   🎯 YOLO 识别到目标格子: {sorted(list(click_indices))}")
            
            if TIMING:
                print(f"   [计时] 检测推理 {time.time()-t_det:.1f}s")
            sorted_indices = sorted(list(click_indices))
            model_name = "GroundingDINO" if use_yolo_world else "YOLO"
            print(f"🎯 最终识别结果 ({model_name}): 需点击网格 {sorted_indices}")
            
            # 空转守卫：连续多轮一个目标都检不出，说明这道题已经没法推进
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
            if not is_dynamic_mode and clicked_indices_history:
                filtered_indices = [idx for idx in sorted_indices if idx not in clicked_indices_history]
                if len(filtered_indices) < len(sorted_indices):
                    print(f"   🔒 静态模式：过滤已点击 {sorted_indices} -> {filtered_indices}")
                sorted_indices = filtered_indices
            
            # 执行点击
            if sorted_indices:
                print(f"🖱️ 点击 {len(sorted_indices)} 个图块: {sorted_indices}")
                payload_before = payload_hits[0]
                click_order = sorted_indices.copy()
                if len(click_order) > 2 and random.random() > 0.3:
                    random.shuffle(click_order)
                
                for idx in click_order:
                    if idx >= len(tiles_elements):
                        continue
                    try:
                        tiles_elements[idx].click(timeout=CLICK_TIMEOUT_MS)
                    except Exception as e:
                        # 单个图块点不动不应中断整轮，其余图块继续点
                        print(f"   ⚠️ 图块 {idx} 点击失败，跳过: {type(e).__name__}")
                        continue
                    if not is_dynamic_mode:
                        clicked_indices_history.add(idx)
                    time.sleep(0.15)
                
                # 行为级动态题识别：静态题点击后若出现 dynamic-selected（格子被替换），
                # 说明其实仍是动态题，只是题干措辞没命中关键词
                if not is_dynamic_mode:
                    for _ in range(6):
                        time.sleep(0.2)
                        try:
                            if recaptcha_frame.locator(".rc-imageselect-dynamic-selected").count() > 0:
                                print("   🔁 格子被替换 → 升级为动态模式 (题干措辞未命中)")
                                is_dynamic_mode = True
                                dynamic_category = category_name
                                clicked_indices_history.difference_update(sorted_indices)
                                break
                        except Exception:
                            pass
            else:
                print("🤷 本轮未发现目标")
            
            # 动态模式等待 - 智能等待图片刷新完成
            if is_dynamic_mode and sorted_indices:
                print("   ⏳ 动态模式：智能等待新图片加载...")
                
                # 智能等待：用 JavaScript 检测图片是否真正刷新完成。
                # 计时必须走墙钟 —— 早先按 poll_interval 累加但每次迭代实际要睡
                # 1s 以上，导致声明的 "20s 上限" 实际能拖到 120s，挑战都过期了还在等
                max_wait_time = 20.0  # 最长等待（秒，墙钟）
                poll_interval = 0.2   # 每 200ms 检查一次
                t_wait_start = time.time()
                
                # 首先等"图片确实开始被替换"：被点格子的新图会重新拉一次 payload。
                # 这一步是关键——直接进入就绪判断的话，替换尚未开始时旧图就已满足
                # "已加载+不透明"，我们会截到旧图并检测为空。
                t_repl = time.time()
                while payload_hits[0] == payload_before and (time.time() - t_repl) < 8.0:
                    time.sleep(0.2)
                if TIMING:
                    print(f"   [计时] 等新图拉取 {time.time()-t_repl:.1f}s")
                new_payloads = payload_hits[0] - payload_before
                if new_payloads > 0:
                    print(f"   🔄 已拉取 {new_payloads} 张新图块，等待渲染完成...")
                else:
                    print("   ⚠️ 未观察到新图块拉取（可能没有可替换的格子）")
                
                # 再等动画开始
                time.sleep(0.3)
                
                # 获取 iframe 的 frame 对象用于执行 JS
                bframe_selector = "iframe[src*='recaptcha/api2/bframe'], iframe[src*='recaptcha/enterprise/bframe']"
                bframe_element = page.query_selector(bframe_selector)
                bframe = bframe_element.content_frame() if bframe_element else None
                
                loaded = False
                while (time.time() - t_wait_start) < max_wait_time:
                    waited = time.time() - t_wait_start
                    try:
                        if bframe:
                            # 在 iframe 内部执行 JavaScript 检测图片加载状态
                            all_loaded = bframe.evaluate("""
                                () => {
                                    const tiles = document.querySelectorAll('.rc-imageselect-tile');
                                    if (tiles.length === 0) return false;
                                    
                                    for (const tile of tiles) {
                                        // 检查是否有选中/加载中的类
                                        if (tile.classList.contains('rc-imageselect-dynamic-selected') ||
                                            tile.classList.contains('rc-imageselect-tileselected')) {
                                            return false;
                                        }
                                        
                                        // 检查图片是否加载完成
                                        const img = tile.querySelector('img');
                                        if (img && (!img.complete || img.naturalWidth === 0)) {
                                            return false;
                                        }
                                        
                                        // 检查 opacity (淡入动画)
                                        const style = getComputedStyle(tile);
                                        if (parseFloat(style.opacity) < 0.95) {
                                            return false;
                                        }
                                    }
                                    return true;
                                }
                            """)
                            if all_loaded:
                                print(f"   ✅ 所有图片加载完成 (等待了 {waited:.1f}s)")
                                loaded = True
                                break
                        else:
                            # 回退：使用 Playwright locator 检测
                            loading_count = recaptcha_frame.locator(".rc-imageselect-dynamic-selected, .rc-imageselect-tileselected").count()
                            if loading_count == 0:
                                print(f"   ✅ 图片加载完成 (等待了 {waited:.1f}s)")
                                loaded = True
                                break
                            
                    except Exception as e:
                        # 如果 JS 执行失败，回退到简单等待
                        if DEBUG:
                            print(f"      等待检测出错: {e}")
                    
                    time.sleep(poll_interval)
                
                if TIMING:
                    print(f"   [计时] 等渲染完成 {time.time()-t_wait_start:.1f}s")

                if not loaded:
                    print(f"   ⚠️ 等待超时 ({max_wait_time:.0f}s)，继续下一轮检测")
                
                # 额外等待确保渲染完成
                time.sleep(0.3)
                # 继续下一轮循环检测新图片
                continue
            
            # 提交验证
            try:
                verify_btn = recaptcha_frame.locator("#recaptcha-verify-button").first
                if verify_btn.is_enabled():
                    print("🖱️ 点击提交按钮...")
                    verify_btn.click(timeout=CLICK_TIMEOUT_MS)
                    time.sleep(0.4)
                    
                    error_msgs = recaptcha_frame.locator("[class*='rc-imageselect-error']").all()
                    has_error = False
                    err_text = ""
                    for msg in error_msgs:
                        try:
                            if msg.is_visible():
                                has_error = True
                                err_text += " " + (msg.inner_text(timeout=1000) or "")
                        except Exception:
                            pass
                    err_text = err_text.lower().strip()
                    
                    if has_error:
                        print(f"❌ Google 提示：{err_text or '请选择更多...'}")
                        if any(k in err_text for k in ("新的圖片", "新图片", "新的图片", "new images")):
                            # "请同时勾选新的图片" → 其实是动态题，下一轮按动态处理
                            print("   🔁 提示需勾选新图片 → 标记为动态模式")
                            dynamic_category = category_name
                            relaxed_retry = False
                        elif not sorted_indices or relaxed_retry:
                            # 降阈值重检后仍不过，或已无可点格子 → 换题
                            print("⚠️ 陷入死局，强制刷新!")
                            relaxed_retry = False
                            try:
                                reload_btn = recaptcha_frame.locator("#recaptcha-reload-button").first
                                reload_btn.click(timeout=CLICK_TIMEOUT_MS)
                                time.sleep(2.5)
                            except:
                                pass
                        else:
                            # 首轮漏检 → 下一轮降阈值重检，别白扔一轮
                            relaxed_retry = True
                    else:
                        relaxed_retry = False
                    time.sleep(0.6)
            except Exception as e:
                print(f"⚠️ 验证按钮操作异常: {e}")
        
        raise Exception("failed to solve recaptcha")
    
    # ==================== 音频模式 ====================
    # 思路参考 yfe404/recaptcha-audio-solver：切到语音挑战 → 取音频 →
    # 用 Whisper 转写 → 填 #audio-response → 点 #recaptcha-verify-button
    
    @staticmethod
    def _clean_transcript(text: str) -> str:
        """规整 Whisper 输出：去标点、转小写、数字单词转阿拉伯数字"""
        cleaned = re.sub(r"[^\w\s]", "", text or "").lower().strip()
        number_words = {
            'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4',
            'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
            'ten': '10', 'oh': '0', 'o': '0',
        }
        return " ".join(number_words.get(w, w) for w in cleaned.split())
    
    def _transcribe(self, audio_path: str) -> str:
        """Whisper 转写。whisper 实例不支持并发调用，这里用锁串行化"""
        model = self._load_whisper()
        t0 = time.time()
        with RecaptchaSolver._whisper_lock:
            if getattr(RecaptchaSolver, "_whisper_backend", "openai") == "faster":
                segments, _info = model.transcribe(
                    audio_path, language="en", beam_size=5,
                    vad_filter=False, condition_on_previous_text=False)
                raw = " ".join(s.text for s in segments).strip()
            else:
                raw = (model.transcribe(audio_path, language="en").get("text") or "")
        text = self._clean_transcript(raw)
        if DEBUG:
            print(f"      [转写 {time.time()-t0:.1f}s] raw={raw!r} -> {text!r}")
        return text

    def _play_and_wait(self, page: Page, bframe) -> None:
        """点 PLAY 并等到音频播放结束
        
        不播放直接填答案是明显的机器行为；这里读 <audio> 的 duration/currentTime
        判断播完，读不到就固定等待兜底。
        """
        play_btn = bframe.locator(".rc-audiochallenge-play-button button, .rc-audiochallenge-play-button").first
        try:
            play_btn.click(timeout=CLICK_TIMEOUT_MS)
            print("   ▶️ 点击 PLAY")
        except Exception as e:
            print(f"   ⚠️ PLAY 点击失败({type(e).__name__})")
            return
        deadline = time.time() + 20
        duration = None
        while time.time() < deadline:
            try:
                st = bframe.locator("#audio-source").first.evaluate(
                    "a => ({d: a.duration, t: a.currentTime, ended: a.ended, paused: a.paused})")
            except Exception:
                st = None
            if st and st.get("d") and st["d"] == st["d"]:  # 排除 NaN
                duration = st["d"]
                if st.get("ended") or (st.get("paused") and st.get("t", 0) > 0):
                    break
            time.sleep(0.25)
        if duration:
            print(f"   ⏱️ 音频 {duration:.1f}s 播放完毕")
        else:
            print("   ⏱️ 未读到音频状态，固定等待 4s")
            time.sleep(4)
        time.sleep(random.uniform(0.6, 1.5))
    
    @staticmethod
    def _fetch_audio(page: Page, bframe, captured: Optional[list] = None) -> Optional[bytes]:
        """取音频数据
        
        依次尝试：
        1. 网络监听抓到的 payload 响应体 —— 实际生效的就是这条
        2. 页面内 fetch + 逐字节 base64 —— 在 Camoufox 的 forceScopeAccess
           (Xray) 下多半会报 "Accessing TypedArray data over Xrays is slow"，
           仅作兜底
        3. Playwright 请求上下文 —— 实测常因代理 ECONNRESET，兜底中的兜底
        
        注意：图片挑战和音频挑战的 payload 共用 /recaptcha/api2/payload 路径，
        靠 content-type 区分（见 _on_response），否则会把图片 JPEG 当音频。
        """
        why = []
        
        # 先拿到当前这段音频的地址：既用于页面内 fetch，也用于和网络响应精确对应，
        # 避免把上一段音频的响应当成当前这段
        audio_url = None
        try:
            audio_url = bframe.evaluate(
                "() => { const a = document.querySelector('#audio-source'); return a ? a.src : null; }")
        except Exception as e:
            why.append(f"取src异常:{type(e).__name__}")
        if not audio_url:
            try:
                el = bframe.query_selector("a.rc-audiochallenge-tdownload-link, a[href*='payload']")
                if el:
                    audio_url = el.get_attribute("href")
            except Exception as e:
                why.append(f"取下载链异常:{type(e).__name__}")
        
        # 1) 网络监听拿到的响应体
        if captured:
            pool = [r for r in captured if audio_url and r.url == audio_url] or captured[-3:]
            for resp in reversed(pool):
                try:
                    body = resp.body()
                    if body:
                        return body
                except Exception as e:
                    why.append(f"响应体读取失败:{type(e).__name__}")
        
        if audio_url:
            # 2) 页面内 fetch
            try:
                b64 = bframe.evaluate("""
                    async (url) => {
                        const r = await fetch(url, {credentials: 'include'});
                        if (!r.ok) return 'ERR' + r.status;
                        const bytes = new Uint8Array(await r.arrayBuffer());
                        let s = '';
                        for (let i = 0; i < bytes.length; i++) {
                            s += String.fromCharCode(bytes[i]);
                        }
                        return btoa(s);
                    }
                """, audio_url)
                if b64 and not str(b64).startswith("ERR"):
                    data = base64.b64decode(b64)
                    if data:
                        return data
                    why.append("base64 解码为空")
                else:
                    why.append(f"页面fetch返回:{str(b64)[:40]}")
            except Exception as e:
                why.append(f"页面fetch异常:{type(e).__name__}:{str(e)[:60]}")
            
            # 3) 请求上下文兜底
            try:
                resp = page.request.get(audio_url)
                if resp.ok:
                    return resp.body()
                why.append(f"request状态:{resp.status}")
            except Exception as e:
                why.append(f"request异常:{type(e).__name__}")
        else:
            why.append("拿不到音频 URL")
        
        if DEBUG:
            print(f"      [取音频] 失败: {'; '.join(why)}")
        return None
    
    @staticmethod
    def _looks_like_audio(data: Optional[bytes]) -> bool:
        """粗判是不是音频：图片与音频 payload 同路径，拿到 JPEG 时早报错更省事"""
        if not data or len(data) < 100:
            return False
        if data[:3] == b"ID3":                       # mp3 带 ID3 标签
            return True
        if data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa"):  # mp3 帧同步
            return True
        if data[:4] == b"RIFF":                      # wav
            return True
        if data[:4] == b"OggS":                      # ogg
            return True
        return False
    
    @staticmethod
    def _reload_audio(cf) -> bool:
        """换一段音频（音频挑战自带的刷新按钮）"""
        for sel in [".rc-audiochallenge-reload-button", "#recaptcha-reload-button"]:
            try:
                btn = cf.locator(sel).first
                if btn.count() > 0:
                    btn.click(timeout=CLICK_TIMEOUT_MS)
                    return True
            except Exception:
                continue
        return False
    
    @staticmethod
    def _audio_error_text(bframe) -> str:
        """读取音频挑战里的错误提示"""
        try:
            el = bframe.query_selector(".rc-audiochallenge-error-message")
            if el:
                t = (el.inner_text(timeout=1000) or "").strip()
                if t:
                    return t
        except Exception:
            pass
        try:
            txt = bframe.evaluate("document.body ? document.body.innerText : ''") or ""
            low = txt.lower()
            for key in ["multiple correct solutions required", "your answer wasn't correct",
                        "please try again", "try again later"]:
                if key in low:
                    return key
        except Exception:
            pass
        return ""
    
    def _solve_audio_challenge(self, page: Page, timeout: int) -> str:
        """音频模式主循环"""
        start_time = time.time()
        max_retries = 30
        current_try = 0
        blocked_hits = 0
        warned_quota = False
        anchor_clicked = False
        audio_segments = 0
        fetch_fails = 0
        transcribe_fails = 0
        last_transcript = None
        same_answer_hits = 0
        
        os.makedirs(AUDIO_TMP_DIR, exist_ok=True)
        audio_path = os.path.join(AUDIO_TMP_DIR, f"audio_{os.getpid()}_{threading.get_ident()}.mp3")
        bframe_sel = "iframe[src*='recaptcha/api2/bframe'], iframe[src*='recaptcha/enterprise/bframe']"
        
        # 监听 payload 响应，直接拿音频字节，避免在页面里做二进制转换
        captured: list = []
        
        def _on_response(resp):
            try:
                u = resp.url
                if "/recaptcha/api2/payload" not in u:
                    return
                # 图片挑战与音频挑战的 payload 共用同一路径，只能靠 content-type 区分，
                # 否则会把 300x300 的图片 JPEG 当成音频喂给 Whisper
                ct = ""
                try:
                    ct = (resp.headers.get("content-type") or "").lower()
                except Exception:
                    pass
                if "audio" in ct or "/payload/audio" in u:
                    captured.append(resp)
                    del captured[:-5]
            except Exception:
                pass
        
        page.on("response", _on_response)
        
        print("⏳ [音频模式] 等待 reCAPTCHA 加载...")
        page.wait_for_selector("iframe[title='reCAPTCHA']", timeout=30000)
        main_frame = page.frame_locator("iframe[title='reCAPTCHA']")
        
        time.sleep(1)
        
        while current_try < max_retries and time.time() - start_time < timeout:
            current_try += 1
            print(f"\n🔄 [音频] --- 第 {current_try} 次循环 ---")
            
            # 成功判定
            try:
                if main_frame.locator('[aria-checked="true"]').count() > 0:
                    print("✅ 验证成功！(复选框已打钩)")
                    return self._get_token(page)
            except Exception:
                pass
            time.sleep(0.6)
            
            # Google 端状态
            blob = self._page_text(page, main_frame)
            block_reason = find_marker(blob, BLOCK_MARKERS)
            if block_reason:
                blocked_hits += 1
                print(f"⚠️ 检测到 Google 端限制: {block_reason} ({blocked_hits}/2)")
                if blocked_hits >= 2:
                    raise RecaptchaBlockedError(f"{block_reason}；建议退避后重试")
                time.sleep(2)
                continue
            blocked_hits = 0
            if not warned_quota:
                warn_reason = find_marker(blob, WARN_MARKERS)
                if warn_reason:
                    print(f"ℹ️  {warn_reason}")
                    warned_quota = True
            
            # 点复选框（组件刚加载时可能点不动，失败不致命，下一轮重试）
            if not anchor_clicked:
                print("🖱️ 点击验证框...")
                anchor_clicked = self._click_anchor(page, main_frame)
                if not anchor_clicked:
                    print("   ⚠️ 验证框点击失败，本轮跳过")
            
            bf_el = page.query_selector(bframe_sel)
            bframe = bf_el.content_frame() if bf_el else None
            if not bframe:
                print("❓ 验证窗口未找到，等待...")
                time.sleep(1)
                continue
            cf = page.frame_locator(bframe_sel)
            
            # 切到语音挑战：图片挑战里才有 #recaptcha-audio-button
            if bframe.query_selector("#audio-response") is None:
                try:
                    btn = cf.locator("#recaptcha-audio-button").first
                    if btn.count() > 0:
                        print("🎧 切换到音频挑战...")
                        btn.click(timeout=CLICK_TIMEOUT_MS)
                        captured.clear()  # 丢掉图片挑战阶段抓到的 payload
                        time.sleep(1.5)
                        bf_el = page.query_selector(bframe_sel)
                        bframe = bf_el.content_frame() if bf_el else None
                        # 切过去之后音频 payload 还要下载，等 src 真正挂上
                        for _ in range(20):
                            if bframe and bframe.query_selector("#audio-source[src]"):
                                break
                            time.sleep(0.3)
                except Exception as e:
                    print(f"   ⚠️ 切换音频失败: {type(e).__name__}")
                    time.sleep(1)
                    continue
            
            if not bframe or bframe.query_selector("#audio-response") is None:
                print("   ⚠️ 音频挑战未就绪，等待...")
                time.sleep(1)
                continue
            
            # 取音频 + 转写
            audio_bytes = self._fetch_audio(page, bframe, captured)
            if not audio_bytes or not self._looks_like_audio(audio_bytes):
                fetch_fails += 1
                if audio_bytes and not self._looks_like_audio(audio_bytes):
                    print(f"   ⚠️ 取到的不是音频（{audio_bytes[:4]!r}，{len(audio_bytes)} 字节）")
                if fetch_fails >= 3:
                    print("   ⚠️ 连续取音频失败，换一段音频")
                    self._reload_audio(cf)
                    fetch_fails = 0
                    time.sleep(1.5)
                else:
                    print("   ⚠️ 未取到有效音频，稍等重试")
                    time.sleep(1.5)
                continue
            fetch_fails = 0
            with open(audio_path, "wb") as f:
                f.write(audio_bytes)
            try:
                transcript = self._transcribe(audio_path)
                transcribe_fails = 0
            except Exception as e:
                transcribe_fails += 1
                print(f"   ⚠️ 转写失败 ({transcribe_fails}/3): {type(e).__name__}: {e}")
                if transcribe_fails >= 3:
                    raise RuntimeError(f"Whisper 转写连续失败，音频模式不可用: {e}") from e
                time.sleep(1)
                continue
            
            audio_segments += 1
            print(f"   🎙️ 第 {audio_segments} 段音频 ({len(audio_bytes)} 字节) 转写: {transcript!r}")
            
            if not transcript:
                print("   ⚠️ 转写为空，换一段音频")
                self._reload_audio(cf)
                time.sleep(1.5)
                continue
            
            # 同一答案连续不过说明转写本身错了，换一段音频
            if transcript == last_transcript:
                same_answer_hits += 1
                if same_answer_hits >= 2:
                    print("   🔁 同一答案连续未通过，更换音频")
                    self._reload_audio(cf)
                    same_answer_hits = 0
                    last_transcript = None
                    time.sleep(1.5)
                    continue
            else:
                same_answer_hits = 0
            last_transcript = transcript
            
            # 先播放听完，再逐字输入（不播放直接作答是明显的机器行为）
            if AUDIO_PLAY:
                self._play_and_wait(page, bframe)
            
            try:
                inp = cf.locator("#audio-response").first
                inp.click(timeout=CLICK_TIMEOUT_MS)
                inp.fill("")
                page.keyboard.type(transcript, delay=random.randint(60, 140))
            except Exception as e:
                print(f"   ⚠️ 填写答案失败: {type(e).__name__}")
                time.sleep(1)
                continue
            
            time.sleep(random.uniform(0.5, 1.2))
            try:
                print("🖱️ 提交音频答案...")
                cf.locator("#recaptcha-verify-button").first.click(timeout=CLICK_TIMEOUT_MS)
            except Exception as e:
                print(f"   ⚠️ 提交失败: {type(e).__name__}")
            time.sleep(1.2)
            
            err = self._audio_error_text(bframe)
            if err:
                print(f"   ❌ Google 提示: {err}")
                if "multiple" in err.lower():
                    print("   ↻ 需要多段音频，继续下一轮")
                    last_transcript = None
        
        raise Exception("failed to solve recaptcha (audio mode)")
    
    def _get_token(self, page: Page) -> str:
        """获取 token"""
        try:
            textarea = page.query_selector("textarea[name='g-recaptcha-response']")
            if textarea:
                token = textarea.input_value()
                if token and len(token) > 10:
                    return token
            
            hidden_input = page.query_selector("input[name='g-recaptcha-response']")
            if hidden_input:
                token = hidden_input.get_attribute('value')
                if token and len(token) > 10:
                    return token
        except:
            pass
        raise Exception("failed to get token")


def solve_recaptcha(sitekey: str, url: str, headless: bool = True,
                    timeout: int = DEFAULT_SOLVE_TIMEOUT,
                    mode: Optional[str] = None,
                    proxy: Optional[str] = None) -> str:
    """
    便捷函数：解决 ReCAPTCHA v2 挑战
    
    :param sitekey: Google reCAPTCHA sitekey
    :param url: 原始页面 URL（用于伪装）
    :param headless: 是否无头模式
    :param timeout: 超时时间（秒）
    :param mode: "image"（图片挑战）| "audio"（语音挑战）
    :param proxy: 代理地址，如 http://127.0.0.1:7890；浏览器能直连 Google 时留空
    :return: token 字符串
    
    连续调用会自动间隔 MIN_SOLVE_INTERVAL 秒，避免触发 Google 端配额限制；
    若仍被限制会抛 RecaptchaBlockedError，等待一段时间再试即可。
    
    示例:
        token = solve_recaptcha(
            sitekey="6Le-wvkSAAAAAPBMRTvw0Q4Muexq9bi0DJwx_mJ-",
            url="https://www.google.com/recaptcha/api2/demo",
            mode="audio",
        )
    """
    with RecaptchaSolver(headless=headless, mode=mode, proxy=proxy) as solver:
        return solver.solve(sitekey, url, timeout)


# Google 官方 demo，比第三方 demo sitekey 干净（第三方常有配额/风控残留）
DEMO_SITEKEY = "6Le-wvkSAAAAAPBMRTvw0Q4Muexq9bi0DJwx_mJ-"
DEMO_URL = "https://www.google.com/recaptcha/api2/demo"


# === 主程序：命令行入口 ===
if __name__ == "__main__":
    import argparse
    
    ap = argparse.ArgumentParser(description="reCAPTCHA v2 solver")
    ap.add_argument("--sitekey", default=DEMO_SITEKEY)
    ap.add_argument("--url", default=DEMO_URL)
    ap.add_argument("--mode", default=SOLVE_MODE, choices=["image", "audio"],
                    help="image=图片挑战, audio=语音挑战")
    ap.add_argument("--headless", action="store_true", help="无头模式（默认有头）")
    ap.add_argument("--timeout", type=int, default=DEFAULT_SOLVE_TIMEOUT)
    ap.add_argument("--proxy", default=PROXY, help="如 http://127.0.0.1:7890，留空为直连")
    ap.add_argument("--min-interval", type=float, default=MIN_SOLVE_INTERVAL,
                    help="连续求解的最小间隔秒数，0=不限制")
    args = ap.parse_args()
    
    print(f"🔑 Sitekey: {args.sitekey}")
    print(f"🌐 URL: {args.url}")
    print(f"🎯 模式: {args.mode}   ⏱️ 超时: {args.timeout}s   "
          f"🛡️ 代理: {args.proxy or '直连'}   🖥️ headless: {args.headless}")
    print("⏳ 正在解决 ReCAPTCHA v2 挑战...")
    
    t0 = time.time()
    try:
        with RecaptchaSolver(headless=args.headless, mode=args.mode,
                            proxy=args.proxy, min_interval=args.min_interval) as solver:
            token = solver.solve(args.sitekey, args.url, args.timeout)
        print(f"\n🎉 成功! 耗时 {time.time()-t0:.1f}s")
        print(f"Token 长度: {len(token)}")
        print(f"Token: {token}")
    except RecaptchaBlockedError as e:
        print(f"\n🚫 被 Google 端限制 ({time.time()-t0:.1f}s)，退避后重试: {e}")
        raise SystemExit(2)
    except Exception as e:
        print(f"\n❌ 失败 ({time.time()-t0:.1f}s): {type(e).__name__}: {e}")
        raise SystemExit(1)
