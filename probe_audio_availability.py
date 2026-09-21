"""测量音频挑战的可用率（与会话指纹相关）

每次都是全新浏览器进程 = 全新随机指纹。只判断"切到音频后 Google 是否下发"，
不做求解。用来把"Google 不给音频"和"我们没解出来"两个问题分开。

用法:
    python probe_audio_availability.py 10          # 无头，10 次
    python probe_audio_availability.py 10 --headed # 有头对比
"""
import subprocess
import sys
import time

RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 10
HEADED = "--headed" in sys.argv

SNIPPET = r'''
import sys, time
from camoufox.sync_api import Camoufox
import recaptcha_solver as rs

kwargs = dict(
    headless=%HEADLESS%,
    humanize=False,
    i_know_what_im_doing=True,
    config={'forceScopeAccess': True},
    disable_coop=True,
    firefox_user_prefs={'widget.windows.window_occlusion_tracking.enabled': False},
)
if rs.PROXY:
    kwargs['proxy'] = {"server": rs.PROXY}
    kwargs['geoip'] = True

with Camoufox(**kwargs) as browser:
    page = browser.new_page()
    html = rs.HTML_TEMPLATE.replace("{{SITEKEY}}", rs.DEMO_SITEKEY)
    page.route(rs.DEMO_URL, lambda r: r.fulfill(status=200, content_type='text/html', body=html))
    page.goto(rs.DEMO_URL, wait_until="networkidle")
    time.sleep(2)
    page.wait_for_selector("iframe[title='reCAPTCHA']", timeout=30000)
    mf = page.frame_locator("iframe[title='reCAPTCHA']")
    try:
        mf.locator("#recaptcha-anchor").first.click(timeout=15000)
    except Exception:
        pass
    time.sleep(3)
    BF = "iframe[src*='recaptcha/api2/bframe'], iframe[src*='recaptcha/enterprise/bframe']"
    bf_el = page.query_selector(BF)
    if not bf_el:
        print("STATUS=nobframe"); sys.exit(0)
    cf = page.frame_locator(BF)
    try:
        cf.locator("#recaptcha-audio-button").first.click(timeout=8000)
    except Exception:
        print("STATUS=noswitch"); sys.exit(0)
    time.sleep(3)
    bframe = page.query_selector(BF).content_frame()
    txt = (bframe.evaluate("document.body ? document.body.innerText : ''") or "").lower()
    has = bframe.query_selector("#audio-response") is not None
    src = bframe.query_selector("#audio-source[src]") is not None
    if "try again later" in txt:
        print("STATUS=blocked")
    elif has and src:
        print("STATUS=ok")
    else:
        print("STATUS=notready:" + txt[:80].replace("\n", " "))
'''.replace("%HEADLESS%", "False" if HEADED else "True")

print(f"音频可用率探测：{RUNS} 次全新会话（{'有头' if HEADED else '无头'}）\n", flush=True)
counts = {}
t_all = time.time()
for i in range(1, RUNS + 1):
    p = subprocess.run([sys.executable, "-u", "-c", SNIPPET],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    out = (p.stdout or "") + (p.stderr or "")
    line = [l for l in out.splitlines() if l.startswith("STATUS=")]
    status = line[-1][7:] if line else "error"
    counts[status] = counts.get(status, 0) + 1
    print(f"  [{i}/{RUNS}] {status}", flush=True)
    time.sleep(3)

print()
print("=" * 60)
total = sum(counts.values())
for k, v in sorted(counts.items(), key=lambda x: -x[1]):
    print(f"  {k:22} {v}/{total}  {v/total*100:.0f}%")
print(f"  耗时 {time.time()-t_all:.0f}s")
