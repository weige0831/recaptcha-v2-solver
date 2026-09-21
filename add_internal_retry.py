"""把"换出口节点重试"内置进求解器

为什么：节点轮换 + 重试目前在外层台架里，于是"一次 solve() 调用"的成功率仍是 ~80%，
只有批量脚本才有 90%。要让单次调用本身就是高可靠，重试必须属于求解器行为。

设计：
  RecaptchaSolver(nodes=[...], solve_retries=2)
  - nodes 非空时，每次尝试前自动切到下一个出口节点
  - 单次尝试失败就换节点再试，最多 solve_retries 次
  - RecaptchaBlockedError（限流）不内部重试：同一时刻换 IP 也未必救得回来，
    交给上层退避更合理
  - clash_api 只在传入 nodes 时才导入，不引入硬依赖
"""
import sys

PATH = "recaptcha_solver.py"
with open(PATH, encoding="utf-8") as f:
    s = f.read()

OLD_SOLVE = '''        self._respect_cooldown()
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
            # 切换出口节点后首次加载偶发超时（实测 TimeoutError），重试几次
            last_err = None
            for attempt in range(1, 4):
                try:
                    page.goto(url, wait_until="networkidle", timeout=45000)
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    print(f"   ⚠️ 页面加载失败({type(e).__name__})，第 {attempt}/3 次重试...")
                    time.sleep(2)
            if last_err is not None:
                raise last_err
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
'''

NEW_SOLVE = '''        self._respect_cooldown()
        solve_mode = (mode or self.mode or "image").lower()
        total = max(1, int(self.solve_retries) + 1)
        last_err = None
        for attempt in range(1, total + 1):
            # 每次尝试前换一个出口节点。抽到信誉差的 IP 时题链会病态变长
            # （实测到 120 轮仍未完），换 IP 重试比在原 IP 上硬耗有效得多。
            if self.nodes:
                self._rotate_node(attempt)
            try:
                return self._attempt_once(sitekey, url, solve_mode, timeout)
            except RecaptchaBlockedError:
                raise  # 被限流时立刻换 IP 未必有用，交给上层退避
            except Exception as e:
                last_err = e
                if attempt < total:
                    print(f"↻ 本次求解失败({type(e).__name__})，换出口节点重试 "
                          f"({attempt}/{total - 1})")
        raise last_err

    def _rotate_node(self, attempt: int) -> None:
        """切换到下一个出口节点（Clash）。失败不影响求解，只是少了换 IP 的效果"""
        try:
            from urllib.parse import quote as _q
            import clash_api
        except Exception as e:
            print(f"   ⚠️ 无法导入 clash_api（{type(e).__name__}），跳过换节点")
            return
        node = self.nodes[(self._node_cursor + attempt - 1) % len(self.nodes)]
        try:
            st, _ = clash_api.api("PUT", "/proxies/" + _q(clash_api.GROUP, safe=""),
                                  {"name": node})
            print(f"   🌐 出口节点 -> {node}  ({st.split()[1] if ' ' in st else st})")
        except Exception as e:
            print(f"   ⚠️ 切换节点失败 {type(e).__name__}: {e}")
        time.sleep(NODE_SWITCH_WAIT)

    def _attempt_once(self, sitekey: str, url: str, solve_mode: str, timeout: int) -> str:
        """一次完整尝试：开页 → 解挑战 → 取 token → 收尾"""
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
            # 切换出口节点后首次加载偶发超时（实测 TimeoutError），重试几次
            last_err = None
            for attempt in range(1, 4):
                try:
                    page.goto(url, wait_until="networkidle", timeout=45000)
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    print(f"   ⚠️ 页面加载失败({type(e).__name__})，第 {attempt}/3 次重试...")
                    time.sleep(2)
            if last_err is not None:
                raise last_err
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
'''


def sub(old, new, label, count=1):
    global s
    n = s.count(old)
    if n != count:
        print(f"  x [{label}] 命中 {n} 次（期望 {count}）")
        return False
    s = s.replace(old, new, count)
    print(f"  ok [{label}]")
    return True


ok = True
ok &= sub(OLD_SOLVE, NEW_SOLVE, "solve 拆成尝试循环 + _attempt_once")

# __init__ 增加 nodes / solve_retries
ok &= sub(
    """                 whisper_model: Optional[str] = None,
                 proxy: Optional[str] = None):""",
    """                 whisper_model: Optional[str] = None,
                 proxy: Optional[str] = None,
                 nodes: Optional[List[str]] = None,
                 solve_retries: int = SOLVE_RETRIES):""",
    "__init__ 参数")

ok &= sub(
    """        self.proxy = PROXY if proxy is None else proxy
        self.browser = None""",
    """        self.proxy = PROXY if proxy is None else proxy
        # 出口节点列表（Clash 节点名）。非空时每次尝试前自动换一个，
        # 用于规避"按 IP 记"的限流与信誉惩罚
        self.nodes = nodes or []
        self.solve_retries = solve_retries
        self._node_cursor = 0
        self.browser = None""",
    "__init__ 赋值")

# 常量
ok &= sub(
    """BROWSER_CLOSE_TIMEOUT = float(os.environ.get("V2_CLOSE_TIMEOUT", "25"))""",
    """BROWSER_CLOSE_TIMEOUT = float(os.environ.get("V2_CLOSE_TIMEOUT", "25"))
# 单次 solve 内部失败后换出口节点重试的次数（0=不重试）
SOLVE_RETRIES = int(os.environ.get("V2_SOLVE_RETRIES", "2"))
# 切换出口节点后的等待秒数（等连接稳定，避免首次加载超时）
NODE_SWITCH_WAIT = float(os.environ.get("V2_NODE_WAIT", "4"))""",
    "常量")

if not ok:
    print("\n有未命中，未写入")
    sys.exit(1)

with open(PATH, "w", encoding="utf-8") as f:
    f.write(s)
print("\n已写入")
