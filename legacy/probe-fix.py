#!/usr/bin/env python3
"""诊断并给出处置建议：代理能否救活 / 最小必需标识头 / 是否静默换模。

与 audit-upstreams.py 的分工
---------------------------
audit-upstreams.py  判定「通不通」，一次只能用一个标识档案。
本脚本              对每个组合追问三件事，直接产出可写进 config.yaml 的处置：

  1. 基线（UA + Originator）不通  -> 经代理再试一次。
     代理能通就该给该条目加 proxy-url，而不是 weight: 0。
  2. 基线通 -> 用更省的标识组合回退测试，找出【最小必需头】。
     若仅 Originator 就够，写死它不受 Codex 客户端版本升级影响。
  3. 返回 200 -> 比对响应 model 字段，不一致即【静默换模】。
     照常计费却给你另一个模型，比不可用更危险。

处置优先级（与用户要求一致）：proxy-url > headers > 降 priority > weight: 0

用法（在 /opt/deploy 下执行）
    python3 probe-fix.py                              # 全量
    python3 probe-fix.py --only relay-c           # 只测某站，子串匹配
    python3 probe-fix.py --only relay-g,relay-a       # 多站用逗号分隔
    python3 probe-fix.py --proxy http://127.0.0.1:7890
    python3 probe-fix.py --no-proxy                   # 跳过代理对比
    python3 probe-fix.py --gap 3 --models 2           # 间隔秒数 / 每站模型数
    python3 probe-fix.py --json out.json              # 结构化结果另存

只读：本脚本不修改任何配置，只打印建议。
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

TIMEOUT = 25
# 探测负载。不能用 "hi" 之类短问候语 —— 2026-08-29 实测 relay-g.example 会拦：
#   400 反测活已拦截本次请求：短消息命中测活探针关键词（如 hi、你好等）
# 用 "hi" 时测到的是站方反探测机制，不是真实可用性。换成正常技术问句。
PROBE_TEXT = ("Reply with one short sentence: what is the difference between "
              "a hash map and a tree map?")
DEFAULT_PROXY = "http://127.0.0.1:7890"

# 读满响应体。/v1/responses 把整个 Codex 系统提示放在 instructions 字段里，
# 实测单条响应 40KB+，而 model 字段排在它之后。截断会导致误判换模。
MAX_BODY = 4 * 1024 * 1024


UA_CODEX = ("codex_vscode/0.150.0-alpha.12.2 (Windows 10.0.28000; x86_64) "
            "unknown (VS Code; 26.825.31414)")
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")
# CPA 开启 cloaking 时强制覆盖成的值（codex_executor_request.go:26-27）。
# 2026-08-28 07:43 的六组对照里，F 组用的就是这组「故障时原始值」，当时全部 200。
UA_TUI = ("codex-tui/0.146.0 (Mac OS 26.5.0; arm64) iTerm.app/3.6.10 "
          "(codex-tui; 0.146.0)")

# 每个站点默认用哪个标识档案，与 audit-upstreams.py 的 SITE_IDENTITY 保持一致。
# 早先版本对所有站硬编码 codex UA，导致 relay-l 的 6 个组合被误判为「门禁 403」——
# 而 audit 用 browser UA 测同样的组合是 200/2.0s。站方要的标识不同，不能一套打全场。
SITE_IDENTITY = {
    "relay-c.example": "codex",
    "relay-f.example": "browser",
    "relay-l.example": "browser",
    # jdw 与 relay-f / relay-l 同一站长、站点类型相同，同样按 browser 处理。
    "relay-h.example": "browser",
}

IDENTITIES = {
    "codex": {"User-Agent": UA_CODEX, "Originator": "codex_vscode"},
    "browser": {"User-Agent": UA_BROWSER},
    "none": {},
}

# CPA 各协议段实际发出的 User-Agent（源码核对）。
# 「最小必需头」的基线必须是这个，而不是「完全不发 UA」——
# 后者在生产里根本不存在，据此得出的结论对配置没有指导意义。
CPA_DEFAULT_UA = {
    # openai_compat_executor.go:155 硬编码，凭证级 headers 可覆盖
    "openai-compatibility": "cli-proxy-openai-compat",
    # 透传客户端 UA，缺失时回落 CLIProxyAPI/<ver>
    "claude-api-key": "CLIProxyAPI/6.0",
    # applyCodexCloakingHeaders 在 disable-codex-cloaking: false 时强制覆盖成 codex-tui；
    # 当前配置为 true，走透传链，故这里用客户端真实值
    "codex-api-key": UA_CODEX,
    # Go transport 默认，不设置
    "gemini-api-key": None,
}


def site_identity(host: str, override: str | None = None) -> dict[str, str]:
    """该站点应当携带的标识头。"""
    profile = override or SITE_IDENTITY.get(host, "browser")
    return dict(IDENTITIES.get(profile, IDENTITIES["browser"]))


def cpa_baseline_headers(kind: str) -> dict[str, str]:
    """CPA 在该协议段上实际会发出的标识头，用作最小必需头的下界。"""
    ua = CPA_DEFAULT_UA.get(kind)
    return {"User-Agent": ua} if ua else {}


def fallback_combos(t: dict) -> list[tuple[str, dict]]:
    """按该站点应有的标识，生成由省到全的回退序列。

    第一个仍然 200 的组合即为最小必需头。originator-only 排在 ua-only 之前，
    因为 Originator 不含版本号，写死后不受客户端版本升级影响。
    第一档是「CPA 现状」——若它就通过，说明该条目根本不需要加 headers。
    """
    combos: list[tuple[str, dict]] = [("cpa-现状", cpa_baseline_headers(t["kind"]))]
    prof = SITE_IDENTITY.get(t["host"], "browser")
    if prof == "codex":
        combos.append(("originator-only", {"Originator": "codex_vscode"}))
        combos.append(("ua-only(codex)", {"User-Agent": UA_CODEX}))
    else:
        combos.append(("ua-only(browser)", {"User-Agent": UA_BROWSER}))
    return combos



# F 组：cpa-atlas.html 记录 2026-08-28 07:43 用这组值得到 200，而故障时段同样的值
# 拿到 401，据此判定「变量不在我们这一侧」并撤回了 cloaking 根因结论。
# 但 2026-08-28 16:xx 复测 --identity none/browser 全部 401、codex 全部 200，
# 与那次结论冲突。本组用于区分两种解释：
#   F 也 200  -> 站方按时间/额度波动，加 headers 治不了根本
#   F 仍 401  -> 站方确实只认 codex_vscode，加 headers 是正解
FGROUP = ("f-group(codex-tui)", {"User-Agent": UA_TUI, "Originator": "codex-tui"})

# Originator 值矩阵。回答「站方是认某个特定值，还是只看这个头存在」。
# 结论直接决定 config.yaml 里该写死一个值，还是用 $Originator 动态复制：
#   全部 200        -> 只看头存在，随便填，最稳
#   部分 200        -> 白名单，写死名单里的值
#   仅 codex_vscode -> 只认这一个，换客户端也不受影响（headers 是 Set 覆盖）
ORIGINATOR_MATRIX = [
    ("(无此头)", None),
    ("codex_vscode", "codex_vscode"),
    ("codex-tui", "codex-tui"),
    ("codex_cli", "codex_cli"),
    ("claude-cli", "claude-cli"),
    ("垃圾串", "zzz-not-a-real-client-9931"),
    ("空字符串", ""),
]



SECTION_KINDS = ("gemini-api-key", "codex-api-key", "claude-api-key",
                 "openai-compatibility")


def host_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", (url or "").strip())
    return m.group(1) if m else (url or "").strip()


def rank_models(models: list[str]) -> list[str]:
    """优先测主力模型，避免把预算花在边缘模型上。"""
    pri = ("gpt-5.6-sol", "claude-opus-5", "claude-opus-4-8", "gemini-2.5-pro")
    return sorted(models, key=lambda m: (pri.index(m) if m in pri else len(pri), m))


def build_request(kind: str, base: str, model: str, key: str,
                  probe_text: str = PROBE_TEXT) -> tuple[str, dict, dict]:
    """返回 (url, headers, body)。base 按配置原样，不做 /v1 猜测。

    协议路径与 CPA 自身行为一致，见 audit-upstreams.py 的同名函数。
    """
    base = (base or "").rstrip("/")
    h = {"Content-Type": "application/json"}
    if kind == "gemini-api-key":
        url = f"{base}/v1beta/models/{model}:generateContent?key={key}"
        body = {"contents": [{"role": "user", "parts": [{"text": probe_text}]}]}
    elif kind == "codex-api-key":
        url = f"{base}/responses"
        h["Authorization"] = f"Bearer {key}"
        body = {"model": model, "stream": False, "input": probe_text}
    elif kind == "claude-api-key":
        url = f"{base}/v1/messages"
        h["Authorization"] = f"Bearer {key}"
        h["anthropic-version"] = "2023-06-01"
        body = {"model": model, "max_tokens": 32,
                "messages": [{"role": "user", "content": probe_text}]}
    else:  # openai-compatibility
        url = f"{base}/chat/completions"
        h["Authorization"] = f"Bearer {key}"
        body = {"model": model, "max_tokens": 32,
                "messages": [{"role": "user", "content": probe_text}]}
    return url, h, body



def fetch(url: str, headers: dict, body: dict, proxy: str | None) -> tuple[str, str]:
    """返回 (status, body)。status 为 '000' 表示连接失败或超时。"""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            return str(r.status), r.read(MAX_BODY).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return str(e.code), e.read(MAX_BODY).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 - 超时/DNS/TLS 统一归为连接失败
        return "000", f"{type(e).__name__}: {e}"



def resp_model(text: str) -> str | None:
    """从响应正文取出实际使用的模型名。各协议字段位置不同，逐个尝试。"""
    try:
        d = json.loads(text)
    except Exception:  # noqa: BLE001
        m = re.search(r'"model"\s*:\s*"([^"]+)"', text)
        return m.group(1) if m else None
    for path in (("model",), ("response", "model"), ("modelVersion",)):
        cur = d
        for k in path:
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(k)
        if isinstance(cur, str) and cur:
            return cur
    return None


def resp_id(text: str) -> str | None:
    """取响应 id。它比 model 字段可靠 —— 上游能改 model 名，但 id 由真实后端生成。"""
    m = re.search(r'"id"\s*:\s*"((?:msg|resp|chatcmpl)[^"]{0,60})"', text)
    return m.group(1) if m else None


def backend_of(rid: str | None) -> str:
    """按响应 id 形态推断真实后端。口径来自 cpa-atlas.html 的实测对照表：

      msg_01 + base58        Anthropic 官方
      msg_bdrk_*             AWS Bedrock
      msg_ + 32位 hex        中转自造
      chatcmpl-*             OpenAI Chat 兼容层（多为中转）
      resp_ + 长十六进制串   OpenAI Responses 官方形态
      resp_ + 24位 hex       中转自造（实测 agnes-2.0-flash 即此形态）
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



