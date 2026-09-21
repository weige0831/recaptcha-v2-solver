"""诊断：主验证框（复选框）为什么点不动
直接复现 solver 的页面准备流程，逐步打印状态
"""
import time
from camoufox.sync_api import Camoufox
import recaptcha_solver as rs

SITEKEY = "6LfD3PIbAAAAAJs_eEHvoOl75_83eXSqpPSRFJ_u"
URL = "https://2captcha.com/demo/recaptcha-v2"
HTML = rs.HTML_TEMPLATE.replace("{{SITEKEY}}", SITEKEY)

print("启动浏览器...", flush=True)
with Camoufox(headless=True, humanize=False, i_know_what_im_doing=True,
              config={'forceScopeAccess': True}, disable_coop=True) as browser:
    page = browser.new_page()
    page.route(URL, lambda r: r.fulfill(status=200, content_type="text/html", body=HTML))
    page.route(URL + "/", lambda r: r.fulfill(status=200, content_type="text/html", body=HTML))

    print(f"goto {URL}", flush=True)
    page.goto(URL, wait_until="networkidle")
    time.sleep(2)

    page.wait_for_selector("iframe[title='reCAPTCHA']", timeout=30000)
    print("✅ reCAPTCHA iframe 已出现", flush=True)

    main_frame = page.frame_locator("iframe[title='reCAPTCHA']")
    anchor = main_frame.locator("[class^='rc-anchor-center-item']").first

    # 1. 复选框本身的状态
    print("\n--- 复选框状态 ---", flush=True)
    try:
        print(f"  count        : {anchor.count()}", flush=True)
        box = anchor.bounding_box()
        print(f"  bounding_box : {box}", flush=True)
        print(f"  visible      : {anchor.is_visible()}", flush=True)
        print(f"  enabled      : {anchor.is_enabled()}", flush=True)
        print(f"  checked      : {main_frame.locator('[aria-checked]').first.get_attribute('aria-checked')}", flush=True)
    except Exception as e:
        print(f"  ❌ 查询失败: {type(e).__name__}: {str(e)[:200]}", flush=True)

    # 2. anchor iframe 的完整文字（看 Google 到底说了什么）
    print("\n--- anchor iframe 文字 ---", flush=True)
    try:
        txt = main_frame.locator("body").inner_text(timeout=3000)
        print(f"  {txt!r}", flush=True)
    except Exception as e:
        print(f"  ❌ 读取失败: {type(e).__name__}", flush=True)

    # 3. 尝试点击，打印完整错误
    print("\n--- 尝试点击复选框 (timeout=15000ms) ---", flush=True)
    t0 = time.time()
    try:
        anchor.click(timeout=15000)
        print(f"  ✅ 点击成功  耗时 {time.time()-t0:.1f}s", flush=True)
    except Exception as e:
        print(f"  ❌ 点击失败  耗时 {time.time()-t0:.1f}s", flush=True)
        print(f"     类型: {type(e).__name__}", flush=True)
        print(f"     详情: {str(e)[:600]}", flush=True)

    time.sleep(4)

    # 4. 点击后是否有弹出层
    print("\n--- 点击后状态 ---", flush=True)
    bf_sel = "iframe[src*='recaptcha/api2/bframe'], iframe[src*='recaptcha/enterprise/bframe']"
    bf = page.query_selector(bf_sel)
    print(f"  bframe 存在: {bf is not None}", flush=True)
    if bf and bf.content_frame():
        f = bf.content_frame()
        body = f.evaluate("document.body ? document.body.innerText : ''") or ""
        print(f"  bframe 文字: {body[:300]!r}", flush=True)
        print(f"  有图片挑战: {f.query_selector('.rc-imageselect-challenge') is not None}", flush=True)

    print("\n--- _detect_block 判定 ---", flush=True)
    print(f"  -> {rs.RecaptchaSolver._detect_block(None, page, main_frame)}", flush=True)

    print("\n--- 最终 anchor 文字 ---", flush=True)
    try:
        print(f"  {main_frame.locator('body').inner_text(timeout=3000)!r}", flush=True)
    except Exception as e:
        print(f"  ❌ {type(e).__name__}", flush=True)

    print("\n--- 页面是否有 quota 字样(整页) ---", flush=True)
    try:
        html = page.content()
        for kw in ["quota", "exceeding", "Try again later", "automated"]:
            print(f"  {kw!r}: {kw.lower() in html.lower()}", flush=True)
    except Exception as e:
        print(f"  ❌ {type(e).__name__}", flush=True)
