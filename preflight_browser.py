"""预检：验证 Camoufox 能否启动，以及浏览器能否访问 Google reCAPTCHA"""
import os
import time
import traceback

# 与主脚本一致：留空表示浏览器直连；直连不通再设 V2_PROXY
PROXY = os.environ.get("V2_PROXY", "")

print("=" * 60)
print("Camoufox 浏览器预检")
print("=" * 60)

try:
    from camoufox.sync_api import Camoufox
except Exception:
    traceback.print_exc()
    raise SystemExit(1)


def probe(label, **kwargs):
    print()
    print("-" * 60)
    print(f"[{label}] 启动参数: {kwargs}")
    print("-" * 60)
    try:
        with Camoufox(headless=True, i_know_what_im_doing=True, **kwargs) as browser:
            page = browser.new_page()
            print("  ✅ 浏览器启动成功")
            info = page.evaluate("navigator.userAgent")
            print(f"  UA: {info[:100]}")
            page.goto("about:blank")

            # 测试 Google reCAPTCHA 可达性
            try:
                resp = page.goto("https://www.google.com/recaptcha/api.js",
                                 wait_until="domcontentloaded", timeout=25000)
                body = page.content()[:200].replace("\n", " ")
                print(f"  ✅ 访问 Google reCAPTCHA api.js -> HTTP {resp.status if resp else '?'}")
                print(f"     内容片段: {body[:120]}")
            except Exception as e:
                print(f"  ❌ 无法访问 Google: {type(e).__name__}: {str(e)[:200]}")

            # 测试目标站点
            try:
                resp = page.goto("https://2captcha.com/demo/recaptcha-v2",
                                 wait_until="domcontentloaded", timeout=25000)
                print(f"  ✅ 访问 2captcha demo -> HTTP {resp.status if resp else '?'}")
            except Exception as e:
                print(f"  ❌ 无法访问 2captcha: {type(e).__name__}: {str(e)[:200]}")
        return True
    except Exception as e:
        print(f"  ❌ 启动失败: {type(e).__name__}: {str(e)[:400]}")
        return False


# 先测不带代理（与脚本原始写法一致）
ok_direct = probe("直连（无代理）")

# 如果直连不通，再测走本地代理
if not ok_direct and PROXY:
    probe("走本地代理", proxy={"server": PROXY})

print()
print("浏览器预检结束")
