#!/usr/bin/env python3
"""定档算法的回归测试。零外网请求、零文件写入。

    python3 tests/test_tiering.py [config.yaml 路径]

为什么单独一个套件
------------------
用户 2026-08-30 反馈：「之前就是这个项目分析后很多只有几十的优先级，
不晓得是如何分析的，差距特别大」。

复现确认了三个叠加的缺陷，每一个都让定档更保守，合起来把满分候选压到 12：

  ① 目标函数错
     `_shadow_count` 等权计数所有下层站，于是「不挡任何站」成了优化目标，
     必然收敛到最低可插档。而「挡住」在 CPA 里只是「排在后面」——
     只有顶层被抢才是真损害。

  ② tie-break 反向
     `if n < best_shadow or (n == best_shadow and mid < best)`
     挡站数相同时取更低值。挡 0 站的 850 与挡 0 站的 25 打平后选 25。
     后果：score=100 与 score=60 给出同一个值，**分数彻底失效**。

  ③ 不看现存站的健康度
     gemini 段下层 49 个站**全部实测不可用**（逐站 503/401/403/404），
     却被当成「要保护的现有站」。为了不挡死站而把可用新站压到 12。

     健康信号有两个来源，原来一个都没读：
       weight: 0        强信号，CPA 的 selector 已把它整个剔除
       注释里的实测结论  弱信号，是两夜排障的唯一记录

  ④ 别名匹配漏判（修 ③ 时踩到的新坑）
     注释写人读短名，配置里是域名，两者不保证有公共子串：
       jdw -> relay-h.example   （jdw ≠ relay-h）
       sm       -> relay-m.example
     第一版用「短名是域名的点分标签」匹配，jdw 静默漏判 ——
     不报错，只让定档悄悄变保守，极难发现。
     修法：用 openai-compatibility 段的 `name` 字段建权威别名表。

这些全是**静默**降级：不抛异常、不失败，只是给出过低的档位。
前 560 项测试一项都没抓到，因为它们只验「档位落在空档内」这类结构性质，
不验「档位是否合理」。
"""

from __future__ import annotations

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

import fixture_cfg                                       # noqa: E402
import cpa_probe as cp                                   # noqa: E402
from cpa_probe.plan import (                             # noqa: E402
    Band,
    _dead_shadowed,
    _shadow_count,
    unhealthy_from_comments,
)

_pass = 0
_fail: list[str] = []


def section(title: str) -> None:
    print(f"── {title} " + "─" * max(0, 58 - len(title)))


def eq(label: str, got, want) -> None:
    global _pass
    if got == want:
        _pass += 1
        print(f"  ok  {label}")
    else:
        _fail.append(f"{label}\n      期望 {want!r}\n      实得 {got!r}")
        print(f"  FAIL {label}")


def truthy(label: str, got, hint: str = "") -> None:
    global _pass
    if got:
        _pass += 1
        print(f"  ok  {label}")
    else:
        _fail.append(f"{label}\n      实得 {got!r} {hint}")
        print(f"  FAIL {label}")


def mkband(section_name: str, tiers: dict[int, list[str]], model: str,
           *, dead: set[str] | None = None,
           unhealthy: set[str] | None = None,
           alias: dict[str, str] | None = None) -> Band:
    """手搓一个 Band。tiers = {priority: [host, ...]}"""
    b = Band(section=section_name)
    b.tiers = sorted(tiers, reverse=True)
    b.hosts_at = {p: sorted(h) for p, h in tiers.items()}
    b.model_top = {model: b.tiers[0]}
    b.model_tiers = {model: {p: sorted(h) for p, h in sorted(tiers.items(), reverse=True)}}
    b.dead_hosts = dead or set()
    b.unhealthy_hosts = unhealthy or set()
    b.alias = alias or {}
    return b


