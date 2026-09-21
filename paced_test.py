"""节奏化可靠性测试：运行之间留间隔，失败时检测是否为 Google 端配额限制"""
import subprocess
import sys
import re
import time

RUNS = 3
GAP = 45          # 每轮之间等待秒数，避免触发 Google 端限流
SOLVE_TIMEOUT = 420

SITEKEY = "6LfD3PIbAAAAAJs_eEHvoOl75_83eXSqpPSRFJ_u"
URL = "https://2captcha.com/demo/recaptcha-v2"

PROBE_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<script src="https://www.google.com/recaptcha/api.js" async defer></script></head>
<body><div class="g-recaptcha" data-sitekey="%s"></div></body></html>""" % SITEKEY


def probe_quota():
    """失败后立刻检查组件是否报配额超限"""
    try:
        from camoufox.sync_api import Camoufox
        with Camoufox(headless=True, i_know_what_im_doing=True) as b:
            p = b.new_page()
            p.route(URL, lambda r: r.fulfill(status=200, content_type="text/html", body=PROBE_HTML))
            p.goto(URL, wait_until="networkidle", timeout=30000)
            time.sleep(2)
            try:
                p.frame_locator("iframe[title='reCAPTCHA']").locator(
                    "[class^='rc-anchor-center-item']").first.click(timeout=5000)
            except Exception:
                pass
            time.sleep(6)
            bf = p.query_selector("iframe[src*='recaptcha/api2/bframe'], iframe[src*='recaptcha/enterprise/bframe']")
            txt = ""
            if bf and bf.content_frame():
                txt = bf.content_frame().evaluate("document.body ? document.body.innerText : ''") or ""
            anchor_txt = ""
            try:
                af = p.frame_locator("iframe[title='reCAPTCHA']")
                anchor_txt = af.locator("body").inner_text(timeout=3000) or ""
            except Exception:
                pass
            p.close()
            combined = (txt + " " + anchor_txt).lower()
            if "exceeding" in combined or "quota" in combined:
                return "🚫 Google 端配额超限 (exceeding reCAPTCHA Enterprise free quota)"
            if "try again later" in combined:
                return "⏳ Google 提示稍后重试"
            return f"组件正常 (anchor: {anchor_txt.strip()[:40]!r})"
    except Exception as e:
        return f"探测失败: {type(e).__name__}"


print(f"节奏化测试: {RUNS} 轮, 轮间隔 {GAP}s, 单轮求解超时 {SOLVE_TIMEOUT}s\n")
results = []

for i in range(1, RUNS + 1):
    print("=" * 70, flush=True)
    print(f"第 {i}/{RUNS} 轮", flush=True)
    print("=" * 70, flush=True)
    t0 = time.time()
    p = subprocess.run(
        [sys.executable, "-u", "diag_run.py", str(SOLVE_TIMEOUT)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
    )
    out = p.stdout + p.stderr
    elapsed = time.time() - t0
    tok = re.search(r"🎉 Token: (\S+)", out) or re.search(r"token 长度 (\d+)", out)
    success = "✅ 成功" in out
    loops = len(re.findall(r"--- 第 (\d+) 次循环检测 ---", out))
    modes = sorted(set(re.findall(r"检测到(动态|静态)验证模式", out)))
    models = sorted(set(re.findall(r"使用 (YOLO11x|GroundingDINO) 检测", out)))

    if success:
        tl = re.search(r"token 长度 (\d+)", out)
        print(f"  ✅ 成功   耗时 {elapsed:.0f}s   循环 {loops} 轮   模式 {modes}   模型 {models}   token {tl.group(1) if tl else '?'} 字符", flush=True)
        results.append(("成功", elapsed, loops, "—"))
    else:
        print(f"  ❌ 失败   耗时 {elapsed:.0f}s   循环 {loops} 轮   模式 {modes}   模型 {models}", flush=True)
        q = probe_quota()
        print(f"     失败原因探测: {q}", flush=True)
        err = re.findall(r"❌ 失败.*", out)
        if err:
            print(f"     脚本报错: {err[-1].strip()}", flush=True)
        results.append(("失败", elapsed, loops, q))
    print(flush=True)

    if i < RUNS:
        print(f"  ⏸  等待 {GAP}s 后再进行下一轮（避免触发 Google 限流）...\n", flush=True)
        time.sleep(GAP)

print("=" * 70)
print("最终汇总")
print("=" * 70)
ok = sum(1 for r in results if r[0] == "成功")
print(f"成功率: {ok}/{RUNS}  ({ok/RUNS*100:.0f}%)\n")
for i, (st, el, lp, why) in enumerate(results, 1):
    print(f"  第{i}轮: {st}  耗时 {el:.0f}s  循环 {lp} 轮   {('原因: ' + why) if why != '—' else ''}")
