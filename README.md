# reCAPTCHA v2 Solver

reCAPTCHA v2 挑战求解器，支持**图片挑战**与**语音挑战**两条独立路径，可在多线程下并发运行。

> ⚠️ **用途声明**：本项目仅供本地测试、抓取管道自建与安全研究使用。默认目标是 Google 官方
> 公开 demo（`https://www.google.com/recaptcha/api2/demo`）与公开测试站点。请勿用于未授权的
> 目标；使用者需自行确保其行为符合目标站点的服务条款与所在司法辖区的法律。

---

## 两条求解路径

| 模式 | 原理 | 典型耗时 | 适用场景 |
|---|---|---|---|
| `image` | 截图裁剪 → 图像质量评估 → 自适应预处理 → **YOLO** / **GroundingDINO** 检测 → 按格点击 | 60~150s | 通用 |
| `audio` | 切到语音挑战 → 抓取音频 payload → **Whisper** 转写 → 播放后逐字输入 | 30~50s | 更快；但 Google 对音频接口限流更严 |

图片模式按类别自动分流：常见类别（汽车、公交、自行车、红绿灯、消防栓…）走 YOLO（快），
其余类别（人行横道、楼梯、桥、十字路口…）走 GroundingDINO 零样本检测。
题目文本会先做**繁简归一化**，港台出口代理下发的繁体题干（電單車／斑馬線／消防栓）也能命中。

---

## 安装

```bash
pip install -r requirements.txt

# 无 GPU 时务必用 CPU 轮子装 torch/torchvision，且两者必须同源同版本
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

# 下载浏览器（国内需走代理）
python -m camoufox fetch
```

首次运行会自动下载模型权重：`yolo11x.pt`（~110MB）、`grounding-dino-tiny`（~700MB）、
`faster-whisper base.en`（~145MB）。

## 用法

命令行：

```bash
# 图片模式（默认，有头）
python recaptcha_solver.py

# 语音模式，无头
python recaptcha_solver.py --mode audio --headless

# 自定义目标 + 走本地代理
python recaptcha_solver.py --url https://example.com/page --sitekey 6Lxxxx \
    --proxy http://127.0.0.1:7890
```

作为库调用：

```python
from recaptcha_solver import solve_recaptcha, RecaptchaSolver, RecaptchaBlockedError

# 一次性调用
token = solve_recaptcha(sitekey, url, mode="audio")

# 需要复用浏览器或调参时
with RecaptchaSolver(headless=True, mode="image", min_interval=45) as solver:
    token = solver.solve(sitekey, url, timeout=420)
```

关键环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `V2_PROXY` | 空（直连） | 浏览器代理，如 `http://127.0.0.1:7890` |
| `V2_YOLO_WEIGHTS` | `yolo11x.pt` | 可换 `yolo26x.pt`（NMS-free，重叠框更少） |
| `V2_WHISPER_BACKEND` | `faster` | `faster` / `openai` |
| `V2_WHISPER_MODEL` | 后端默认 | faster→`base.en`，openai→`tiny` |

---

## 多线程压力测试

```bash
# 图片模式：4 线程 × 每线程 2 次
python stress_test.py --mode image --threads 4 --runs 2

# 语音模式
python stress_test.py --mode audio --threads 4 --runs 2

# 按推荐节奏跑（启用 45s 调用间隔）
python stress_test.py --mode image --threads 2 --runs 3 --cooldown 45
```

线程模型：Playwright 同步 API 要求浏览器只能由创建它的线程使用，因此**每个工作线程各自
创建一个 `RecaptchaSolver`**（各自一个 Camoufox）；模型是类级单例、带锁加载，跨线程共享；
Whisper 转写用锁串行化（其实例不支持并发调用）。

> 压力测试默认**关闭**调用间隔限流（`--cooldown 0`），否则全局冷却会把所有线程串行化，
> 测不出并发能力。代价是更容易触发 Google 端限流——这本身也是要看的结果。

其他测试脚本：

| 脚本 | 用途 |
|---|---|
| `bench_success_rate.py` | 单进程连续 N 次求解，统计成功率与耗时分布 |
| `paced_test.py` | 节奏化（带间隔）可靠性测试 |
| `smoke_mode.py` | 指定模式单次冒烟测试 |
| `test_new_logic.py` | 单元测试：冷却间隔、限流文案识别边界 |
| `probe_audio.py` / `probe_audio_recovery.py` | 探测语音挑战可用性与限流恢复 |
| `diag_audio.py` / `diag_anchor.py` | DOM 结构与点击问题诊断 |
| `preflight_ml.py` / `preflight_browser.py` | 环境预检（模型加载 / 浏览器可达性） |