def model_matches(requested: str, actual: str | None) -> bool:
    """判断响应模型是否与请求一致。

    容忍三类合法差异：CPA 的 prefix/alias、thinking 后缀、日期版本后缀
    （claude-opus-5 -> claude-opus-5-20260401）。其余视为静默换模。

    actual 为 None（拿不到 model 字段）时返回 True —— 没有证据就不判换模。
    这一点很重要：早先版本把「解析不到」当成「换了」，产生过 100% 的假换模率。
    """
    if not actual:
        return True
    req = requested.split("/")[-1].strip().lower()
    act = actual.strip().lower()
    if req == act:
        return True
    req_base = re.sub(r"-(thinking|latest)$", "", req)
    act_base = re.sub(r"-(thinking|latest)$", "", act)
    if req_base == act_base:
        return True
    # claude-opus-5 vs claude-opus-5-20260401
    return act_base.startswith(req_base) or req_base.startswith(act_base)



def classify(status: str, body: str) -> tuple[str, str]:
    """粗分类，仅用于决定下一步怎么追问。细分类见 audit-upstreams.py。

    判定顺序不可随意调整：额度与反探测的关键词必须排在状态码分支之前，
    因为同一个 403 既可能是 Cloudflare 拦截，也可能是余额耗尽 ——
    2026-08-29 实测 relay-l 的 403 正文是 quota 类，早先版本按 403 归到
    「门禁 站方策略拒绝」，误导成需要找站方，实际只需充值。
    关键词与 audit-upstreams.py:283 的 QUOTA 判定对齐。
    """
    low = (body or "").lower()
    stripped = (body or "").strip()

    # 余额/配额耗尽。可出现在 402，也可出现在 403。
    if re.search(r"budget pool|quota has been exhausted|insufficient_user_quota|"
                 r"insufficient_quota|quota_exceeded|预扣费额度失败|"
                 r"user quota is not enough", body or "", re.I):
        return "余额", "余额或配额耗尽，需充值"
    if "额度已经达到上限" in (body or "") or "额度已达上限" in (body or ""):
        return "余额", "模型额度达上限"

    # 站点级反探测保护：需降频或换探测文本，不是配置问题。
    if "bulk probe" in low or "bulk model probing" in low:
        return "限频", "触发站点反探测保护，加大 --gap 后重试"
    if "反测活" in (body or "") or "测活探针" in (body or ""):
        return "反测活", "站方拦截探测式短消息，需用 --probe-text 换正常问句"


    # 站方敏感词拦截，配置层无解。
    if "sensitive_words" in low:
        return "死路", "站方级敏感词拦截"

    if status == "200":
        return "通", "可用"
    if status == "000":
        return "异常", "连接失败或超时"
    if status == "401":
        return "标识", "认证或客户端标识被拒"
    if status == "403":
        if not stripped:
            return "边缘", "403 正文为空，Cloudflare 边缘概率拦截"
        # CPA 会无条件给 codex 请求注入 image_generation 工具
        # （codex_executor_request.go:455），不支持该工具的上游一律 403。
        # 这不是站方策略问题，改 disable-image-generation 即可。
        if "image generation" in low or "image_generation" in low:
            return "注入", "CPA 注入 image_generation 被上游拒（改 disable-image-generation）"
        if any(k in low for k in ("challenge-platform", "cf-mitigated", "cdn-cgi",
                                  "访问已被拦截", "安全验证", "no-js ie6 oldie",
                                  "attention required", "just a moment")):
            return "IP封", "Cloudflare 拦截页"
        return "门禁", "站方策略拒绝"

    if status in ("500", "502", "503", "504", "522", "520", "524"):
        if any(k in low for k in ("无可用渠道", "no available channel", "分组",
                                  "model_not_found", "可用渠道不存在")):
            return "死路", "分组无该模型渠道"
        if "负载已经达到上限" in (body or "") or "负载已达上限" in (body or ""):
            return "临时", "模型负载上限，稍后可用"
        return "临时", f"上游 {status}，临时故障"
    if status == "404":
        return "死路", "该端点不提供此模型或路径不存在"
    if status == "400":
        if "1m" in low or "上下文" in low:
            return "门禁", "需在站方后台启用 1m 上下文"
        if "group platform is not" in low or "api key group" in low:
            m = re.search(r"group platform is not (\w+)", body or "", re.I)
            need = f"，需 {m.group(1)} 分组" if m else ""
            return "死路", f"Key 分组不匹配该协议{need}"
        if "invalid endpoint" in low or "path must contain" in low:
            return "死路", "该协议路径不受支持"
        return "死路", "请求被拒（分组或参数不匹配）"
    if status == "402":
        return "余额", "预算池耗尽，需充值"
    if status == "429":
        return "限流", "触发速率限制"
    return "其他", f"HTTP {status}"



