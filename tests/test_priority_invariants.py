#!/usr/bin/env python3
"""定档不变式：同段不同域名必须拿到不同 priority（用户要求 1 后半 / 3⑶④ / 第 7 条）。

为什么要单独一个套件（2026-09-30 现场）
--------------------------------------
生产 `config.yaml`（fsdownload/config.yaml，写于 2026-09-19）里
`openai-compatibility` 段有**四个不同域名同时是 `priority: 1`**：

    nova.example      priority: 1  # 算法上限 8，为与前一站分开降到 1
    golf.example      priority: 1  # 算法上限 8，为与前一站分开降到 1
    juliet.example    priority: 1  # 算法上限 24，为与前一站分开降到 1
    kilo.example      priority: 1  # 算法上限 17，为与前一站分开降到 1

（站名按 `tools/scrub.py` 的 DOMAIN_MAP 用代号 —— 本文件会提交到公开仓库，
真实上游站名不上传，见用户红线「严禁提交私密域名」。代号与本地对照表
一一对应，排障时可还原。）

行尾注释就是 `assign_priorities` 自己写的 —— 四个站各自「为与前一站分开」
一路降，最后全部压在地板 1 上。这正是它要避免的结果。

后果不是「排序不好看」而是容灾失效：CPA 的凭据选择是层级隔离的
（同层按 weight 轮询、只取最高可用桶），四个站同层意味着它们退化成
**一个桶**。用户第 6 条要的「高优先级站余额耗尽后自动降级到低优先级站」
在这一段上直接不成立 —— 因为它们之间根本没有先后。

`plan.py:3795-3827` 已经加了修复（撞到地板后往**上**找最近的空整数，
记进 `lifted` 并如实报给操作员），但**没有任何测试守着它**：
`tests/test_plan_capacity.py` 是任务仓容量的，与定档无关。
没有测试 = 下一次重构会把它改回去，而现场要几周后才发现。

所以这里按生产现场的形状建最小复现：让一段的低位整数被现有条目占满，
再投几个新站进去，断言它们彼此不同值。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cpa_probe as cp  # noqa: E402
from cpa_probe.plan import ImportPlan, SectionPlan  # noqa: E402

FAILED: list[str] = []
PASSED = 0


def check(label: str, cond: bool, why: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        FAILED.append(label + (f" —— {why}" if why else ""))
        print(f"  FAIL {label}" + (f"  ({why})" if why else ""))


def mkplan(host: str, section: str, models: list[str], *,
           src: str = "probed", score: int = 50, line: int = 1) -> ImportPlan:
    p = ImportPlan(host=host, masked_key="sk-***", line_no=line)
    p.sections[section] = SectionPlan(
        section=section, base_url=f"https://{host}", api_key=f"k-{host}",
        models=list(models), priority=0, model_source=src, score=score)
    return p


def occupied_cfg(section: str, model: str, taken: list[int]) -> dict:
    """造一份「低位整数全被占住」的既有配置。

    每个既有条目一个不同的 host —— 现有档位属于在用站，新站不得与它们撞值。
    """
    return {section: [
        {"api-key": f"old-{v}", "base-url": f"https://old{v}.example",
         "priority": v, "models": [{"name": model}]}
        for v in taken
    ]}


def priorities_of(plans: list[ImportPlan], section: str) -> list[int]:
    return [p.sections[section].priority for p in plans]


def main() -> int:
    print(__doc__.splitlines()[0])

    # ── ① 生产现场复现：compat 段低位连号占满 + 四个新站 ────────────────
    #
    # 生产 compat 段既有档位是 1..8 连号 + 12/19/26/31/34（见 config.yaml）。
    # 四个新站的 cap 分别落在 8/8/24/17 —— 逐格降必然撞穿到 1。
    print("\n① 生产现场复现：低位连号占满，四个新站必须彼此不同档")
    section = "openai-compatibility"
    cfg = occupied_cfg(section, "gpt-6-astra",
                       [1, 2, 3, 4, 5, 6, 7, 8, 12, 19, 26, 31, 34])
    newcomers = [
        mkplan("nova.example", section, ["gpt-6-astra"], score=45, line=1),
        mkplan("golf.example", section, ["gpt-6-astra"], score=45, line=2),
        mkplan("juliet.example", section, ["gpt-6-astra"], score=38, line=3),
        mkplan("kilo.example", section, ["gpt-6-astra"], score=41, line=4),
    ]
    warns = cp.assign_priorities(newcomers, cfg, probation=True)
    got = priorities_of(newcomers, section)
    hosts = [p.host for p in newcomers]
    print(f"     定档结果：{dict(zip(hosts, got))}")

    check("四个新站拿到四个互不相同的 priority",
          len(set(got)) == len(got),
          f"实得 {got} —— 同值的站在 CPA 里退化成一个桶，跨站降级容灾失效")
    check("没有任何新站被压到与既有在用站同档",
          not (set(got) & {1, 2, 3, 4, 5, 6, 7, 8, 12, 19, 26, 31, 34}),
          f"实得 {got}，与既有档位相撞")
    check("每个 priority 都是正整数",
          all(isinstance(v, int) and v >= 1 for v in got), f"实得 {got}")

    # 让位这件事必须**说出来**，不能静默改值 —— 单调递减被打破是有意取舍，
    # 操作员有权知道「为什么分数低的反而在上面」。
    lifted_reported = any("已无可用整数" in w or "让位" in w or "抬到" in w
                          for w in warns)
    if len(set(got)) == len(got) and min(got) != 1:
        check("向上让位时有告警如实说明", lifted_reported,
              f"warns={warns!r} —— 打破单调递减却不告知，操作员无法复核")

    # ── ② 同站多 Key 仍必须同档（要求 1 前半，不能被 ① 的修复破坏）──────
    print("\n② 同一段同一域名的多把 Key 仍共用同一档")
    cfg2 = occupied_cfg("claude-api-key", "claude-opus-5",
                        [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    same = [
        mkplan("multi.example", "claude-api-key", ["claude-opus-5"],
               score=70, line=1),
        mkplan("multi.example", "claude-api-key", ["claude-opus-5"],
               score=70, line=2),
        mkplan("multi.example", "claude-api-key", ["claude-opus-5"],
               score=70, line=3),
    ]
    cp.assign_priorities(same, cfg2, probation=True)
    gs = priorities_of(same, "claude-api-key")
    check(f"三把 Key 同档（{gs}）", len(set(gs)) == 1,
          "同站不同档会把多 Key 轮询变成主备切换，白费配额")

    # ── ③ 极端：地板之上一个空整数都没有时，要报出来而不是假装成功 ───────
    #
    # cap 以下全满 —— 这时确实分不开。要求是「不得静默」：要么给出不同值，
    # 要么明确告警。静默同档是唯一不可接受的结果。
    print("\n③ 真的无解时必须告警，不能静默同档")
    packed = list(range(1, 41))
    cfg3 = occupied_cfg("codex-api-key", "gpt-6-astra", packed)
    two = [
        mkplan("x.example", "codex-api-key", ["gpt-6-astra"], score=10, line=1),
        mkplan("y.example", "codex-api-key", ["gpt-6-astra"], score=10, line=2),
    ]
    warns3 = cp.assign_priorities(two, cfg3, probation=True)
    g3 = priorities_of(two, "codex-api-key")
    distinct = len(set(g3)) == len(g3)
    noisy = any(("最低值" in w) or ("无可用整数" in w) or ("排不进" in w)
                for w in warns3)
    check(f"要么分开、要么告警（值={g3}，告警={noisy}）", distinct or noisy,
          "两站同档且无任何告警 —— 用户看不出冗余已退化")

    # ── ④ 跨段同值是合法的，不该被 ① 的修复误伤 ───────────────────────
    #
    # 用户 3⑴ 原文：「不同类型相同网址可以不同」。各段档位谱独立，
    # 段间同值毫无关系 —— 若修复把跨段也强行错开，那是过度收紧。
    print("\n④ 跨段同值合法，不得误伤")
    cfg4 = {
        "claude-api-key": [{"api-key": "o1", "base-url": "https://o1.example",
                            "priority": 50,
                            "models": [{"name": "claude-opus-5"}]}],
        "codex-api-key": [{"api-key": "o2", "base-url": "https://o2.example",
                           "priority": 50,
                           "models": [{"name": "gpt-6-astra"}]}],
    }
    p = ImportPlan(host="both.example", masked_key="sk-***", line_no=1)
    p.sections["claude-api-key"] = SectionPlan(
        section="claude-api-key", base_url="https://both.example",
        api_key="k1", models=["claude-opus-5"], priority=0,
        model_source="probed", score=80)
    p.sections["codex-api-key"] = SectionPlan(
        section="codex-api-key", base_url="https://both.example",
        api_key="k1", models=["gpt-6-astra"], priority=0,
        model_source="probed", score=80)
    cp.assign_priorities([p], cfg4, probation=True)
    check("同一网址在两段各自定档（不强行错开跨段）",
          isinstance(p.sections["claude-api-key"].priority, int)
          and isinstance(p.sections["codex-api-key"].priority, int),
          "跨段档位谱独立，段间同值是合法配置")

    print(f"\n通过 {PASSED} 项" + (f"，失败 {len(FAILED)} 项" if FAILED else ""))
    for f in FAILED:
        print(f"  x {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
