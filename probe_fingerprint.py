"""探测浏览器指纹与实际出口 IP 是否自洽

Google 对"可疑会话"会拒绝下发音频挑战（try again later）。
指纹不自洽（时区/语言/地理与实际出口 IP 不符）是常见诱因。
"""
import sys
import time

from camoufox.sync_api import Camoufox
import recaptcha_solver as rs

HEADLESS = "--headed" not in sys.argv

kwargs = dict(
    headless=HEADLESS,
    humanize=False,
    i_know_what_im_doing=True,
    config={'forceScopeAccess': True},
    disable_coop=True,
    firefox_user_prefs={'widget.windows.window_occlusion_tracking.enabled': False},
)
if rs.PROXY:
    kwargs['proxy'] = {"server": rs.PROXY}
    kwargs['geoip'] = True

print(f"headless={HEADLESS}  proxy={rs.PROXY or '直连'}  geoip={'geoip' in kwargs}\n", flush=True)

with Camoufox(**kwargs) as browser:
    page = browser.new_page()

    print("=== 浏览器侧指纹 ===", flush=True)
    fp = page.evaluate("""() => ({
        tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
        tzOffsetMin: new Date().getTimezoneOffset(),
        locale: navigator.language,
        langs: navigator.languages,
        platform: navigator.platform,
        hardwareConcurrency: navigator.hardwareConcurrency,
        deviceMemory: navigator.deviceMemory,
        webdriver: navigator.webdriver,
    })""")
    for k, v in fp.items():
        print(f"  {k:22}: {v}", flush=True)

    print("\n=== 出口 IP 与地理 ===", flush=True)
    try:
        page.goto("https://ipinfo.io/json", wait_until="domcontentloaded", timeout=25000)
        time.sleep(1)
        txt = page.evaluate("document.body ? document.body.innerText : ''") or ""
        print("  " + txt.replace("\n", " ")[:400], flush=True)
    except Exception as e:
        print(f"  ❌ ipinfo 失败: {type(e).__name__}", flush=True)

    print("\n=== 时间一致性 ===", flush=True)
    try:
        js_now = page.evaluate("new Date().toString()")
        print(f"  JS 时间: {js_now}", flush=True)
        print(f"  本机时间: {time.strftime('%a %b %d %Y %H:%M:%S')}", flush=True)
    except Exception as e:
        print(f"  失败: {type(e).__name__}", flush=True)

    print("\n=== Google 是否信任该会话（检查 reCAPTCHA 是否给音频）===", flush=True)
    sitekey = rs.DEMO_SITEKEY
    html = rs.HTML_TEMPLATE.replace("{{SITEKEY}}", sitekey)
    p2 = browser.new_page()
    p2.route(rs.DEMO_URL, lambda r: r.fulfill(status=200, content_type="text/html", body=html))
    p2.goto(rs.DEMO_URL, wait_until="networkidle")
    time.sleep(2)
    p2.wait_for_selector("iframe[title='reCAPTCHA']", timeout=30000)
    mf = p2.frame_locator("iframe[title='reCAPTCHA']")
    anchor = mf.locator("#recaptcha-anchor").first
    try:
        anchor.click(timeout=15000)
        print("  ✅ 复选框已点", flush=True)
    except Exception as e:
        print(f"  ⚠️ 点击失败 {type(e).__name__}", flush=True)
    time.sleep(3)
    BF = "iframe[src*='recaptcha/api2/bframe'], iframe[src*='recaptcha/enterprise/bframe']"
    bf_el = p2.query_selector(BF)
    if not bf_el:
        print("  ❓ 无 bframe", flush=True)
    else:
        cf = p2.frame_locator(BF)
        try:
            cf.locator("#recaptcha-audio-button").first.click(timeout=8000)
            print("  🎧 已切到音频", flush=True)
        except Exception as e:
            print(f"  ⚠️ 切换失败 {type(e).__name__}", flush=True)
        time.sleep(3)
        bframe = p2.query_selector(BF).content_frame()
        txt = (bframe.evaluate("document.body ? document.body.innerText : ''") or "")
        has = bframe.query_selector("#audio-response") is not None
        src = bframe.query_selector("#audio-source[src]") is not None
        print(f"  bframe 文字: {txt[:160]!r}", flush=True)
        print(f"  -> {'⛔ 被限流' if 'try again later' in txt.lower() else ('✅ 音频可用' if has and src else '⚠️ 未就绪')}", flush=True)