def collect_targets(cfg: dict, limit_models: int, only: list[str],
                    include_disabled: bool = False) -> list[dict]:
    """展开 (段, 站点, 模型, Key) 组合。

    默认跳过 disabled 与 weight<=0 的条目 —— 那些是有意排除的，不该占探测预算。
    include_disabled=True 时一并纳入，用于定性「为什么当初把它禁掉」，
    例如 relay-e 在 compat 段 disabled、在 codex 段 weight: 0，默认两处都测不到。

    同一 (段, 站点, 模型) 只保留一个凭证：诊断的是站点行为，不是逐个 Key 查余额。
    同站多 Key 会让「余额」类结果重复 N 遍，把真正的信号淹掉。
    """
    out: list[dict] = []
    seen: set[tuple] = set()



    def want(host: str) -> bool:
        return (not only) or any(s.lower() in host.lower() for s in only)

    def models_of(entry: dict) -> list[str]:
        names = []
        for m in entry.get("models") or []:
            if isinstance(m, dict):
                n = (m.get("alias") or m.get("name") or "").strip()
                if n:
                    names.append(n)
            elif isinstance(m, str) and m.strip():
                names.append(m.strip())
        return rank_models(names)[:limit_models]

    for kind in ("gemini-api-key", "codex-api-key", "claude-api-key"):
        for idx, entry in enumerate(cfg.get(kind) or []):
            if not isinstance(entry, dict):
                continue
            skipped = None
            if entry.get("disabled") is True:
                skipped = "disabled"
            else:
                w = entry.get("weight")
                if isinstance(w, (int, float)) and w <= 0:
                    skipped = f"weight={w}"
            if skipped and not include_disabled:
                continue
            base = (entry.get("base-url") or "").strip()
            key = (entry.get("api-key") or "").strip()
            if not base or not key or not want(host_of(base)):
                continue
            for model in models_of(entry):
                dedup = (kind, host_of(base), model)
                if dedup in seen:
                    continue
                seen.add(dedup)
                out.append({"kind": kind, "label": kind, "host": host_of(base),
                            "base": base, "model": model, "key": key,
                            "prio": entry.get("priority"), "idx": idx,
                            "prefix": entry.get("prefix"),
                            "has_headers": bool(entry.get("headers")),
                            "proxy_url": entry.get("proxy-url"),
                            "excluded": skipped})



    for idx, prov in enumerate(cfg.get("openai-compatibility") or []):
        if not isinstance(prov, dict):
            continue
        skipped = "disabled" if prov.get("disabled") is True else None
        if skipped and not include_disabled:
            continue
        base = (prov.get("base-url") or "").strip()
        name = (prov.get("name") or f"#{idx}").strip()
        if not base or not want(host_of(base)):
            continue
        keys = []
        for e in prov.get("api-key-entries") or []:
            if isinstance(e, dict):
                w = e.get("weight")
                if isinstance(w, (int, float)) and w <= 0 and not include_disabled:
                    continue
                k = (e.get("api-key") or "").strip()
                if k:
                    keys.append(k)
        if not keys and prov.get("api-key"):
            keys = [str(prov["api-key"]).strip()]
        if not keys:
            continue
        for model in models_of(prov):
            dedup = ("openai-compatibility", host_of(base), model)
            if dedup in seen:
                continue
            seen.add(dedup)
            out.append({"kind": "openai-compatibility", "label": f"compat:{name}",
                        "host": host_of(base), "base": base, "model": model,
                        "key": keys[0], "prio": prov.get("priority"), "idx": idx,
                        "prefix": prov.get("prefix"),
                        "has_headers": bool(prov.get("headers")),
                        "proxy_url": prov.get("proxy-url"),
                        "excluded": skipped})
    return out




