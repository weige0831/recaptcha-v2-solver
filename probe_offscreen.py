"""探测 bframe 被摆到屏幕外（y≈-9875）时的真实 DOM 状态

现象：bounding_box() 返回 y=-9875 且持续存在，scrollTo(0,0) 无效，
导致截图空白、按钮点不动，空转到轮数上限。
"""
import time

from camoufox.sync_api import Camoufox
import recaptcha_solver as rs

SITEKEY = "6LfD3PIbAAAAAJs_eEHvoOl75_83eXSqpPSRFJ_u"
URL = "https://2captcha.com/demo/recaptcha-v2"
HTML = rs.HTML_TEMPLATE.replace("{{SITEKEY}}", SITEKEY)
BF = "iframe[src*='recaptcha/api2/bframe'], iframe[src*='recaptcha/enterprise/bframe']"


def dump(page, label):
    print(f"\n--- {label} ---", flush=True)
    info = page.evaluate("""() => {
        const bf = document.querySelector("iframe[src*='bframe']");
        const out = {
            scrollY: window.scrollY, innerH: window.innerHeight, innerW: window.innerWidth,
            docH: document.documentElement.scrollHeight,
            bodyOverflow: getComputedStyle(document.body).overflow,
        };
        if (bf) {
            const r = bf.getBoundingClientRect();
            const cs = getComputedStyle(bf);
            out.bf = {rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
                      top: cs.top, left: cs.left, transform: cs.transform,
                      visibility: cs.visibility, display: cs.display, opacity: cs.opacity,
                      inlineStyle: (bf.getAttribute('style')||'').slice(0,200)};
        } else { out.bf = null; }
        return out;
    }""")
    for k, v in info.items():
        print(f"   {k:14}: {v}", flush=True)
    try:
        box = page.frame_locator("iframe[title='reCAPTCHA']").locator("#recaptcha-anchor").first.bounding_box()
        print(f"   anchor box    : {box}", flush=True)
    except Exception as e:
        print(f"   anchor box 失败: {type(e).__name__}", flush=True)
    try:
        checked = page.frame_locator("iframe[title='reCAPTCHA']").locator('[aria-checked="true"]').count()
        print(f"   aria-checked  : {checked}", flush=True)
    except Exception:
        pass
    try:
        el = page.frame_locator(BF).locator(".rc-imageselect-challenge").first
        print(f"   挑战元素 box  : {el.bounding_box()}", flush=True)
    except Exception as e:
        print(f"   挑战元素 box 失败: {type(e).__name__}", flush=True)
    try:
        bf_el = page.query_selector(BF)
        fr = bf_el.content_frame()
        print(f"   bframe 文字   : {(fr.evaluate('document.body?document.body.innerText:\'\'') or '')[:100]!r}", flush=True)
    except Exception as e:
        print(f"   bframe 文字失败: {type(e).__name__}", flush=True)


with Camoufox(headless=True, i_know_what_im_doing=True,
              firefox_user_prefs={'widget.windows.window_occlusion_tracking.enabled': False}) as browser:
    page = browser.new_page()
    page.route(URL, lambda r: r.fulfill(status=200, content_type="text/html", body=HTML))
    page.route(URL + "/", lambda r: r.fulfill(status=200, content_type="text/html", body=HTML))
    page.goto(URL, wait_until="networkidle")
    time.sleep(2)
    page.wait_for_selector("iframe[title='reCAPTCHA']", timeout=30000)

    mf = page.frame_locator("iframe[title='reCAPTCHA']")
    anchor = mf.locator("#recaptcha-anchor").first
    for _ in range(6):
        try:
            anchor.click(timeout=12000)
            break
        except Exception:
            time.sleep(2)
    time.sleep(3)
    dump(page, "点击后（挑战刚出现）")

    # 模拟求解器循环：反复测量 + 偶尔点击
    for i in range(1, 9):
        try:
            el = page.frame_locator(BF).locator(".rc-imageselect-challenge").first
            el.wait_for(state="visible", timeout=2500)
            tiles = page.frame_locator(BF).locator(".rc-image-tile-target").all()
            if tiles:
                tiles[0].click(timeout=5000)
        except Exception as e:
            print(f"   [轮{i}] 点击异常 {type(e).__name__}", flush=True)
        time.sleep(1.0)
        if i in (1, 3, 5, 8):
            dump(page, f"循环第 {i} 轮后")
