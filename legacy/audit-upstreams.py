#!/usr/bin/env python3
"""只读探测：逐个 (配置段, 站点, 模型) 组合发一次最小请求，判定真实可用性。

为什么需要
----------
CPA 的 priority 决定选站顺序，但配置里声明某个模型不代表该站在那条协议路径上
真的提供它。2026-08-27/28 实测出五种「声明了却不可用」的情形：

  relay-b.example   /v1/chat/completions  model_not_found，group 91普通用户 无渠道
  relay-d.example    /v1/chat/completions  404 当前 API 不支持所选模型
  relay-d.example    /v1beta/…generateContent  404 Invalid URL（路径不存在）
  relay-g.example    任意端点              403 访问已被拦截（真按出口 IP 拉黑）
  relay-c.example  任意端点              401 unauthorized client（要 codex_vscode 标识）
  relay-f/relay-l 任意端点            403 Attention Required（缺 UA，非 IP 封）

这些都不是余额问题，充值无效。前四种靠调 priority 或改 models 规避，
后两种靠给凭证加 headers 解决 —— 二者的处置完全相反，务必分清。
本脚本把这类信息一次性列出来，供人工决策，**不修改任何配置**。

协议路径由配置段决定（与 CPA 自身的行为一致）：
  gemini-api-key  -> POST {base}/v1beta/models/{model}:generateContent?key=
  codex-api-key   -> POST {base}/responses          （base 已含 /v1）
  claude-api-key  -> POST {base}/v1/messages
  openai-compat   -> POST {base}/chat/completions   （base 已含 /v1）

客户端标识
----------
早先版本不发任何 UA，导致 24 个组合被误判为「Cloudflare 按 IP 拦截」，其中 12 个
（relay-f / relay-l）实际只是缺 UA。现在默认按 SITE_IDENTITY 逐站选标识，
配置里已写 headers 的条目直接沿用它自己的，与 CPA 实际发出的请求保持一致。

CPA 各段默认发出的 UA（源码核对，用于理解审计结果与线上行为的差异）：
  openai-compatibility  cli-proxy-openai-compat            硬编码，凭证级 headers 可覆盖
  codex-api-key         codex-tui/0.146.0 (Mac OS…)        applyCodexCloakingHeaders 覆盖，
                                                           需 codex.disable-codex-cloaking: true
                                                           才能让 headers 或客户端值生效
  claude-api-key        透传客户端 UA，缺失则 CLIProxyAPI/<ver>
  gemini-api-key        不设置（Go transport 默认）

用法（在 /opt/deploy 下执行）
-----------------------------
    python3 audit-upstreams.py                      # 全量，终端表格 + HTML
    python3 audit-upstreams.py --gap 5              # 每次请求间隔 5 秒（默认 3）
    python3 audit-upstreams.py --skip relay-b        # 跳过指定站点（子串匹配）
    python3 audit-upstreams.py --only relay-d     # 只测指定站点
    python3 audit-upstreams.py --models 1           # 每站只测 1 个模型（默认 2）
    python3 audit-upstreams.py --no-html            # 只要终端输出

判断某站是否按标识鉴权 —— 跑两次对比，看哪些从非 200 变 200：
    python3 audit-upstreams.py --identity none    --no-html   # 裸请求基线
    python3 audit-upstreams.py --identity browser --no-html
    python3 audit-upstreams.py --identity codex   --no-html

反探测风险
----------
config.yaml 记录 relay-b.example 有 bulk probe guard：60 秒内请求 4 个不同模型
即触发，返回 403 "bulk probe guard: ip x.x.x.x requested N distinct models in 60s"
并伴随 429。默认 --gap 3 与 --models 2 就是为此收敛的；脚本识别该提示并单独标记。
若某站被这样封了，等几分钟自然恢复，不影响 CPA 正常流量。
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

# 每个配置段对应的协议路径与认证方式。与 CPA 实际行为一致。
SEGMENTS = {
    "gemini-api-key": "gemini",
    "codex-api-key": "responses",
    "claude-api-key": "messages",
}

VERDICTS = {
    "OK": "通",
    "IDENT": "标识",
    "EDGE": "边缘",
    "GATE": "门禁",
    "DEAD": "死路",
    "TEMP": "临时",
    "QUOTA": "余额",
    "BLOCK": "IP封",
    "GUARD": "反探测",
    "ERR": "异常",
    "SKIP": "已禁用",
}

# 挑模型的优先顺序。聚合站的 models 列表可能有 838 项且按字母排序，
# 直接取前 N 个会测到 "42"、"abab5.5-chat" 这类无关模型，白费请求。
# 这里按"你实际会用的模型"排序，命中的先测。
PREFERRED_MODELS = [
    "gpt-5.6-sol", "claude-opus-5", "claude-sonnet-5", "claude-opus-4-8",
    "gpt-5-codex", "gemini-2.5-pro", "gemini-3.1-pro-preview", "grok-4.6",
    "claude-opus-5-thinking", "gpt-5.6-terra", "claude-opus-4.8", "claude-fable-5",
]


def rank_models(models: list[str]) -> list[str]:
    """把 PREFERRED_MODELS 里的排前面，其余保持原序。"""
    pref = {name: i for i, name in enumerate(PREFERRED_MODELS)}
    return sorted(models, key=lambda m: (pref.get(m, len(pref)), models.index(m)))


def host_of(url: str) -> str:
    m = re.search(r"https?://([^/]+)", url or "")
    return m.group(1) if m else ""


# 客户端标识档案。2026-08-28 实测：多个站点按 UA / Originator 鉴权，
# 不带或带错一律拒绝，与出口 IP、API Key 都无关（逐一排除法验证过）。
# 早先版本不发 UA，导致 24 个组合被误判为「Cloudflare 按 IP 拦截」，
# 其中 12 个（relay-f / relay-l）实际只是缺 UA。
IDENTITIES = {
    # 官方 Codex VSCode 插件的标识。relay-c 只认这一种：
    # 浏览器 UA 直连 401、经代理 401、6 个 Key 全 401；换成本档案后
    # /v1/responses、/v1/messages、/v1/chat/completions 三条路径全部 200。
    "codex": {
        "User-Agent": "codex_vscode/0.150.0-alpha.8 (Windows 10.0.28000; x86_64) unknown (VS Code; 26.820.60940)",
        "Originator": "codex_vscode",
    },
    # 普通浏览器。relay-f / relay-l 用这个即可：
    # 无 UA 时连首页 GET 都是 403「Attention Required! | Cloudflare」，
    # 带上就 200，且 /v1/messages 返回真实模型内容。
    "browser": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    },
    # 不带任何标识，复现 CPA openai-compat 执行器的裸请求
    # （openai_compat_executor.go:155 把 UA 固定成 cli-proxy-openai-compat）。
    # 只在 --identity none 时使用，用于判断某站是否真的要求标识。
    "none": {},
}

# 每个站点默认用哪个标识档案。未列出的站点用 --identity 的全局值（默认 browser）。
# 2026-08-28 依据实测填写；新增站点先用 --identity none 与 browser 各跑一次对比。
SITE_IDENTITY = {
    "relay-c.example": "codex",
    "relay-f.example": "browser",
    "relay-l.example": "browser",
    # jdw 与 relay-f / relay-l 同一站长、站点类型相同（用户 2026-08-28 指出），
    # 故同样按 browser 处理；审计里它的 compat 段本来就已经 200，
    # 说明它对标识的要求不比那两个站更严。
    "relay-h.example": "browser",
}


def identity_headers(host: str, override: str | None) -> dict[str, str]:
    """解析某站点该用哪套标识头。--identity 显式指定时全局覆盖。"""
    profile = override or SITE_IDENTITY.get(host, "browser")
    return IDENTITIES.get(profile, IDENTITIES["browser"])


def build_request(kind: str, base: str, model: str, key: str,
                  ident: dict[str, str]) -> tuple[str, list[str], str]:
    """返回 (url, curl 额外参数, 请求体)。base 按配置原样，不做 /v1 猜测。

    ident 里的标识头最后追加，因此会覆盖前面同名的头。
    """
    base = base.rstrip("/")
    if kind == "gemini":
        url = f"{base}/v1beta/models/{model}:generateContent?key={key}"
        hdr = ["-H", "Content-Type: application/json"]
        body = json.dumps({"contents": [{"parts": [{"text": "hi"}]}]})
    elif kind == "responses":
        url = f"{base}/responses"
        hdr = ["-H", f"Authorization: Bearer {key}", "-H", "Content-Type: application/json"]
        body = json.dumps({"model": model, "input": "hi", "max_output_tokens": 16})
    elif kind == "messages":
        url = f"{base}/v1/messages"
        hdr = ["-H", f"x-api-key: {key}", "-H", "anthropic-version: 2023-06-01",
               "-H", "Content-Type: application/json"]
        body = json.dumps({"model": model, "max_tokens": 8,
                           "messages": [{"role": "user", "content": "hi"}]})
    else:  # chat
        url = f"{base}/chat/completions"
        hdr = ["-H", f"Authorization: Bearer {key}", "-H", "Content-Type: application/json"]
        body = json.dumps({"model": model, "max_tokens": 8,
                           "messages": [{"role": "user", "content": "hi"}]})
    for name, value in ident.items():
        hdr += ["-H", f"{name}: {value}"]
    return url, hdr, body


def classify(status: str, body: str, sent_identity: bool = True) -> tuple[str, str]:
    """把 (状态码, 响应正文) 归成一个判定 + 一句人类可读的原因。

    分类顺序有讲究：先认正文特征，再落到状态码。因为同一个状态码在不同站点
    含义可能完全不同 —— 例如 403 既可能是 Cloudflare IP 拦截，也可能是余额不足。

    sent_identity 表示本次请求是否带了客户端标识（UA 等）。带了还被 Cloudflare
    拦下，就不能再归因于"缺标识"，见下面 EDGE 分支。
    """
    low = body.lower()
    stripped = body.strip()

    # 客户端标识不被接受。必须排在 Cloudflare 判定之前：这类响应也可能是 403，
    # 但它是应用层拒绝，改标识就能解决，与出口 IP 无关。
    # 2026-08-28 实测 relay-c：codex_vscode UA + Originator 头即 200。
    if "unauthorized client" in low:
        return "IDENT", "站方要求特定客户端标识"

    # 403 + 空正文 = Cloudflare 边缘直接拒，请求没进到站点。
    # 2026-08-28 实测 relay-f：同一 Key、同一模型连打 10 次，3 次 403，
    # 每次响应只有 content-length: 0 + server: cloudflare + cf-ray（SEA/PDX 交替），
    # 403 之后紧接着的请求立刻 200 —— 既不是模型权限也不是速率限制，
    # 是边缘节点的概率性拦截。这类不需要改配置：
    # max-retry-credentials 为 4 时单请求全灭概率约 0.3^4 ≈ 0.8%。
    if status == "403" and not stripped:
        return "EDGE", "Cloudflare 边缘概率拦截（正文为空，重试即可）"

    # Cloudflare 拦截页。三类区分：
    #   Attention Required / Just a moment + 本次没带标识 -> 可能只是缺 UA
    #   Attention Required / Just a moment + 本次带了标识 -> 已排除标识因素
    #   访问已被拦截 / challenge-platform                  -> relay-g 那种按出口 IP 拉黑
    # 2026-08-28 实测：relay-f / relay-l 无 UA 时连首页 GET 都拿这个页面，
    # 带浏览器 UA 后 200；而 relay-j 的 Gemini 原生路径带 UA 仍拿这个页面，
    # 且它其他三条协议路径都返回 401 Invalid token —— 那里的真实原因是
    # 该路径在站上不存在，被 Cloudflare 的默认规则接住，与标识无关。
    if re.search(r"attention required|just a moment", body, re.I):
        if sent_identity:
            return "BLOCK", "带标识仍被 Cloudflare 拦（路径不存在或按 IP 拦）"
        return "IDENT", "缺客户端标识，被 Cloudflare 拦下"
    if re.search(r"challenge-platform|cf-mitigated|cdn-cgi|访问已被拦截|安全验证", body):
        return "BLOCK", "Cloudflare 按出口 IP 拦截"

    # 该站的批量探测防护，非配置问题，等几分钟自愈
    if "bulk probe" in low or "bulk model probing" in low:
        return "GUARD", "触发站点反探测，稍后重试"

    # Key 分组不对：Key 有效但被分到了不支持该协议/模型的分组。
    # 2026-08-28 实测 relay-a 的 gemini 原生路径：
    #   400 {"code":400,"message":"API key group platform is not gemini"}
    # 与 relay-j 同类，配置层改不了，要去站方后台调分组。
    if "group platform is not" in low or "api key group" in low:
        m = re.search(r"group platform is not (\w+)", body, re.I)
        want = f"，需 {m.group(1)} 分组" if m else ""
        return "DEAD", f"Key 分组不匹配该协议{want}"

    # 站方要求先开通某功能
    if "1m 上下文" in body:
        return "GATE", "需在站方后台启用 1m 上下文"

    # 分组/渠道权限：模型存在但当前账号分组没有渠道。
    # 中文变体必须一起匹配 —— 2026-08-28 实测 relay-c 返回
    #   503 {"message":"当前分组 core 下对于模型 claude-opus-5 无可用渠道"}
    # 只匹配英文时它落到状态码分支被判成「临时故障」，而这是持续性的渠道缺失。
    if ("no available channel" in low or "model_not_found" in low
            or "无可用渠道" in body or "可用渠道不存在" in body):
        m = re.search(r"under group ([^\s(]+)", body)
        grp = f"，分组 {m.group(1)}" if m else ""
        return "DEAD", f"分组无该模型渠道{grp}"

    # 端点/路径不提供
    if "不支持所选模型" in body:
        return "DEAD", "该端点不提供此模型"
    if "invalid url" in low:
        return "DEAD", "该协议路径不存在"
    if "invalid endpoint" in low or "path must contain" in low:
        return "DEAD", "该协议路径不受支持"

    # 余额与配额
    if re.search(r"budget pool|quota has been exhausted|insufficient_user_quota|"
                 r"insufficient_quota|quota_exceeded|预扣费额度失败|user quota is not enough", body, re.I):
        return "QUOTA", "余额或配额耗尽"
    if "额度已经达到上限" in body or "额度已达上限" in body:
        return "QUOTA", "模型额度达上限"

    # 临时性
    if "负载已经达到上限" in body or "负载已达上限" in body:
        return "TEMP", "模型负载上限，稍后可用"
    if "sensitive_words" in low:
        return "DEAD", "站点级敏感词拦截"

    if status == "200":
        return "OK", "可用"
    if status in ("401",):
        return "DEAD", "认证被拒"
    if status in ("402",):
        return "QUOTA", "需付费"
    if status in ("404",):
        return "DEAD", f"HTTP 404"
    if status in ("429",):
        return "TEMP", "限流"
    if status in ("500", "502", "503", "504", "520", "521", "522", "524"):
        return "TEMP", f"上游 {status}，临时故障"
    if status == "000":
        return "ERR", "连接失败或超时"
    return "ERR", f"HTTP {status}"


def probe(kind: str, base: str, model: str, key: str, timeout: int,
          ident: dict[str, str]) -> tuple[str, str, str, float, str]:
    """发一次请求，返回 (判定, 原因, 响应正文前 2KB, 耗时秒, 状态码)。

    正文保留 2KB 而非 400B：Cloudflare 拦截页的错误码（1015 速率限制 /
    1020 WAF 封禁）在页面靠后位置，截太短会漏掉这个关键区分。
    """
    url, hdr, body = build_request(kind, base, model, key, ident)
    cmd = ["curl", "-sS", "--max-time", str(timeout), "-X", "POST",
           *hdr, "-d", body, "-w", "\n__S__%{http_code}__T__%{time_total}", url]
    t0 = time.time()
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             errors="replace", timeout=timeout + 10).stdout
    except subprocess.TimeoutExpired:
        return "ERR", "curl 超时", "", time.time() - t0, "000"

    m = re.search(r"__S__(\d+)__T__([\d.]+)", out)
    status = m.group(1) if m else "000"
    elapsed = float(m.group(2)) if m else time.time() - t0
    resp = (out[: m.start()] if m else out).strip()
    verdict, reason = classify(status, resp, sent_identity=bool(ident))
    return verdict, reason, resp[:2048], elapsed, status


def collect_targets(cfg: dict, limit_models: int) -> list[dict]:
    """从 config.yaml 展开待探测目标。

    同一站点在一个段里通常有多个 key（relay-l 14 个），只取第一个作代表 ——
    2026-08-27 实测三个 relay-d key 结果完全一致，key 级差异用 CPA 的冷却机制
    处理即可，本脚本要回答的是"这条协议路径上这个模型能不能用"。

    配置里已写 headers 的条目沿用它自己的标识（那是人工确认过的），
    没写的按 SITE_IDENTITY 推断。这样审计结果与 CPA 实际发出的请求一致。
    """
    out: list[dict] = []
    for seg, kind in SEGMENTS.items():
        seen = set()
        for entry in cfg.get(seg) or []:
            base = entry.get("base-url") or ""
            h = host_of(base)
            if not h or h in seen:
                continue
            seen.add(h)
            models = rank_models([m.get("name") for m in (entry.get("models") or []) if m.get("name")])
            for model in models[:limit_models]:
                out.append({"seg": seg, "kind": kind, "host": h, "base": base,
                            "priority": entry.get("priority") or 0,
                            "key": entry.get("api-key") or "", "model": model,
                            "cfg_headers": entry.get("headers") or {},
                            "disabled": False})
    for prov in cfg.get("openai-compatibility") or []:
        base = prov.get("base-url") or ""
        h = host_of(base)
        key = next((e.get("api-key") for e in (prov.get("api-key-entries") or [])
                    if e.get("api-key")), "")
        models = rank_models([m.get("name") for m in (prov.get("models") or []) if m.get("name")])
        for model in models[:limit_models]:
            out.append({"seg": f"compat:{prov.get('name')}", "kind": "chat", "host": h,
                        "base": base, "priority": prov.get("priority") or 0,
                        "key": key, "model": model,
                        "cfg_headers": prov.get("headers") or {},
                        "disabled": bool(prov.get("disabled"))})
    return out


HTML_HEAD = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CPA 上游可用性报告</title>
<style>
:root{
  --paper:#EDF0F3;--card:#F7F9FA;--sunk:#E2E7EB;--ink:#131A21;--ink2:#3C4A55;--ink3:#6A7A86;
  --line:#CBD4DB;--line2:#DDE4E9;--teal:#0B6E76;--plum:#8A3A62;
  --ok:#1E7A4D;--ident:#0B6E76;--gate:#9C6510;--dead:#B0433A;--temp:#5A6B8C;--quota:#8A5A1E;--block:#7A2F52;--err:#6A7A86;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
  --body:"Noto Sans SC",system-ui,-apple-system,sans-serif;
}
@media(prefers-color-scheme:dark){:root{
  --paper:#0F1519;--card:#161F26;--sunk:#1D272F;--ink:#DFE7EC;--ink2:#A8B6C0;--ink3:#76868F;
  --line:#2A363F;--line2:#222D35;--teal:#4FB3B8;--plum:#D583AC;
  --ok:#5CBE8A;--ident:#4FB3B8;--gate:#D6A03F;--dead:#E58177;--temp:#8FA3C8;--quota:#D6A03F;--block:#D583AC;--err:#76868F;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--body);font-size:15px;line-height:1.7}
.wrap{max-width:1240px;margin:0 auto;padding:0 22px 80px}
header{background:var(--card);border-bottom:1px solid var(--line);margin-bottom:34px}
.hi{max-width:1240px;margin:0 auto;padding:28px 22px 22px}
.kick{font-family:var(--mono);font-size:11px;letter-spacing:.15em;text-transform:uppercase;color:var(--teal);margin:0 0 10px}
h1{font-size:30px;margin:0 0 8px;letter-spacing:-.01em}
.sub{color:var(--ink2);margin:0 0 20px;font-size:15px}
.tallies{display:flex;flex-wrap:wrap;gap:1px;background:var(--line);border:1px solid var(--line);border-radius:3px;overflow:hidden}
.tal{background:var(--card);padding:11px 16px;min-width:96px}
.tal dt{font-family:var(--mono);font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink3);margin:0 0 4px}
.tal dd{margin:0;font-family:var(--mono);font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
h2{font-size:20px;margin:38px 0 14px;padding-bottom:7px;border-bottom:2px solid var(--ink)}
h2:first-of-type{margin-top:0}
.tw{overflow-x:auto;border:1px solid var(--line);border-radius:4px;margin:0 0 20px}
table{border-collapse:collapse;width:100%;font-size:13.5px;background:var(--card)}
th{font-family:var(--mono);font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--ink3);
   text-align:left;padding:9px 12px;background:var(--sunk);border-bottom:1px solid var(--line);white-space:nowrap;font-weight:400}
td{padding:8px 12px;border-bottom:1px solid var(--line2);vertical-align:top;color:var(--ink2)}
tr:last-child td{border-bottom:none}
td.k{color:var(--ink);font-weight:500}
td.n{font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap;color:var(--ink)}
.tag{font-family:var(--mono);font-size:10.5px;font-weight:600;letter-spacing:.05em;
     padding:2px 8px;border-radius:2px;white-space:nowrap;display:inline-block}
.t-OK{color:var(--ok);border:1px solid var(--ok)}
.t-IDENT{color:var(--ident);border:1px solid var(--ident)}
.t-EDGE{color:var(--temp);border:1px dashed var(--temp)}
.t-GATE{color:var(--gate);border:1px solid var(--gate)}
.t-DEAD{color:var(--dead);border:1px solid var(--dead)}
.t-TEMP{color:var(--temp);border:1px solid var(--temp)}
.t-QUOTA{color:var(--quota);border:1px solid var(--quota)}
.t-BLOCK{color:var(--block);border:1px solid var(--block)}
.t-GUARD{color:var(--gate);border:1px dashed var(--gate)}
.t-SKIP{color:var(--err);border:1px dashed var(--err)}
.t-ERR{color:var(--err);border:1px solid var(--err)}
details{margin:0}
summary{cursor:pointer;font-family:var(--mono);font-size:11px;color:var(--ink3);list-style:none}
summary::-webkit-details-marker{display:none}
summary::before{content:"▸ 正文";}
details[open] summary::before{content:"▾ 正文";}
pre{margin:6px 0 0;padding:9px 11px;background:var(--sunk);border-radius:3px;overflow-x:auto;
    font-family:var(--mono);font-size:11.5px;line-height:1.55;color:var(--ink2);white-space:pre-wrap;word-break:break-all}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin:0 0 22px;font-size:13px;color:var(--ink2)}
.legend span{display:flex;align-items:center;gap:6px}
footer{margin-top:44px;padding-top:18px;border-top:1px solid var(--line);
       font-family:var(--mono);font-size:11px;color:var(--ink3)}
.note{border-left:3px solid var(--teal);background:var(--sunk);padding:12px 15px;border-radius:0 3px 3px 0;margin:0 0 20px;font-size:13.5px;color:var(--ink2)}
</style></head><body>
"""