def probe_one(t: dict, headers_extra: dict, proxy: str | None,
              probe_text: str = PROBE_TEXT) -> dict:
    url, h, body = build_request(t["kind"], t["base"], t["model"], t["key"],
                                 probe_text)
    h.update(headers_extra)
    t0 = time.time()
    status, text = fetch(url, h, body, proxy)
    rid = resp_id(text) if status == "200" else None
    m = re.search(r'"(?:input_tokens|prompt_tokens)"\s*:\s*(\d+)', text or "")
    return {"status": status, "body": text, "secs": round(time.time() - t0, 1),
            "actual_model": resp_model(text) if status == "200" else None,
            "resp_id": rid, "backend": backend_of(rid),
            "tokens": int(m.group(1)) if m else None}



def body_excerpt(text: str, limit: int = 400) -> str:
    """把非 200 正文压成一行摘要。HTML 抽标题，JSON 抽 message，其余截断。"""
    s = (text or "").strip()
    if not s:
        return "(正文为空)"
    if s.lstrip().lower().startswith(("<!doctype", "<html")):
        title = re.search(r"<title[^>]*>([\s\S]{0,120}?)</title>", s, re.I)
        marks = [k for k in ("challenge-platform", "cf-mitigated", "cf-ray",
                             "Attention Required", "Just a moment", "访问已被拦截",
                             "安全验证", "cdn-cgi") if k.lower() in s.lower()]
        return (f"[HTML {len(s)}B] title={title.group(1).strip() if title else '?'}"
                f"  特征={marks or '无'}")
    for pat in (r'"message"\s*:\s*"([^"]{0,300})',
                r'"error"\s*:\s*"([^"]{0,300})',
                r'"detail"\s*:\s*"([^"]{0,300})'):
        m = re.search(pat, s)
        if m:
            return m.group(1)
    return re.sub(r"\s+", " ", s)[:limit]



