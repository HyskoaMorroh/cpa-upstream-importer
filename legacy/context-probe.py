#!/usr/bin/env python3
"""二分探测各上游实际能接受的最大上下文，用来定 Claude Code 的自动压缩阈值。

为什么需要
----------
2026-08-28 实测：Claude Code 的 settings.json 里模型带 [1M] 后缀，它就按
1,000,000 计算本地上下文窗口，到约 967K tokens 才触发自动压缩
（claude-code CHANGELOG.md:83）。但上游未必吃得下那么多：

  relay-h.example  priority 1000（最高，永远第一个被选）
    406,870 tokens -> 200
    约 70-90 万     -> 400 "请精简对话历史或缩小工具/文件输出后重试"

而 400 不在 isCredentialRetryRoundStatus 白名单
（conductor_selection.go:1038 只含 403/408/429/500/502/503/504），
命中即终止本轮，尝试=1，其余可用上游一次都不试。
于是「无论怎么发消息都是这个错误」。

本脚本给出每个上游的真实上限，据此：
  1. 定 CLAUDE_CODE_MAX_CONTEXT_TOKENS（取最低可用上限的 85% 左右）
  2. 判断 priority 1000 的站是否该让位给上限更大的站

计费提醒
--------
探测会真实消耗 tokens。单次 40 万 tokens 约 $0.057（实测 relay-h 计费字段）。
默认二分 5 轮 ≈ 每站 $0.3 上下。用 --dry-run 先看会发多少请求、估多少钱。

用法（在 /opt/deploy 下执行）
    python3 context-probe.py --dry-run                 # 只列计划，不发请求
    python3 context-probe.py                           # 全部 claude 段上游
    python3 context-probe.py --only relay-h          # 只测一个站
    python3 context-probe.py --lo 200000 --hi 1000000  # 二分区间
    python3 context-probe.py --rounds 4                # 二分轮数（默认 5）
    python3 context-probe.py --model claude-opus-4-8

只读：不修改任何配置。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("缺少 pyyaml：pip3 install pyyaml")

TIMEOUT = 240
# 实测：重复单字符约 1 char/token（40 万个 'x' -> 406,870 tokens）。
# 真实文本约 3-4 char/token，所以这里的 tokens 是保守上界。
CHARS_PER_TOKEN = 1.0
COST_PER_MTOK = 0.139  # 由实测 406,870 tokens = $0.05660 反推，仅用于估算

UA_CODEX = ("codex_vscode/0.150.0-alpha.12.2 (Windows 10.0.28000; x86_64) "
            "unknown (VS Code; 26.825.31414)")


def host_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", (url or "").strip())
    return m.group(1) if m else (url or "").strip()


def post(base: str, key: str, model: str, chars: int) -> tuple[str, dict]:
    """向 claude 段协议路径发一次指定规模的请求。返回 (status, 解析出的信息)。"""
    url = (base or "").rstrip("/") + "/v1/messages"
    body = json.dumps({
        "model": model, "max_tokens": 16,
        "messages": [{"role": "user", "content": "x" * chars}],
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
        "anthropic-version": "2023-06-01",
        "User-Agent": UA_CODEX,
        "Originator": "codex_vscode",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            text = r.read(8000).decode("utf-8", "replace")
            status = str(r.status)
    except urllib.error.HTTPError as e:
        text = e.read(8000).decode("utf-8", "replace")
        status = str(e.code)
    except Exception as e:  # noqa: BLE001
        return "000", {"err": f"{type(e).__name__}: {e}"}

    info: dict = {}
    m = re.search(r'"input_tokens"\s*:\s*(\d+)', text)
    if m:
        info["input_tokens"] = int(m.group(1))
    m = re.search(r'"model"\s*:\s*"([^"]+)"', text)
    if m:
        info["actual_model"] = m.group(1)
    m = re.search(r'"cost"\s*:\s*([0-9.]+)', text)
    if m:
        info["cost"] = float(m.group(1))
    if status != "200":
        m = re.search(r'"message"\s*:\s*"([^"]{0,160})', text)
        info["err"] = m.group(1) if m else text[:160].replace("\n", " ")
    return status, info


def collect(cfg: dict, only: list[str], model: str) -> list[dict]:
    """取 claude-api-key 段里声明了目标模型、且未被 disabled/weight<=0 排除的条目。"""
    out = []
    for idx, e in enumerate(cfg.get("claude-api-key") or []):
        if not isinstance(e, dict) or e.get("disabled") is True:
            continue
        w = e.get("weight")
        if isinstance(w, (int, float)) and w <= 0:
            continue
        base = (e.get("base-url") or "").strip()
        key = (e.get("api-key") or "").strip()
        if not base or not key:
            continue
        host = host_of(base)
        if only and not any(s.lower() in host.lower() for s in only):
            continue
        names = set()
        for m in e.get("models") or []:
            if isinstance(m, dict):
                names.add((m.get("alias") or m.get("name") or "").strip())
            elif isinstance(m, str):
                names.add(m.strip())
        if model not in names:
            continue
        if any(o["host"] == host for o in out):  # 同站只测一次
            continue
        out.append({"host": host, "base": base, "key": key,
                    "prio": e.get("priority"), "idx": idx})
    return sorted(out, key=lambda o: -(o["prio"] or 0))


def bisect_limit(t: dict, model: str, lo: int, hi: int, rounds: int,
                 gap: float) -> dict:
    """二分找最大可接受字符数。返回 {ok, fail, tokens, notes}。

    ok   = 已确认能通过的最大规模
    fail = 已确认被拒的最小规模

    可信度校验：若响应的 input_tokens 显著小于发送字符数，说明上游把请求
    截断了，200 不代表它真吃下了那么多上下文。2026-08-29 实测 relay-m.example
    发 1,050,000 字符只回 input_tokens=132,696、且 model 变成 codex-auto-review
    —— 那次「上限 >= 1050k」是假数据。此类情形标记 untrusted 并停止二分。
    """
    res = {"host": t["host"], "prio": t["prio"], "ok": 0, "fail": None,
           "ok_tokens": 0, "cost": 0.0, "actual_model": None, "notes": [],
           "untrusted": False}

    def check(chars: int) -> tuple[str, dict]:
        st, info = post(t["base"], t["key"], model, chars)
        res["cost"] += info.get("cost", 0.0)
        if info.get("actual_model"):
            res["actual_model"] = info["actual_model"]
        tok = info.get("input_tokens")
        # 重复字符实测约 1 char/token，低于 50% 即视为上游截断
        if st == "200" and tok and tok < chars * 0.5:
            res["untrusted"] = True
            res["notes"].append(
                f"{chars//1000}k 字符只回 input_tokens={tok:,}"
                f"（{tok*100//chars}%）—— 上游截断了请求，200 不可信")
        return st, info

    # 先测下界，确认基础可用
    status, info = check(lo)
    if status != "200":
        res["notes"].append(f"下界 {lo//1000}k 即失败({status}): {info.get('err','')[:90]}")
        res["fail"] = lo
        return res
    res["ok"] = lo
    res["ok_tokens"] = info.get("input_tokens", 0)
    if res["untrusted"]:
        res["notes"].append("下界即不可信，停止二分（该站上限无法测量）")
        return res

    # 再测上界，若直接通过就无需二分
    time.sleep(gap)
    status, info = check(hi)
    if res["untrusted"]:
        res["notes"].append("上界不可信，停止二分（该站上限无法测量）")
        return res
    if status == "200":
        res["ok"] = hi
        res["ok_tokens"] = info.get("input_tokens", res["ok_tokens"])
        res["notes"].append(f"上界 {hi//1000}k 直接通过，实际上限 >= {hi//1000}k"
                            f"（可提高 --hi 继续测）")
        return res
    res["fail"] = hi
    res["notes"].append(f"上界 {hi//1000}k 被拒({status}): {info.get('err','')[:90]}")

    left, right = res["ok"], res["fail"]
    for _ in range(rounds):
        if right - left <= 20000:
            break
        mid = (left + right) // 2
        time.sleep(gap)
        status, info = check(mid)
        if res["untrusted"]:
            res["notes"].append(f"{mid//1000}k 处发现截断，停止二分")
            break
        if status == "200":
            left = mid
            res["ok"] = mid
            res["ok_tokens"] = info.get("input_tokens", res["ok_tokens"])
        else:
            right = mid
            res["fail"] = mid
    res["ok"], res["fail"] = left, right
    return res



def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--model", default="claude-opus-4-8")
    ap.add_argument("--only", default="", help="只测站点，子串匹配，逗号分隔")
    ap.add_argument("--lo", type=int, default=200000, help="二分下界字符数（默认 200000）")
    ap.add_argument("--hi", type=int, default=1000000, help="二分上界字符数（默认 1000000）")
    ap.add_argument("--rounds", type=int, default=5, help="二分轮数（默认 5）")
    ap.add_argument("--gap", type=float, default=5.0, help="请求间隔秒数（默认 5）")
    ap.add_argument("--dry-run", action="store_true", help="只列计划与成本估算")
    ap.add_argument("--json", default="", help="结果另存 JSON")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        sys.exit(f"找不到配置文件：{cfg_path}")
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    targets = collect(cfg, [s.strip() for s in args.only.split(",") if s.strip()],
                      args.model)
    if not targets:
        sys.exit(f"没有匹配的上游（模型 {args.model}，检查 --only 与 models 列表）")

    per_site = 2 + args.rounds
    avg_tok = (args.lo + args.hi) / 2 / CHARS_PER_TOKEN
    est_cost = len(targets) * per_site * avg_tok / 1_000_000 * COST_PER_MTOK
    print(f"模型: {args.model}   二分区间: {args.lo//1000}k - {args.hi//1000}k 字符   "
          f"轮数: {args.rounds}")
    print(f"待测 {len(targets)} 个上游，每站约 {per_site} 次请求，"
          f"合计约 {len(targets)*per_site} 次")
    print(f"成本粗估: 约 ${est_cost:.2f}（按实测 $0.139/百万 tokens）")
    print()
    print(f"{'站点':<26}{'pri':>6}")
    for t in targets:
        print(f"{t['host']:<26}{str(t['prio'] or '-'):>6}")
    if args.dry_run:
        print("\n--dry-run：未发出任何请求。")
        return
    print()

    rows = []
    for i, t in enumerate(targets, 1):
        print(f"[{i}/{len(targets)}] {t['host']} 探测中 …", flush=True)
        r = bisect_limit(t, args.model, args.lo, args.hi, args.rounds, args.gap)
        rows.append(r)
        for n in r["notes"]:
            print(f"    {n}")
        if r["ok"]:
            print(f"    通过上限 >= {r['ok']//1000}k 字符"
                  f"（input_tokens={r['ok_tokens']}）"
                  f"{'  实返模型=' + r['actual_model'] if r['actual_model'] else ''}")
        if r["fail"]:
            print(f"    被拒下限 <= {r['fail']//1000}k 字符")
        print(f"    本站花费约 ${r['cost']:.4f}")
        if i < len(targets):
            time.sleep(args.gap)

    print("\n" + "=" * 84)
    print(f"{'站点':<26}{'pri':>6}{'可用tokens':>13}{'被拒字符':>11}  {'实返模型':<22}可信")
    print("-" * 84)
    for r in sorted(rows, key=lambda x: -(x["prio"] or 0)):
        print(f"{r['host']:<26}{str(r['prio'] or '-'):>6}"
              f"{r['ok_tokens']:>13,}"
              f"{(str(r['fail']//1000)+'k' if r['fail'] else '-'):>11}  "
              f"{str(r['actual_model'] or '-'):<22}"
              f"{'否' if r.get('untrusted') else '是'}")
    print("-" * 84)
    print(f"总花费约 ${sum(r['cost'] for r in rows):.4f}")

    bad = [r for r in rows if r.get("untrusted")]
    if bad:
        print(f"\n不可信 {len(bad)} 站（上游截断请求，测不出真实上限）:")
        for r in bad:
            print(f"  {r['host']}  实返模型={r['actual_model']}")
        print("  这类站点的 200 不代表它真吃下了那么多上下文，数值不可用于定阈值。")
        print("  处置：不纳入 CLAUDE_CODE_MAX_CONTEXT_TOKENS 的计算依据；"
              "若它 priority 高，考虑降权。")

    usable = [r["ok_tokens"] for r in rows
              if r["ok_tokens"] > 0 and not r.get("untrusted")]
    if usable:
        lowest = min(usable)
        highest = max(usable)
        rec_low = int(lowest * 0.85 / 10000) * 10000
        rec_high = int(highest * 0.85 / 10000) * 10000
        low_host = [r['host'] for r in rows
                    if r['ok_tokens'] == lowest and not r.get('untrusted')][0]
        high_host = [r['host'] for r in rows
                     if r['ok_tokens'] == highest and not r.get('untrusted')][0]
        print(f"\n可信站点上限区间: {lowest:,}（{low_host}） - "
              f"{highest:,}（{high_host}）")
        print(f"\n两种取值策略:")
        print(f"  保守 CLAUDE_CODE_MAX_CONTEXT_TOKENS = \"{rec_low}\"")
        print(f"    最低站的 85%。任何站都不会超限，代价是可用上下文被最弱一环拉低。")
        print(f"  激进 CLAUDE_CODE_MAX_CONTEXT_TOKENS = \"{rec_high}\"")
        print(f"    最高站的 85%。轮到低上限站时会 400，靠 config.yaml 里 400 规则的")
        print(f"    \"Context window is full\" 关键词触发 continue-and-cooldown 换站。")
        print(f"    前提：该关键词已加（本项目 192 处已加），否则 400 会直接返回客户端。")
        print("  写入位置：cc-switch -> 编辑通用配置 -> env 段，值必须带引号。")


    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"\nJSON 结果: {args.json}")


if __name__ == "__main__":
    main()
