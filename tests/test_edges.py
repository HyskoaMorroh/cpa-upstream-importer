#!/usr/bin/env python3
"""写回的边界情形穷举。零外网请求。

    python3 tests/test_edges.py [config.yaml 路径]

为什么单独一个套件：写回逻辑的错都是「跑完才发现」型 ——
compat 重名 provider、撞已有站、同 Key 重复导入，三者都在真实探测后
才暴露。这里把所有已知形状一次全测，改动后立刻能看出打破了哪一项。

覆盖的形状（每一项都对应一次真实踩坑或源码语义）：
    ① 同站 100 个 Key           compat 只出 1 个 provider，claude 出 100 条
    ② 多站各多 Key              按站分组，互不串
    ③ 单 Key 单站               退化情形不能特殊化
    ④ 需要代理                  proxy-url 写在每个 key 上（compat 的结构要求）
    ⑤ 需要 headers              headers 是 provider 级，只写一次
    ⑥ compat 段不可用           不生成 compat 条目
    ⑦ claude 段不可用           不生成 claude 条目
    ⑧ 输入含完全重复行          批内判重
    ⑨ 撞已有 provider           追加进 api-key-entries，不新建同名条目
    ⑩ 撞已有站 + 多个新 Key     一次追加多个
    ⑪ 已存在的 Key 再导一次     两段都判重（含五元组撞不上的情形）
    ⑫ prefix 沿用该段主导值     gemini=GLE / codex=CDX / claude=ANT，compat 留空
"""

from __future__ import annotations

import collections
import copy
import io
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)   # 让 fixture_cfg 可导入

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

import fixture_cfg                                            # noqa: E402
import cpa_probe as cp                                        # noqa: E402
from cpa_probe.pipeline import CandidateResult, SectionVerdict  # noqa: E402
from cpa_probe.writeback import (                              # noqa: E402
    apply_diffs,
    build_diffs,
    validate,
)

_fail: list[str] = []
_pass = 0


def eq(name: str, got, want) -> None:
    global _pass
    if got != want:
        _fail.append(f"{name}\n      got  = {got!r}\n      want = {want!r}")
    else:
        _pass += 1
        print(f"  ok  {name}")


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 56 - len(title)))


MODELS = ["claude-opus-5", "claude-opus-4-8"]


def make_result(row, *, compat_ok=True, claude_ok=True,
                headers=None, proxy=False, models=None):
    """造一个探测结果。gemini/codex 固定不通 —— 那两段的形状已被别处覆盖。"""
    m = models if models is not None else MODELS
    res = CandidateResult(row=row)
    res.sections = {
        "gemini-api-key": SectionVerdict(
            section="gemini-api-key", usable=False,
            category="死路", action="分组无该模型渠道"),
        "codex-api-key": SectionVerdict(
            section="codex-api-key", usable=False,
            category="边缘", action="403 且正文为空"),
        "claude-api-key": SectionVerdict(
            section="claude-api-key", usable=claude_ok,
            base_url=row.base_for("claude-api-key"),
            models=list(m) if claude_ok else [],
            # 需不需要代理是**主机**级属性（站方的边缘防护），不是段级 ——
            # 所以两段都要带上，否则测不到 claude 段的 proxy-url 落点。
            need_proxy=proxy,
            min_headers=dict(headers or {}),
            category="可用"),
        "openai-compatibility": SectionVerdict(
            section="openai-compatibility", usable=compat_ok,
            base_url=row.base_for("openai-compatibility"),
            models=list(m) if compat_ok else [],
            min_headers=dict(headers or {}),
            need_proxy=proxy, category="可用"),
    }
    return res


