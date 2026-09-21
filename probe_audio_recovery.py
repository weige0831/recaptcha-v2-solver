"""探测音频挑战的限流恢复时间

不做完整求解，只判断：切到音频后组件是否下发音频（还是直接给 try again later）
"""
import sys
import time

from camoufox.sync_api import Camoufox
import recaptcha_solver as rs

SITEKEY = "6LfD3PIbAAAAAJs_eEHvoOl75_83eXSqpPSRFJ_u"
URL = "https://2captcha.com/demo/recaptcha-v2"
HTML = rs.HTML_TEMPLATE.replace("{{SITEKEY}}", SITEKEY)
BF = "iframe[src*='recaptcha/api2/bframe'], iframe[src*='recaptcha/enterprise/bframe']"

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 4
GAP = int(sys.argv[2]) if len(sys.argv) > 2 else 75

print(f"音频限流恢复探测：{ROUNDS} 轮，间隔 {GAP}s\n", flush=True)

with Camoufox(headless=True, i_know_what_im_doing=True,
              config={'forceScopeAccess': True}, disable_coop=True) as browser:
    for r in range(1, ROUNDS + 1):
        page = browser.new_page()
        t0 = time.time()
        try:
            page.route(URL, lambda x: x.fulfill(status=200, content_type="text/html", body=HTML))
            page.route(URL + "/", lambda x: x.fulfill(status=200, content_type="text/html", body=HTML))
            page.goto(URL, wait_until="networkidle")
            time.sleep(2)
            page.wait_for_selector("iframe[title='reCAPTCHA']", timeout=30000)

            mf = page.frame_locator("iframe[title='reCAPTCHA']")
            anchor = mf.locator("[class^='rc-anchor-center-item']").first
            for _ in range(6):
                try:
                    anchor.click(timeout=12000)
                    break
                except Exception:
                    time.sleep(3)
            time.sleep(2.5)

            cf = page.frame_locator(BF)
            try:
                cf.locator("#recaptcha-audio-button").first.click(timeout=8000)
            except Exception:
                pass
            time.sleep(3)

            bf_el = page.query_selector(BF)
            bframe = bf_el.content_frame() if bf_el else None
            if not bframe:
                print(f"[{r}] ❓ 无 bframe", flush=True)
            else:
                txt = (bframe.evaluate("document.body ? document.body.innerText : ''") or "")
                has_audio = bframe.query_selector("#audio-response") is not None
                has_src = bframe.query_selector("#audio-source[src]") is not None
                low = txt.lower()
                if "try again later" in low:
                    state = "⛔ 被限流 (try again later)"
                elif has_audio and has_src:
                    state = "✅ 音频挑战可用"
                elif has_audio:
                    state = "⚠️ 音频挑战已出但音频未就绪"
                else:
                    state = f"❓ 其他: {txt[:60]!r}"
                print(f"[{r}] {state}   ({time.time()-t0:.0f}s)", flush=True)
        except Exception as e:
            print(f"[{r}] ❌ 异常 {type(e).__name__}: {str(e)[:70]}", flush=True)
        finally:
            page.close()
        if r < ROUNDS:
            time.sleep(GAP)
