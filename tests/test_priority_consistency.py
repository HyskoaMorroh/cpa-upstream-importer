#!/usr/bin/env python3
"""同域名同 priority 的回归测试（用户 2026-09-13 要求 3⑴ + 7）。

守的是 `assign_priorities` 的既有行为，不是新加的逻辑：
  · 同一 (段, 域名) 下的多把 Key 必须共用一个 priority
  · 不同域名之间必须拿到不同的 priority

为什么要有这份测试：这条约束原本只由 `assign_priorities` 的实现保证
（按 host 分组后 `for sp in sps: sp.priority = keep`），没有任何测试
守着它。改那个函数的人看不到约束，回归了也没人发现 —— 而落盘后的
表现是「同站两把 Key 分到两层」，层级隔离下低档那把等于冷备。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import cpa_probe as cp
from cpa_probe.plan import ImportPlan, SectionPlan


def _plan(line_no: int, host: str, base: str, key: str, pri: int) -> ImportPlan:
    """造一个只含 claude 段的方案。models 非空才 writable。"""
    p = ImportPlan(host=host, masked_key="sk-***", line_no=line_no)
    p.sections["claude-api-key"] = SectionPlan(
        section="claude-api-key",
        base_url=base,
        api_key=key,
        models=["claude-opus-5"],
        priority=pri,
        model_source="probed",
        score=100,
    )
    return p


def test_same_host_same_priority() -> None:
    """同域名两把 Key，定档后必须同值。"""
    cfg = {"claude-api-key": []}
    plans = [
        _plan(1, "api.example.com", "https://api.example.com", "sk-a", 10),
        _plan(2, "api.example.com", "https://api.example.com", "sk-b", 5),
    ]
    cp.assign_priorities(plans, cfg, probation=True)

    a = plans[0].sections["claude-api-key"].priority
    b = plans[1].sections["claude-api-key"].priority
    assert a == b, f"同域名两把 Key 必须同档，实得 {a} 与 {b}"
    assert a >= 1, f"priority 必须 >= 1，实得 {a}"
    print("[PASS] same host -> same priority")


def test_different_host_different_priority() -> None:
    """不同域名必须分开 —— 同值等于同层轮询，取消了站间次序。"""
    cfg = {"claude-api-key": []}
    plans = [
        _plan(1, "a.example.com", "https://a.example.com", "sk-a", 10),
        _plan(2, "b.example.com", "https://b.example.com", "sk-b", 10),
    ]
    cp.assign_priorities(plans, cfg, probation=True)

    a = plans[0].sections["claude-api-key"].priority
    b = plans[1].sections["claude-api-key"].priority
    assert a != b, f"不同域名必须分档，实得两站都是 {a}"
    print("[PASS] different host -> different priority")


def test_collision_check_reports_same_value() -> None:
    """人为把两站改成同值，priority_collisions 必须报出来。"""
    cfg = {"claude-api-key": []}
    plans = [
        _plan(1, "a.example.com", "https://a.example.com", "sk-a", 100),
        _plan(2, "b.example.com", "https://b.example.com", "sk-b", 100),
    ]
    warns = cp.priority_collisions(plans)
    assert warns, "两站同值时必须给出警告"
    assert "claude-api-key" in warns[0], f"警告要点明段名，实得 {warns[0]}"
    print("[PASS] collision reported")


def test_split_within_host_is_blocking() -> None:
    """同域名被拆成两档是阻断级错误，必须报出来。"""
    plans = [
        _plan(1, "api.example.com", "https://api.example.com", "sk-a", 100),
    ]
    plans[0].sections["codex-api-key"] = SectionPlan(
        section="codex-api-key",
        base_url="https://api.example.com/v1",
        api_key="sk-a",
        models=["gpt-5.6"],
        priority=50,          # 与同域名的 claude 段不同 —— 违规
        model_source="probed",
        score=100,
    )
    warns = cp.priority_split_within_host(plans)
    assert warns, "同域名跨段分档必须给出阻断级警告"
    assert "api.example.com" in warns[0], f"警告要点明域名，实得 {warns[0]}"
    print("[PASS] split within host reported")


def main() -> int:
    test_same_host_same_priority()
    test_different_host_different_priority()
    test_collision_check_reports_same_value()
    test_split_within_host_is_blocking()
    print("\n全部通过 · 4 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
