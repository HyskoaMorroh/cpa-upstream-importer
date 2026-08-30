#!/usr/bin/env python3
"""探测 mihomo 各节点能否通过按 IP 拦截的上游站点，并把结果写回 AUTO 组的 filter。

解决的问题
----------
2026-08-27 实测：relay-g.example 对 VPS 出口 IP 下发 Cloudflare 人机验证（403 +
"安全验证"），换成机场节点后变成 "访问已被拦截" —— 不同出口 IP 的风险评分不同，
只能逐个试。手工试一个节点要切面板、发请求、看响应，几十个节点不现实。

工作方式
--------
mihomo 的 select 组可以被 REST API 在运行时切换选中项，容器不必重启：
    PUT /proxies/PROXY  {"name": "<节点名>"}
本脚本对每个候选节点做一次切换 + 一次真实 HTTPS 请求，按响应判定：
    PASS  HTTP 200/401/403(JSON)  —— 过了拦截层（401/403 只是缺 key 或余额）
    BLOCK 响应含 Cloudflare 挑战特征词，或是 HTML 拦截页
    FAIL  连接失败、超时
探测完可用 --apply 把可用节点写进 mihomo/config.yaml 的 AUTO 组 filter，
让 AUTO 只在这批节点里按延迟选择，重启后依然生效。

多订阅
------
mihomo 会把 PROXY 组 use: 列出的所有 proxy-providers 的节点合并成一个候选池，
本脚本读的就是这个合并后的池子。因此：
  - 只配了 1 个订阅 -> 只在这 1 个订阅的节点里探测，无需任何额外参数
  - 配了 N 个订阅   -> 自动跨订阅一起探测，同样无需额外参数
加订阅的方法见 mihomo/config.yaml 的 proxy-providers 段注释。

多目标
------
--targets 可以一次探测多个上游，只有对全部目标都 PASS 的节点才算可用。
这适合"想让同一个代理同时服务 relay-g 与另一个被封站点"的场景。

用法（在 /opt/deploy 下执行）
-----------------------------
    python3 probe-upstreams.py                          # 探测 relay-g，全部节点
    python3 probe-upstreams.py --exclude 5倍率           # 跳过高倍率节点，省流量
    python3 probe-upstreams.py --filter 美国             # 只试美国节点
    python3 probe-upstreams.py --targets https://relay-g.example/v1/models,https://relay-b.example/v1/models
    python3 probe-upstreams.py --exclude 5倍率 --apply   # 探测完直接写回 filter
    python3 probe-upstreams.py --show                   # 只看当前节点池与选中项
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONTROLLER = "http://127.0.0.1:9090"
PROXY_ADDR = "http://127.0.0.1:7890"
GROUP = "PROXY"
DEFAULT_TARGETS = ["https://relay-g.example/v1/models"]
CONFIG_PATH = Path("mihomo/config.yaml")
FILTER_ANCHOR = "# PROBE_FILTER_ANCHOR"

# 命中即判定为被拦截。前三个来自 CPA 源码 conductor_cooldown.go:1538 的
# isCloudflareChallengeErrorMessage，与 CPA 自身的判定保持一致；
# 后两个是 2026-08-27 实测 relay-g 两种拦截页的标题。
BLOCK_MARKERS = [
    "challenge-platform",
    "cf-mitigated",
    "cdn-cgi",
    "访问已被拦截",
    "安全验证",
]


def api_get(path: str) -> dict:
    with urllib.request.urlopen(f"{CONTROLLER}{path}", timeout=10) as resp:
        return json.load(resp)


def select_node(name: str) -> None:
    url = f"{CONTROLLER}/proxies/{urllib.parse.quote(GROUP)}"
    body = json.dumps({"name": name}).encode()
    req = urllib.request.Request(
        url, data=body, method="PUT", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10):
        pass


def probe_once(target: str) -> tuple[str, str]:
    """经代理请求一个目标，返回 (判定, 细节)。

    用 curl 而不是 urllib：需要 -x 走 HTTP 代理、拿到状态码、同时读响应体前段
    以匹配拦截特征，curl 一条命令就够，也避免 urllib 对代理 CONNECT 的差异。
    """
    cmd = [
        "curl", "-sS", "--max-time", "20",
        "-x", PROXY_ADDR,
        "-w", "\n__STATUS__%{http_code}",
        target,
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=30
        ).stdout
    except subprocess.TimeoutExpired:
        return "FAIL", "超时"

    m = re.search(r"__STATUS__(\d+)", out)
    status = m.group(1) if m else "000"
    body = out[: m.start()] if m else out

    lower = body.lower()
    for marker in BLOCK_MARKERS:
        if marker.lower() in lower:
            return "BLOCK", f"HTTP {status} 命中 {marker}"

    if status == "000":
        return "FAIL", "连接失败"
    # 200/401/403 都算过了拦截层：401 是缺 API key，403 可能是余额或配额，
    # 两者都说明请求已经到达上游应用层，而不是被 IP 层挡在门外。
    if status in ("200", "401", "403"):
        return "PASS", f"HTTP {status}"
    return "FAIL", f"HTTP {status} {body.strip()[:50]}"


def probe_node(targets: list[str]) -> tuple[str, str]:
    """对全部目标探测。只有每个目标都 PASS 才算该节点可用。"""
    details = []
    verdict = "PASS"
    for t in targets:
        v, d = probe_once(t)
        host = urllib.parse.urlparse(t).netloc
        details.append(f"{host}={d}")
        if v != "PASS":
            verdict = v if verdict == "PASS" else verdict
    return verdict, "  ".join(details)


def build_filter(nodes: list[str]) -> str:
    """把节点名列表压成一个 Go 正则。

    优先提取"编号+地区"（如 ⑩美国）作为短标识：整节点名含 ｜ × 等字符，
    直接 escape 后正则会很长，而 mihomo 的 filter 是对节点名做部分匹配。
    提不出编号的退回整名 escape。
    """
    keys = []
    for n in nodes:
        m = re.search(r"[①-⑳]\s*[一-龥]+", n)
        keys.append(m.group(0).replace(" ", "") if m else re.escape(n))
    # 去重且保持顺序
    seen, uniq = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return f'(?i)({"|".join(uniq)})'


def apply_filter(config: Path, expr: str) -> None:
    """把 filter 写进 AUTO 组。靠 FILTER_ANCHOR 注释行定位，幂等。"""
    if not config.is_file():
        raise FileNotFoundError(config)
    lines = config.read_text(encoding="utf-8").split("\n")

    anchor = next((i for i, l in enumerate(lines) if FILTER_ANCHOR in l), None)
    if anchor is None:
        raise KeyError(
            f"在 {config} 里找不到锚点 {FILTER_ANCHOR}。\n"
            "该注释行标记 AUTO 组的 filter 插入位置，请勿删除；\n"
            "如已删除，请手工在 AUTO 组内加一行 filter，格式见本脚本输出。"
        )

    new_line = f'    filter: "{expr}"'
    # 锚点上一行若已是脚本写过的 filter，替换而不是追加
    if anchor > 0 and re.match(r"^    filter:", lines[anchor - 1]):
        lines[anchor - 1] = new_line
    else:
        lines.insert(anchor, new_line)

    config.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--targets", default=",".join(DEFAULT_TARGETS),
                    help="逗号分隔的探测目标 URL。多个目标时只有全部 PASS 才算可用")
    ap.add_argument("--filter", default="", help="只试节点名含该子串的（不区分大小写）")
    ap.add_argument("--exclude", default="", help="排除节点名含该子串的，如 5倍率")
    ap.add_argument("--limit", type=int, default=0, help="最多试多少个，0 为不限")
    ap.add_argument("--apply", action="store_true",
                    help=f"探测完把可用节点写进 {CONFIG_PATH} 的 AUTO 组 filter")
    ap.add_argument("--config", default=str(CONFIG_PATH), help="mihomo 配置路径")
    ap.add_argument("--show", action="store_true", help="只打印节点池与当前选中项，不探测")
    args = ap.parse_args()

    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    if not targets:
        print("错误: --targets 为空", file=sys.stderr)
        sys.exit(1)

    try:
        group = api_get(f"/proxies/{urllib.parse.quote(GROUP)}")
    except Exception as exc:
        print(f"错误: 连不上 mihomo API {CONTROLLER} ({exc})", file=sys.stderr)
        print("排查: docker compose ps mihomo && docker logs --tail=20 mihomo", file=sys.stderr)
        sys.exit(1)

    original = group.get("now") or ""
    # 组内候选里 AUTO 是策略组不是节点，探测时跳过
    all_nodes = [n for n in group.get("all", []) if n != "AUTO"]

    if args.show:
        print(f"节点池共 {len(all_nodes)} 个，当前选中: {original or '(无)'}")
        for i, n in enumerate(all_nodes, 1):
            print(f"  {i:>3}. {n}")
        return

    nodes = all_nodes
    if args.filter:
        nodes = [n for n in nodes if args.filter.lower() in n.lower()]
    if args.exclude:
        nodes = [n for n in nodes if args.exclude.lower() not in n.lower()]
    if args.limit:
        nodes = nodes[: args.limit]

    if not nodes:
        print(f"节点池 {len(all_nodes)} 个，但没有符合 --filter/--exclude 条件的")
        return

    print(f"目标 {len(targets)} 个: {', '.join(targets)}")
    print(f"待试 {len(nodes)} / {len(all_nodes)} 个节点，当前选中: {original or '(无)'}\n")

    passed: list[str] = []
    interrupted = False
    try:
        for i, node in enumerate(nodes, 1):
            try:
                select_node(node)
            except Exception as exc:
                print(f"[{i:>3}/{len(nodes)}] SKIP  {node}  (切换失败: {exc})")
                continue
            # 给 mihomo 时间让新选择生效，否则首个请求可能仍走旧节点
            time.sleep(0.5)
            verdict, detail = probe_node(targets)
            if verdict == "PASS":
                passed.append(node)
            print(f"[{i:>3}/{len(nodes)}] {verdict:5} {node}\n            {detail}")
    except KeyboardInterrupt:
        interrupted = True
        print("\n已中断，下面是已完成部分的结果")
    finally:
        # 不论中断还是跑完都还原，避免留下一个被拦的节点当出口
        if original:
            try:
                select_node(original)
                print(f"\n已还原选中项: {original}")
            except Exception as exc:
                print(f"\n警告: 还原失败 ({exc})，请手动切回", file=sys.stderr)

    print(f"\n=== 可用节点 {len(passed)} / {len(nodes)} ===")
    if not passed:
        print("没有节点能过。目标站的 IP 拉黑范围覆盖了整个候选池。")
        print("可选做法：")
        print("  1. 换一个订阅（改 mihomo/config.yaml 的 proxy-providers.wog.url）")
        print("  2. 加第二个订阅并重跑本脚本（方法见该文件注释）")
        print("  3. 放弃走代理：把 CPA config.yaml 里对应的 proxy-url 改回空值")
        return

    for n in passed:
        print(f"  {n}")

    expr = build_filter(passed)
    if args.apply:
        if interrupted:
            print("\n已中断，未写入 filter（结果不完整）。确认后重跑并加 --apply。")
            return
        try:
            apply_filter(Path(args.config), expr)
        except (FileNotFoundError, KeyError) as exc:
            print(f"\n写入失败: {exc}", file=sys.stderr)
            print(f"可手工把下面这行加进 AUTO 组:\n    filter: \"{expr}\"", file=sys.stderr)
            sys.exit(1)
        print(f"\n已写入 {args.config} 的 AUTO 组:")
        print(f'    filter: "{expr}"')
        print("\n生效: docker compose restart mihomo")
        print("复验: curl -s http://127.0.0.1:9090/proxies/AUTO | python3 -c \\")
        print("        \"import json,sys;d=json.load(sys.stdin);print('候选',len(d['all']),'选中',d['now'])\"")
    else:
        print("\n把下面这行加到 mihomo/config.yaml 的 AUTO 组，")
        print("让 AUTO 只在这批能过的节点里按延迟选择：")
        print(f'    filter: "{expr}"')
        print("\n或者重跑本脚本时加 --apply 自动写入。")


if __name__ == "__main__":
    main()