def write_html(path: Path, results: list[dict], meta: dict) -> None:
    from collections import Counter, defaultdict
    tal = Counter(r["verdict"] for r in results)
    order = ["OK", "EDGE", "IDENT", "GATE", "TEMP", "QUOTA", "DEAD", "BLOCK", "GUARD", "ERR"]

    p: list[str] = [HTML_HEAD]
    p.append('<header><div class="hi">')
    p.append(f'<p class="kick">只读探测 · {meta["when"]} · {meta["total"]} 个组合</p>')
    p.append("<h1>CPA 上游可用性报告</h1>")
    p.append('<p class="sub">每行是一次真实请求：按配置段决定协议路径，用该站自己声明的模型。'
             "本报告不修改任何配置。</p>")
    p.append('<dl class="tallies">')
    for v in order:
        if tal.get(v):
            p.append(f'<div class="tal"><dt>{VERDICTS[v]}</dt>'
                     f'<dd style="color:var(--{v.lower()})">{tal[v]}</dd></div>')
    p.append("</dl></div></header>")
    p.append('<div class="wrap">')

    p.append('<div class="legend">')
    for v, cn in [("OK", "请求到达并成功"),
                  ("EDGE", "Cloudflare 边缘概率拦截，重试即可，无需改配置"),
                  ("IDENT", "站方要求特定客户端标识，加 headers 可解"),
                  ("GATE", "需在站方后台开通"),
                  ("TEMP", "临时故障，会自愈"), ("QUOTA", "余额或配额，充值可解"),
                  ("DEAD", "该路径不提供，配置层需规避"), ("BLOCK", "IP 被拦，换出口才行"),
                  ("GUARD", "触发反探测，稍后重试"), ("ERR", "连接异常")]:
        if tal.get(v):
            p.append(f'<span><i class="tag t-{v}">{VERDICTS[v]}</i> {cn}</span>')
    p.append("</div>")

    ident = [r for r in results if r["verdict"] == "IDENT"]
    if ident:
        p.append("<h2>站方要求客户端标识</h2>")
        p.append('<div class="note">这类站点按 User-Agent / Originator 鉴权，与出口 IP 和 API Key 都无关。'
                 "<b>不要降 priority、也不要配代理</b> —— 给对应条目加 headers 即可。"
                 "排查方式：同一站点用 <code>--identity codex</code> 与 "
                 "<code>--identity browser</code> 各跑一次，哪个出 200 就用那套头。"
                 "2026-08-28 实测 relay-c 认 codex_vscode，relay-f / relay-l 认浏览器 UA。</div>")
        p.append(_table(ident, show_body=True))

    actionable = [r for r in results if r["verdict"] in ("DEAD", "BLOCK", "GATE")]
    if actionable:
        p.append("<h2>需要处理</h2>")
        p.append('<div class="note">DEAD 与 BLOCK 意味着配置里声明了却拿不到 —— '
                 "对应站点的 priority 该降，或从 models 列表移除。"
                 "GATE 需要你去站方后台操作，配置层改不了。</div>")
        p.append(_table(actionable, show_body=True))

    p.append("<h2>全部结果</h2>")
    by_host = defaultdict(list)
    for r in results:
        by_host[r["host"]].append(r)
    for h in sorted(by_host, key=lambda x: -max(y["priority"] for y in by_host[x])):
        rows = sorted(by_host[h], key=lambda r: -r["priority"])
        p.append(f'<h2 style="font-size:16px;border-bottom-width:1px">{html.escape(h)}</h2>')
        p.append(_table(rows, show_body=True))

    p.append(f'<footer>{html.escape(meta["cmd"])}<br>config.yaml: {meta["cfg"]}</footer>')
    p.append("</div></body></html>")
    path.write_text("\n".join(p), encoding="utf-8")


