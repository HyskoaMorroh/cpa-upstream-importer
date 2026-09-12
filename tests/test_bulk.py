#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量管理（cpa_probe/bulk.py）的回归测试。

这个模块会**改写整份 config.yaml 文本**，一旦出错是不可逆的数据损坏，
所以每条断言都钉住一个具体的不变式：
  · 条目数守恒（绝不因为改一个字段而增删条目）
  · 注释守恒（本项目写回路径的硬要求）
  · 往返可逆（停用→启用回到逐字节相同的原文）
  · 幂等（同一操作做两次，第二次无改动）
  · 两套启停语义不串（key 类段用 excluded-models，compat 段用 disabled）
  · 改不动的要**报出来**，不静默跳过
"""

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml                                                      # noqa: E402

from cpa_probe import bulk, host_of                              # noqa: E402

_fail = 0
_pass = 0


def eq(name, got, want):
    global _fail, _pass
    if got == want:
        _pass += 1
        print(f"  ok  {name}")
    else:
        _fail += 1
        print(f"\n  ✗ {name}\n      got  = {got!r}\n      want = {want!r}")


def section(t):
    print(f"\n── {t} " + "─" * max(0, 62 - len(t)))


# 夹具：四段俱全，含注释、含既有 excluded-models、含 compat 的多 Key
RAW = '''# 顶部说明，不该被动
gemini-api-key:
  # A 站的结论：实测 200
  - api-key: "kA"
    base-url: "https://a.example.com"
    priority: 200
    models:
      - name: "gemini-3.1-pro"
  - api-key: "kA2"
    base-url: "https://a.example.com"
    priority: 180
    models:
      - name: "gemini-3.1-pro"
codex-api-key:
  # B 站：门票是 codex-tui
  - api-key: "kB"
    base-url: "https://b.example.com/v1"
    priority: 300
    excluded-models: ["gpt-4o"]
    headers:
      originator: "codex-tui"
    models:
      - name: "gpt-6"
  - api-key: "kB2"
    base-url: "https://b.example.com/v1"
    priority: 300
    models:
      - name: "gpt-6"
claude-api-key:
  - api-key: "kC"
    base-url: "https://c.example.com"
    priority: 400
    models:
      - name: "claude-opus-5"
openai-compatibility:
  # C 站 provider
  - name: "c.example.com"
    base-url: "https://c.example.com/v1"
    priority: 50
    api-key-entries:
      - api-key: "kC"
      - api-key: "kC2"
    models:
      - name: "gpt-6"
other-top-level: 1
'''


def n_comments(t):
    return sum(1 for x in t.splitlines() if x.strip().startswith("#"))


def comment_lines(t):
    """整行注释的原文（去掉缩进）。用来判「原有注释一条都没少」。"""
    return [x.strip() for x in t.splitlines() if x.strip().startswith("#")]


def comments_kept(got, ref):
    """原文里的每条整行注释都还在（允许新增出处注释）。

    2026-09-12：原来断言 `n_comments(got) == n_comments(ref)`，与紧邻的
    「改档带出处注释」自相矛盾 —— 批量改档**必须**写一行
    `# 批量改档 …` 说明这个值的来由，那一行就让总数 +1。
    用户第 8 条要的是「原有注释全部保留」，不是「禁止新增注释」。
    所以判据改成「原有的都在」，多出来的算合法增量。
    """
    have = comment_lines(got)
    return [c for c in comment_lines(ref) if c not in have]


def counts(t):
    c = yaml.safe_load(t)
    return {s: len(c.get(s) or []) for s in
            ("gemini-api-key", "codex-api-key", "claude-api-key",
             "openai-compatibility")}


def main():
    section("① 停用 / 启用：key 类段走 excluded-models")
    d, notes, probs = bulk.apply_bulk(
        RAW, [{"section": "codex-api-key", "index": 0, "action": "disable"}])
    eq("有改动记录", notes, ["codex-api-key[0] → 停用"])
    eq("无问题", probs, [])
    c = yaml.safe_load(d)
    eq("通配符加进既有清单，原值保留",
       c["codex-api-key"][0]["excluded-models"], ["gpt-4o", "*"])
    eq("条目数守恒", counts(d), counts(RAW))
    eq("注释守恒", n_comments(d), n_comments(RAW))

    section("② 幂等：同一操作做两次，第二次无改动")
    d2, notes2, _ = bulk.apply_bulk(
        d, [{"section": "codex-api-key", "index": 0, "action": "disable"}])
    eq("第二次无改动", notes2, [])
    eq("文本逐字节相同", d2 == d, True)

    section("③ 往返可逆")
    e, _n, _p = bulk.apply_bulk(
        d, [{"section": "codex-api-key", "index": 0, "action": "enable"}])
    eq("停用→启用回到原文", e == RAW, True)
    # 原本没有 excluded-models 的条目，往返后也不能凭空多出一行
    d3, _n, _p = bulk.apply_bulk(
        RAW, [{"section": "codex-api-key", "index": 1, "action": "disable"}])
    e3, _n, _p = bulk.apply_bulk(
        d3, [{"section": "codex-api-key", "index": 1, "action": "enable"}])
    eq("无 excluded-models 的条目往返也回到原文", e3 == RAW, True)

    section("④ compat 段走布尔 disabled，不碰 excluded-models")
    d4, notes4, _p = bulk.apply_bulk(
        RAW, [{"section": "openai-compatibility", "index": 0,
               "action": "disable"}])
    c4 = yaml.safe_load(d4)
    eq("有改动记录", notes4, ["openai-compatibility[0] → 停用"])
    eq("写的是 disabled", c4["openai-compatibility"][0].get("disabled"), True)
    eq("没有误写 excluded-models",
       "excluded-models" in c4["openai-compatibility"][0], False)
    eq("api-key-entries 没动",
       len(c4["openai-compatibility"][0]["api-key-entries"]), 2)
    eq("往返回到原文",
       bulk.apply_bulk(d4, [{"section": "openai-compatibility", "index": 0,
                             "action": "enable"}])[0] == RAW, True)

    section("⑤ 批量改档 + 同网址统一优先级")
    cfg = yaml.safe_load(RAW)
    ops = bulk.unify_priority_ops(cfg, "gemini-api-key", "a.example.com")
    eq("算出 1 条待改（180 → 200，往高对齐）", len(ops), 1)
    eq("目标值取组内最高", ops[0]["value"], 200)
    d5, notes5, probs5 = bulk.apply_bulk(RAW, ops, stamp="2026-09-11")
    eq("无问题", probs5, [])
    c5 = yaml.safe_load(d5)
    pr = sorted({e.get("priority") for e in c5["gemini-api-key"]
                 if host_of(str(e.get("base-url") or "")) == "a.example.com"})
    eq("同网址档位已统一", pr, [200])
    eq("条目数守恒", counts(d5), counts(RAW))
    # 改档会**新增**一行出处注释（下一条断言要求它），所以判「原有的都还在」
    # 而不是判总数相等 —— 见 comments_kept 的说明。
    eq("原有注释一条都没少", comments_kept(d5, RAW), [])
    eq("改档带出处注释", "批量改档" in d5, True)
    eq("已经一致的组不产生操作",
       bulk.unify_priority_ops(cfg, "codex-api-key", "b.example.com"), [])

    section("⑥ 改不动的要报出来，不静默跳过")
    _t, _n, p6 = bulk.apply_bulk(
        RAW, [{"section": "codex-api-key", "index": 99, "action": "disable"}])
    eq("下标越界有说明", len(p6) == 1 and "越界" in p6[0], True)
    _t, _n, p7 = bulk.apply_bulk(
        RAW, [{"section": "codex-api-key", "index": 0, "action": "nope"}])
    eq("未知操作有说明", len(p7) == 1 and "未知操作" in p7[0], True)
    _t, _n, p8 = bulk.apply_bulk(
        RAW, [{"section": "not-a-section", "index": 0, "action": "disable"}])
    eq("未知段有说明", len(p8) == 1 and "未知段" in p8[0], True)
    _t, _n, p9 = bulk.apply_bulk(
        RAW, [{"section": "claude-api-key", "index": 0,
               "action": "priority", "value": "abc"}])
    eq("非整数档位有说明", len(p9) == 1 and "不是整数" in p9[0], True)

    section("⑦ 一次多条：下标不因插入行而错位")
    ops7 = [{"section": "codex-api-key", "index": i, "action": "disable"}
            for i in (0, 1)]
    d7, notes7, probs7 = bulk.apply_bulk(RAW, ops7)
    c7 = yaml.safe_load(d7)
    eq("两条都改到", len(notes7), 2)
    eq("无问题", probs7, [])
    eq("第 0 条：原值保留 + 通配符",
       c7["codex-api-key"][0]["excluded-models"], ["gpt-4o", "*"])
    eq("第 1 条：新建清单只有通配符",
       c7["codex-api-key"][1]["excluded-models"], ["*"])
    eq("条目数守恒", counts(d7), counts(RAW))

    section("⑧ 删除：三道保护缺一不可")
    _t, _n, p10 = bulk.apply_bulk(
        RAW, [{"section": "codex-api-key", "index": 0, "action": "delete"}])
    eq("缺 expect 拒绝", len(p10) == 1 and "必须带 expect" in p10[0], True)
    _t, _n, p11 = bulk.apply_bulk(
        RAW, [{"section": "codex-api-key", "index": 0, "action": "delete",
               "expect": "https://WRONG.example.com/v1"}])
    eq("指纹不符拒绝", len(p11) == 1 and "内容指纹不符" in p11[0], True)
    d8, n8, p8b = bulk.apply_bulk(
        RAW, [{"section": "codex-api-key", "index": 0, "action": "delete",
               "expect": "https://b.example.com/v1"}])
    c8 = yaml.safe_load(d8)
    eq("指纹相符才删", len(n8), 1)
    eq("无问题", p8b, [])
    eq("删掉的是第 0 条", [e["api-key"] for e in c8["codex-api-key"]], ["kB2"])
    eq("其他段一条没动",
       (len(c8["gemini-api-key"]), len(c8["claude-api-key"]),
        len(c8["openai-compatibility"])), (2, 1, 1))

    section("⑨ 删除的下标不错位（降序执行）")
    d9, n9, p9b = bulk.apply_bulk(RAW, [
        {"section": "gemini-api-key", "index": 0, "action": "delete",
         "expect": "https://a.example.com"},
        {"section": "gemini-api-key", "index": 1, "action": "delete",
         "expect": "https://a.example.com"}])
    eq("两条都删到", len(n9), 2)
    # 段被删空要报出来 —— 那会让该键变成 null
    eq("删空段有提示", any("全部删除" in x for x in p9b), True)

    section("⑩ 删除与改档混在一批也不互相干扰")
    d10, n10, p10b = bulk.apply_bulk(RAW, [
        {"section": "codex-api-key", "index": 1, "action": "delete",
         "expect": "https://b.example.com/v1"},
        {"section": "codex-api-key", "index": 0, "action": "priority",
         "value": 999}])
    c10 = yaml.safe_load(d10)
    eq("无问题", p10b, [])
    eq("删对了也改对了",
       [(e["api-key"], e["priority"]) for e in c10["codex-api-key"]],
       [("kB", 999)])
    # 这一组既删条目又改档：改档同样写出处注释（+1），而被删掉的那条
    # 条目本来没有自己的整行注释，所以原有注释应当一条不少。
    eq("原有注释一条都没少", comments_kept(d10, RAW), [])

    section("⑪ 全量重建不许让文件膨胀（2026-09-11 实跑发现的生产缺陷）")
    from cpa_probe import writeback as wb
    # 一条方案都不给 = 「一条都没勾选」。守恒的极端情形：输出必须与输入等价。
    # 原缺陷：`rebuild_config_full` 末尾是 `"\n".join(out_lines)`，而三处
    # `extend(original_lines[...])` 搬运的是调用方传的 `keepends=True` 行
    # （每行自带 \n），于是每行后面多出一个空行 —— 生产文件 6081 行会变
    # 12161 行，且**内容与注释都不丢**，逐字段对账看不出来。
    cfg11 = yaml.safe_load(RAW)
    out11, _w = wb.rebuild_config_full(cfg11, {}, RAW.splitlines(keepends=True))
    eq("行数不变", len(out11.splitlines()), len(RAW.splitlines()))
    eq("空行不变",
       sum(1 for x in out11.splitlines() if not x.strip()),
       sum(1 for x in RAW.splitlines() if not x.strip()))
    eq("注释不变", n_comments(out11), n_comments(RAW))
    eq("条目数守恒", counts(out11), counts(RAW))
    # 幂等：再重建一次不能再变（膨胀型缺陷在这一条上必然暴露）
    out11b, _w = wb.rebuild_config_full(
        yaml.safe_load(out11), {}, out11.splitlines(keepends=True))
    eq("二次重建幂等", out11b == out11, True)

    print("\n" + "=" * 66)
    if _fail:
        print(f"失败 {_fail} 项 / 通过 {_pass} 项")
        return 1
    print(f"全部通过 · {_pass} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
