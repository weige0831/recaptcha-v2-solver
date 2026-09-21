"""探测：音频挑战在当前限制状态下是否可用，以及 DOM 结构

参考 yfe404/recaptcha-audio-solver 的选择器：
  #recaptcha-audio-button / #audio-source / a.rc-audiochallenge-tdownload-link / #audio-response
"""
import time
from camoufox.sync_api import Camoufox
import recaptcha_solver as rs

SITEKEY = "6LfD3PIbAAAAAJs_eEHvoOl75_83eXSqpPSRFJ_u"
URL = "https://2captcha.com/demo/recaptcha-v2"
HTML = rs.HTML_TEMPLATE.replace("{{SITEKEY}}", SITEKEY)

print("启动浏览器并打开组件...", flush=True)
with Camoufox(headless=True, i_know_what_im_doing=True,
              config={'forceScopeAccess': True}, disable_coop=True) as browser:
    page = browser.new_page()
    page.route(URL, lambda r: r.fulfill(status=200, content_type="text/html", body=HTML))
    page.route(URL + "/", lambda r: r.fulfill(status=200, content_type="text/html", body=HTML))
    page.goto(URL, wait_until="networkidle")
    time.sleep(2)

    page.wait_for_selector("iframe[title='reCAPTCHA']", timeout=30000)
    main_frame = page.frame_locator("iframe[title='reCAPTCHA']")
    anchor = main_frame.locator("[class^='rc-anchor-center-item']").first
    clicked = False
    for attempt in range(1, 7):
        try:
            print(f"  尝试点击复选框 #{attempt} ...", flush=True)
            anchor.click(timeout=12000)
            print(f"✅ 已点击复选框（第 {attempt} 次尝试）", flush=True)
            clicked = True
            break
        except Exception as e:
            print(f"  失败: {type(e).__name__}", flush=True)
            time.sleep(3)
    if not clicked:
        try:
            print("  改用 force 点击...", flush=True)
            anchor.click(timeout=8000, force=True)
            clicked = True
            print("✅ force 点击成功", flush=True)
        except Exception as e:
            print(f"❌ force 点击也失败: {type(e).__name__}", flush=True)
            try:
                print(f"  anchor 文字: {main_frame.locator('body').inner_text(timeout=2000)!r}", flush=True)
            except Exception:
                pass

    time.sleep(3)

    bf_sel = "iframe[src*='recaptcha/api2/bframe'], iframe[src*='recaptcha/enterprise/bframe']"
    bf_el = page.query_selector(bf_sel)
    print(f"\nbframe 存在: {bf_el is not None}", flush=True)
    if not bf_el:
        print("❌ 没有弹出层，音频模式无从谈起", flush=True)
        raise SystemExit(0)

    cf = page.frame_locator(bf_sel)
    bframe = bf_el.content_frame()

    print(f"bframe 文字: {(bframe.evaluate('document.body.innerText') or '')[:200]!r}", flush=True)
    print(f"当前是图片挑战: {bframe.query_selector('.rc-imageselect-challenge') is not None}", flush=True)
    print(f"当前是音频挑战: {bframe.query_selector('.rc-audiochallenge-response-field') is not None}", flush=True)

    # 找切换音频的按钮
    print("\n--- 音频按钮 ---", flush=True)
    for sel in ["#recaptcha-audio-button",
                "button#recaptcha-audio-button",
                "[aria-labelledby*='audio']",
                ".rc-button-audio"]:
        try:
            n = cf.locator(sel).count()
            print(f"  {sel:42} count={n}", flush=True)
        except Exception as e:
            print(f"  {sel:42} 查询异常 {type(e).__name__}", flush=True)

    # 点击切换
    print("\n--- 切换到音频挑战 ---", flush=True)
    clicked = False
    for sel in ["#recaptcha-audio-button", ".rc-button-audio"]:
        try:
            loc = cf.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=8000)
                print(f"  ✅ 点击了 {sel}", flush=True)
                clicked = True
                break
        except Exception as e:
            print(f"  {sel} 点击失败: {type(e).__name__}", flush=True)

    if clicked:
        time.sleep(3)
        bframe = page.query_selector(bf_sel).content_frame()
        print(f"\nbframe 文字: {(bframe.evaluate('document.body.innerText') or '')[:300]!r}", flush=True)
        print(f"  音频响应框存在: {bframe.query_selector('#audio-response') is not None}", flush=True)
        print(f"  音频元素存在  : {bframe.query_selector('#audio-source') is not None}", flush=True)

        # 各种取音频 URL 的途径
        print("\n--- 音频 URL 来源 ---", flush=True)
        try:
            src = bframe.evaluate("() => { const a = document.querySelector('#audio-source'); return a ? a.src : null; }")
            print(f"  #audio-source src : {str(src)[:130]}", flush=True)
        except Exception as e:
            print(f"  #audio-source 读取失败 {type(e).__name__}", flush=True)
        for sel in ["a.rc-audiochallenge-tdownload-link", "a[href*='payload/audio']", "a[href*='payload']"]:
            try:
                n = bframe.locator(sel).count() if hasattr(bframe, "locator") else -1
            except Exception:
                n = -1
            try:
                el = bframe.query_selector(sel)
                href = el.get_attribute("href") if el else None
            except Exception:
                href = None
            print(f"  {sel:38} -> {str(href)[:110]}", flush=True)

        print("\n--- 音频元素/播放按钮 ---", flush=True)
        for sel in [".rc-audiochallenge-play-button", "button[aria-label*='Play']", "#audio-source"]:
            try:
                print(f"  {sel:38} exists={bframe.query_selector(sel) is not None}", flush=True)
            except Exception as e:
                print(f"  {sel:38} 异常 {type(e).__name__}", flush=True)
