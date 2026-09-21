"""量化音频挑战的放行率（不同指纹配置对比）

只判断"切到音频后 Google 是否下发"，不做求解。用于把
"Google 不给音频（网络被标记 automated queries）" 与 "我们没解出来" 分开。

用法:
    python probe_audio_grant.py --runs 10
    python probe_audio_grant.py --runs 10 --geoip --locale en-US
"""
import argparse
import subprocess
import sys
import time

SNIPPET = r'''
import time
from camoufox.sync_api import Camoufox
import recaptcha_solver as rs

kw = dict(headless=True, i_know_what_im_doing=True,
          firefox_user_prefs={'widget.windows.window_occlusion_tracking.enabled': False})
%EXTRA%

with Camoufox(**kw) as b:
    p = b.new_page()
    p.goto('https://api.ipify.org?format=json', wait_until='domcontentloaded', timeout=25000)
    time.sleep(1)
    fp = p.evaluate("() => Intl.DateTimeFormat().resolvedOptions().timeZone + '/' + navigator.language")
    p2 = b.new_page()
    html = rs.HTML_TEMPLATE.replace("{{SITEKEY}}", rs.DEMO_SITEKEY)
    p2.route(rs.DEMO_URL, lambda r: r.fulfill(status=200, content_type='text/html', body=html))
    p2.goto(rs.DEMO_URL, wait_until="networkidle")
    time.sleep(2)
    p2.wait_for_selector("iframe[title='reCAPTCHA']", timeout=30000)
    mf = p2.frame_locator("iframe[title='reCAPTCHA']")
    try:
        mf.locator("#recaptcha-anchor").first.click(timeout=15000)
    except Exception:
        pass
    time.sleep(3)
    BF = "iframe[src*='recaptcha/api2/bframe'], iframe[src*='recaptcha/enterprise/bframe']"
    el = p2.query_selector(BF)
    if not el:
        print("FP=" + fp); print("STATUS=nobframe"); raise SystemExit
    cf = p2.frame_locator(BF)
    try:
        cf.locator("#recaptcha-audio-button").first.click(timeout=8000)
    except Exception:
        print("FP=" + fp); print("STATUS=noswitch"); raise SystemExit
    time.sleep(6)
    bf = p2.query_selector(BF).content_frame()
    txt = (bf.evaluate("document.body ? document.body.innerText : ''") or "").lower()
    ok = bf.query_selector("#audio-response") is not None and bf.query_selector("#audio-source[src]") is not None
    if "automated queries" in txt or "try again later" in txt:
        st = "blocked(automated-queries)"
    elif ok:
        st = "granted"
    else:
        st = "notready:" + txt[:60].replace("\n", " ")
    print("FP=" + fp)
    print("STATUS=" + st)
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--geoip", action="store_true")
    ap.add_argument("--locale", default=None)
    ap.add_argument("--gap", type=float, default=3.0)
    args = ap.parse_args()

    extra = ""
    if args.geoip:
        extra += "kw['geoip'] = True\n"
    if args.locale:
        extra += f"kw['locale'] = {args.locale!r}\n"

    label = f"geoip={args.geoip} locale={args.locale or '默认'}"
    print(f"音频放行率探测：{args.runs} 次全新会话   {label}\n", flush=True)

    code = SNIPPET.replace("%EXTRA%", extra)
    counts, t_all = {}, time.time()
    for i in range(1, args.runs + 1):
        try:
            p = subprocess.run([sys.executable, "-u", "-c", code],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=180)
            out = (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            out = "STATUS=timeout"
        st = [l for l in out.splitlines() if l.startswith("STATUS=")]
        fp = [l for l in out.splitlines() if l.startswith("FP=")]
        s = st[-1][7:].split(":")[0] if st else "error"
        counts[s] = counts.get(s, 0) + 1
        print(f"  [{i}/{args.runs}] {s:24} {fp[-1][3:] if fp else ''}", flush=True)
        if i < args.runs:
            time.sleep(args.gap)

    total = sum(counts.values())
    print()
    print("=" * 64)
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {k:24} {v}/{total}  {v/total*100:.0f}%")
    print(f"  耗时 {time.time()-t_all:.0f}s")
    return 0 if counts.get("granted", 0) / max(1, total) >= 0.95 else 1


if __name__ == "__main__":
    sys.exit(main())