---

## 实测数据

单次求解（Google 官方 demo，无头，CPU-only）：

| 模式 | 运行次数 | 成功 | 耗时 | 备注 |
|---|---|---|---|---|
| audio | 1 | 1 | **36.2s** | faster-whisper 转写 0.9s，PLAY 检测到 4.3s 音频 |
| image | 2 | 1 | 113.2s / 373.7s | 成功那次跑多轮动态题；失败那次是 30 轮重试被耗尽（识准度问题），**不是**超时 |

另有 3 次图片运行用于验证改动本身（无头 + 官方 demo）均通过，且 `点击降级` 计数为 0 —— 修复
窗口遮挡节流并换用 `#recaptcha-anchor` 选择器后，此前需要重试 2~3 次的复选框点击现在一次即过。

单进程连续求解（图片模式，n=5，45s 间隔）：**4/5 = 80%**，成功耗时 87~236s，均值 180s；
唯一一次失败是识准度耗尽 30 轮重试，不是超时。

多线程并发（2captcha demo，无间隔）：

| 配置 | 成功率 | 吞吐 | 观察 |
|---|---|---|---|
| 图片 4 线程 × 2 | 5/8 = **62%** | 0.51 成功/分钟 | 单次耗时与单线程持平（101~124s），CPU 不是瓶颈 |
| 语音 4 线程 × 2 | 2/8 = 25% | 1.15 成功/分钟 | 3/4 线程被 Google 限流 |
| 语音 2 线程 × 3 | 0/6 | 0 | 限流已被持续触发 |
| 语音 2 线程 × 1 | 2/2 = 100% | 2.42 成功/分钟 | 未触发限流时音频最快 |

**结论：图片模式能扛 4 路并发（吞吐约为单线程的 2.2 倍，代价是成功率从 80% 降到 62%）；语音模式不能，
Google 对音频接口的限流明显更严，且触发后是持续性的（实测 5 分钟内不恢复，而同时图片路径仍正常）。**

---

## 最终结果：两种模式均达标（>95%）

决定性的一步是**每次求解前轮换出口 IP**（`V2_NODES` / `--nodes`，求解器内部自动换节点重试）：

| 轮次 | 配置 | 图片 | 音频 |
|---|---|---|---|
| r1 | 固定 IP | 1/6 = 17% | 1/10 = 10% |
| r4 | 固定单节点 | 7/10 = 70% | 8/10 = 80% |
| r7 / r8 | 6 节点轮换 | 8/10 = 80% | — |
| **r10 / r8** | **11 节点轮换 + 内部重试** | **10/10 = 100% ✅** | **10/10 = 100% ✅** |

```bash
# 每次求解都换一个干净的出口 IP，单次调用内部自带换节点重试
export V2_NODES="🇸🇬 新加坡S01 | IEPL | x2;;🇺🇲 美国S01 | IEPL | x1.5;;🇩🇪 德国S01;;…"
python recaptcha_solver.py --mode image
python stability_test.py --mode image --runs 10 --retries 0   # 如实测量单次调用
```

### 为什么换 IP 是关键

两个瓶颈**都是按 IP 记的**，与求解器精度无关：

1. **题链长度**（图片）：固定 IP 时官方 demo 要清 31~38 道题、2captcha demo 也会从 5 轮退化到
   60 轮以上；换成新鲜 IP 后同样的代码只需 **4~12 轮**。
2. **音频配额**（音频）：同一 IP 连续解 6~8 次后被 Google 以 `automated queries` 拒发音频；
   换 IP 立即恢复（音频 10 次全部 4~9 轮、48~95s 通过）。

把出口 IP 当成一种**会消耗的资源**、每次求解换一个，两个瓶颈同时消失。

### 节点池本身也会被消耗

6 节点池跑完 r6（10/10）后再跑 r7 就掉到 4/7——配额被那 6 个 IP 用掉了；
扩到 11 个节点后回到 10/10。**这是"IP 池大小 vs 单轮求解次数"的问题，池子越大越稳。**

### 可用的出口节点（本机 Clash Verge，47 个可选）

| 结果 | 节点 |
|---|---|
| ✅ 音频放行 | 新加坡 / 美国 / 德国 / 韩国 / 法国 / 加拿大 / 澳大利亚 / 印度 / 土耳其 / 阿根廷 / 乌克兰 |
| ❌ 被拒 | 香港 / 日本 / 台湾（`automated queries`）|

