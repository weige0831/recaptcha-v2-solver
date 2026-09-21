"""探测：连续多次加载 reCAPTCHA 组件，统计 Google 实际下发的状态
用于区分「求解器代码问题」与「Google 端配额/风控限制」
"""
import time
from camoufox.sync_api import Camoufox

SITEKEY = "6LfD3PIbAAAAAJs_eEHvoOl75_83eXSqpPSRFJ_u"
URL = "https://2captcha.com/demo/recaptcha-v2"
ROUNDS = 6

HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<script src="https://www.google.com/recaptcha/api.js" async defer></script></head>
<body style="display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0">
<div class="g-recaptcha" data-sitekey="%s"></div></body></html>""" % SITEKEY


def classify(page):
    """判定当前组件处于什么状态"""
    states = []
    try:
        bf = page.query_selector("iframe[src*='recaptcha/api2/bframe'], iframe[src*='recaptcha/enterprise/bframe']")
        if bf:
            f = bf.content_frame()
            if f:
                body = (f.evaluate("document.body ? document.body.innerText : ''") or "")
                # 是否出现配额/风控提示
                for kw, tag in [("exceeding", "配额超限(exceeding free quota)"),
                                ("quota", "配额相关提示"),
                                ("Try again later", "稍后重试"),
                                ("automated queries", "检测到自动化查询"),
                                ("not a robot", "正常挑战(可见文字)")]:
                    if kw.lower() in body.lower():
                        states.append(tag)
                # 是否有图片挑战网格
                if f.query_selector(".rc-imageselect-challenge"):
                    states.append("有图片挑战网格")
                else:
                    states.append("无图片挑战")
    except Exception as e:
        states.append(f"读取bframe失败:{type(e).__name__}")
    return states or ["未知"]


print(f"连续探测 {ROUNDS} 次组件状态（每次新建页面）\n")
summary = {}
with Camoufox(headless=True, i_know_what_im_doing=True) as browser:
    for i in range(1, ROUNDS + 1):
        page = browser.new_page()
        try:
            page.route(URL, lambda r: r.fulfill(status=200, content_type="text/html", body=HTML))
            page.route(URL + "/", lambda r: r.fulfill(status=200, content_type="text/html", body=HTML))
            page.goto(URL, wait_until="networkidle", timeout=30000)
            time.sleep(2)
            # 点击复选框
            try:
                page.frame_locator("iframe[title='reCAPTCHA']").locator(
                    "[class^='rc-anchor-center-item']").first.click(timeout=5000)
            except Exception as e:
                print(f"[{i}] 点击复选框失败: {type(e).__name__}")
            time.sleep(6)  # 等待 Google 下发结果

            # 复选框是否打钩
            checked = False
            try:
                checked = page.frame_locator("iframe[title='reCAPTCHA']").locator('[aria-checked="true"]').count() > 0
            except Exception:
                pass

            states = classify(page)
            if checked:
                states = ["✅ 复选框已通过(免图片挑战)"] + states
            print(f"[{i}] {' | '.join(states)}")
            for s in states:
                summary[s] = summary.get(s, 0) + 1
        finally:
            page.close()

print("\n" + "=" * 60)
print("统计")
print("=" * 60)
for k, v in sorted(summary.items(), key=lambda x: -x[1]):
    print(f"  {v}/{ROUNDS}  {k}")