def diagnose(t: dict, args, gap: float) -> dict:
    """对单个组合完成四问：基线 -> 换模 -> 最小头/F 组 -> 代理。

    基线用该站点应有的标识（SITE_IDENTITY），不是一套 codex UA 打全场。
    """
    r = dict(t)
    ident = site_identity(t["host"])
    r["identity"] = SITE_IDENTITY.get(t["host"], "browser")

    # 第一问：基线
    pt = getattr(args, "probe_text", "") or PROBE_TEXT
    base_res = probe_one(t, ident, None, pt)
    r["status"] = base_res["status"]
    r["secs"] = base_res["secs"]
    verdict, reason = classify(base_res["status"], base_res["body"])
    r["verdict"], r["reason"] = verdict, reason
    r["actual_model"] = base_res["actual_model"]
    r["resp_id"] = base_res["resp_id"]
    r["backend"] = base_res["backend"]
    r["tokens"] = base_res["tokens"]
    r["min_headers"] = None
    r["f_group"] = None
    r["proxy_status"] = None
    r["proxy_secs"] = None
    r["excerpt"] = None if base_res["status"] == "200" else body_excerpt(base_res["body"])
    r["proxy_excerpt"] = None


    # 第二问先判：200 就检查是否换模（换模比通更该处置）
    if base_res["status"] == "200":
        if not model_matches(t["model"], base_res["actual_model"]):
            r["verdict"] = "换模"
            r["reason"] = (f"请求 {t['model']} 实返 {base_res['actual_model']}"
                           f"（后端={base_res['backend']}）")
        # 第三问：找最小必需头
        if not args.no_minimize:
            for name, combo in fallback_combos(t):
                time.sleep(gap)
                res = probe_one(t, combo, None, pt)
                if res["status"] == "200" and model_matches(t["model"],
                                                            res["actual_model"]):
                    r["min_headers"] = name
                    break
            if r["min_headers"] is None:
                r["min_headers"] = f"需完整 {r['identity']} 标识"
        return r


    # 401 专项：跑 F 组。用「故障时原始值」区分站方波动与真标识门禁。
    if base_res["status"] == "401" and not args.no_fgroup:
        time.sleep(gap)
        fres = probe_one(t, FGROUP[1], None, pt)
        r["f_group"] = fres["status"]
        if fres["status"] == "200":
            r["verdict"] = "标识歧义"
            r["reason"] = (f"codex_vscode 拿 401，codex-tui 拿 200 —— "
                           f"站方并非只认 codex_vscode")

    # 第四问：代理能否救活
    if not args.no_proxy and verdict in ("IP封", "异常", "边缘", "临时", "标识"):
        time.sleep(gap)
        pres = probe_one(t, ident, args.proxy, pt)
        r["proxy_status"] = pres["status"]
        r["proxy_secs"] = pres["secs"]
        if pres["status"] != "200":
            r["proxy_excerpt"] = body_excerpt(pres["body"])
        if pres["status"] == "200":
            if model_matches(t["model"], pres["actual_model"]):
                r["verdict"] = "代理可救"
                r["reason"] = f"直连 {base_res['status']}，经代理 200（{pres['secs']}s）"
            else:
                r["verdict"] = "换模"
                r["reason"] = (f"代理下请求 {t['model']} 实返 "
                               f"{pres['actual_model']}（后端={pres['backend']}）")
        elif verdict == "IP封" and pres["status"] not in ("403", "000"):
            # 403 CF 挑战页变成别的码 = 代理已穿透 Cloudflare，
            # 剩下的是上游应用层问题（分组无渠道等），不再是 IP 封禁。
            r["verdict"] = "代理穿透"
            r["reason"] = (f"直连 403 CF，经代理转为 {pres['status']}"
                           f" —— 已穿透 CF，余下为应用层问题")
    return r




