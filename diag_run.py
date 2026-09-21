"""诊断运行：完整日志落盘，超时放宽，定位失败原因"""
import sys
import time
import traceback

from recaptcha_solver import RecaptchaSolver

SITEKEY = "6LfD3PIbAAAAAJs_eEHvoOl75_83eXSqpPSRFJ_u"
URL = "https://2captcha.com/demo/recaptcha-v2"
TIMEOUT = int(sys.argv[1]) if len(sys.argv) > 1 else 420

print(f"诊断运行：sitekey={SITEKEY}  timeout={TIMEOUT}s", flush=True)
t0 = time.time()
try:
    with RecaptchaSolver(headless=True) as solver:
        token = solver.solve(SITEKEY, URL, TIMEOUT)
    print(f"\n✅ 成功  总耗时 {time.time()-t0:.0f}s  token 长度 {len(token)}", flush=True)
except Exception as e:
    print(f"\n❌ 失败  总耗时 {time.time()-t0:.0f}s  {type(e).__name__}: {e}", flush=True)
    traceback.print_exc()
