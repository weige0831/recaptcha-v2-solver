"""给 stability_test.py 加入"每次求解前轮换出口节点"

背景：音频接口的限流按 IP 记，同一 IP 连续解 8 次左右就开始返回
"your computer or network may be sending automated queries"。
轮换节点让每次求解都用新 IP，可绕开这个上限。

（用 Write 写补丁而不是 heredoc：heredoc 会把 \\n 吞掉，导致断言失败。）
"""
import sys

PATH = "stability_test.py"
with open(PATH, encoding="utf-8") as f:
    s = f.read()

applied = []


def sub(old, new, label, count=1):
    global s
    n = s.count(old)
    if n != count:
        print(f"  x [{label}] 命中 {n} 次（期望 {count}）")
        return False
    s = s.replace(old, new, count)
    applied.append(label)
    print(f"  ok [{label}]")
    return True


ok = True

ok &= sub(
    "import subprocess\nimport sys\nimport time",
    "import subprocess\nimport sys\nimport time\n\nimport clash_api",
    "import clash_api")

ok &= sub(
    "def run_once(mode, idx, solve_timeout, tag, outdir, headed=False, url=None, sitekey=None):",
    '''def rotate_node(nodes, idx):
    """每次求解前换一个出口节点

    音频接口的限流是按 IP 记的：同一 IP 连续解 8 次左右就开始返回
    "your computer or network may be sending automated queries"。
    轮换节点让每次求解都换新 IP，可绕开这个上限。
    """
    if not nodes:
        return None
    node = nodes[(idx - 1) % len(nodes)]
    try:
        from urllib.parse import quote as _q
        st, _ = clash_api.api("PUT", "/proxies/" + _q(clash_api.GROUP, safe=""),
                              {"name": node})
        print(f"  [节点] 已切到 {node}  ({st})", flush=True)
    except Exception as e:
        print(f"  [节点] 切换失败 {type(e).__name__}: {e}", flush=True)
    return node


def run_once(mode, idx, solve_timeout, tag, outdir, headed=False, url=None, sitekey=None):''',
    "rotate_node 函数")

LOOP_OLD = '    results = []\n    t_all = time.time()\n    for i in range(1, args.runs + 1):\n'
LOOP_NEW = ('    nodes = [n.strip() for n in args.rotate_nodes.split("|") if n.strip()] '
            'if args.rotate_nodes else []\n'
            '    if nodes:\n'
            '        print(f"启用节点轮换: {len(nodes)} 个节点，每次求解前切换")\n\n'
            '    results = []\n'
            '    t_all = time.time()\n'
            '    for i in range(1, args.runs + 1):\n'
            '        if nodes:\n'
            '            rotate_node(nodes, i)\n'
            '            time.sleep(args.node_wait)\n')
ok &= sub(LOOP_OLD, LOOP_NEW, "主循环加入轮换")

ok &= sub(
    '    ap.add_argument("--sitekey", default=DEMO_SITEKEY)',
    '    ap.add_argument("--sitekey", default=DEMO_SITEKEY)\n'
    '    ap.add_argument("--rotate-nodes", default="",\n'
    '                    help="每次求解前轮换出口节点，用 | 分隔")\n'
    '    ap.add_argument("--node-wait", type=float, default=4.0, help="切换节点后等待秒数")',
    "命令行参数")

if not ok:
    print("\n有未命中，未写入")
    sys.exit(1)

with open(PATH, "w", encoding="utf-8") as f:
    f.write(s)
print(f"\n已应用 {len(applied)} 处修改")
