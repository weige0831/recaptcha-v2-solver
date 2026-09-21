"""音频模式单次冒烟测试"""
import sys
import time
import traceback

import recaptcha_solver as rs

MODE = sys.argv[1] if len(sys.argv) > 1 else "audio"
SITEKEY = "6LfD3PIbAAAAAJs_eEHvoOl75_83eXSqpPSRFJ_u"
URL = "https://2captcha.com/demo/recaptcha-v2"

print(f"模式: {MODE}   超时: {rs.DEFAULT_SOLVE_TIMEOUT}s\n", flush=True)
t0 = time.time()
try:
    with rs.RecaptchaSolver(headless=True, min_interval=0, mode=MODE) as solver:
        token = solver.solve(SITEKEY, URL)
    print(f"\n✅ 成功  耗时 {time.time()-t0:.0f}s  token {len(token)} 字符", flush=True)
    print(f"   前缀 {token[:30]}", flush=True)
except rs.RecaptchaBlockedError as e:
    print(f"\n🚫 被限流  耗时 {time.time()-t0:.0f}s  {e}", flush=True)
except Exception as e:
    print(f"\n❌ 失败  耗时 {time.time()-t0:.0f}s  {type(e).__name__}: {e}", flush=True)
    traceback.print_exc()