HEADER_SNIPPET = {
    "originator-only": '    headers:\n      Originator: "codex_vscode"',
    "ua-only(codex)": ('    headers:\n      User-Agent: '
                       f'"{UA_CODEX}"'),
    "ua-only(browser)": ('    headers:\n      User-Agent: '
                         f'"{UA_BROWSER}"'),
}



def run_originator_matrix(targets: list[dict], gap: float,
                          probe_text: str = PROBE_TEXT) -> None:
    """对每个 (段, 站点, 模型) 逐个试 ORIGINATOR_MATRIX 里的值，判断站方认什么。"""
    print("Originator 值矩阵：判断站方是认特定值，还是只看该头是否存在")
    print(f"待测 {len(targets)} 个组合 × {len(ORIGINATOR_MATRIX)} 个值 = "
          f"{len(targets) * len(ORIGINATOR_MATRIX)} 次请求，间隔 {gap}s\n")

    for t in targets:
        print(f"=== {t['label']}  {t['host']}  {t['model']}  pri={t.get('prio')}")
        results: list[tuple[str, str]] = []
        for i, (label, val) in enumerate(ORIGINATOR_MATRIX):
            extra = {"User-Agent": UA_CODEX}
            if val is not None:
                extra["Originator"] = val
            res = probe_one(t, extra, None, probe_text)
            verdict, _ = classify(res["status"], res["body"])
            results.append((label, res["status"]))
            print(f"    {label:<16}{res['status']:>5}  {verdict}")
            if i < len(ORIGINATOR_MATRIX) - 1:
                time.sleep(gap)

        oks = [lab for lab, st in results if st == "200"]
        no_header_ok = any(lab == "(无此头)" and st == "200" for lab, st in results)
        junk_ok = any(lab in ("垃圾串", "空字符串") and st == "200"
                      for lab, st in results)

        print("  判读: ", end="")
        if not oks:
            print("全部未通过 —— 本轮无法区分（可能额度耗尽或其他门禁在先）")
            print("        排除该因素后再跑，例如充值后、或 --only 换一个可用站")
        elif no_header_ok:
            print("不带该头也 200 —— 站方不按 Originator 鉴权，无需在配置里加")
        elif junk_ok:
            print("任意值都 200 —— 站方只检查该头是否存在")
            print('        写法：Originator: "codex_vscode"（值不敏感，永不过期）')
        elif len(oks) == 1:
            print(f"仅 {oks[0]} 通过 —— 站方只认这一个值")
            print(f'        写法：Originator: "{oks[0]}"')
            print("        headers 是 Set 覆盖，换客户端不影响"
                  "（header_helpers.go:81-94）")
        else:
            print(f"白名单：{', '.join(oks)}")
            print(f'        写法：Originator: "{oks[0]}"，其余为备选')
        print()
        if t is not targets[-1]:
            time.sleep(gap)



