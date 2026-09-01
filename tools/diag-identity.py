#!/usr/bin/env python3
"""客户端画像排查：哪些站要求特定客户端身份，要的是哪一套。

    # 查一个还没进 config.yaml 的站（决定要不要加它之前用这个）
    docker exec -i upstream-importer python3 /app/tools/diag-identity.py \
        --url https://anyrouter.top --key sk-xxx --section claude-api-key

    # 容器内跑（VPS 上没有源码，用这条）
    docker exec -i upstream-importer python3 /app/tools/diag-identity.py /data/config.yaml

    # 只查一个段
    docker exec -i upstream-importer python3 /app/tools/diag-identity.py \
        /data/config.yaml --section claude-api-key

只读：不改 config.yaml，不写任何文件。每站只取**一个** Key 与一个模型，
按画像从省到全各打一次，第一个过了门禁就停 —— 要的是「最小必需画像」，
不是穷举。

为什么需要这个工具（2026-08-31 实测）
------------------------------------
探测把某站四段全判死，而该站在 config.yaml 里已配好、面板显示 207 次成功。
逐项剔除后测出它的门票是两项**缺一不可**：

    anthropic-beta      含 claude-code-20250219
    metadata.user_id    且格式为 user_<64hex>_account_<uuid>_session_<uuid>

而 pipeline 的 identity_combos **四段共用一份 codex 形态**（User-Agent +
Originator），claude 段一个对的都没有 —— 那五种组合全试也过不去。

更要紧的是 metadata.user_id 是**请求体字段**，而 identity_combos 只能返回
headers，现有抽象压根表达不了它。所以这里用「画像」：一个画像同时改 headers
与 body，且带模板变量（user_id 必须随 Key 变，写死等于所有 Key 共用一个
假身份，站方一眼看穿）。

排查结果用来定 pipeline 的画像表。别拿一个站的结论推广到 13 个站 ——
我在这上面已经错过两次（先说要补标识头，又说 TLS 层根本复制不了）。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from cpa_probe import classify, client, mask_key, request      # noqa: E402
from cpa_probe.parse import SECTIONS, host_of                  # noqa: E402


# ── 画像表 ──────────────────────────────────────────────────────────
#
# 每个画像 = 一组 headers + 一份 body 补丁。按段分开 —— Originator 是 codex
# 独有的头，对 claude / gemini 段毫无意义，四段共用一份是原来的缺陷。
#
# body 补丁里的模板变量在发请求时按当前 Key 求值：
#   {key_hash}  该 Key 的 sha256 前 64 位十六进制
#   {uuid1..3}  每次请求新生成的 uuid4
# metadata.user_id 必须随 Key 变 —— 写死等于所有 Key 共用一个假身份。

_CC_UA = "claude-cli/2.0.14 (external, cli)"
_CC_BETA = ("claude-code-20250219,oauth-2025-04-20,"
            "interleaved-thinking-2025-05-14,"
            "fine-grained-tool-streaming-2025-05-14,"
            "context-management-2025-06-27")
_CC_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
_CC_UID = "user_{key_hash}_account_{uuid1}_session_{uuid2}"

# X-Stainless 是 Anthropic 官方 SDK 的指纹头族。实测**不是**门票（去掉仍过），
# 但有的站可能查，所以留作 full 画像的一部分。
_STAINLESS = {
    "X-Stainless-Lang": "js",
    "X-Stainless-Runtime": "node",
    "X-Stainless-Runtime-Version": "v22.14.0",
    "X-Stainless-Package-Version": "0.60.0",
    "X-Stainless-OS": "Linux",
    "X-Stainless-Arch": "x64",
    "X-Stainless-Async": "false",
    "X-Stainless-Retry-Count": "0",
}

_CODEX_UA = "codex_vscode/1.0.0"
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# (画像名, headers, body 补丁)。顺序即尝试顺序：由省到全，第一个过就停。
PROFILES: dict[str, list[tuple[str, dict, dict]]] = {
    "claude-api-key": [
        ("baseline", {}, {}),
        # 实测的最小必需集：这两项缺一不可
        ("cc-min", {"user-agent": _CC_UA, "anthropic-beta": _CC_BETA},
         {"metadata": {"user_id": _CC_UID}}),
        # 再加 x-app / system 标记 / Accept
        ("cc-mid", {"user-agent": _CC_UA, "anthropic-beta": _CC_BETA,
                    "x-app": "cli", "Accept": "application/json"},
         {"metadata": {"user_id": _CC_UID},
          "system": [{"type": "text", "text": _CC_SYSTEM}]}),
        # 全套：加 X-Stainless 族与 session 头
        ("cc-full", {"user-agent": _CC_UA, "anthropic-beta": _CC_BETA,
                     "x-app": "cli", "Accept": "application/json",
                     "X-Claude-Code-Session-Id": "{uuid3}", **_STAINLESS},
         {"metadata": {"user_id": _CC_UID},
          "system": [{"type": "text", "text": _CC_SYSTEM,
                      "cache_control": {"type": "ephemeral"}}],
          "stream": True}),
    ],
    "codex-api-key": [
        ("baseline", {}, {}),
        ("ua-only", {"User-Agent": _CODEX_UA}, {}),
        ("originator-only", {"Originator": "codex_vscode"}, {}),
        ("codex-full", {"User-Agent": _CODEX_UA, "Originator": "codex_vscode"}, {}),
        ("browser-ua", {"User-Agent": _BROWSER_UA}, {}),
    ],
    "gemini-api-key": [
        ("baseline", {}, {}),
        # gemini 段原来给的是 Originator（codex 的头），毫无意义。
        # 这里试真实客户端会带的：gemini-cli 的 UA，以及浏览器 UA。
        ("gemini-cli-ua", {"User-Agent": "gemini-cli/0.4.0"}, {}),
        ("browser-ua", {"User-Agent": _BROWSER_UA}, {}),
    ],
    "openai-compatibility": [
        ("baseline", {}, {}),
        ("openai-sdk", {"User-Agent": "OpenAI/Python 1.59.0",
                        "X-Stainless-Lang": "python"}, {}),
        ("browser-ua", {"User-Agent": _BROWSER_UA}, {}),
        # compat 段也可能被 claude 形态的门禁拦（同一站两段共用一个分组）
        ("cc-min", {"user-agent": _CC_UA, "anthropic-beta": _CC_BETA},
         {"metadata": {"user_id": _CC_UID}}),
    ],
}


def _render(obj, key: str):
    """把模板变量按当前 Key 求值。深拷贝，不改原表。"""
    ctx = {
        "key_hash": hashlib.sha256(key.encode("utf-8")).hexdigest()[:64],
        "uuid1": str(uuid.uuid4()),
        "uuid2": str(uuid.uuid4()),
        "uuid3": str(uuid.uuid4()),
    }
    if isinstance(obj, str):
        out = obj
        for k, v in ctx.items():
            out = out.replace("{" + k + "}", v)
        return out
    if isinstance(obj, dict):
        return {k: _render(v, key) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_render(v, key) for v in obj]
    return obj


# 门禁特征：正文说「只允许某某客户端」。判据不能只看状态码 ——
# 实测那个站回的是 503，而 503 在 classify 里是「临时」（可用、该重试），
# 于是探测白重试两次，而重试永远不可能过。
_GATE_RE = re.compile(
    r"only allows? [\w\s-]*clients?"
    r"|restricted to [\w\s-]*clients?"
    r"|client[\s_-]?not[\s_-]?allowed"
    r"|unauthorized client"
    r"|仅(?:支持|允许)[^，。]{0,20}客户端",
    re.I)


def gated(body: str) -> bool:
    return bool(_GATE_RE.search(body or ""))


def probe_one(section: str, base: str, key: str, model: str,
              prof: tuple[str, dict, dict], timeout: int):
    """打一次。返回 (状态码, 类别, 是否被门禁挡, 正文摘要)。"""
    name, hdrs, patch = prof
    url, headers, body = request.build_request(
        section, base, model, key,
        extra_headers=_render(hdrs, key) or None)
    if patch:
        body.update(_render(patch, key))
    r = client.send(url, headers=headers, body=json.dumps(body).encode("utf-8"),
                    method="POST", timeout=timeout)
    cat, _why = classify(r.status, r.body)
    return r.status, cat, gated(r.body), (r.body or "")[:150].replace("\n", " ")


def collect(cfg: dict) -> dict[str, dict]:
    """按 (站, 段) 收一个代表性的 (key, model)。每站只测一个 Key。"""
    out: dict[str, dict] = {}
    for sec in ("gemini-api-key", "codex-api-key", "claude-api-key"):
        for e in cfg.get(sec) or []:
            if not isinstance(e, dict):
                continue
            base = str(e.get("base-url") or "")
            key = str(e.get("api-key") or "")
            if not base or not key:
                continue
            models = [str((m or {}).get("name") or "") for m in (e.get("models") or [])]
            models = [m for m in models if m]
            if not models:
                continue
            h = host_of(base)
            out.setdefault(h, {}).setdefault(sec, (key, base, models[0]))

    for p in cfg.get("openai-compatibility") or []:
        if not isinstance(p, dict):
            continue
        base = str(p.get("base-url") or "")
        ents = [k for k in (p.get("api-key-entries") or []) if isinstance(k, dict)]
        models = [str((m or {}).get("name") or "") for m in (p.get("models") or [])]
        models = [m for m in models if m]
        if not base or not ents or not models:
            continue
        key = str(ents[0].get("api-key") or "")
        if key:
            out.setdefault(host_of(base), {}).setdefault(
                "openai-compatibility", (key, base, models[0]))
    return out


def main() -> int:
    args = sys.argv[1:]
    want_sec = ""
    if "--section" in args:
        i = args.index("--section")
        if i + 1 < len(args):
            want_sec = args[i + 1]
    timeout = 40
    if "--timeout" in args:
        i = args.index("--timeout")
        if i + 1 < len(args):
            timeout = max(5, int(args[i + 1]))

    # --url / --key：不经 config.yaml 直接查一个站。
    #
    # 为什么需要这个入口：这个工具原本只从 config.yaml 读站点，而「这个站要
    # 什么头」恰恰是在**决定是否加进 config.yaml 之前**要回答的问题。
    # 手工先写进配置再查，等于为了诊断去改生产文件。
    #
    # 取值不能用「第一个非 - 开头的参数」那套 —— `--url` 后面漏了值时，
    # 那种写法会静默回落去读默认 config.yaml 并真发请求（自查发现）。
    # 这里显式检查下一个参数存在且不是另一个选项。
    def _opt(flag: str) -> str | None:
        if flag not in args:
            return None
        i = args.index(flag)
        if i + 1 >= len(args) or args[i + 1].startswith("-"):
            return ""          # 给了选项但没给值 —— 与「没给选项」区分开
        return args[i + 1]

    direct_url = _opt("--url")
    direct_key = _opt("--key")

    if direct_url is not None or direct_key is not None:
        if not direct_url or not direct_key:
            print("用法：--url <站点地址> --key <密钥>  （两者都必须有值）")
            print("例：  --url https://api.example.com --key sk-xxx "
                  "--section claude-api-key")
            return 2
        from cpa_probe.parse import parse_lines
        res = parse_lines(f"{direct_url},{direct_key}")
        if not res.valid:
            why = res.invalid[0].error if res.invalid else "解析失败"
            print(f"解析不了：{why}")
            return 2
        row = res.valid[0]
        secs = [want_sec] if want_sec else list(SECTIONS)
        # 每段取该段的第一个种子模型 —— 与 pipeline 的 SEED_MODELS 同源，
        # 不另猜一套。
        from cpa_probe.pipeline import SEED_MODELS
        sites = {
            row.host: {
                s: (row.api_key, row.base_for(s), SEED_MODELS[s][0])
                for s in secs
            }
        }
        return _run(sites, secs, timeout)

    # 位置参数（config.yaml 路径）。放在 --url 分支之后取，避免把
    # `--section claude-api-key` 的值当成路径。
    path = ""
    skip = set()
    for flag in ("--section", "--timeout", "--url", "--key"):
        if flag in args:
            skip.add(args.index(flag) + 1)
    for i, a in enumerate(args):
        if i in skip or a.startswith("-"):
            continue
        path = a
        break
    if not path:
        path = os.path.join(os.path.dirname(ROOT), "config.yaml")
    if not os.path.isfile(path):
        print(f"找不到 {path}")
        return 2
    try:
        import yaml
    except ImportError:
        print("需要 PyYAML")
        return 2

    cfg = yaml.safe_load(io.open(path, encoding="utf-8").read()) or {}
    sites = collect(cfg)
    secs = [want_sec] if want_sec else list(SECTIONS)
    return _run(sites, secs, timeout)


def _run(sites: dict, secs: list, timeout: int) -> int:

    n_req = sum(len(PROFILES.get(s, [])) for h in sites for s in sites[h] if s in secs)
    print(f"站点 {len(sites)} 个 · 段 {', '.join(secs)}")
    print(f"最多 {n_req} 次请求（第一个过门禁就停，实际更少）· 只读，不改配置\n")

    # (站, 段) -> 最小必需画像名 或 None
    need: dict[tuple[str, str], str] = {}
    gate_hit: list[tuple[str, str]] = []
    # 连不上的 (站, 段)。与「不需要画像」必须分开报 —— 混在一起会让
    # 超时的站看起来像「baseline 就够」。
    unreachable: set[tuple[str, str]] = set()

    for host in sorted(sites):
        for sec in secs:
            got = sites[host].get(sec)
            if not got:
                continue
            key, base, model = got
            print(f"── {host}  {sec}  [{model}]  {mask_key(key)}")
            passed = None
            for prof in PROFILES.get(sec, []):
                name = prof[0]
                try:
                    st, cat, g, tail = probe_one(sec, base, key, model, prof, timeout)
                except Exception as ex:                       # noqa: BLE001
                    print(f"     {name:16} ERR  {type(ex).__name__}")
                    continue
                flag = "门禁" if g else ("通" if st == "200" else cat)
                print(f"     {name:16} {st:4} {flag:6} {tail[:88]}")
                if g:
                    gate_hit.append((host, sec))
                    continue
                if st == "000":
                    # 连接失败：没收到任何响应，**不能**据此判定画像够用。
                    # 冒烟测试抓到的缺陷 —— 假域名全部 000，结论却写成
                    # 「baseline 就够」。真实环境里站点超时会被误报为
                    # 「不需要画像」，那比不给结论更糟。
                    unreachable.add((host, sec))
                    continue
                # 收到了响应且没被门禁挡住 —— 后面的 502/403 是站方自身状态，
                # 与客户端身份无关，继续试更全的画像没有意义。
                passed = name
                break
            need[(host, sec)] = passed

    print("\n" + "=" * 74)
    print("结论：每个 (站, 段) 过门禁所需的最小画像")
    print("=" * 74)
    by_prof: dict[str, list[str]] = {}
    for (host, sec), prof in sorted(need.items()):
        if prof:
            tag = prof
        elif (host, sec) in unreachable:
            tag = "连不上 —— 无法判定"
        else:
            tag = "全部画像都被门禁挡"
        by_prof.setdefault(tag, []).append(f"{host}/{sec.replace('-api-key','')}")
    for prof, items in sorted(by_prof.items()):
        print(f"\n  {prof}  ({len(items)} 个)")
        for it in items:
            print(f"     {it}")

    gated_sites = sorted({h for h, _ in gate_hit})
    print(f"\n出现过客户端门禁的站：{len(gated_sites)} 个")
    for h in gated_sites:
        print(f"   {h}")
    if not gated_sites:
        print("   （无 —— 说明 baseline 就够，identity 画像对这批站不是必需）")

    print("\n提示：baseline 之外的画像才需要写进 config.yaml 的 headers；")
    print("      body 补丁（metadata.user_id）CPA 自己会补，条目里不用写。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
