"""修复：iframe 导航导致 execution context 被销毁时，整个求解被中断

证据（stability/r4_audio，10 次里 2 次失败，都是同一个错误、都在第 1 轮 ~28s 就挂）：
    Error: Frame.query_selector: Execution context was destroyed,
           most likely because of a navigation

音频挑战所在的 iframe 会自行导航/重载，此时 frame 的执行上下文被销毁，
任何 query_selector / content_frame 调用都会抛异常。原来这些调用没有保护，
一次瞬时导航就把整次求解判死（而重试本可以救回来）。

修法：所有 frame 查询走 _safe_query 包一层，失败按"查不到"处理并进入下一轮重试。
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

# 1) 辅助方法
ok &= sub(
    """    @staticmethod
    def _looks_like_audio(data: Optional[bytes]) -> bool:""",
    '''    @staticmethod
    def _safe_query(frame, selector):
        """查询 frame 内元素，失败按"查不到"处理，返回 None

        挑战 iframe 会自行导航/重载，此时 frame 的执行上下文被销毁，
        query_selector / content_frame 都会抛
        "Execution context was destroyed, most likely because of a navigation"。
        一次瞬时导航不该把整次求解判死——返回 None 让主循环下一轮重试。
        """
        try:
            return frame.query_selector(selector)
        except Exception:
            return None

    @staticmethod
    def _looks_like_audio(data: Optional[bytes]) -> bool:''',
    "_safe_query 辅助方法")

# 2) bframe 获取加保护
ok &= sub(
    """            bf_el = page.query_selector(bframe_sel)
            bframe = bf_el.content_frame() if bf_el else None
            if not bframe:
                print("❓ 验证窗口未找到，等待...")
                time.sleep(1)
                continue
            cf = page.frame_locator(bframe_sel)""",
    """            try:
                bf_el = page.query_selector(bframe_sel)
                bframe = bf_el.content_frame() if bf_el else None
            except Exception as e:
                # iframe 正在导航，执行上下文被销毁，下一轮再来
                print(f"❓ 取验证窗口失败({type(e).__name__})，等待后重试...")
                time.sleep(1)
                continue
            if not bframe:
                print("❓ 验证窗口未找到，等待...")
                time.sleep(1)
                continue
            cf = page.frame_locator(bframe_sel)""",
    "bframe 获取加保护")

# 3) 音频路径里的 query_selector 全部改走 _safe_query
ok &= sub(
    """            if bframe.query_selector("#audio-response") is None:""",
    """            if self._safe_query(bframe, "#audio-response") is None:""",
    "query(音频响应框)")

ok &= sub(
    """                        bf_el = page.query_selector(bframe_sel)
                        bframe = bf_el.content_frame() if bf_el else None
                        # 切过去之后音频 payload 还要下载，等 src 真正挂上
                        for _ in range(20):
                            if bframe and bframe.query_selector("#audio-source[src]"):
                                break
                            time.sleep(0.3)""",
    """                        try:
                            bf_el = page.query_selector(bframe_sel)
                            bframe = bf_el.content_frame() if bf_el else None
                        except Exception:
                            bframe = None
                        # 切过去之后音频 payload 还要下载，等 src 真正挂上
                        for _ in range(20):
                            if bframe and self._safe_query(bframe, "#audio-source[src]"):
                                break
                            time.sleep(0.3)""",
    "切换后重取 bframe 加保护")

ok &= sub(
    """            if not bframe or bframe.query_selector("#audio-response") is None:""",
    """            if not bframe or self._safe_query(bframe, "#audio-response") is None:""",
    "query(音频就绪判断)")

if not ok:
    print("\n有未命中，未写入")
    sys.exit(1)

with open(PATH, "w", encoding="utf-8") as f:
    f.write(s)
print(f"\n已应用 {len(applied)} 处修改")
