#!/usr/bin/env python3
"""拿真实 config.yaml 演练一次全量重建，逐项对账。

    python3 tests/rehearse_real_rebuild.py /path/to/config.yaml

为什么需要它（2026-09-03）
------------------------
单元测试用的是几十行的合成配置，而这一轮改的三处（跨段新增、compat 组内
孤儿 Key、per-key proxy-url）全都只在**真实文件的形状**上才显出差别：
生产配置 13 个 provider、69 把 compat Key、8 把带 per-key proxy-url，
合成样本一个都没有。

不是测试，不进 tests/run.py —— 它要真实文件才有意义。退出码 0 = 全部对上。
"""

from __future__ import annotations

import collections
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

import yaml                                             # noqa: E402
import cpa_probe as cp                                  # noqa: E402
from cpa_probe.plan import ImportPlan, SectionPlan      # noqa: E402
from cpa_probe.writeback import _scalar_value as _scalar  # noqa: E402

SECS = ("gemini-api-key", "codex-api-key", "claude-api-key",
        "openai-compatibility")
# extract_existing_entries 返回短名，写回要全名。
FULL = {"gemini": "gemini-api-key", "codex": "codex-api-key",
        "claude": "claude-api-key", "compat": "openai-compatibility"}

_bad: list[str] = []
_ok = 0


def check(label: str, got, want) -> None:
    """对账一项。**通过时不打印值** —— 只打标签。

    为什么（2026-09-03 自查）：这个脚本拿真实 config.yaml 跑，比较的东西里有
    per-key 续行表，键是完整的 api-key。原来通过时 `print(f"...: {got}")`
    把 8 把真实 Key 整条打进终端，而终端输出会进日志、进截图、进交接文件。
    只打标签不损失任何诊断价值：值相等时它没有信息量。
    """
    global _ok
    if got == want:
        _ok += 1
        # 标量照打（条目数、计数这类），容器只报「一致」——
        # 容器里可能有凭据，且长得看不出对错。
        shown = got if isinstance(got, (int, float, bool, str)) else "一致"
        print(f"  ok  {label}: {shown}")
    else:
        # 失败时也要能排障，但不能泄密：容器只报差异的**规模与位置**。
        print(f"  FAIL {label}: {_diff_brief(got, want)}")
        _bad.append(f"{label}: {_diff_brief(got, want)}")


def _diff_brief(got, want) -> str:
    """差异摘要。标量直说，容器只报数量与差异键（键脱敏）。"""
    if isinstance(got, (int, float, bool, str)) and \
            isinstance(want, (int, float, bool, str)):
        return f"期望 {want!r}，实得 {got!r}"
    if isinstance(got, dict) and isinstance(want, dict):
        only_g = sorted(set(got) - set(want))
        only_w = sorted(set(want) - set(got))
        diff_v = sorted(k for k in set(got) & set(want) if got[k] != want[k])
        return (f"期望 {len(want)} 项、实得 {len(got)} 项；"
                f"多出 {[_mask(k) for k in only_g[:3]]}、"
                f"缺失 {[_mask(k) for k in only_w[:3]]}、"
                f"值不同 {[_mask(k) for k in diff_v[:3]]}")
    try:
        return f"期望 {len(want)} 项，实得 {len(got)} 项"
    except TypeError:
        return "类型不一致"


def _mask(key) -> str:
    """脱敏容器键。(host, api_key) 元组里的 Key 只留前 6 后 4。"""
    if isinstance(key, tuple):
        return "/".join(_mask(x) for x in key)
    s = str(key)
    return s if len(s) <= 12 else f"{s[:6]}…{s[-4:]}"


def slots(cfg: dict) -> int:
    """(凭据, 段) 槽位总数。compat 段按 api-key-entries 里的把数算。"""
    n = sum(len(cfg.get(s) or []) for s in SECS[:3])
    for prov in cfg.get("openai-compatibility") or []:
        if isinstance(prov, dict):
            n += len(prov.get("api-key-entries") or [])
    return n