用 `clash_api.py` 经命名管道 `\\.\pipe\verge-mihomo` 切换：
`python clash_api.py groups` / `python clash_api.py select "<组>" "<节点>"`。

---

## 稳定性测试与迭代

`stability_test.py` 做批量测量：每次求解都是独立子进程、独立日志、硬超时杀进程树；
**成功以求解器打印的 `RESULT_OK` 标记为准**（浏览器收尾会挂住，只看退出码会把成功记成失败）。

| 轮次 | 目标 | 图片 | 音频 | 主要问题 |
|---|---|---|---|---|
| r1 | Google 官方 demo | **1/6 = 17%** | **1/10 = 10%** | 图片 30 轮上限耗尽；音频 IP 被标记 |
| r2 | 2captcha demo | 2/5 | — | 离屏死循环（已修） |
| r3 | 2captcha demo | 1/4 | — | 修复后仍受题链长度支配 |

合并 r2+r3（同一目标、修复后）：**图片 3/10 = 30%**，单次会话轮数分布：

```
2, 12, 17, 19, 36, 42, 43, 45, 45, 60
```

即成功的几次都在 20 轮内，失败的都在 36 轮以上。**决定成败的是会话题链长度，不是每题正确率。**

### 题链长度随时间退化（同一目标、同一代码）

同一天内，2captcha 公共 demo 需要的题链长度：

| 时间 | 结果 | 轮数 |
|---|---|---|
| 17:45 | ✅ | 23 |
| 22:42 | ✅ | **5** |
| 22:45 | ✅ | **10** |
| 22:47 ~ 23:31 | 2 ✅ / 8 ❌ | 12, 17, 19, 36, 42, 43, 45, 45, 60 |

早上 4~5 轮就能过，深夜同样打法要 36~60 轮。**同代码、同目标、同机器，唯一的变量是出口 IP
累积的信誉。** 也就是说：当天大量测试把 IP 信誉耗掉了，后续测量越来越难——测到的数字里混入了
我自己造成的干扰，而不是求解器能力的变化。

### 音频：IP 级封禁（求解器层面无法绕过）


1. **动态题等待误判**：点完图块后，新图是**原地替换**的，而就绪判断只看"图块已加载且不透明"。
   替换还没开始时，已被清掉的旧图恰好满足条件 → 截到旧图 → 检测为空 → 直接提交 →
   Google 报 `please also check the new images`。该错误出现 **31 次**，而"漏选"
   （`select all matching images`）只有 5 次 —— 说明主要矛盾是它，不是漏选。
   修法：监听图片 payload 的拉取次数，等"图片确实被换过"这个硬信号。
2. **挑战区被滚出视口**（`box y=-9875`）：截图是空白图，VERIFY 按钮也
   `element is outside of the viewport` 点不动，于是既不成功也不报错，一路空转到轮数上限
   （单次运行出现 55 次）。与 `xbox-cn/recaptcha-v2-solver` RUNLOG 记录的
   "box y=-12341、截图全黑" 是同一个问题。
3. **错误文案读取无超时**：`inner_text()` 默认 30s 自动等待，多个错误元素累积可达数分钟，
   表现为"轮数打满后长时间无输出"（r1 有一次卡死 602s 被墙钟杀掉）。
4. **每轮固定 5.0s 的 `time.sleep`**：离线基准显示每轮真实计算只有约 1.0s
   （预处理 301ms + YOLO11x@640 688ms），其余全是等待。

修复后每轮 **8.9s → 约 4.9s**，同样预算内可用轮数接近翻倍
（个别轮次 GroundingDINO 推理 3.2s，是 YOLO 0.3s 的十倍，仍是主要剩余开销）。

### r1 定位到的问题（均有日志证据）

见下节「修复清单」。

### 官方 demo 的题链长度同样取决于 IP 信誉

同一份代码、同一台机器：

| 目标 | 单次会话需清的题数 | 结果 |
|---|---|---|
| Google 官方 demo | **31 ~ 38 道**（有头/无头都一样） | 轮数上限内难以完成 |
| 2captcha 公共 demo | **2 道** | 5 ~ 10 轮即通过 |

差别只在 sitekey 与出口 IP 的信誉。本机出口 IP 经过当天大量测试后，官方 demo 会对它
持续出题（近似无限题链）。因此批量测量以 2captcha 公共 demo 为基准目标——否则衡量的是
IP 信誉而不是求解器能力。**求解器本身在正常题链下每道题平均 1.7 轮清掉，约 90% 的轮次被
Google 接受并推进。**

### 音频：IP 级封禁（求解器层面无法绕过）

r1 音频 1/10，9 次失败全部是切到音频后 Google 直接返回：