def report(rows: list[dict], args) -> None:
    W = "{:<10}{:<20}{:<22}{:<24}{:>6}{:>6}{:>7}  {}"
    print(W.format("判定", "段", "站点", "模型", "pri", "码", "耗时", "说明"))
    print("-" * 132)
    for r in rows:
        print(W.format(r["verdict"], r["label"][:19], r["host"][:21],
                       str(r["model"])[:23], str(r.get("prio") or "-"),
                       r["status"], f"{r['secs']}s", r["reason"][:42]))
    print("-" * 132)

    tally: dict[str, int] = {}
    for r in rows:
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
    print("汇总: " + "  ".join(f"{k}={v}" for k, v in sorted(tally.items())))

    def group(pred) -> list[dict]:
        return [r for r in rows if pred(r)]

    # 后端指纹表：id 形态比 model 字段可靠，上游能改 model 名但改不了 id 生成方式
    g = group(lambda r: r.get("resp_id"))
    if g:
        print("\n【0】响应 id 指纹（判断真实后端，口径见 cpa-atlas 实测表）:")
        seen = set()
        for r in g:
            k = (r["host"], r["backend"])
            if k in seen:
                continue
            seen.add(k)
            tk = f"  tokens={r['tokens']:,}" if r.get("tokens") else ""
            print(f"  {r['host']:<22}{r['backend']:<28}{str(r['resp_id'])[:34]}{tk}")

    # 1. 代理可救 —— 最高优先处置，不要 weight: 0
    g = group(lambda r: r["verdict"] == "代理可救")
    if g:
        print("\n【1】经代理可恢复（优先于降权，不要 weight: 0）:")
        for r in g:
            cur = r.get("proxy_url") or "(未设)"
            print(f"  {r['label']:<20}{r['host']:<22}{r['model']:<24}"
                  f"pri={r.get('prio')}  {r['reason']}")
            print(f"      当前 config 里该条目 proxy-url = {cur}")
        unset = [r for r in g if not r.get("proxy_url")]
        if unset:
            print("\n  尚未设置 proxy-url 的条目需补一行（缩进 4 空格，priority 之后）:")
            print('    proxy-url: "http://mihomo:7890"   # CPA 在容器内，用容器名')
        else:
            print("\n  以上条目 config 里都已设 proxy-url，无需再改；本节仅确认代理有效。")


    # 2. 标识歧义 —— 与 cpa-atlas 的六组对照冲突，需人工定夺
    g = group(lambda r: r["verdict"] == "标识歧义")
    if g:
        print("\n【2a】标识结论冲突（codex_vscode 401 但 codex-tui 200）:")
        for r in g:
            print(f"  {r['label']:<20}{r['host']:<22}{r['model']:<24}{r['reason']}")
        print("  说明：站方不是只认 codex_vscode。cpa-atlas 记录 2026-08-28 07:43")
        print("  六组对照（含故障时原始值）全部 200，判为站方侧波动并撤回 cloaking 根因。")
        print("  此时加 headers 未必治本，建议同时查 402 额度：")
        print("    grep -c 'Budget pool' logs/cli-proxy-api/error-*.log")

    # 2b. 标识问题 —— 给最小必需头。cpa-现状 已通过的不需要任何改动。
    g = group(lambda r: r["verdict"] == "通" and r.get("min_headers")
              and r["min_headers"] != "cpa-现状")
    if g:
        print("\n【2b】需要客户端标识（加 headers，不要降 priority）:")
        by_combo: dict[str, list[dict]] = {}
        for r in g:
            by_combo.setdefault(r["min_headers"], []).append(r)
        for combo, items in by_combo.items():
            hosts = sorted({f"{i['label']}/{i['host']}" for i in items})
            print(f"\n  最小必需头 = {combo}")
            for h in hosts:
                print(f"    {h}")
            print(HEADER_SNIPPET.get(combo, "    （需完整标识，见下）"))
            if combo == "originator-only":
                print("  Originator 不含版本号，客户端升级后无需再改。")
                print("  headers 是 Set 覆盖（header_helpers.go:81-94），"
                      "换客户端也不受影响。")

    ok_asis = group(lambda r: r["verdict"] == "通" and r.get("min_headers") == "cpa-现状")
    if ok_asis:
        print(f"\n【2c】CPA 当前发出的标识已足够，无需加 headers（{len(ok_asis)} 项）:")
        seen2c = set()
        for r in ok_asis:
            k = (r["label"], r["host"])
            if k in seen2c:
                continue
            seen2c.add(k)
            print(f"  {r['label']:<20}{r['host']:<22}"
                  f"（站点标识档案={r.get('identity')}）")


    # 3. 换模 —— 比不可用更危险
    g = group(lambda r: r["verdict"] == "换模")
    if g:
        print("\n【3】静默换模（照常计费却返回别的模型，建议 disabled: true）:")
        for r in g:
            print(f"  {r['label']:<20}{r['host']:<22}{r['model']:<24}{r['reason']}")
        print("  已知同类：betterclau -> agnes-2.0-flash（token 289 vs 4393）、"
              "relay-e -> grok-4.6（已 disabled）")

    # 3b. CPA 注入 image_generation 被拒 —— 改一个全局开关即可，与站方无关
    g = group(lambda r: r["verdict"] == "注入")
    if g:
        print("\n【3b】CPA 注入的 image_generation 被上游拒绝:")
        for r in g:
            print(f"  {r['label']:<20}{r['host']:<22}{r['model']:<24}码={r['status']}")
        print("  成因：codex_executor_request.go:455 无条件把 "
              '{"type":"image_generation"} 追加进 tools。')
        print("  处置（config.yaml 顶层单行，热重载生效）:")
        print('    disable-image-generation: "chat"')
        print("  语义：非 images 端点不再注入，/v1/images/generations 与 "
              "/v1/images/edits 仍可用。")
        print("  注意：这些 403 与站方策略无关，不要因此降 priority 或 weight: 0。")


    # 4. 代理穿透了 CF 但仍不通 —— 剩下是应用层问题，不是 IP 封禁
    g = group(lambda r: r["verdict"] == "代理穿透")
    if g:
        print("\n【4a】代理已穿透 Cloudflare，余下为应用层问题:")
        for r in g:
            print(f"  {r['label']:<20}{r['host']:<22}{r['model']:<24}"
                  f"直连=403 代理={r.get('proxy_status')}  {r['reason'][:44]}")
        print("  处置：仍应加 proxy-url（CF 已绕过），再按代理下的错误码处理")
        print("  503=该分组无此模型（改 models 列表）  400=参数或分组不匹配")

    # 4b. 代理也救不了 —— 才轮到降权
    g = group(lambda r: r["verdict"] in ("IP封", "异常", "边缘")
              and r.get("proxy_status") not in (None, "200"))
    if g:
        print("\n【4b】直连与代理都不通（此时才 weight: 0 并降 priority）:")
        for r in g:
            ps = r.get("proxy_status") or "未测"
            print(f"  {r['label']:<20}{r['host']:<22}{r['model']:<24}"
                  f"直连={r['status']} 代理={ps}  {r['reason']}")


    # 5. 站方后台 / 充值
    g = group(lambda r: r["verdict"] in ("门禁", "余额", "限频", "限流"))
    if g:
        print("\n【5】需站方后台操作、充值或降频（配置层解决不了）:")
        seen5 = set()
        for r in g:
            k = (r["host"], r["verdict"], r["reason"])
            if k in seen5:
                continue
            seen5.add(k)
            same = [x for x in g if (x["host"], x["verdict"], x["reason"]) == k]
            n = f"  ×{len(same)}" if len(same) > 1 else ""
            print(f"  {r['verdict']:<6}{r['label']:<20}{r['host']:<22}"
                  f"{r['model']:<22}{r['reason']}{n}")
        if any(r["verdict"] == "余额" for r in g):
            print("  余额类不要 weight: 0 —— 充值后自行恢复。要临时避开只降 priority。")
        if any(r["verdict"] == "限频" for r in g):
            print("  限频类是本脚本探测过密触发的，加大 --gap 重跑即可，不是配置问题。")


    # 6. 死路 —— 改 models 列表
    g = group(lambda r: r["verdict"] == "死路")
    if g:
        print("\n【6】声明了却拿不到（从该条目 models 列表移除）:")
        seen6 = set()
        for r in g:
            k = (r["label"], r["host"], r["model"])
            if k in seen6:
                continue
            seen6.add(k)
            print(f"  {r['label']:<20}{r['host']:<22}{r['model']:<24}{r['reason']}")

    # 7. 非 200 的正文摘要 —— 定性「门禁」「边缘」「IP封」到底是什么
    if args.dump_body:
        g = [r for r in rows if r.get("excerpt")]
        if g:
            print("\n【7】非 200 正文摘要（用于定性，HTML 抽标题+CF 特征）:")
            for r in g:
                print(f"  {r['verdict']:<8}{r['label']:<20}{r['host']:<22}"
                      f"{r['model']:<22}码={r['status']}")
                print(f"      直连: {r['excerpt'][:180]}")
                if r.get("proxy_excerpt"):
                    print(f"      代理({r.get('proxy_status')}): "
                          f"{r['proxy_excerpt'][:180]}")