def build_plans(cfg: dict, *, source: str = "probed",
                only: set[str] | None = None) -> dict:
    """把既有条目原样变成方案。only 给定时只保留那些段（模拟未勾选）。

    weight / proxy-url 必须照 server 的全量重探那条路搬运（existing_weights /
    existing_proxies）。不搬的话这份演练自己就把它们丢了，对不上账不是产品
    的问题而是演练的问题 —— 而那正好会掩盖真实的丢字段缺陷。
    """
    from cpa_probe.batch import (existing_prefixes, existing_provider_names,
                                 existing_proxies, existing_weights)

    weights = existing_weights(cfg)
    proxies = existing_proxies(cfg)
    prefixes = existing_prefixes(cfg)
    pnames = existing_provider_names(cfg)

    plans: dict = {}
    for short, base, key, orig in cp.extract_existing_entries(cfg):
        sec = FULL[short]
        if only is not None and sec not in only:
            continue
        k = (base, key)
        p = plans.get(k)
        if p is None:
            p = plans[k] = ImportPlan(host=cp.host_of(base),
                                      masked_key=key[:6] + "…",
                                      line_no=len(plans) + 1)
        models = [str(m.get("name")) for m in (orig.get("models") or [])
                  if isinstance(m, dict) and m.get("name")]
        h = cp.host_of(base)
        sp = SectionPlan(
            section=sec, base_url=base, api_key=key,
            models=models or ["claude-opus-5"],
            priority=int(orig.get("priority") or 100),
            weight=weights.get((sec, h, key)),
            proxy_url=proxies.get((sec, h, key), ""),
            model_source=source)
        if (sec, h, key) in prefixes:
            sp.prefix = prefixes[(sec, h, key)]
        if sec == "openai-compatibility":
            sp.provider_name = pnames.get(h, "")
        p.sections[sec] = sp
    return plans


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else ""
    if not path or not os.path.isfile(path):
        print("用法：python3 tests/rehearse_real_rebuild.py /path/to/config.yaml")
        return 2

    raw = io.open(path, encoding="utf-8").read()
    lines = raw.splitlines(keepends=True)
    cfg = yaml.safe_load(raw) or {}

    print(f"── ① 原样重建：一切守恒 ({os.path.basename(path)}) "
          + "─" * 12)
    plans = build_plans(cfg)
    new, warns = cp.rebuild_config_full(cfg, plans, lines)
    ok, msg = cp.validate(new)
    check("YAML 合法", ok, True)
    if not ok:
        print("    " + msg[:300])
    n2 = yaml.safe_load(new) or {}

    for s in SECS:
        check(f"{s} 条目数", len(n2.get(s) or []), len(cfg.get(s) or []))
    check("(凭据, 段) 槽位总数", slots(n2), slots(cfg))
    check("顶层键数", len(n2), len(cfg))

    # 全局键逐个 deep-equal —— 四段之外的一切必须**逐字节**没动过。
    # 第一版事故就是「排在第一个段之后的全局键全部消失」（api-keys 丢了
    # 所有客户端立刻断连），而那次 validate 报的是成功。
    check("四段之外的全局键全部一致",
          [k for k in cfg if k not in SECS and cfg[k] != n2.get(k)], [])

    # 每个条目的**每个字段** deep-equal，只豁免本次有意改的三个。
    #
    # 为什么必须逐字段比值（2026-09-03）：上一版只比字段的**出现次数**
    # （`text.count("prefix:")`），于是「121 个条目的 prefix 全被抹掉、
    # 同时注释里多出 121 处提到 prefix」这种情况两边都数得对 —— 计数相等，
    # 值全错。实测这一关抓到两处：prefix 121/121 被 dominant_prefix 覆盖、
    # compat 的 provider name 12/13 被改成 host（那是 CPA 的 provider 身份）。
    INTENT = {"priority", "models", "headers"}
    # 这两个字段「显式写空串」与「不写」在 CPA 侧完全等价：synthesizer 存的是
    # `strings.TrimSpace(...)`，proxy 判 `!= ""` 才建 transport
    # （proxy_helpers.go:34-42），prefix 走 normalizeModelPrefix 也 trim。
    # 所以比值时把 None 与 "" 归一 —— 否则 10 处 `proxy-url: ""` 会报成差异，
    # 而那是假报警（行为零差异，且紧邻的注释说明照旧保留）。
    # 其余字段不做这个归一：models 空与缺失不等价（compat 段必填），
    # weight 缺失默认 1 而不是 0。
    EMPTY_OK = {"proxy-url", "prefix"}

    def fval(entry: dict, field: str):
        v = entry.get(field)
        if field in EMPTY_OK and (v is None or (isinstance(v, str) and not v.strip())):
            return None
        return v

    def entries(c: dict) -> dict:
        out = {}
        for s in SECS[:3]:
            for e in c.get(s) or []:
                if isinstance(e, dict):
                    out[(s, cp.host_of(str(e.get("base-url") or "")),
                         str(e.get("api-key") or ""))] = e
        for pr in c.get("openai-compatibility") or []:
            if isinstance(pr, dict):
                out[("openai-compatibility",
                     cp.host_of(str(pr.get("base-url") or "")), "")] = pr
        return out

    ea, eb = entries(cfg), entries(n2)
    check("条目键集合一致", sorted(ea) == sorted(eb), True)
    fdiff = collections.Counter()
    fex: dict = {}
    for k in set(ea) & set(eb):
        for f in set(ea[k]) | set(eb[k]):
            if f in INTENT:
                continue
            if fval(ea[k], f) != fval(eb[k], f):
                fdiff[f] += 1
                fex.setdefault(f, (k[1], k[0], ea[k].get(f), eb[k].get(f)))
    check("非预期字段差异（priority/models/headers 之外）", dict(fdiff), {})
    for f, (h, s, a, b) in fex.items():
        print(f"       {f} @ {h}/{s}: {str(a)[:52]} → {str(b)[:52]}")

    # 注释：判据是「**每一种**注释都还在」，不是「行数相等」。
    #
    # 行数不可能相等，而且不该相等（2026-09-03 拿注释最全的那份文件才看清）：
    # 注释按 (段, 站) 索引，同一个站在前三段每个 Key 各占一条目 —— 原文那块
    # 说明在 gorouter 的 15 条里被复制了 15 遍（作者手工粘的），重建后按站
    # 挂一次。实测 4676 → 2452 行，其中 2224 行全是这种复制。
    #
    # 真正要守的是「有没有哪一种结论整个消失」：那才是不可逆的损失。
    # 反方向也要守 —— 重建不该让任何一种注释比原文出现得更多（那说明按站
    # 挂载写错了，同一份被复制到多个条目上）。
    cm_before = collections.Counter(l.strip() for l in lines
                                   if l.lstrip().startswith("#"))
    cm_after = collections.Counter(l.strip() for l in new.splitlines()
                                   if l.lstrip().startswith("#"))
    gone = [k for k in cm_before if k not in cm_after]
    dup = [k for k in cm_after if cm_after[k] > cm_before.get(k, 0)]
    check("完全消失的注释种类", len(gone), 0)
    if gone:
        for k in gone[:6]:
            print(f"       丢: {k[:96]}")
    check("被复制出多余份数的注释种类", len(dup), 0)
    if dup:
        for k in dup[:6]:
            print(f"       多: {cm_before.get(k,0)}→{cm_after[k]} {k[:88]}")
    print(f"  ·  注释 {sum(cm_before.values())} → {sum(cm_after.values())} 行"
          f"（{len(cm_before)} 种全部保留；减少的全是同一份被复制到同站多个"
          f"条目上的重复）")

    # 白名单外字段：按**生效行**数比，不按 `count(字段名 + ":")` 比。
    #
    # 上一版用后者，于是注释里提到这个字段名也被算进去（原文有 139 行
    # `# weight: 0` 这类注释掉的示例）—— 注释按站去重之后计数自然变，
    # 而那与「字段有没有丢」无关。weight/proxy-url 的生效项另有
    # existing_weights / existing_proxies 两张表逐项对账，见下面。
    def live(text: str, field: str) -> int:
        return sum(1 for l in text.splitlines()
                   if re.match(rf"^\s*{re.escape(field)}\s*:", l))

    def live_nonempty(text: str, field: str) -> int:
        """生效且**值非空**的行数。"""
        n = 0
        for l in text.splitlines():
            m = re.match(rf"^\s*{re.escape(field)}\s*:(.*)$", l)
            if m and _scalar(m.group(1)):
                n += 1
        return n

    for f in ("request-scoped-errors", "excluded-models", "websockets",
              "fingerprint-profile", "disabled", "weight"):
        check(f"{f} 生效行数", live(new, f), live(raw, f))
    # proxy-url 只比**非空**的。
    #
    # `proxy-url: ""` 与不写这个字段在 CPA 侧完全等价：synthesizer 存的是
    # `strings.TrimSpace(entry.ProxyURL)`，而 NewProxyAwareHTTPClient
    # （proxy_helpers.go:34-42）判 `proxyURL != ""` 才建代理 transport，否则回落
    # 全局 `cfg.ProxyURL`（这份文件里也是 `""`）。指纹里那一位也同样是空串。
    #
    # 实测原文有 10 行 `proxy-url: ""`（操作员显式记录「这个站探过 93 个节点、
    # 走代理无效」），重建后不写 —— 行为零差异，而那条结论本身在紧邻的注释里
    # 保住了。比总行数会把这 10 行算成丢失，那是假报警。
    check("proxy-url 非空生效行数",
          live_nonempty(new, "proxy-url"), live_nonempty(raw, "proxy-url"))
    empty_before = live(raw, "proxy-url") - live_nonempty(raw, "proxy-url")
    empty_after = live(new, "proxy-url") - live_nonempty(new, "proxy-url")
    if empty_before != empty_after:
        print(f'  ·  `proxy-url: ""` 空值行 {empty_before} → {empty_after}'
              f"（与不写该字段在 CPA 侧等价，行为零差异）")

    # weight / proxy-url 的**值**逐 (段, host, key) 对账 —— 行数相等还不够，
    # 跨段或跨 Key 串了值行数也不变（2026-09-03 实测踩到两次）。
    from cpa_probe.batch import existing_proxies as _ep, existing_weights as _ew
    check("weight 逐项一致", _ew(n2), _ew(cfg))
    check("proxy-url 逐项一致", _ep(n2), _ep(cfg))

    # per-key proxy-url：compat 段每把 Key 自己那条必须原样在
    kb = cp.compat_key_blocks(lines)
    kb2 = cp.compat_key_blocks(new.splitlines(keepends=True))
    per_key = {(h, k): [x.strip() for x in v]
               for h, d in kb.items() for k, v in d.items() if v}
    per_key2 = {(h, k): [x.strip() for x in v]
                for h, d in kb2.items() for k, v in d.items() if v}
    check("compat per-key 续行逐条一致", per_key2, per_key)

    print("\n── ② 只勾一个段：其余段与组内其余 Key 都不能丢 " + "─" * 12)
    plans_one = build_plans(cfg, only={"claude-api-key"})
    new3, _w3 = cp.rebuild_config_full(cfg, plans_one, lines)
    ok3, msg3 = cp.validate(new3)
    check("YAML 合法", ok3, True)
    if not ok3:
        print("    " + msg3[:300])
    n3 = yaml.safe_load(new3) or {}
    for s in SECS:
        check(f"{s} 条目数不变", len(n3.get(s) or []), len(cfg.get(s) or []))
    check("(凭据, 段) 槽位总数不变", slots(n3), slots(cfg))

    print("\n── ③ compat 只勾组内一把 Key：同组其余 Key 必须留下 " + "─" * 6)
    # 取 Key 最多的那个 provider，只让它的第一把进方案
    big = max((p for p in (cfg.get("openai-compatibility") or [])
               if isinstance(p, dict) and p.get("api-key-entries")),
              key=lambda p: len(p["api-key-entries"]), default=None)
    if big is None:
        print("  --  跳过：这份配置的 compat 段没有多 Key 的 provider")
    else:
        host = cp.host_of(str(big.get("base-url") or ""))
        want_n = len(big["api-key-entries"])
        first = str(big["api-key-entries"][0].get("api-key") or "")
        p = ImportPlan(host=host, masked_key=first[:6] + "…", line_no=1)
        p.sections["openai-compatibility"] = SectionPlan(
            section="openai-compatibility",
            base_url=str(big.get("base-url") or ""), api_key=first,
            models=[str(m.get("name")) for m in (big.get("models") or [])
                    if isinstance(m, dict) and m.get("name")]
            or ["claude-opus-5"],
            priority=int(big.get("priority") or 100), model_source="probed")
        new4, w4 = cp.rebuild_config_full(
            cfg, {(str(big.get("base-url")), first): p}, lines)
        ok4, msg4 = cp.validate(new4)
        check("YAML 合法", ok4, True)
        if not ok4:
            print("    " + msg4[:300])
        n4 = yaml.safe_load(new4) or {}
        got = next((len(x.get("api-key-entries") or [])
                    for x in (n4.get("openai-compatibility") or [])
                    if isinstance(x, dict)
                    and cp.host_of(str(x.get("base-url") or "")) == host), -1)
        check(f"{host} 的 Key 数（只勾了 1 把）", got, want_n)
        check("其余 provider 条目数不变",
              len(n4.get("openai-compatibility") or []),
              len(cfg.get("openai-compatibility") or []))
        check("组内孤儿 Key 有警告",
              any("不在本次方案内" in x for x in w4), True)
        check("(凭据, 段) 槽位总数不变", slots(n4), slots(cfg))

    print("\n── ④ 跨段新增：探测发现别的段也能用 " + "─" * 20)
    owned = cp.owned_sections(cfg)
    total = len(owned) * 4
    have = sum(len(v) for v in owned.values())
    print(f"  ·  凭据 {len(owned)} 个 × 4 段 = {total} 个组合，"
          f"实占 {have}，空位 {total - have}")

    # 给每个凭据补齐四段：seed 的一个都不该进，probed 的全该进
    def fill_all(source: str) -> dict:
        plans_x: dict = {}
        for (h, key), secs in owned.items():
            base_by_sec = {}
            for short, base, k2, _o in cp.extract_existing_entries(cfg):
                if k2 == key and cp.host_of(base) == h:
                    base_by_sec[FULL[short]] = base
            any_base = next(iter(base_by_sec.values()), f"https://{h}")
            p = ImportPlan(host=h, masked_key=key[:6] + "…",
                           line_no=len(plans_x) + 1)
            for sec in SECS:
                base = base_by_sec.get(sec) or cp.base_for_section(
                    cp.host_of(any_base), sec)
                p.sections[sec] = SectionPlan(
                    section=sec, base_url=base, api_key=key,
                    models=["claude-opus-5"], priority=100,
                    model_source=("probed" if sec not in secs else "probed")
                    if source == "probed" else
                    ("seed" if sec not in secs else "probed"))
            plans_x[(any_base, key)] = p
        return plans_x

    seed_plans = fill_all("seed")
    new5, w5 = cp.rebuild_config_full(cfg, seed_plans, lines)
    check("YAML 合法（seed 填满四段）", cp.validate(new5)[0], True)
    n5 = yaml.safe_load(new5) or {}
    check("seed 猜测不新增任何槽位", slots(n5), slots(cfg))
    check("seed 被拦下有警告", any("已跳过" in x for x in w5), True)

    probed_plans = fill_all("probed")
    new6, w6 = cp.rebuild_config_full(cfg, probed_plans, lines)
    check("YAML 合法（probed 填满四段）", cp.validate(new6)[0], True)
    n6 = yaml.safe_load(new6) or {}
    check("probed 把空位全部补上", slots(n6), total)
    check("新增有警告并写明依据",
          any("新增" in x and "本次实测通过" in x for x in w6), True)

    print("\n" + "=" * 60)
    if _bad:
        for b in _bad:
            print(f"  ✗  {b}")
        print(f"失败 {len(_bad)} 项 · 通过 {_ok} 项")
        return 1
    print(f"全部对上 · {_ok} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
