#!/usr/bin/env python3
"""统计静默换模率：经 CPA 反复请求同一模型，看返回的 model 字段是否被替换。

为什么需要
----------
2026-08-28 实测经 cpa.example.com 请求 gpt-5.6-sol，HTTP 200，但响应：
    "model":"agnes-2.0-flash"
    "text":"Hi! I'm Agnes, developed by Sapiens AI."
    input_tokens=285          （正常应为约 4390）

config.yaml 里已记录过同类现象：betterclau 网关把 gpt-5.6-sol 换成
agnes-2.0-flash，relay-e 换成 grok-4.6。这类站照常计费却返回另一个模型，
比不可用更危险 —— 而且它是【间歇性】的，取决于 weighted-round-robin
这次轮到哪个凭证，所以单次探测测不出来。

本脚本连续采样，按「实返模型」分组计数，直接给出坏凭证在轮询池里的占比。
input_tokens 是比 model 名更可靠的指纹（285 vs 4390，差 15 倍），两者都记。

用法（在 /opt/deploy 下执行，也可在本机跑，把 --base 换成公网域名）
    python3 swap-watch.py --n 12
    python3 swap-watch.py --n 20 --model gpt-5.6-sol --endpoint responses
    python3 swap-watch.py --n 10 --model claude-opus-4-8 --endpoint messages
    python3 swap-watch.py --base http://127.0.0.1:8317 --key sk-xxx --n 12

Key 来源：默认从 config.yaml 的 api-keys 第一条读，也可用 --key 显式指定。
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
from collections import Counter
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

TIMEOUT = 120
# 读满响应体。/v1/responses 把整个 Codex 系统提示放在 instructions 字段里，
# 实测单条响应 40KB+，而 model 字段排在它之后。截断会导致误判换模。
MAX_BODY = 4 * 1024 * 1024
UA_CODEX = ("codex_vscode/0.150.0-alpha.12.2 (Windows 10.0.28000; x86_64) "
            "unknown (VS Code; 26.825.31414)")



def backend_of(rid: str | None) -> str:
    """按响应 id 形态推断真实后端。

    这比 model 字段可靠：上游可以随意改 model 名，但 id 由真实后端生成。
    口径来自 cpa-atlas.html 的实测对照表：
      msg_01 + base58     Anthropic 官方（relay-m.example 实测）
      msg_bdrk_*          AWS Bedrock（relay-c.example 实测）
      msg_ + 32位 hex     中转自造（relay-f.example 实测）
      chatcmpl-*          OpenAI Chat 兼容层（betterclau 换模时即此形态）
      resp_ + 长 hex      OpenAI Responses 官方形态
      resp_ + 短 hex      中转自造（实测 agnes-2.0-flash 为 resp_ + 24hex）
    """
    if not rid:
        return "?"
    if rid.startswith("msg_bdrk_"):
        return "AWS Bedrock"
    if re.fullmatch(r"msg_01[A-HJ-NP-Za-km-z1-9]{10,}", rid):
        return "Anthropic 官方"
    if re.fullmatch(r"msg_[0-9a-f]{32}", rid):
        return "中转自造(msg+32hex)"
    if rid.startswith("chatcmpl-"):
        return "OpenAI Chat 兼容(多为中转)"
    if re.fullmatch(r"resp_[0-9a-f]{40,}", rid):
        return "OpenAI Responses 官方形态"
    if re.fullmatch(r"resp_[0-9a-f]{16,32}", rid):
        return "中转自造(resp+短hex)"
    if rid.startswith("msg_"):
        return "msg_ 其他形态"
    if rid.startswith("resp_"):
        return "resp_ 其他形态"
    return "未知形态"



def load_key(cfg_path: str) -> str:
    if yaml is None:
        sys.exit("缺少 pyyaml，请用 --key 显式指定入口 Key")
    p = Path(cfg_path)
    if not p.exists():
        sys.exit(f"找不到 {cfg_path}，请用 --key 显式指定入口 Key")
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    keys = cfg.get("api-keys") or []
    if not keys:
        sys.exit("config.yaml 的 api-keys 为空，请用 --key 指定")
    return str(keys[0]).strip()


def build(base: str, key: str, model: str, endpoint: str) -> tuple[str, dict, bytes]:
    base = base.rstrip("/")
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {key}",
         "User-Agent": UA_CODEX, "Originator": "codex_vscode"}
    if endpoint == "responses":
        url, body = f"{base}/v1/responses", {"model": model, "stream": False,
                                             "input": "hi"}
    elif endpoint == "messages":
        url = f"{base}/v1/messages"
        h["anthropic-version"] = "2023-06-01"
        body = {"model": model, "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}]}
    else:  # chat
        url = f"{base}/v1/chat/completions"
        body = {"model": model, "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}]}
    return url, h, json.dumps(body).encode()


def once(base: str, key: str, model: str, endpoint: str) -> dict:
    url, headers, data = build(base, key, model, endpoint)
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    t0 = time.time()
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            # 必须读完整响应体。/v1/responses 的字段顺序是
            #   id -> object -> created_at -> ... -> instructions（整个 Codex
            #   系统提示，实测 40KB+）-> ... -> model
            # 早先只读前 20KB，model 字段被截断切掉，导致「解析不到」被误判成换模。
            text, status = r.read(MAX_BODY).decode("utf-8", "replace"), str(r.status)
    except urllib.error.HTTPError as e:
        text, status = e.read(MAX_BODY).decode("utf-8", "replace"), str(e.code)
    except Exception as e:  # noqa: BLE001
        return {"status": "000", "secs": round(time.time() - t0, 1),
                "model": None, "tokens": None, "id": None, "backend": "?",
                "truncated": False, "err": f"{type(e).__name__}: {e}"[:120]}

    out = {"status": status, "secs": round(time.time() - t0, 1), "err": None,
           "truncated": len(text) >= MAX_BODY}
    m = re.search(r'"model"\s*:\s*"([^"]+)"', text)
    out["model"] = m.group(1) if m else None
    m = re.search(r'"(?:input_tokens|prompt_tokens)"\s*:\s*(\d+)', text)
    out["tokens"] = int(m.group(1)) if m else None
    m = re.search(r'"id"\s*:\s*"((?:msg|resp|chatcmpl)[^"]{0,60})"', text)
    out["id"] = m.group(1) if m else None
    out["backend"] = backend_of(out["id"])
    if status != "200":
        m = re.search(r'"message"\s*:\s*"([^"]{0,120})', text)
        out["err"] = (m.group(1) if m else text[:120]).replace("\n", " ")
    return out




def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="http://127.0.0.1:8317",
                    help="CPA 地址（默认 http://127.0.0.1:8317，VPS 本机直连避开 Cloudflare）")
    ap.add_argument("--config", default="config.yaml", help="用于读取入口 Key")
    ap.add_argument("--key", default="", help="显式指定入口 Key，优先于 --config")
    ap.add_argument("--model", default="gpt-5.6-sol")
    ap.add_argument("--endpoint", default="responses",
                    choices=("responses", "messages", "chat"))
    ap.add_argument("--n", type=int, default=12, help="采样次数（默认 12）")
    ap.add_argument("--gap", type=float, default=2.0, help="间隔秒数（默认 2）")
    ap.add_argument("--json", default="", help="结果另存 JSON")
    args = ap.parse_args()

    key = args.key.strip() or load_key(args.config)
    print(f"目标: {args.base}  端点: /v1/{args.endpoint}  模型: {args.model}  "
          f"采样: {args.n} 次  间隔: {args.gap}s")
    print(f"入口 Key: {key[:8]}…{key[-4:]}")
    print()
    print(f"{'#':>3}  {'码':>4}  {'耗时':>6}  {'实返模型':<24}{'tokens':>8}  "
          f"{'后端指纹':<26}id / 错误")
    print("-" * 122)

    rows = []
    for i in range(1, args.n + 1):
        r = once(args.base, key, args.model, args.endpoint)
        rows.append(r)
        tail = r["err"] if r["err"] else (r["id"] or "")
        print(f"{i:>3}  {r['status']:>4}  {str(r['secs'])+'s':>6}  "
              f"{str(r['model'] or '-'):<24}{str(r['tokens'] or '-'):>8}  "
              f"{str(r.get('backend') or '-'):<26}{tail[:34]}", flush=True)
        if i < args.n:
            time.sleep(args.gap)

    print("-" * 122)
    ok = [r for r in rows if r["status"] == "200"]
    print("\n状态分布: " + "  ".join(
        f"{k}×{v}" for k, v in sorted(Counter(r["status"] for r in rows).items())))

    if not ok:
        print("\n本轮 0 个 200，无法判断换模。先看错误文案：")
        errs = Counter((r["status"], (r["err"] or "")[:60]) for r in rows)
        for (st, e), n in errs.most_common(6):
            print(f"  {st} ×{n}  {e}")
        if any("Image generation" in (r["err"] or "") for r in rows):
            print("\n  出现 image_generation 403 —— 这是 CPA 自己注入的工具被上游拒绝，")
            print("  不是站方策略问题。成因 codex_executor_request.go:455。")
            print("  处置（config.yaml 顶层，热重载生效）:")
            print('    disable-image-generation: "chat"')
            print("  改完再跑本脚本，否则换模检测始终拿不到 200 样本。")
        if any("Budget pool" in (r["err"] or "") for r in rows):
            print("\n  出现 402 Budget pool exhausted —— 该站预算池耗尽，需充值。")
            print("  不要 weight: 0，充值后会自行恢复；要临时避开只降 priority。")

    if ok:
        want = args.model.split("/")[-1].lower()
        # 三分类，不把「解析不到」当成换模：
        #   一致 = model 字段与请求模型匹配
        #   换模 = 有 model 字段但不匹配（真信号）
        #   未知 = 拿不到 model 字段（响应过大被截断，或该协议不回该字段）
        same_n = swap_n = unknown_n = 0
        by_model = Counter()
        for r in ok:
            name = r["model"]
            if not name:
                unknown_n += 1
                by_model["(未返回 model 字段)"] += 1
                continue
            by_model[name] += 1
            if name.lower().startswith(want) or want.startswith(name.lower()):
                same_n += 1
            else:
                swap_n += 1

        print(f"\n200 响应按实返模型分组（共 {len(ok)} 次）:")
        for name, n in by_model.most_common():
            if name == "(未返回 model 字段)":
                tag = "   <-- 未知，非换模证据"
            else:
                s = name.lower().startswith(want) or want.startswith(name.lower())
                tag = "" if s else "   <-- 换模"
            toks = [r["tokens"] for r in ok
                    if (r["model"] or "(未返回 model 字段)") == name and r["tokens"]]
            tk = f"  tokens 中位 {sorted(toks)[len(toks)//2]:,}" if toks else ""
            print(f"  {name:<30}{n:>4} 次  ({n*100//len(ok)}%){tk}{tag}")

        by_backend = Counter(r.get("backend") or "?" for r in ok)
        print("\n按后端指纹分组（id 形态比 model 名可靠）:")
        for name, n in by_backend.most_common():
            samples = [r["id"] for r in ok if r.get("backend") == name and r["id"]]
            print(f"  {name:<30}{n:>4} 次  ({n*100//len(ok)}%)  "
                  f"例 {samples[0][:36] if samples else '-'}")
        if len(by_backend) > 1:
            print("  出现多种后端形态 = 请求被分发到了不同真实后端。")
            print("  结合 model 名判断：model 名相同但后端形态不同，说明有站在冒充。")

        print(f"\n模型一致性: 一致 {same_n}  换模 {swap_n}  未知 {unknown_n}"
              f"  （共 {len(ok)} 次 200）")
        if swap_n:
            rate = swap_n * 100 // max(same_n + swap_n, 1)
            print(f"换模率: {swap_n}/{same_n + swap_n} = {rate}%（未知不计入分母）")
            print("  已知同类：betterclau -> agnes-2.0-flash（token 289 vs 4393）、"
                  "relay-e -> grok-4.6")
            print("  处置：定位实返模型对应的上游，给该条目加 disabled: true。")
            print("  查上游：VPS 上 grep -n '^Upstream URL:' "
                  "logs/cli-proxy-api/error-*.log")
            print("  （必须锚定行首 ^，否则会匹配到 JSON 请求体里的同名字段）")
        elif same_n:
            print("  未发现换模。换模是间歇性的，可加大 --n 再采一轮。")
        if unknown_n:
            trunc = sum(1 for r in ok if r.get("truncated"))
            print(f"\n{unknown_n} 次拿不到 model 字段"
                  f"{f'（其中 {trunc} 次响应达到读取上限被截断）' if trunc else ''}。")
            print("  /v1/responses 把整个 Codex 系统提示放在 instructions 里，"
                  "model 排在其后。")
            print("  这些不构成换模证据。可改用 --endpoint chat 复核，"
                  "该端点响应短、model 靠前。")



        toks = sorted(r["tokens"] for r in ok if r["tokens"])
        if len(toks) >= 2 and toks[-1] > toks[0] * 3:
            print(f"\n注意：input_tokens 跨度异常（{toks[0]:,} - {toks[-1]:,}，差 "
                  f"{toks[-1]//max(toks[0],1)} 倍）。")
            print("  同一请求在不同后端上 prompt tokens 差数倍，说明连系统提示都不是同一套，"
                  "是换模的强信号。")

        secs = sorted(r["secs"] for r in ok)
        print(f"\n耗时: 最快 {secs[0]}s  中位 {secs[len(secs)//2]}s  最慢 {secs[-1]}s")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"\nJSON 结果: {args.json}")


if __name__ == "__main__":
    main()