def main() -> int:
    # ── ① 分数必须真的影响档位 ──────────────────────────────────────
    section("① 分数必须影响档位（原来 100 与 60 给同一个值）")
    # 一个宽敞的档位谱：顶层 1000，下面全是死站 —— 新站该拿高档
    b = mkband("codex-api-key",
               {1000: ["top.example"], 800: ["dead1.example"],
                400: ["dead2.example"], 100: ["dead3.example"]},
               "m1",
               dead={"dead1.example", "dead2.example", "dead3.example"})
    vals = {}
    for sc in (100, 80, 60, 40, 20):
        v, note = cp.suggest_priority(b, sc, models=["m1"])
        vals[sc] = v
    truthy(f"不同分数给出不同档位（{vals}）",
           len(set(vals.values())) > 1,
           "分数不影响结果说明 probation 分支丢弃了 by_score")
    truthy(f"高分档位 >= 低分档位（100->{vals[100]}, 20->{vals[20]}）",
           vals[100] >= vals[20])

    # ── ② 挡住死站零代价，不该压低档位 ──────────────────────────────
    section("② 挡住死站零代价")
    b_all_dead = mkband("codex-api-key",
                        {900: ["alive.example"], 500: ["d1.example"],
                         200: ["d2.example"], 50: ["d3.example"]},
                        "m1",
                        dead={"d1.example", "d2.example", "d3.example"})
    eq("挡 3 个死站算 0 个在用站",
       _shadow_count(b_all_dead, ["m1"], 600), 0)
    eq("_dead_shadowed 数得出那 3 个",
       len(_dead_shadowed(b_all_dead, ["m1"], 600)), 3)
    v, note = cp.suggest_priority(b_all_dead, 100, models=["m1"])
    truthy(f"满分候选拿到高档（实得 {v}）", v >= 500,
           "下层全是死站却仍压到最低档 —— 就是用户报的那个问题")
    truthy("理由说明「其下 N 个站已实测不可用」", "无代价" in note, note)

    # ── ③ 活站必须被保护 ────────────────────────────────────────────
    section("③ 活站必须被保护（不能为了拿高档就压过在用站）")
    b_alive = mkband("claude-api-key",
                     {1000: ["top.example"], 500: ["alive2.example"],
                      100: ["alive3.example"]},
                     "m1")            # 没有 dead / unhealthy，全是活站
    eq("挡 2 个活站就算 2 个",
       _shadow_count(b_alive, ["m1"], 700), 2)
    # 这个谱只有两个空档：750 挡 2 站、300 挡 1 站 —— 没有「挡 0 站」的选项。
    # 真实约束是**挡活站数最少**，不是「绝不挡活站」（那样往往无解）。
    v, note = cp.suggest_priority(b_alive, 100, models=["m1"])
    eq(f"取代价最小的那一档（300 挡 1 站，750 挡 2 站）", v, 300)
    truthy("理由说明为什么没取更高档", "没直接取" in note, note)
    # 有「挡 0 站」选项时必须选它，即便它比某个挡站的档更低。
    # 造一个三档谱：顶层 1000、活站 500、活站 100 —— 但把 100 那个标成死站，
    # 于是 300 档变成零代价，而 750 仍挡 1 个活站（500 那个）。
    b_zero = mkband("claude-api-key",
                    {1000: ["top.example"], 500: ["alive2.example"],
                     100: ["dead.example"]},
                    "m1", dead={"dead.example"})
    vz, nz = cp.suggest_priority(b_zero, 100, models=["m1"])
    eq("零代价档（300）胜过挡 1 站的更高档（750）", vz, 300)
    truthy("理由标明不挡在用站", "不挡任何**在用**的站" in nz, nz)

    # ── ④ 不劫持顶层 ────────────────────────────────────────────────
    section("④ 不劫持顶层（层级隔离下抢顶层=完全取代）")
    v, note = cp.suggest_priority(b_alive, 100, models=["m1"])
    truthy(f"档位低于顶层 1000（实得 {v}）", v < 1000)
    # 多模型：取各模型顶层的**最小**值当天花板
    b_multi = mkband("claude-api-key", {1000: ["a.example"], 50: ["b.example"]}, "m1")
    b_multi.model_top = {"m1": 1000, "m2": 120}
    b_multi.model_tiers["m2"] = {120: ["c.example"], 30: ["d.example"]}
    v2, _n = cp.suggest_priority(b_multi, 100, models=["m1", "m2"])
    truthy(f"多模型时不越过最低那个顶层 120（实得 {v2}）", v2 <= 120,
           "取 max 会让 m2 的顶层被整个换掉")

    # ── ⑤ 别名表：注释短名 vs 配置域名 ──────────────────────────────
    section("⑤ 别名匹配（jdw -> relay-h.example 这类）")
    alias = {"jdw": "relay-h.example", "sm": "relay-m.example",
             "relay-a": "relay-a.example", "relay-e": "relay-e.example",
             "relay-f": "relay-f.example"}
    for name, host in alias.items():
        eq(f"{name} 匹配 {host}",
           cp.host_matches_note(host, {name}, alias), True)
    # 反例：不能误匹配
    eq("relay-h 不误匹配 relay-l.example",
       cp.host_matches_note("relay-l.example", {"jdw"}, alias), False)
    eq("sm 不误匹配 relay-m.example.ai（别名表里是 relay-m.example）",
       cp.host_matches_note("other.example", {"sm"}, alias), False)
    # 没有别名表时的兜底：仍能匹配共享标签的
    eq("无别名表时 relay-f 仍匹配 relay-f.example",
       cp.host_matches_note("relay-f.example", {"relay-f"}, None), True)

    section("⑤ 别名表从 openai-compatibility 的 name 字段建")
    cfg_fake = {"openai-compatibility": [
        {"name": "jdw", "base-url": "https://relay-h.example/v1"},
        {"name": "sm", "base-url": "https://relay-m.example/v1"},
        {"name": "", "base-url": "https://noname.example/v1"},
        {"name": "nobase"},
    ]}
    m = cp.name_alias_map(cfg_fake)
    eq("解析出 2 项（空 name 与无 base-url 都跳过）", len(m), 2)
    eq("name 归一化为小写", m.get("jdw"), "relay-h.example")
    eq("sm 对上 relay-m.example", m.get("sm"), "relay-m.example")

    # ── ⑥ 注释解析：按段隔离 ────────────────────────────────────────
    section("⑥ 注释里的实测结论按段隔离")
    raw = "\n".join([
        "gemini-api-key:",
        "# relay-f：实测 503 No available channel",
        '  - api-key: "k1"',
        '    base-url: "https://relay-f.example"',
        "    priority: 30",
        "claude-api-key:",
        "# relay-f：实测 200，3.6 秒",
        '  - api-key: "k2"',
        '    base-url: "https://relay-f.example"',
        "    priority: 900",
        "other-key: 1",
    ])
    g = unhealthy_from_comments(raw, "gemini-api-key")
    c = unhealthy_from_comments(raw, "claude-api-key")
    truthy(f"gemini 段判 relay-f 不可用（{sorted(g)}）", "relay-f" in g)
    truthy(f"claude 段不判它不可用（{sorted(c)}）", "relay-f" not in c,
           "同一个站在不同段的结论完全不同，跨段套用是错的")

    section("⑥ 各种「不可用」注释形态都要认")
    forms = [
        "# xxx：实测 503 No available channel",
        "# xxx：实测 403 WAF 按 IP 拦截",
        "# xxx：实测 401 unauthorized client",
        "# xxx：实测 400 Model name not specified",
        "# xxx 永久排除（2026-08-27）：本站会静默重映射模型",
        "# xxx：站点级不可用",
        "# xxx：实测 500 not implemented",
        "# xxx：实测 404 model_not_found",
        "# xxx：实测 403 分组权限被回收",
    ]
    for f in forms:
        raw2 = f"claude-api-key:\n{f}\nother: 1"
        got = unhealthy_from_comments(raw2, "claude-api-key")
        truthy(f"认得「{f[:34]}…」", "xxx" in got, f"实得 {got}")
    # 反例：「实测 200」不该被当成不可用
    raw3 = "claude-api-key:\n# xxx：实测 200，3.6 秒\nother: 1"
    eq("「实测 200」不判为不可用",
       "xxx" in unhealthy_from_comments(raw3, "claude-api-key"), False)

    # ── ⑦ weight: 0 的解析 ──────────────────────────────────────────
    section("⑦ weight: 0 是强信号（selector 已把它剔除）")
    cfg_w = {"claude-api-key": [
        {"api-key": "a", "base-url": "https://dead.example", "priority": 900,
         "weight": 0},
        {"api-key": "b", "base-url": "https://dead.example", "priority": 900,
         "weight": 0},
        {"api-key": "c", "base-url": "https://mixed.example", "priority": 800,
         "weight": 0},
        {"api-key": "d", "base-url": "https://mixed.example", "priority": 800},
        {"api-key": "e", "base-url": "https://live.example", "priority": 700},
    ]}
    bw = cp.build_band(cfg_w, "claude-api-key")
    eq("全部 key 都 weight:0 的站算死站", "dead.example" in bw.dead_hosts, True)
    eq("还有活 key 的站不算死站", "mixed.example" in bw.dead_hosts, False)
    eq("没设 weight 的站不算死站", "live.example" in bw.dead_hosts, False)

    # ── ⑧ 真实 config.yaml 上的端到端 ──────────────────────────────
    # 不传路径就用自带样本 —— 这一轮验的是「定档不变式在真实形状上成立」，
    # 样本的形状是按这些不变式凑的，不需要真实凭据。
    cfg_path, _synthetic, _tmp8 = fixture_cfg.resolve(
        sys.argv, label="定档不变式")
    if os.path.exists(cfg_path):
        import yaml
        section("⑧ config.yaml 端到端"
                + ("（自带样本）" if _synthetic else "（真实文件）"))
        raw_real = io.open(cfg_path, encoding="utf-8").read()
        cfg_real = yaml.safe_load(raw_real)
        MODELS = {"claude-api-key": ["claude-opus-5"],
                  "codex-api-key": ["gpt-5.6-sol"],
                  "gemini-api-key": ["gemini-2.5-pro"]}
        for sec, ms in MODELS.items():
            if not cfg_real.get(sec):
                continue
            band = cp.build_band(cfg_real, sec, raw=raw_real)
            truthy(f"{sec} 建出了别名表（{len(band.alias)} 项）",
                   len(band.alias) > 0,
                   "别名表为空会让健康度信号大面积漏判")
            v100, n100 = cp.suggest_priority(band, 100, models=ms)
            v40, _n40 = cp.suggest_priority(band, 40, models=ms)
            truthy(f"{sec} 满分档位 >= 40 分档位（{v100} >= {v40}）", v100 >= v40)
            # 不劫持顶层这条硬约束在真实数据上也必须成立
            top = band.model_top.get(ms[0], band.top)
            truthy(f"{sec} 满分档位不越过该模型顶层 {top}（实得 {v100}）",
                   v100 <= top)
            truthy(f"{sec} 理由带得分上限", "得分" in n100, n100[:60])

        # 传 raw 与不传 raw 必须有区别 —— 否则说明健康度信号没接上
        section("⑧ raw 参数真的起作用")
        for sec, ms in MODELS.items():
            if not cfg_real.get(sec):
                continue
            b_with = cp.build_band(cfg_real, sec, raw=raw_real)
            b_without = cp.build_band(cfg_real, sec)
            eq(f"{sec} 不传 raw 时 unhealthy 为空",
               len(b_without.unhealthy_hosts), 0)
            truthy(f"{sec} 传 raw 时解析出实测不可用站"
                   f"（{len(b_with.unhealthy_hosts)} 个）",
                   len(b_with.unhealthy_hosts) > 0,
                   "解析不出说明注释形态没覆盖到，健康度信号形同虚设")

    # ── ⑨ 自查发现的 7 处缺陷（2026-08-30 第二轮） ──────────────────
    section("⑨-1 YAML 键名不能被当成站名")
    # `_HOST_IN_NOTE` 只要求「# 后跟字母数字再跟冒号」，于是注释掉的 YAML 键
    # 全被抓成站名。8 种形态实测全部误判。垃圾进 unhealthy_hosts 后会让
    # 活站被误判成死站 —— 新站拿到过高档位，压住真正可用的站。
    for note, junk in [("# weight: 0  站点级不可用", "weight"),
                       ("# disabled: true  实测 503", "disabled"),
                       ("# 2026-08-27：实测 403 WAF", "2026-08-27"),
                       ("# cZone: 实测 500 not implemented", "czone"),
                       ("# priority: 30  永久排除", "priority"),
                       ("# base-url: https://x.com 实测 503", "base-url"),
                       ("# models: x  永久排除", "models"),
                       ("# headers: y  实测 403", "headers")]:
        got = unhealthy_from_comments(
            "claude-api-key:\n" + note + "\nother: 1", "claude-api-key")
        eq("「" + junk + "」不被当站名", junk in got, False)

    section("⑨-1 真站名仍要认得")
    for note, want in [("# relay-f：实测 503 No available channel", "relay-f"),
                       ("# relay-a：实测 403 分组权限被回收", "relay-a"),
                       ("# relay-e 永久排除（2026-08-27）", "relay-e"),
                       ("# relay-b.example：实测 400", "relay-b.example"),
                       ("# jdw：实测 503", "jdw")]:
        got = unhealthy_from_comments(
            "claude-api-key:\n" + note + "\nother: 1", "claude-api-key")
        eq("认得「" + want + "」", want in got, True)

    section("⑨-2 段头与段尾都要认带引号的键")
    raw_q = ('"claude-api-key":\n# aaa：实测 503\n  - x: 1\n'
             '"codex-api-key":\n# bbb：实测 503\n  - y: 2\n')
    gc = unhealthy_from_comments(raw_q, "claude-api-key")
    gx = unhealthy_from_comments(raw_q, "codex-api-key")
    eq("引号段头能找到（否则整段信号丢失）", "aaa" in gc, True)
    eq("引号段尾正确切断（否则跨段串扰）", "bbb" in gc, False)
    eq("下一段自己也能解析", "bbb" in gx, True)
    raw_q2 = "'claude-api-key':\n# ccc：实测 503\nother: 1"
    eq("单引号段头也认",
       "ccc" in unhealthy_from_comments(raw_q2, "claude-api-key"), True)

    section("⑨-3 「已恢复」的注释不判为当前不可用")
    for note, should_dead in [
            ("# oldhost：实测 503（2026-08-01），已于 2026-08-20 恢复正常", False),
            ("# xxx：实测 403 WAF，换出口 IP 后已可通", False),
            ("# yyy：实测 503，现已可用", False),
            ("# zzz：实测 503，站方已修复", False),
            ("# www：实测 503 No available channel", True)]:
        got = unhealthy_from_comments(
            "claude-api-key:\n" + note + "\nother: 1", "claude-api-key")
        eq(("判死：" if should_dead else "不判死：") + note[:28], bool(got), should_dead)

    section("⑨-4 不做前缀匹配（会把不同站判成同一个）")
    # 曾经有 len(n)>=4 and startswith 的宽松匹配，实测会把不同站判成同一个。
    # 误判方向是「把活站当死站」—— 新站因此拿到过高档位，比漏判严重得多。
    for host, name in [("api.aliyuncs.com", "aliyun"),
                       ("api.oaipro.com", "oaiproxy"),
                       ("api.justdo.com", "jdw"),
                       ("relay-f.examplex.com", "relay-f.example")]:
        eq(host + " 不被 " + name + " 误匹配",
           cp.host_matches_note(host, {name}, None), False)
    for host, name in [("relay-f.example", "relay-f"),
                       ("relay-l.example", "relay-l"),
                       ("relay-b.example", "relay-b")]:
        eq(host + " 与 " + name + " 精确匹配",
           cp.host_matches_note(host, {name}, None), True)

    section("⑨-5 compat 段的 weight 在 api-key-entries 里")
    # build_band 原来统一按条目级读 weight，compat 段永远读到 None ——
    # 一个全部 key 都 weight:0 的 provider 被当成活站保护。
    W, Z = cp.entry_weights, cp.entry_all_zero_weight
    eq("compat 全 0 → 整站死",
       Z("openai-compatibility",
         {"api-key-entries": [{"weight": 0}, {"weight": 0}]}), True)
    eq("compat 混合（一个 0 一个未设）→ 不算死",
       Z("openai-compatibility",
         {"api-key-entries": [{"weight": 0}, {"api-key": "d"}]}), False)
    eq("compat 全未设 → 不算死",
       Z("openai-compatibility",
         {"api-key-entries": [{"api-key": "a"}, {"api-key": "b"}]}), False)
    eq("compat 空 entries → 不算死",
       Z("openai-compatibility", {"api-key-entries": []}), False)
    eq("未设 weight 记成 None 而非跳过",
       W("openai-compatibility",
         {"api-key-entries": [{"weight": 0}, {"api-key": "d"}]}), [0, None])
    cfg_c = {"openai-compatibility": [
        {"name": "dead", "base-url": "https://dead.example/v1", "priority": 500,
         "api-key-entries": [{"api-key": "a", "weight": 0},
                             {"api-key": "b", "weight": 0}]},
        {"name": "mixed", "base-url": "https://mixed.example/v1", "priority": 400,
         "api-key-entries": [{"api-key": "c", "weight": 0}, {"api-key": "d"}]},
        {"name": "live", "base-url": "https://live.example/v1", "priority": 300,
         "api-key-entries": [{"api-key": "e"}]}]}
    bc = cp.build_band(cfg_c, "openai-compatibility")
    eq("dead.example 判死", "dead.example" in bc.dead_hosts, True)
    eq("mixed.example 不判死", "mixed.example" in bc.dead_hosts, False)
    eq("live.example 不判死", "live.example" in bc.dead_hosts, False)

    section("⑨-6 priority 必须 >= 1（负档位谱也不能出 0）")
    b_neg = mkband("claude-api-key",
                   {100: ["a.example"], -50: ["b.example"]}, "m1")
    for sc in (100, 50, 0):
        v, _n = cp.suggest_priority(b_neg, sc, models=["m1"])
        truthy("score=" + str(sc) + " 的档位 " + str(v) + " >= 1", v >= 1,
               "priority 0 或负数在 CPA 里语义未定义")

    section("⑨-7 匹配不上的短名要可见（静默漏判可见化）")
    # 别名表只能从 compat 段的 name 建；只在别的段出现的站会漏判。
    cfg_u = {"claude-api-key": [
                 {"api-key": "k", "base-url": "https://relay-h.example",
                  "priority": 900}],
             "openai-compatibility": []}
    raw_u = ('claude-api-key:\n# jdw：实测 503\n'
             '  - api-key: "k"\nopenai-compatibility: []\n')
    bu = cp.build_band(cfg_u, "claude-api-key", raw=raw_u)
    truthy("解析出了 jdw", "jdw" in bu.unhealthy_hosts)
    truthy("但它匹配不上任何站 —— 记入 unmatched_notes",
           "jdw" in bu.unmatched_notes,
           "漏判必须可见，否则定档悄悄变保守而无人知晓")
    cfg_ok = dict(cfg_u)
    cfg_ok["openai-compatibility"] = [
        {"name": "jdw", "base-url": "https://relay-h.example/v1",
         "priority": 100, "api-key-entries": [{"api-key": "z"}]}]
    bo = cp.build_band(cfg_ok, "claude-api-key", raw=raw_u)
    eq("有别名表时不算未匹配", "jdw" in bo.unmatched_notes, False)

    section("⑨ 死站恢复的风险必须写进理由")
    # 「下层全是死站 → 新站拿高档」这条推理的前提是它们**保持**不可用。
    # 若只是暂时故障，恢复后会被新站永久压住 —— 而本工具没有任何自动
    # 重新评估机制（weight:0 与注释都不会过期）。所以必须写进理由。
    b_d = mkband("codex-api-key",
                 {900: ["alive.example"], 500: ["d1.example"], 100: ["d2.example"]},
                 "m1", dead={"d1.example", "d2.example"})
    _v, nd = cp.suggest_priority(b_d, 100, models=["m1"])
    truthy("理由提示不会自动重算", "不会自动重算" in nd, nd)
    truthy("理由提示需手工复查", "手工复查" in nd, nd)

    section("⑩ weight 判定必须与 CPA 的 Normalize 对齐")
    # CPA: internal/credentialweight/weight.go:21-24
    #     if weight <= 0 { return 0, nil }        ← **负数也归零**
    # 归零后 positiveWeightAuths（selector.go:423-430）把它整个剔除。
    # 所以 weight:-1 与 weight:0 效果完全相同 —— 只判 `== 0` 会漏掉负数，
    # 把一个已被逐出的站当成活站保护（自查 2026-08-30 发现）。
    Z2 = cp.entry_all_zero_weight
    for val, desc, want in [
            (0, "整数 0", True),
            (-1, "负数 -1", True),
            (-5, "负数 -5", True),
            (0.0, "浮点 0.0", True),
            (False, "布尔 False", True),
            ("0", '字符串 "0"', True),
            ("-2", '字符串 "-2"', True),
            (None, "显式 null", False),
            (1, "整数 1", False),
            (100, "整数 100", False),
            ("1", '字符串 "1"', False),
            ("abc", "非数字字符串", False)]:
        eq("weight=" + desc + " → " + ("判死" if want else "算活"),
           Z2("claude-api-key", {"weight": val}), want)

    section("⑩ 同站多条目：任一条目有活 key 就不算整站死")
    # build_band 的 alive 集合按条目算，一个站可能有多个条目。
    cfg_m = {"claude-api-key": [
        {"api-key": "a", "base-url": "https://multi.example", "priority": 900,
         "weight": 0},
        {"api-key": "b", "base-url": "https://multi.example", "priority": 900,
         "weight": 0},
        {"api-key": "c", "base-url": "https://multi.example", "priority": 800},
        {"api-key": "d", "base-url": "https://alldead.example", "priority": 700,
         "weight": 0},
        {"api-key": "e", "base-url": "https://alldead.example", "priority": 700,
         "weight": -3},
    ]}
    bm = cp.build_band(cfg_m, "claude-api-key")
    eq("multi.example 有活 key → 不判死",
       "multi.example" in bm.dead_hosts, False)
    eq("alldead.example 全被逐出（含负数）→ 判死",
       "alldead.example" in bm.dead_hosts, True)

    section("⑩ 顶层键正则不误判缩进行与注释")
    from cpa_probe.plan import _TOP_KEY
    for line in ("claude-api-key:", '"codex-api-key":', "'gemini-api-key':",
                 "api-keys:", "x_y.z-1:"):
        eq("认得顶层键 " + repr(line), bool(_TOP_KEY.match(line)), True)
    for line in ("  nested: 1", "    - name: x", "  - api-key: k",
                 "# comment:", "#claude-api-key:", "   deep.key:", "- item"):
        eq("不误判 " + repr(line), bool(_TOP_KEY.match(line)), False)

    section("⑩ 「仍不可用」的说法不能被当成已恢复")
    # _RECOVERED 要求「已+恢复」连写。这些是反例：措辞里有「恢复」二字，
    # 但语义是仍然不可用 —— 误放过会把死站当活站保护，定档偏保守。
    for note in ("# xxx：实测 503，等站方恢复后再启用",
                 "# xxx：实测 503，恢复无望",
                 "# xxx：实测 503，已确认无法恢复",
                 "# xxx：实测 403，站方未恢复",
                 "# xxx：实测 503，恢复前保持 weight:0",
                 "# xxx：实测 403，站点恢复后删除本行"):
        got = unhealthy_from_comments(
            "claude-api-key:\n" + note + "\nother: 1", "claude-api-key")
        eq("仍判死：" + note[14:34], bool(got), True)

    section("⑩ 黑名单不误杀真实站名形态")
    from cpa_probe.plan import _NOT_A_HOST, _looks_like_host
    for n in ("123.com", "relay-b", "8gpt", "api2", "x-ai", "a1", "ai",
              "2api", "v1", "relay-i.example"):
        eq(n + " 可以是站名", _looks_like_host(n), True)
    for n in ("weight", "priority", "base-url", "models", "2026-08-27",
              "20260830", "123", "1.2.3"):
        eq(n + " 不能是站名", _looks_like_host(n), False)
    # 黑名单与真实配置零冲突 —— 加词时要跑这条
    truthy("黑名单非空且都是小写",
           _NOT_A_HOST and all(w == w.lower() for w in _NOT_A_HOST))

    section("⑪ 「已恢复」被转折词否决时仍要判死")
    # 交叉审计（2026-08-30）抓到：_RECOVERED 只看有没有「已恢复」，于是
    #     # xxx：实测 503，已恢复但又挂了
    #     # xxx：实测 503，站方称已恢复，实测仍 503
    # 4/4 被误放过 —— 误判方向是**把死站当活站**，定档会为保护一个实际
    # 不可用的站而压低新站。注释是累积写的，「恢复了又挂」本来就常见。
    for note in ("# xxx：实测 503，已恢复但又挂了",
                 "# xxx：实测 503，站方称已恢复，实测仍 503",
                 "# xxx：实测 403，已恢复后再次被封",
                 "# xxx：实测 503，已修复但仍不稳定",
                 "# xxx：实测 503，已恢复，然而又 403",
                 "# xxx：实测 503，据说已修复，实测仍 500",
                 "# xxx：实测 403，已恢复，不过依旧不稳"):
        got = unhealthy_from_comments(
            "claude-api-key:\n" + note + "\nother: 1", "claude-api-key")
        eq("转折否决 → 仍判死：" + note[14:36], bool(got), True)

    section("⑪ 真正的恢复仍要放过")
    for note in ("# xxx：实测 503（08-01），已于 08-20 恢复正常",
                 "# xxx：实测 403 WAF，换出口 IP 后已可通",
                 "# xxx：实测 503，现已可用",
                 "# xxx：实测 503，站方已修复",
                 "# xxx：实测 503，已解封"):
        got = unhealthy_from_comments(
            "claude-api-key:\n" + note + "\nother: 1", "claude-api-key")
        eq("真恢复 → 不判死：" + note[14:34], bool(got), False)

    section("⑪ 「未恢复」类说法必须判死（不能被恢复分支放过）")
    for note in ("# xxx：实测 503，等站方恢复后再启用",
                 "# xxx：实测 503，恢复无望",
                 "# xxx：实测 503，已确认无法恢复",
                 "# xxx：实测 403，站方未恢复",
                 "# xxx：实测 503，恢复前保持 weight:0",
                 "# xxx：实测 403，站点恢复后删除本行",
                 "# xxx：实测 503 No available channel"):
        got = unhealthy_from_comments(
            "claude-api-key:\n" + note + "\nother: 1", "claude-api-key")
        eq("仍判死：" + note[14:34], bool(got), True)

    section("⑪ 注释解析在真实形状上的不变式")
    # 原来这里写死了四个数字（11/10/6/0），是 2026-08-30 那份 config.yaml 的
    # 实测值。2026-08-31 踩到：那份文件被改了（891 KB → 468 KB），四个数字
    # 全部失效，测试报红却指不出任何真缺陷 —— 而代码一行没动。
    #
    # 断言挂在会变的外部文件上，本身就是缺陷。改断**不变式**：
    #   · 每段解析出的站名互不重复（set 语义）
    #   · 解析出的名字都长得像站名（不是从正文里误抓的词）
    #   · 同一段解析两次结果相同（无顺序依赖）
    #   · 带「已恢复」且无转折词的注释不算不可用
    # 这些性质与文件内容无关，改文件不会假报警，改坏逻辑一定报警。
    from cpa_probe.plan import _looks_like_host as _lh
    _raw = io.open(cfg_path, encoding="utf-8").read()
    for _sec in cp.SECTIONS:
        if _sec not in yaml.safe_load(_raw):
            continue
        _got = unhealthy_from_comments(_raw, _sec)
        _again = unhealthy_from_comments(_raw, _sec)
        eq(_sec + " 解析可重复", _got, _again)
        truthy(_sec + " 站名都合法（共 " + str(len(_got)) + " 个）",
               all(_lh(h) for h in _got),
               "混进了非站名的词，说明 _HOST_IN_NOTE 抓错了位置")
    if _tmp8:
        import shutil
        shutil.rmtree(_tmp8, ignore_errors=True)

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