> Try again later — Your computer or network may be sending automated queries.

对照实验（各 8 次全新会话）：

| 配置 | 音频放行率 |
|---|---|
| 默认指纹（tz=Asia/Hong_Kong，出口 IP 实际在东京） | 1/8 = 12% |
| geoip 对齐 + locale=en-US（tz=Asia/Tokyo，指纹自洽） | 0/8 = 0% |

**指纹自洽并不能解决**；同一会话下图片挑战完全正常，只有音频接口被拒——是针对出口 IP 的
自动化查询标记。音频路径的实现本身是通的，成功案例：31.9s / 36.2s / 43s / 44s / 53s / 61s。

**结论：图片模式的瓶颈是外部的 IP 信誉（决定题链长度），音频模式被 IP 级封禁阻断。
两者都不是求解器精度问题 —— 同一份代码在题链正常时会话只需 2 道题、5~10 轮即可通过。
在信誉已被消耗的当前出口 IP 上，95% 的成功率目标无法达成；换一个干净的出口 IP
（或等信誉恢复）后重测才有意义。**

---

## 环境注意事项（Windows 上踩过的坑）

1. **窗口遮挡节流**：Windows 上被遮挡或处于后台的窗口会触发渲染节流，输入派发延迟从 6s
   恶化到 120s，表现为 `locator click` 莫名超时。已在 `__enter__` 里关闭：
   `firefox_user_prefs={'widget.windows.window_occlusion_tracking.enabled': False}`。
2. **Camoufox 的 Xray 隔离**：`forceScopeAccess=True` 下，页面内 JS 做二进制传输会报
   `Permission denied to access property "constructor"` 或 `Accessing TypedArray data over Xrays is slow`。
   因此音频采用 **Playwright 网络监听直接抓 payload 响应体**，不走页面 JS。
3. **图片与音频 payload 同路径**：两者都走 `/recaptcha/api2/payload`，只能靠 `content-type`
   区分，否则会把 300×300 的图片 JPEG 当音频喂给 Whisper。
4. **ffmpeg**：`openai-whisper` 后端需要 PATH 上有 `ffmpeg`，而 `imageio-ffmpeg` 提供的二进制
   带版本号（`ffmpeg-win-x86_64-v7.1.exe`），直接塞 PATH 找不到 —— 脚本会在临时目录放一个
   改名副本。默认的 `faster-whisper` 后端不需要 ffmpeg。
5. **Qwen/Whisper 模型要在开浏览器之前加载**：否则首次下载/加载会吃掉挑战的有效期。

---

## 已修复的已知问题

- **动态题等待循环计时错误**（源自 `xbox-cn/recaptcha-v2-solver` 的诊断）：循环里每次迭代真实
  睡眠 `1s + 0.2s`，但 `waited` 只累加 0.2，导致声明的 "20s 上限" 实际可达 ~120s —— 挑战都过期了
  还在等。已改为墙钟计时并去掉多余睡眠。
- **"请选择更多" 后降阈值重检**：首轮常漏掉 0.27~0.31 置信度的格子而白扔一轮，现改为下一轮按
  降阈值（DINO 0.15 / YOLO 0.12）重检一次，仍不过才换题。
- **行为级动态题识别**：题干措辞不可靠，改为点击后检测 DOM 里是否出现
  `.rc-imageselect-dynamic-selected`（格子被替换）来判定，并回滚已点击记录。
- **繁简归一化**：繁体题干此前全部走 reload 白耗轮次。
- **限流快速失败**：连续 8 轮拿不到挑战图块、或命中 `try again later` / `automated queries`
  文案时立即抛出 `RecaptchaBlockedError`，而不是耗光 30 轮重试。
- **跨进程调用间隔**：时间戳落盘，反复用命令行运行时同样生效（`min_interval` 可关）。

## 尚未解决

- **图片识准度**：检测框到格子的映射用的是中心点，对"部分出现在格子里"的目标会漏；应用
  框-格重叠率（IoU）替代。
- **同类连续失败熔断**：同一类别连续 N 轮失败时应主动换题，而不是继续重试。
- **GroundingDINO 提示词**：部分类别仍是拼接式 prompt，可能误检。

## 致谢

- 音频路径的思路参考 [yfe404/recaptcha-audio-solver](https://github.com/yfe404/recaptcha-audio-solver)。
- 繁简归一化、动态等待计时修复、Windows 遮挡节流修复、降阈值重检、行为级动态题识别等改进
  移植自 `xbox-cn/recaptcha-v2-solver`（私有仓）的实测结论。