def _dead_section_params() -> None:
    """判死的段只要拿到目录，方案里就得是完整参数 —— 不留待定。

    这是硬要求：方案会落进 config.yaml，priority 之类留空等于写坏配置。
    覆盖两条路：目录来源（catalog）和人工手填（forced）。
    """
    row = cp.parse_lines("https://dead.example.com,sk-dead-key").valid[0]
    res = CandidateResult(row=row)
    res.sections = {
        "claude-api-key": SectionVerdict(
            section="claude-api-key", usable=False,
            base_url=row.base_for("claude-api-key"),
            # 站方目录报得出模型，但这把 Key 的分组跑不通 —— 现场就是这形态
            catalog=["claude-opus-5", "claude-sonnet-5"],
            category="死路", action="分组无该模型渠道"),
        "gemini-api-key": SectionVerdict(
            section="gemini-api-key", usable=False,
            base_url=row.base_for("gemini-api-key"),
            category="门禁", action="只放行官方客户端"),
    }

    cfg = {"claude-api-key": [], "gemini-api-key": []}
    seen = cp.existing_fingerprints(cfg)
    plan = cp.build_plan(row, res, cfg, bands={}, seen=seen, probation=True)

    sp = plan.sections.get("claude-api-key")
    eq("目录来源的判死段进得了方案", bool(sp is not None), True)
    if sp:
        eq("模型取自目录", sp.models, ["claude-opus-5", "claude-sonnet-5"])
        eq("标注来源是目录", sp.model_source, "catalog")
        eq("priority 是确定的整数", bool(isinstance(sp.priority, int)), True)
        eq("priority 有理由", bool(bool(sp.priority_reason)), True)
        eq("headers 非空（门票不能空着写进去）", bool(bool(sp.headers)), True)
        eq("可勾选", bool(sp.writable), True)
        eq("带警告说明它没跑通",
           bool(any("目录" in w or "未跑通" in w for w in sp.warnings)), True)

    # 无目录、无手填 —— 这段该缺席，不能凭空编模型名
    # 既无目录也无手填 —— 现在用种子模型兜底进方案（严禁参数未定，
    # 所以宁可给种子也不留空档），但标注来源为 seed 且不建议写。
    sp_seed = plan.sections.get("gemini-api-key")
    eq("无目录无手填的段也进方案", bool(sp_seed is not None), True)
    if sp_seed:
        eq("标注来源是种子", sp_seed.model_source, "seed")
        eq("种子段可勾选", sp_seed.writable, True)
        eq("种子段不建议写", sp_seed.recommended, False)
        eq("种子段 priority 是确定整数",
           bool(isinstance(sp_seed.priority, int)), True)

    # 手填接管：同一个段补上模型名就该进来
    plan2 = cp.build_plan(row, res, cfg, bands={},
                          seen=cp.existing_fingerprints(cfg), probation=True,
                          force={"gemini-api-key": ["gemini-2.5-pro"]})
    sp2 = plan2.sections.get("gemini-api-key")
    eq("手填后判死段进方案", bool(sp2 is not None), True)
    if sp2:
        eq("标注来源是手填", sp2.model_source, "manual")
        eq("手填段 priority 也是确定值", bool(isinstance(sp2.priority, int)), True)
        eq("手填段 headers 非空", bool(bool(sp2.headers)), True)


class Harness:
    def __init__(self, raw: str, cfg: dict):
        self.raw = raw
        self.cfg = cfg
        self.n_provider = len(cfg.get("openai-compatibility") or [])
        self.n_claude = len(cfg.get("claude-api-key") or [])

    def run(self, text: str, *, pick=None, **kw):
        """跑一批输入，返回 (合并后的 cfg, plans, diffs, 增量)。

        `pick` 复刻服务端 /api/plan 的选择集：None = 默认（只写系统建议的段），
        "all" = 用户点了「全勾」。判死段现在也进方案且 writable，所以
        **必须**在这里剪一遍，否则测的就不是生产路径 —— server.py 的
        selected=None 分支正是退到 recommended。
        """
        rows = cp.parse_lines(text).valid
        bands: dict = {}
        seen = cp.existing_fingerprints(self.cfg)
        pairs = cp.existing_pairs(self.cfg)
        plans = [cp.build_plan(r, make_result(r, **kw), self.cfg, bands=bands,
                               seen=seen, seen_pairs=pairs) for r in rows]
        for_write = []
        for pl in plans:
            keep = {sec: sp for sec, sp in pl.sections.items()
                    if pick == "all" or sp.recommended}
            if not keep:
                continue
            shallow = copy.copy(pl)
            shallow.sections = keep
            for_write.append(shallow)
        diffs = build_diffs(self.raw, for_write)
        out = apply_diffs(self.raw, diffs)
        ok, msg = validate(out)
        new = yaml.safe_load(out)
        return {
            "rows": rows, "plans": plans, "diffs": diffs, "new": new,
            "yaml_ok": ok, "yaml_msg": msg,
            "d_provider": len(new.get("openai-compatibility") or []) - self.n_provider,
            "d_claude": len(new.get("claude-api-key") or []) - self.n_claude,
            "merged": [d for d in diffs if d.merged_into],
            "per_section": collections.Counter(d.section for d in diffs),
        }

    def compat_named(self, new: dict, name: str) -> list[dict]:
        return [e for e in (new.get("openai-compatibility") or [])
                if isinstance(e, dict) and str(e.get("name")) == name]

    def compat_by_base(self, new: dict, needle: str) -> list[dict]:
        return [e for e in (new.get("openai-compatibility") or [])
                if isinstance(e, dict) and needle in str(e.get("base-url", ""))]

    @staticmethod
    def no_dup_names(new: dict) -> list[str]:
        names = [e.get("name") for e in (new.get("openai-compatibility") or [])
                 if isinstance(e, dict)]
        return [k for k, v in collections.Counter(names).items() if v > 1]


