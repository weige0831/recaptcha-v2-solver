"""预检：验证 ML 依赖能否正常加载与推理（不涉及浏览器）"""
import time
import traceback

print("=" * 60)
print("[1/4] 检查基础库版本")
print("=" * 60)
import numpy as np
import cv2
print(f"  numpy        : {np.__version__}")
print(f"  opencv       : {cv2.__version__}")
import torch
print(f"  torch        : {torch.__version__}  (cuda available: {torch.cuda.is_available()})")
import transformers
print(f"  transformers : {transformers.__version__}")

print()
print("=" * 60)
print("[2/4] 加载 YOLO11x（首次会自动下载权重）")
print("=" * 60)
t0 = time.time()
try:
    from ultralytics import YOLO
    yolo = YOLO("yolo11x.pt")
    print(f"  ✅ YOLO11x 加载成功  耗时 {time.time()-t0:.1f}s")
    print(f"  类别数: {len(yolo.names)}")
except Exception:
    print("  ❌ YOLO 加载失败")
    traceback.print_exc()

print()
print("=" * 60)
print("[3/4] 加载 GroundingDINO-tiny（首次会自动下载权重）")
print("=" * 60)
t0 = time.time()
processor = None
model = None
try:
    from transformers import AutoProcessor, AutoModelForZeroShotObjectDetection
    processor = AutoProcessor.from_pretrained("IDEA-Research/grounding-dino-tiny")
    model = AutoModelForZeroShotObjectDetection.from_pretrained("IDEA-Research/grounding-dino-tiny")
    model.eval()
    print(f"  ✅ GroundingDINO 加载成功  耗时 {time.time()-t0:.1f}s")
except Exception:
    print("  ❌ GroundingDINO 加载失败")
    traceback.print_exc()

print()
print("=" * 60)
print("[4/4] 用合成图片测试推理 + post_process API 兼容性")
print("=" * 60)
if processor is not None and model is not None:
    try:
        from PIL import Image
        # 合成一张带矩形“车辆”的图片，验证端到端推理路径
        arr = np.full((300, 300, 3), 200, dtype=np.uint8)
        arr[120:200, 80:220] = (40, 40, 180)
        img = Image.fromarray(arr)

        prompt = "car. truck."
        inputs = processor(images=img, text=prompt, return_tensors="pt")
        with torch.no_grad():
            outputs = model(**inputs)
        results = processor.post_process_grounded_object_detection(
            outputs, inputs.input_ids, threshold=0.25, text_threshold=0.25,
            target_sizes=[img.size[::-1]]
        )[0]
        boxes = results["boxes"].cpu().numpy()
        scores = results["scores"].cpu().numpy()
        print(f"  ✅ 推理成功，检测到 {len(boxes)} 个目标")
        print(f"  results 可用键: {list(results.keys())}")
        # 脚本里用到的取标签方式，验证是否兼容
        labels = results.get("text_labels", results.get("labels", ["object"] * len(boxes)))
        if hasattr(labels, "tolist"):
            labels = labels.tolist()
        print(f"  text_labels 取值示例: {labels[:3] if len(labels) else '(空)'}")
        for b, s in zip(boxes[:3], scores[:3]):
            print(f"    box={[int(c) for c in b]} score={s:.3f}")
    except Exception:
        print("  ❌ 推理/后处理失败 —— 可能是 transformers v5 API 变更")
        traceback.print_exc()
else:
    print("  ⏭️  跳过（模型未加载成功）")

print()
print("预检结束")
