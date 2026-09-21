r"""Clash (mihomo) 控制接口客户端 —— 走 Windows 命名管道

Clash Verge Rev 默认把 external-controller 暴露在命名管道
\\.\pipe\verge-mihomo 上（external-controller 为空），Python 可直接当文件读写。

用法:
    python clash_api.py groups                # 列出代理组与当前选中
    python clash_api.py nodes "<组名>"         # 列出某组的可选节点
    python clash_api.py select "<组名>" "<节点>" # 切换节点
    python clash_api.py ip                    # 查看当前出口 IP
"""
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import quote

PIPE = os.environ.get("CLASH_PIPE", r"\\.\pipe\verge-mihomo")
SECRET = os.environ.get("CLASH_SECRET", "set-your-secret")
# 主节点选择组（Clash Verge 默认配置里的名字）
GROUP = os.environ.get("CLASH_GROUP", "🔰 选择节点")


def _read_all(f, timeout=15):
    """从管道读到 EOF"""
    chunks = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            b = f.read(65536)
        except Exception:
            break
        if not b:
            break
        chunks.append(b)
    return b"".join(chunks)


def _dechunk(body: bytes) -> bytes:
    """处理 Transfer-Encoding: chunked"""
    out = bytearray()
    i = 0
    while i < len(body):
        j = body.find(b"\r\n", i)
        if j < 0:
            break
        try:
            size = int(body[i:j].split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        out += body[j + 2: j + 2 + size]
        i = j + 2 + size + 2
    return bytes(out)


def api(method, path, body=None, timeout=15):
    """通过命名管道发一个 HTTP 请求，返回 (状态行, 解析后的 JSON 或原始文本)"""
    payload = b""
    req = (f"{method} {path} HTTP/1.1\r\n"
           f"Host: localhost\r\n"
           f"Authorization: Bearer {SECRET}\r\n"
           f"Connection: close\r\n")
    if body is not None:
        payload = json.dumps(body).encode()
        req += f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
    req += "\r\n"
    raw = req.encode() + payload

    with open(PIPE, "r+b", buffering=0) as f:
        f.write(raw)
        data = _read_all(f, timeout)

    if not data:
        raise RuntimeError("命名管道无响应（Clash 未运行？）")
    head, _, body_bytes = data.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].decode("utf-8", "replace")
    if b"chunked" in head.lower():
        body_bytes = _dechunk(body_bytes)
    text = body_bytes.decode("utf-8", "replace").strip()
    try:
        return status, json.loads(text or "{}")
    except Exception:
        return status, text


def cmd_groups():
    st, data = api("GET", "/proxies")
    print(f"  {st}")
    for name, p in (data.get("proxies") or {}).items():
        if p.get("type") in ("Selector", "URLTest", "Fallback", "LoadBalance"):
            print(f"  组: {name}   [{p.get('type')}]   当前 = {p.get('now')}")
            print(f"      可选 {len(p.get('all') or [])} 个")


def cmd_nodes(group):
    st, data = api("GET", "/proxies")
    p = (data.get("proxies") or {}).get(group)
    if not p:
        print(f"  找不到组: {group}")
        return
    print(f"  组 {group} 当前 = {p.get('now')}")
    for i, n in enumerate(p.get("all") or [], 1):
        mark = "  <- 当前" if n == p.get("now") else ""
        print(f"   {i:>3}. {n}{mark}")


def cmd_select(group, node):
    # 组名含 emoji / 空格，必须百分号编码，否则请求行非法 -> 400 Bad Request
    from urllib.parse import quote
    st, data = api("PUT", f"/proxies/{quote(group, safe='')}", {"name": node})
    print(f"  切换 {group} -> {node}: {st} {data if isinstance(data, str) else ''}")


def cmd_ip():
    """用系统 HTTP 栈查出口 IP（走注册表代理）"""
    import urllib.request
    for url in ("http://ip-api.com/json/?fields=query,country,city,timezone,isp",
                "https://api.ipify.org?format=json"):
        try:
            r = urllib.request.urlopen(url, timeout=20).read().decode("utf-8", "replace")
            print(f"  {r[:200]}")
            return
        except Exception as e:
            print(f"  {url} 失败: {type(e).__name__}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    c = sys.argv[1]
    try:
        if c == "groups":
            cmd_groups()
        elif c == "nodes":
            cmd_nodes(sys.argv[2])
        elif c == "select":
            cmd_select(sys.argv[2], sys.argv[3])
        elif c == "ip":
            cmd_ip()
        else:
            print(__doc__)
    except Exception as e:
        print(f"  失败: {type(e).__name__}: {e}")
        sys.exit(1)
