"""诊断：音频挑战的 DOM 结构与取音频各步骤"""
import time
from camoufox.sync_api import Camoufox
import recaptcha_solver as rs

SITEKEY = "6LfD3PIbAAAAAJs_eEHvoOl75_83eXSqpPSRFJ_u"
URL = "https://2captcha.com/demo/recaptcha-v2"
HTML = rs.HTML_TEMPLATE.replace("{{SITEKEY}}", SITEKEY)
BF = "iframe[src*='recaptcha/api2/bframe'], iframe[src*='recaptcha/enterprise/bframe']"

with Camoufox(headless=True, i_know_what_im_doing=True,
              config={'forceScopeAccess': True}, disable_coop=True) as browser:
    page = browser.new_page()
    page.route(URL, lambda r: r.fulfill(status=200, content_type="text/html", body=HTML))
    page.route(URL + "/", lambda r: r.fulfill(status=200, content_type="text/html", body=HTML))
    page.goto(URL, wait_until="networkidle")
    time.sleep(2)
    page.wait_for_selector("iframe[title='reCAPTCHA']", timeout=30000)

    mf = page.frame_locator("iframe[title='reCAPTCHA']")
    anchor = mf.locator("[class^='rc-anchor-center-item']").first
    for i in range(6):
        try:
            anchor.click(timeout=12000)
            print(f"✅ 复选框已点（第{i+1}次）", flush=True)
            break
        except Exception:
            time.sleep(3)

    time.sleep(3)
    cf = page.frame_locator(BF)
    try:
        cf.locator("#recaptcha-audio-button").first.click(timeout=8000)
        print("✅ 已切到音频挑战", flush=True)
    except Exception as e:
        print(f"⚠️ 切换失败 {type(e).__name__}", flush=True)
    time.sleep(3)

    bframe = page.query_selector(BF).content_frame()
    print(f"\nbframe 文字: {(bframe.evaluate('document.body.innerText') or '')[:200]!r}", flush=True)

    # 音频相关元素全量 dump
    print("\n--- 音频区域 HTML ---", flush=True)
    html = bframe.evaluate("""() => {
        const box = document.querySelector('.rc-audiochallenge-control') ||
                    document.querySelector('.rc-audiochallenge-response-field') ||
                    document.querySelector('audio');
        return box ? box.outerHTML.slice(0, 1500) : '(找不到音频区域)';
    }""")
    print(html, flush=True)

    print("\n--- audio 元素属性 ---", flush=True)
    info = bframe.evaluate("""() => {
        const a = document.querySelector('#audio-source') || document.querySelector('audio');
        if (!a) return {found: false};
        return {
            found: true,
            id: a.id,
            hasSrcAttr: a.hasAttribute('src'),
            src: a.getAttribute('src'),
            currentSrc: a.currentSrc,
            readyState: a.readyState,
            networkState: a.networkState,
            duration: a.duration,
            srcObject: a.srcObject ? 'yes' : null,
            outerStart: a.outerHTML.slice(0, 300),
        };
    }""")
    for k, v in (info or {}).items():
        print(f"  {k:14}: {str(v)[:220]}", flush=True)

    print("\n--- 所有 audio/source 标签 ---", flush=True)
    tags = bframe.evaluate("""() => Array.from(document.querySelectorAll('audio, source'))
        .map(e => e.tagName + ' src=' + (e.getAttribute('src')||'null'))""")
    print(f"  {tags}", flush=True)

    print("\n--- 下载链接 ---", flush=True)
    links = bframe.evaluate("""() => Array.from(document.querySelectorAll('a'))
        .map(e => (e.className||'') + ' | ' + (e.getAttribute('href')||'null').slice(0,120))""")
    for l in links or []:
        print(f"  {l}", flush=True)

    # 直接测 fetch
    print("\n--- 在 bframe 内 fetch 测试 ---", flush=True)
    url = info.get("currentSrc") or info.get("src") if info else None
    print(f"  目标 URL: {str(url)[:120]}", flush=True)
    if url:
        try:
            res = bframe.evaluate("""async (u) => {
                try {
                    const r = await fetch(u, {credentials:'include'});
                    const b = await r.arrayBuffer();
                    return {ok: r.ok, status: r.status, len: b.byteLength,
                            ct: r.headers.get('content-type')};
                } catch (e) { return {err: String(e)}; }
            }""", url)
            print(f"  fetch 结果: {res}", flush=True)
        except Exception as e:
            print(f"  fetch 异常: {type(e).__name__}: {str(e)[:200]}", flush=True)

        try:
            r = page.request.get(url)
            print(f"  page.request.get: status={r.status} len={len(r.body())}", flush=True)
        except Exception as e:
            print(f"  page.request.get 异常: {type(e).__name__}: {str(e)[:200]}", flush=True)
    else:
        print("  ⚠️ 拿不到 URL，无法测试", flush=True)

    # 点 PLAY 后再看
    print("\n--- 点击 PLAY 之后 ---", flush=True)
    try:
        cf.locator(".rc-audiochallenge-play-button").first.click(timeout=8000)
        print("  ✅ 已点 PLAY", flush=True)
    except Exception as e:
        print(f"  PLAY 点击失败: {type(e).__name__}", flush=True)
    time.sleep(3)
    bframe = page.query_selector(BF).content_frame()
    info2 = bframe.evaluate("""() => {
        const a = document.querySelector('#audio-source') || document.querySelector('audio');
        return a ? {hasSrcAttr: a.hasAttribute('src'), src: a.getAttribute('src'),
                    currentSrc: a.currentSrc, readyState: a.readyState,
                    duration: a.duration} : null;
    }""")
    print(f"  PLAY 后: {info2}", flush=True)
    if info2 and (info2.get("currentSrc") or info2.get("src")):
        u2 = info2.get("currentSrc") or info2.get("src")
        try:
            res = bframe.evaluate("""async (u) => {
                try { const r = await fetch(u, {credentials:'include'});
                      const b = await r.arrayBuffer();
                      return {ok:r.ok, status:r.status, len:b.byteLength}; }
                catch(e){ return {err:String(e)}; }
            }""", u2)
            print(f"  PLAY 后 fetch: {res}", flush=True)
        except Exception as e:
            print(f"  PLAY 后 fetch 异常: {type(e).__name__}", flush=True)