def _table(rows: list[dict], show_body: bool) -> str:
    out = ['<div class="tw"><table><thead><tr>',
           "<th>判定</th><th>段</th><th>站点</th><th>模型</th><th>pri</th>"
           "<th>状态</th><th>耗时</th><th>说明</th>"]
    if show_body:
        out.append("<th>响应</th>")
    out.append("</tr></thead><tbody>")
    for r in rows:
        v = r["verdict"]
        out.append("<tr>")
        out.append(f'<td><span class="tag t-{v}">{VERDICTS[v]}</span></td>')
        out.append(f'<td class="n">{html.escape(r["seg"])}</td>')
        out.append(f'<td class="k">{html.escape(r["host"])}</td>')
        out.append(f'<td class="n">{html.escape(r["model"])}</td>')
        out.append(f'<td class="n">{r["priority"]}</td>')
        out.append(f'<td class="n">{r["status"]}</td>')
        out.append(f'<td class="n">{r["elapsed"]:.1f}s</td>')
        out.append(f'<td>{html.escape(r["reason"])}</td>')
        if show_body:
            body = html.escape(r["body"]) or "（空）"
            out.append(f"<td><details><summary></summary><pre>{body}</pre></details></td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--out", default="upstream-report.html")
    ap.add_argument("--gap", type=float, default=3.0,
                    help="每次请求之间的间隔秒数，默认 3（收敛站点反探测风险）")
    ap.add_argument("--timeout", type=int, default=25)
    ap.add_argument("--models", type=int, default=2, help="每个 (段,站点) 最多测几个模型，默认 2")
    ap.add_argument("--skip", default="", help="跳过站点名含该子串的，逗号分隔")
    ap.add_argument("--only", default="", help="只测站点名含该子串的，逗号分隔")
    ap.add_argument("--include-disabled", action="store_true",
                    help="也测 disabled: true 的 provider（默认跳过）")
    ap.add_argument("--identity", choices=sorted(IDENTITIES),
                    help="全局覆盖客户端标识档案。不指定时按 SITE_IDENTITY 逐站选择，"
                         "未登记的站点用 browser。用 --identity none 可复现"
                         "「不带标识」的旧行为，与默认跑一次对比即可看出某站是否按标识鉴权")
    ap.add_argument("--no-html", action="store_true")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.is_file():
        print(f"错误: 找不到 {cfg_path}", file=sys.stderr)
        sys.exit(1)
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    targets = collect_targets(cfg, args.models)
    if not args.include_disabled:
        targets = [t for t in targets if not t["disabled"]]
    skips = [s.strip().lower() for s in args.skip.split(",") if s.strip()]
    onlys = [s.strip().lower() for s in args.only.split(",") if s.strip()]
    if skips:
        targets = [t for t in targets if not any(s in t["host"].lower() for s in skips)]
    if onlys:
        targets = [t for t in targets if any(s in t["host"].lower() for s in onlys)]
    targets = [t for t in targets if t["key"]]

    if not targets:
        print("没有符合条件的探测目标")
        return

    est = len(targets) * (args.gap + 2)
    print(f"待探测 {len(targets)} 个组合，间隔 {args.gap}s，预计约 {est/60:.0f} 分钟")
    print(f"标识: {args.identity or '按站点自动选择（SITE_IDENTITY，缺省 browser）'}")
    print(f"{'判定':<7}{'段':<22}{'站点':<24}{'模型':<26}{'pri':>5}  {'码':>4} {'耗时':>6}  说明")
    print("-" * 118)

    results: list[dict] = []
    try:
        for i, t in enumerate(targets, 1):
            # 配置里已有 headers 的条目优先沿用它 —— 那是人工确认过的标识，
            # 用它探测才与 CPA 实际发出的请求一致。--identity 显式指定时仍以命令行为准。
            if args.identity:
                ident = IDENTITIES[args.identity]
            elif t["cfg_headers"]:
                ident = {k: str(v) for k, v in t["cfg_headers"].items()}
            else:
                ident = identity_headers(t["host"], None)
            verdict, reason, body, elapsed, status = probe(
                t["kind"], t["base"], t["model"], t["key"], args.timeout, ident)
            rec = {**t, "verdict": verdict, "reason": reason, "body": body,
                   "elapsed": elapsed, "status": status,
                   "ident": ident.get("User-Agent", "(无)")[:40]}
            results.append(rec)
            print(f"{VERDICTS[verdict]:<7}{t['seg']:<22}{t['host']:<24}{t['model']:<26}"
                  f"{t['priority']:>5}  {status:>4} {elapsed:>5.1f}s  {reason}")
            if i < len(targets):
                time.sleep(args.gap)
    except KeyboardInterrupt:
        print("\n已中断，下面汇总已完成部分")

    from collections import Counter
    tal = Counter(r["verdict"] for r in results)
    print("-" * 118)
    print("汇总: " + "  ".join(f"{VERDICTS[v]}={tal[v]}" for v in
                              ["OK", "EDGE", "IDENT", "GATE", "TEMP", "QUOTA", "DEAD", "BLOCK", "GUARD", "ERR"]
                              if tal.get(v)))

    ident_bad = [r for r in results if r["verdict"] == "IDENT"]
    if ident_bad:
        print("\n站方要求特定客户端标识（给该条目加 headers 即可，不要降 priority）:")
        for r in ident_bad:
            print(f"  {r['seg']:<22}{r['host']:<24}{r['model']:<26}pri={r['priority']:<5}{r['reason']}")
        print("  排查：同一站点用 --identity codex 与 --identity browser 各跑一次，")
        print("        哪个出 200 就把对应的头写进 config.yaml 的该条目 headers 段。")

    bad = [r for r in results if r["verdict"] in ("DEAD", "BLOCK")]
    if bad:
        print("\n需要在配置层规避的（声明了却拿不到）:")
        for r in bad:
            print(f"  {r['seg']:<22}{r['host']:<24}{r['model']:<26}pri={r['priority']:<5}{r['reason']}")

    gate = [r for r in results if r["verdict"] == "GATE"]
    if gate:
        print("\n需要你去站方后台操作的:")
        for r in gate:
            print(f"  {r['host']:<24}{r['model']:<26}{r['reason']}")

    if not args.no_html and results:
        out = Path(args.out)
        write_html(out, results, {
            "when": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "total": len(results),
            "cmd": " ".join(sys.argv),
            "cfg": str(cfg_path.resolve()),
        })
        print(f"\nHTML 报告: {out.resolve()}")
        print("下载到本机查看:  scp root@<VPS>:/opt/deploy/" + out.name + " .")


if __name__ == "__main__":
    main()