def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--only", default="",
                    help="只测站点，子串匹配，逗号分隔。例：--only relay-g,relay-a")
    ap.add_argument("--models", type=int, default=2, help="每条目最多测几个模型（默认 2）")
    ap.add_argument("--gap", type=float, default=3.0,
                    help="请求间隔秒数（默认 3，防触发反探测）")
    ap.add_argument("--proxy", default=DEFAULT_PROXY,
                    help=f"对比用代理地址（默认 {DEFAULT_PROXY}）")
    ap.add_argument("--no-proxy", action="store_true", help="跳过代理对比")
    ap.add_argument("--no-minimize", action="store_true", help="跳过最小必需头回退测试")
    ap.add_argument("--no-fgroup", action="store_true",
                    help="跳过 401 的 F 组对照（默认开，用于区分站方波动与真标识门禁）")
    ap.add_argument("--originator-matrix", action="store_true",
                    help="只跑 Originator 值矩阵：判断站方认特定值还是只看头存在")
    ap.add_argument("--include-disabled", action="store_true",
                    help="连 disabled / weight<=0 的条目也测，用于定性当初为何禁用")
    ap.add_argument("--probe-text", default="",
                    help="自定义探测文本。默认是一句技术问句；某些站（relay-g）会拦 hi/你好")
    ap.add_argument("--dump-body", action="store_true",
                    help="输出非 200 的正文摘要（HTML 抽标题与 CF 特征），用于定性 403")
    ap.add_argument("--json", default="", help="结构化结果另存为 JSON")

    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        sys.exit(f"找不到配置文件：{cfg_path}")
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    only = [s.strip() for s in args.only.split(",") if s.strip()]
    targets = collect_targets(cfg, args.models, only, args.include_disabled)
    if not targets:
        sys.exit("没有匹配的组合（检查 --only，或全部条目已 disabled / weight<=0）")

    if args.originator_matrix:
        run_originator_matrix(targets, args.gap,
                              args.probe_text or PROBE_TEXT)
        return

    # 预估请求数：基线 1 + 可能的最小头回退 1-3 + 可能的 F 组 1 + 可能的代理 1

    est = len(targets) * (1 + (0 if args.no_minimize else 2)
                          + (0 if args.no_fgroup else 1)
                          + (0 if args.no_proxy else 1))
    print(f"待诊断 {len(targets)} 个组合，约 {est} 次请求，间隔 {args.gap}s，"
          f"预计约 {int(est * (args.gap + 2) / 60) + 1} 分钟")
    print(f"代理: {'跳过' if args.no_proxy else args.proxy}   "
          f"最小头探测: {'跳过' if args.no_minimize else '开'}   "
          f"F 组对照: {'跳过' if args.no_fgroup else '开'}")
    print()


    rows = []
    for i, t in enumerate(targets, 1):
        rows.append(diagnose(t, args, args.gap))
        if i < len(targets):
            time.sleep(args.gap)

    report(rows, args)

    if args.json:
        slim = [{k: v for k, v in r.items() if k not in ("key", "body")} for r in rows]
        Path(args.json).write_text(json.dumps(slim, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"\nJSON 结果: {args.json}")


if __name__ == "__main__":
    main()