def main() -> int:
    # 不传路径就用自带最小样本 —— 绝不回落到 ../config.yaml（生产配置）。
    # 这个套件的断言依赖样本形状（compat 有个 3 Key 的站、该站首个 Key 也在
    # claude 段且带 prefix + headers），挂在会变的生产文件上必然随它一起漂。
    cfg_path, _synthetic, _tmp = fixture_cfg.resolve(sys.argv, label="写回边界")

    raw = io.open(cfg_path, encoding="utf-8").read()
    cfg = yaml.safe_load(raw)
    h = Harness(raw, cfg)
    print(f"基线：compat {h.n_provider} 个 provider · claude {h.n_claude} 条")

    # ---------------------------------------------------------------- ①
    section("① 同站 100 个 Key")
    r = h.run("\n".join(f"https://big.example.com,sk-big{i:04d}aaaabbbbcc"
                        for i in range(100)))
    eq("YAML 校验", r["yaml_ok"], True)
    eq("claude 段 100 条（每 Key 一条）", r["d_claude"], 100)
    eq("compat 段只 +1 个 provider", r["d_provider"], 1)
    eq("compat diff 只 1 处", r["per_section"]["openai-compatibility"], 1)
    prov = h.compat_by_base(r["new"], "big.example.com")
    eq("provider 唯一", len(prov), 1)
    eq("100 个 Key 全在 api-key-entries",
       len(prov[0].get("api-key-entries") or []), 100)
    eq("模型清单不重复", len(prov[0].get("models") or []), len(MODELS))
    eq("无重名 provider", h.no_dup_names(r["new"]), [])

    # ---------------------------------------------------------------- ②
    section("② 3 站各 4 Key")
    r = h.run("\n".join(f"https://s{s}.example.com,sk-mix{s}{i:03d}aaaabbbb"
                        for s in range(3) for i in range(4)))
    eq("YAML 校验", r["yaml_ok"], True)
    eq("claude 段 12 条", r["d_claude"], 12)
    eq("compat 段 3 个 provider", r["d_provider"], 3)
    for s in range(3):
        prov = h.compat_by_base(r["new"], f"s{s}.example.com")
        eq(f"s{s} provider 唯一且 4 个 Key",
           (len(prov), len(prov[0].get("api-key-entries") or [])), (1, 4))
    eq("无重名 provider", h.no_dup_names(r["new"]), [])

    # ---------------------------------------------------------------- ③
    section("③ 单 Key 单站（退化情形）")
    r = h.run("https://solo.example.com,sk-solo0001aaaabbbbcc")
    eq("YAML 校验", r["yaml_ok"], True)
    eq("claude +1", r["d_claude"], 1)
    eq("compat +1", r["d_provider"], 1)
    prov = h.compat_by_base(r["new"], "solo.example.com")
    eq("单 Key 也走 api-key-entries",
       len(prov[0].get("api-key-entries") or []), 1)

    # ---------------------------------------------------------------- ④
    section("④ 需要代理 · proxy-url 写在每个 Key 上")
    r = h.run("\n".join(f"https://prox.example.com,sk-prox{i:04d}aaaabbbb"
                        for i in range(5)), proxy=True)
    eq("YAML 校验", r["yaml_ok"], True)
    prov = h.compat_by_base(r["new"], "prox.example.com")[0]
    ents = prov.get("api-key-entries") or []
    eq("5 个 Key", len(ents), 5)
    # compat 段的 proxy-url 在 api-key-entries 每项上，不是 provider 级
    # （config_types.go:691-701）
    eq("每个 Key 都带 proxy-url",
       sum(1 for e in ents if e.get("proxy-url")), 5)
    eq("proxy 值正确",
       {e.get("proxy-url") for e in ents}, {"http://mihomo:7890"})
    eq("provider 级没有 proxy-url", "proxy-url" in prov, False)
    cl = [e for e in r["new"]["claude-api-key"]
          if isinstance(e, dict) and "prox.example.com" in str(e.get("base-url", ""))]
    eq("claude 段生成 5 条", len(cl), 5)
    # claude 段的 proxy-url 在**条目级**（config_types.go:342-408），
    # 与 compat 段挂在 api-key-entries 上的位置不同 —— 两段不能共用渲染逻辑
    eq("claude 段 proxy-url 在条目级",
       all(e.get("proxy-url") == "http://mihomo:7890" for e in cl), True)

    # ---------------------------------------------------------------- ⑤
    section("⑤ 需要 headers · provider 级只写一次")
    r = h.run("\n".join(f"https://uas.example.com,sk-uas{i:04d}aaaabbbbc"
                        for i in range(5)),
              headers={"User-Agent": "cli-proxy-openai-compat"})
    eq("YAML 校验", r["yaml_ok"], True)
    prov = h.compat_by_base(r["new"], "uas.example.com")[0]
    eq("headers 在 provider 级",
       prov.get("headers"), {"User-Agent": "cli-proxy-openai-compat"})
    ents = prov.get("api-key-entries") or []
    eq("Key 条目里没有 headers",
       any("headers" in e for e in ents), False)
    # claude 段的 headers 也在条目级，每条都要带
    cl = [e for e in r["new"]["claude-api-key"]
          if isinstance(e, dict) and "uas.example.com" in str(e.get("base-url", ""))]
    eq("claude 段生成 5 条", len(cl), 5)
    eq("claude 段每条都带 headers",
       all(e.get("headers") == {"User-Agent": "cli-proxy-openai-compat"} for e in cl),
       True)

    # ---------------------------------------------------------------- ⑥⑦
    section("⑥ compat 段不可用")
    r = h.run("\n".join(f"https://noc.example.com,sk-noc{i:04d}aaaabbbbc"
                        for i in range(4)), compat_ok=False)
    eq("YAML 校验", r["yaml_ok"], True)
    eq("claude +4", r["d_claude"], 4)
    eq("compat 不动", r["d_provider"], 0)
    eq("compat 无 diff", r["per_section"]["openai-compatibility"], 0)

    section("⑦ claude 段不可用")
    r = h.run("\n".join(f"https://ncl.example.com,sk-ncl{i:04d}aaaabbbbc"
                        for i in range(4)), claude_ok=False)
    eq("YAML 校验", r["yaml_ok"], True)
    eq("claude 不动", r["d_claude"], 0)
    eq("compat +1", r["d_provider"], 1)

    # ---------------------------------------------------------------- ⑧
    section("⑧ 输入含完全重复行")
    r = h.run("https://dup.example.com,sk-dup0001aaaabbbbcc\n"
              "https://dup.example.com,sk-dup0001aaaabbbbcc\n"
              "https://dup.example.com,sk-dup0002aaaabbbbcc\n")
    eq("YAML 校验", r["yaml_ok"], True)
    eq("claude 只 +2（第 2 行判重）", r["d_claude"], 2)
    prov = h.compat_by_base(r["new"], "dup.example.com")[0]
    eq("compat 也只收 2 个 Key",
       len(prov.get("api-key-entries") or []), 2)
    dupes = [p for p in r["plans"]
             if p.sections.get("claude-api-key")
             and p.sections["claude-api-key"].duplicate]
    eq("有 1 个候选被判重", len(dupes), 1)

    # ---------------------------------------------------------------- ⑨⑩
    existing_prov = None
    for e in (cfg.get("openai-compatibility") or []):
        if isinstance(e, dict) and len(e.get("api-key-entries") or []) >= 3:
            existing_prov = e
            break

    if existing_prov is None:
        print("\n  -- config.yaml 里没有多 Key 的 compat provider，跳过 ⑨⑩⑪")
    else:
        pname = str(existing_prov.get("name"))
        pbase = str(existing_prov.get("base-url") or "")
        pn0 = len(existing_prov.get("api-key-entries") or [])
        # 用 provider 的 host 造输入 —— base_for 会按段补 /v1
        import re as _re
        phost = _re.sub(r"^https?://", "", pbase).split("/")[0]

        section(f"⑨ 撞已有 provider（{pname}）· 1 个新 Key")
        r = h.run(f"https://{phost},sk-brandnew01aaaabbbbcc")
        eq("YAML 校验", r["yaml_ok"], True)
        eq("走的是合并而非新建", bool(r["merged"]), True)
        eq("provider 总数不变", r["d_provider"], 0)
        eq("merged_into 记了 provider 名",
           r["merged"][0].merged_into, pname)
        prov = h.compat_named(r["new"], pname)
        eq(f"{pname} 仍唯一", len(prov), 1)
        eq("Key 数 +1", len(prov[0].get("api-key-entries") or []), pn0 + 1)
        eq("无重名 provider", h.no_dup_names(r["new"]), [])
        # 现有 provider 的其他字段不能被动
        eq("现有 models 未被改",
           len(prov[0].get("models") or []),
           len(existing_prov.get("models") or []))
        eq("现有 priority 未被改",
           prov[0].get("priority"), existing_prov.get("priority"))

        section(f"⑩ 撞已有 provider · 3 个新 Key")
        r = h.run("\n".join(f"https://{phost},sk-multi{i:04d}aaaabbbb"
                            for i in range(3)))
        eq("YAML 校验", r["yaml_ok"], True)
        eq("仍是合并", bool(r["merged"]), True)
        eq("provider 总数不变", r["d_provider"], 0)
        prov = h.compat_named(r["new"], pname)
        eq("Key 数 +3", len(prov[0].get("api-key-entries") or []), pn0 + 3)
        eq("无重名 provider", h.no_dup_names(r["new"]), [])

        # ------------------------------------------------------------ ⑪
        section("⑪ 已存在的 Key 再导一次 · 两层判重")
        # 这一项锁住的坑：现有条目常带 prefix / headers（relay-f 的 claude
        # 条目有 prefix: ANT 和一个 UA），探测方案不带那些，五元组指纹撞不上，
        # 只查五元组会把同一个凭据在同一个站重复写入。
        ekey = (existing_prov.get("api-key-entries") or [{}])[0].get("api-key", "")
        r = h.run(f"https://{phost},{ekey}")
        eq("YAML 校验", r["yaml_ok"], True)
        eq("claude 不新增", r["d_claude"], 0)
        eq("compat provider 不新增", r["d_provider"], 0)
        eq("不产生合并 diff", bool(r["merged"]), False)
        prov = h.compat_named(r["new"], pname)
        eq("Key 数不变", len(prov[0].get("api-key-entries") or []), pn0)
        p = r["plans"][0]
        for sec in ("claude-api-key", "openai-compatibility"):
            sp = p.sections.get(sec)
            if sp is not None:
                eq(f"{sec} 判重", sp.duplicate, True)
                eq(f"{sec} 不可写", sp.writable, False)
                eq(f"{sec} 有说明", bool(sp.duplicate_note), True)

        section("两层判重的分工")
        # 现有 claude 条目（带 prefix/headers）与探测方案的五元组必然不同
        cl = [e for e in (cfg.get("claude-api-key") or [])
              if isinstance(e, dict) and e.get("api-key") == ekey]
        if cl:
            ce = cl[0]
            fp_existing = cp.dedup_key(
                "claude-api-key",
                api_key=str(ce.get("api-key") or ""),
                base_url=str(ce.get("base-url") or ""),
                proxy_url=str(ce.get("proxy-url") or ""),
                prefix=str(ce.get("prefix") or ""),
                headers=ce.get("headers") or {})
            fp_probe = cp.dedup_key(
                "claude-api-key", api_key=ekey,
                base_url=str(ce.get("base-url") or ""),
                proxy_url="", prefix="", headers={})
            if ce.get("prefix") or ce.get("headers"):
                eq("五元组指纹确实撞不上（所以第二层必需）",
                   fp_existing == fp_probe, False)
            eq("(key, base) 对能撞上",
               cp.credential_pair(ekey, str(ce.get("base-url") or "")),
               cp.credential_pair(str(ce.get("api-key") or ""),
                                  str(ce.get("base-url") or "")))

    section("⑫ prefix 沿用该段主导值")
    # prefix 在 force-model-prefix: false 下是**额外加别名**不取代原名
    # （applyModelPrefixes, service_models.go:600-614）：有 prefix: ANT 时
    # 同时注册 claude-opus-5 与 ANT/claude-opus-5。
    # 所以缺它不会让站不可用，但按 ANT/xxx 发的请求就命中不到新站 ——
    # 而现有条目 gemini 段 GLE 64/64、codex CDX 63/65、claude ANT 61/65，
    # 新条目不带就成了异类。
    for sec in cp.SECTIONS:
        got = cp.dominant_prefix(cfg, sec)
        entries = [e for e in (cfg.get(sec) or []) if isinstance(e, dict)]
        counts: dict[str, int] = {}
        for e in entries:
            counts[str(e.get("prefix") or "")] = counts.get(str(e.get("prefix") or ""), 0) + 1
        best, n = max(counts.items(), key=lambda kv: kv[1]) if counts else ("", 0)
        want = best if (best and entries and n / len(entries) >= 0.7) else ""
        eq(f"{sec} 主导 prefix 判定", got, want)

    rows = cp.parse_lines("https://pfxtest.example.com,sk-pfx0001aaaabbbbcc").valid
    res = CandidateResult(row=rows[0])
    seed = {"gemini-api-key": ["gemini-2.5-pro"], "codex-api-key": ["gpt-5.6-sol"],
            "claude-api-key": ["claude-opus-5"], "openai-compatibility": ["claude-opus-5"]}
    for sec in cp.SECTIONS:
        res.sections[sec] = SectionVerdict(
            section=sec, usable=True, base_url=rows[0].base_for(sec),
            models=seed[sec], category="可用")
    plan = cp.build_plan(rows[0], res, cfg)
    for sec in cp.SECTIONS:
        eq(f"{sec} 方案带上主导 prefix",
           plan.sections[sec].prefix, cp.dominant_prefix(cfg, sec))

    d = build_diffs(raw, [plan])
    out2 = apply_diffs(raw, d)
    ok2, _ = validate(out2)
    eq("带 prefix 后 YAML 校验", ok2, True)
    new2 = yaml.safe_load(out2)
    for sec in ("gemini-api-key", "codex-api-key", "claude-api-key"):
        got = [e for e in new2[sec] if isinstance(e, dict)
               and "pfxtest.example.com" in str(e.get("base-url", ""))]
        eq(f"{sec} 落盘条目带 prefix",
           str(got[0].get("prefix") or ""), cp.dominant_prefix(cfg, sec))
    comp = [e for e in new2["openai-compatibility"] if isinstance(e, dict)
            and "pfxtest.example.com" in str(e.get("base-url", ""))]
    # 期望值从**被测的那份文件**现算，不写死。
    # 原来这里断言的是「compat 段现有全无 prefix，新条目也不加」，那是自带
    # 样本的形状；传真实 config.yaml 进来时该段可能已经有主导 prefix，
    # 断言就假失败 —— 与今天修掉的那批「基线钉在会变的文件上」同一类缺陷。
    _want_pfx = cp.dominant_prefix(cfg, "openai-compatibility")
    eq("compat 段新条目的 prefix 跟随该段主导值",
       str(comp[0].get("prefix") or ""), _want_pfx)
    # prefix 参与 CPA 的五元组指纹，必须在算 fp 前定下来
    eq("prefix 进了指纹计算",
       cp.dedup_key("claude-api-key", api_key="k", base_url="https://x.com",
                    prefix="ANT")
       != cp.dedup_key("claude-api-key", api_key="k", base_url="https://x.com"),
       True)

    section("原文件")
    eq("config.yaml 逐字节未变",
       io.open(cfg_path, encoding="utf-8").read(), raw)
    if _tmp:
        import shutil
        shutil.rmtree(_tmp, ignore_errors=True)

    section("判不可用的段：勾选后参数不许留「未定」")
    _dead_section_params()

    print("\n" + "=" * 62)
    if _fail:
        print(f"失败 {len(_fail)} 项 / 通过 {_pass} 项\n")
        for f in _fail:
            print(f"  ✗ {f}\n")
        return 1
    print(f"全部通过 · {_pass} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
