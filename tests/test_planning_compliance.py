"""Offline planning contracts. Run directly; no pytest or live configuration."""
import copy
import os
import sys
import traceback
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cpa_probe import model_catalog as mc
from cpa_probe import plan as pp
from cpa_probe.parse import ParsedRow, SECTIONS
from cpa_probe.pipeline import SectionVerdict

C = "codex-api-key"
G = "gemini-api-key"
L = "claude-api-key"
O = "openai-compatibility"


def verdict(section=C, models=None, catalog=None, **kwargs):
    return SectionVerdict(
        section=section, usable=True, models=models or [],
        catalog=catalog or [], min_headers={"originator": "fixture"},
        **kwargs)


def build(verdicts, remote, cfg=None, row=None, **kwargs):
    row = row or ParsedRow(1, "", "https://site.example/a", "fixture-key")
    result = SimpleNamespace(sections={v.section: v for v in verdicts})
    ident = SimpleNamespace(claude_cloak_modes=["always"],
                            claude_fingerprint_profiles=["claude-code-cli"])
    with patch.object(mc, "remote_names", return_value=(remote, "")), \
            patch("cpa_probe.cpa_source_probe.cached_identity", return_value=ident), \
            patch.object(pp, "_fallback_headers",
                         return_value={"originator": "fixture"}):
        return pp.build_plan(row, result, cfg or {}, **kwargs)


def test_family_generation_not_suffix_buckets():
    names = ["gpt-5.6-astra", "gpt-6", "gpt-6-future",
             "claude-opus-4-8", "claude-sonnet-5", "claude-newline-5",
             "kimi-k2-experimental", "kimi-k3"]
    assert mc.newest_generation_per_line(names) == [
        "gpt-6", "gpt-6-future", "claude-sonnet-5", "claude-newline-5", "kimi-k3"]


def test_codex_reasoning_line_is_independent_of_gpt():
    """o 系列与 gpt 系列各自比世代；mini 档一律不选。

    用户 2026-09-12 亲自裁定的三个判例（原话：「["o1","o3","gpt-5.6"]
    检测后应勾选后两个，["o3","o4-mini","gpt-6","gpt-6-codex"] 检测应勾选
    第 1、3、4 个模型，["o1-pro","o3-pro","o4-mini"] 检测后应勾选第 2 个」，
    并追加「本项目无论什么类型，凡是模型名称中带 mini 的就算版本很高也不
    应该勾选应该排除」）。

    这一项原来断言 `names[2:]` —— 即 gpt-6 把 o3 也挤掉。那与判例二相反：
    o3 要留。两条规则合起来才成立：
      · `generation_family` 把 o 系列单独分族，gpt 的世代号压不到它头上；
      · `is_low_tier` 先把 o4-mini 剔除，于是它也挤不掉 o3 / o3-pro。
    """
    assert mc.newest_generation_per_line(["o1", "o3", "gpt-5.6"]) == [
        "o3", "gpt-5.6"]
    assert mc.newest_generation_per_line(
        ["o3", "o4-mini", "gpt-6", "gpt-6-codex"]) == [
        "o3", "gpt-6", "gpt-6-codex"]
    assert mc.newest_generation_per_line(
        ["o1-pro", "o3-pro", "o4-mini"]) == ["o3-pro"]
    # 手填不受选型偏好约束 —— 与放行四族之外的 grok-4.6 同一条原则
    assert mc.newest_generation_per_line(
        ["o3-pro", "o4-mini"], keep_low_tier=True) == ["o4-mini"]


def test_gemini_highest_pro_not_flash_generation():
    names = ["gemini-3.1-pro", "gemini-4-pro", "gemini-4-pro-new",
             "gemini-5-flash"]
    sp = build([verdict(G, names)], names).sections[G]
    assert set(sp.models) == set(names[1:3])


def test_same_generation_topup_without_limit():
    names = ["gpt-5.6"] + [f"gpt-5.6-peer{i}" for i in range(20)]
    models, added, _ = mc.topup_to_market_top(C, [names[0]], remote=names)
    assert set(models) == set(names)
    assert set(added) == set(names[1:])


def test_topup_proven_generation_survives_higher_market_major():
    """`proven`（实测通过的名字）那一代不被市面更高主版本淘汰。

    直接测归并函数，不走 build_plan —— `proven` 的语义边界就在这里：
    它是「本轮真的打通了」的第二证据层，与「目录声称有」必须分开。
    """
    R = ["gpt-5", "gpt-5.6", "gpt-5.6-sol",
         "gpt-6", "gpt-6-astra", "gpt-6-sol"]

    # 实测通了 gpt-5.6 → 它和目录最高代 gpt-6 并存
    got, added, _ = mc.topup_to_market_top(C, ["gpt-5.6"], remote=R,
                                          proven=["gpt-5.6"])
    assert "gpt-5.6" in got, got
    assert "gpt-6" in got, got
    # 同代变体跟着补进来（用户「同等级系列全勾上」）
    assert "gpt-5.6-sol" in got, got
    # 同主版本的更旧代不搭救
    assert "gpt-5" not in got, got

    # 没有实测依据时，判据退到第二层「站方报过 + 市面名录里那一代还在售」。
    #
    # 2026-09-16 用户第 2 条：「如果检测出来没有最高级模型，比如检测出来
    # 最新模型 gpt-6 系列不通，直接按最新模型 gpt-6 填充。但是次高级模型
    # 如 gpt-5.6 通的，这个时候将 gpt-5.6 系列与 gpt-6 系列都勾选保留。」
    #
    # 这里 `have=["gpt-5.6"]` 就是「站方报过 gpt-5.6」，而 R 里 gpt-5.6 与
    # gpt-5.6-sol 都在售 —— 名录还认识这一代，所以它与 gpt-6 并存。
    # 原来断言 `"gpt-5.6" not in got2` 钉的是 2026-09-11 的旧口径（只留顶代），
    # 那条在 mhtml 快照上的后果是：整轮 0 次 200 时，站方报过的名字被换成
    # 站方从没报过的 gpt-6-astra，CPA 对着不存在的型号发请求。
    got2, _, _ = mc.topup_to_market_top(C, ["gpt-5.6"], remote=R)
    assert "gpt-5.6" in got2, got2
    assert "gpt-5.6-sol" in got2, got2          # 同代变体一起补齐
    assert "gpt-6" in got2, got2
    assert "gpt-5" not in got2, got2            # 同主版本的更旧代仍不搭救

    # 名录里连同代的影子都没有（陈年目录）→ 照常顶成市面最高级
    R_old = ["gpt-5.6", "gpt-5.6-sol", "gpt-6"]
    got_old, _, _ = mc.topup_to_market_top(C, ["gpt-4", "gpt-4-32k"],
                                           remote=R_old)
    assert not any(m.startswith("gpt-4") for m in got_old), got_old

    # 实测通的就是最高代时，行为与「站方报的就是最高代」一致（无副作用）
    got3, _, _ = mc.topup_to_market_top(C, ["gpt-6"], remote=R,
                                        proven=["gpt-6"])
    got4, _, _ = mc.topup_to_market_top(C, ["gpt-6"], remote=R)
    assert got3 == got4, (got3, got4)


def test_topup_proven_does_not_resurrect_unlisted_names():
    """`proven` 只能豁免「归并后仍在清单里」的那一代，不能凭空复活名录不认识的名字。

    边界：`have` 是进入归并的清单（plan.py 传的 `models`）。`proven` 的职责
    只是阻止阶段 A **再删一次**，不是恢复上游已决的结论。所以：

      · 名录里有 `gpt-5.6` 时它被 Phase B 的正常补齐路径带回来 —— 这是
        补齐规则的作用，不是 `proven` 的作用（下面第一个断言钉住这点）。
      · 名录里**没有** `gpt-5.6-site-only` 时，`proven` 带着它也不会让它
        进清单 —— 否则 `proven` 就变成了「绕过目录直接写名字」的后门。
    """
    # ① 名录里有这个名字 → 走补齐路径进来（与 proven 无关）
    got, _, _ = mc.topup_to_market_top(C, ["gpt-6"], remote=["gpt-5.6", "gpt-6"],
                                       proven=["gpt-5.6"])
    assert "gpt-5.6" in got, got

    # ② 名录里没有这个名字 → proven 也不放行
    got2, _, _ = mc.topup_to_market_top(C, ["gpt-6"], remote=["gpt-6"],
                                        proven=["gpt-5.6-site-only"])
    assert "gpt-5.6-site-only" not in got2, got2


def test_build_probed_peers_and_provenance():
    v = verdict(models=["gpt-5.6"], catalog=["gpt-5.6-local"])
    before = copy.deepcopy(v)
    sp = build([v], ["gpt-5.6", "gpt-5.6-sol"]).sections[C]
    assert set(sp.models) == {"gpt-5.6", "gpt-5.6-sol", "gpt-5.6-local"}
    assert set(sp.highest_models) == set(sp.models)
    assert sp.model_provenance == {
        "gpt-5.6": "verified", "gpt-5.6-sol": "inferred",
        "gpt-5.6-local": "inferred"}
    # 补齐同代变体**不**把 model_source 从 probed 降级（2026-09-12 改）
    # ------------------------------------------------------------
    # 这一行原来要求降级。而补齐逻辑对任何非空清单都会加同档变体 ——
    # inferred 几乎必然非空，于是**每一个实测成功的段**都被降级；降级后
    # `SectionPlan.recommended` 的第二条判据 `model_source != "probed"`
    # 直接 False，界面默认一个都不勾、`for_write` 为 0、写回没有 diff：
    # 探测跑完却什么都写不进去，与 docx 第 4 条「填充勾选」正相反。
    #
    # 依据强度没有丢，只是记在更精确的地方：逐模型的 verified / inferred
    # 在 `model_provenance` 里（上面刚断言过），界面据它逐行显示徽标，
    # 方案里还有一条列出名字的警告。`model_source` 说的是「这一段这次有没有
    # 实测依据」—— 段真的探通了就是 probed，补几个同族同档变体不改变这件事。
    assert sp.model_source == "probed", sp.model_source
    assert sp.recommended, "实测通过的段该默认勾上，否则写回是空的"
    # 补进来的名字必须**可辨认**，否则「默认勾上」就成了盲勾
    assert [m for m, src in sp.model_provenance.items() if src == "inferred"]
    assert any("未经本次推理验证" in w for w in sp.warnings), sp.warnings
    assert v == before


def test_failed_and_lower_probe_fill_highest():
    """探测没探到就填最高代；探到的那一代不被「更高代」顶掉。

    用户 2026-09-16 给的口径（原话）：「gpt-5 和 gpt-5.6 理论上不可能保留，
    因为目前最新模型为 gpt-6 系列，但是**如果检测 gpt-6 系列明显不通，这个
    时候 gpt-6 系列按模型目录最高级别保留同时保留实测最高的 gpt-5.6 系列**」。

    两种情形必须分开，差别只在「`v.models` 里有没有实测通过的名字」：

      · `models=[]` —— 什么都没探到。`have` 只有历史遗留（这里为空或低代），
        `proven` 为空 → 实测豁免集合为空 → 阶段 A 逐字回到原判据：
        目录最高代 `gpt-6` 与它的同代变体 `gpt-6-sol` 都进，低代不留。
      · `models=["gpt-5.5"]` —— 这是**实测通过**的名字（`SectionVerdict.models`
        的语义就是「本轮 `_accept` 收下的名字」），由 plan.py 以 `proven` 传进
        归并。它所属的那一代因此豁免「被更高主版本作废」。

    第二条是 2026-09-16 改的：原来这一项两种情形都断言 `gpt-5.5` 不留。
    那在 `models=["gpt-5.5"]` 下与用户新口径相反 —— 实测通的模型被删掉、
    只留一串从没打通的目录名，CPA 每次轮到这个站都对着死模型发请求。

    注意豁免的粒度是**完整世代**不是主版本：`gpt-5.6` 被豁免时 `gpt-5`
    仍然出局（用户同一句里点名的「gpt-5 不可能保留」）。
    """
    # 情形一：什么也没探到 → 只留目录最高代
    sp = build([verdict(models=[], catalog=["gpt-5.5"])], ["gpt-6", "gpt-6-sol"]).sections[C]
    assert set(sp.models) == {"gpt-6", "gpt-6-sol"}
    assert set(sp.model_provenance.values()) == {"inferred"}
    assert not sp.catalog_stale

    # 情形二：gpt-5.5 是**实测通过**的 → 它与目录最高代并存
    sp = build([verdict(models=["gpt-5.5"], catalog=["gpt-5.5"])],
               ["gpt-6", "gpt-6-sol"]).sections[C]
    assert set(sp.models) == {"gpt-5.5", "gpt-6", "gpt-6-sol"}
    assert sp.model_provenance["gpt-5.5"] == "verified"
    assert sp.model_provenance["gpt-6"] == "inferred"


def test_proven_generation_exempts_only_its_own_generation():
    """实测豁免只放行**那一代**，同主版本的更旧代不搭救。

    `gpt-5.6` 与 `gpt-5` 主版本相同（都是 5）。按主版本放行会把 `gpt-5`
    一起留下，而用户口径是「gpt-5 和 gpt-5.6 理论上不可能保留」——
    要留的只有实测过的那一代本身。所以判据用完整世代 `(5, 6)` 而不是 `5`。
    """
    v = verdict(models=["gpt-5.6"],
                catalog=["gpt-5", "gpt-5.6", "gpt-5.6-sol"])
    sp = build([v], ["gpt-5", "gpt-5.6", "gpt-5.6-sol",
                     "gpt-6", "gpt-6-sol"]).sections[C]
    assert "gpt-5" not in sp.models, sp.models
    assert "gpt-5.6" in sp.models, sp.models
    # 实测那一代的同代变体照样补齐（用户「同等级系列全勾上」的要求）
    assert "gpt-5.6-sol" in sp.models, sp.models


def test_proven_exemption_keeps_verified_names_when_catalog_is_dead():
    """站方实测通 gpt-5.6、目录报的 gpt-6 实际不通时，两代都在。

    这是用户那句话的完整场景：目录（以及市面名录）报 gpt-6，而 gpt-6
    明显不通 —— 只有 gpt-5.6 实测出了 200。此时清单必须同时含
    「目录最高代」（保证站方以后补上 gpt-6 时立刻可用）与
    「实测最高的那一代」（保证现在就有能用的模型）。
    """
    v = verdict(models=["gpt-5.6"], catalog=["gpt-6"])
    sp = build([v], ["gpt-5.6", "gpt-6"]).sections[C]
    assert {"gpt-5.6", "gpt-6"} <= set(sp.models), sp.models
    assert sp.model_provenance["gpt-5.6"] == "verified"


def test_low_tier_never_survives_any_path():
    """降级档（mini / nano / lite）**从任何一条路进来都不许留**。

    用户 2026-09-16 第 1 条原话：「凡是 mini 系列永远不可能勾选，因为这是
    低档次模型」。

    为什么要专门钉这一条（2026-09-16 实测的漏网）
    ------------------------------------------
    `section_allows` 早就拒降级档，但 `topup_to_market_top` 的 `have` 通道
    **不过那道闸** —— 它直接收调用方传进来的站方清单。而降级档在世代比较里
    是「认不出版本」的（`_cmp_gen` 对 `is_low_tier` 的名字返回 None），于是
    `_stale_major` 一律放行，整条淘汰逻辑碰都碰不到它们：

        have=['gpt-4o-mini', 'gpt-5.6']  名录 gpt-6
        → ['gpt-4o-mini', 'gpt-5.6', 'gpt-5.6-sol', 'gpt-6']
                   ^^^^ 2024 年的降级档混进 2026 年的清单

    三条路径各测一次：站方清单带 mini、名录里有 mini、两边都有。
    """
    R = ["gpt-5.6", "gpt-5.6-sol", "gpt-6", "gpt-6-mini", "gpt-6-nano"]

    # ① 站方清单里带降级档
    got, _a, _s = mc.topup_to_market_top(C, ["gpt-4o-mini", "gpt-5.6"],
                                         remote=R)
    assert not any(mc.is_low_tier(m) for m in got), got

    # ② 站方只报了降级档 —— 清单不能空，但也不能留它
    got2, _a, _s = mc.topup_to_market_top(C, ["gpt-6-mini"], remote=R)
    assert not any(mc.is_low_tier(m) for m in got2), got2
    assert got2, "清空会让这个段勾不上"

    # ③ 名录里的降级档也不许被补进来
    got3, _a, _s = mc.topup_to_market_top(C, ["gpt-6"], remote=R)
    assert not any(mc.is_low_tier(m) for m in got3), got3

    # ④ 走完整 build_plan 也一样（探测与目录两条来源）
    sp = build([verdict(models=["gpt-6-mini"], catalog=["gpt-6-mini", "gpt-6"])],
               R).sections[C]
    assert not any(mc.is_low_tier(m) for m in sp.models), sp.models


def test_station_reported_generation_survives_when_market_still_sells_it():
    """站方报过、市面名录里那一代**还在售** → 与顶代并存；已下线才顶掉。

    用户 2026-09-16 第 2 条原话：「如果检测出来没有最高级模型，比如检测出来
    最新模型 gpt-6 系列不通，直接按最新模型 gpt-6 填充。但是次高级模型如
    gpt-5.6 通的，这个时候将 gpt-5.6 系列与 gpt-6 系列都勾选保留。」

    为什么判据不能是 `proven`（本轮实测 200）
    -------------------------------------
    真实现场可以整轮一次都不成功：那份 10.9 MB 的全量检测快照实测 45 次请求，
    403×698 / 503×287 / 429×269，**几乎 0 次 200**。`proven` 恒空则豁免恒不
    成立，站方报过的 `gpt-5.6-sol` 被换成站方从没报过的 `gpt-6-astra` ——
    写进 config.yaml 后 CPA 每次轮到这个站都对着不存在的型号发请求。

    为什么判据也不能是「主版本差几代」
    ------------------------------
    主版本号不连续（gpt 4 → 5 之间没有真正的世代），差值恒为 1 的两个场景
    一个该豁免一个该顶掉，阈值分不开。真正的区别是**市面名录里还认不认识
    站方那一代**：还在售 = 站方只是新代没上架；名录里连同代的影子都没有
    = 已下线。
    """
    # ① 还在售：5.6 与 6 并存，且同代变体一起补齐
    R = ["gpt-5.6", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-6", "gpt-6-sol"]
    got, _a, _s = mc.topup_to_market_top(C, ["gpt-5.6"], remote=R)
    assert "gpt-5.6" in got, got
    assert "gpt-6" in got, got
    assert "gpt-5.6-sol" in got, got

    # ② 已下线（陈年目录）：名录里一个 (4,0) 都没有 → 顶成市面最高级
    got2, _a, _s = mc.topup_to_market_top(C, ["gpt-4", "gpt-4-32k"], remote=R)
    assert not any(m.startswith("gpt-4") for m in got2), got2
    assert "gpt-6" in got2, got2

    # ③ mhtml 快照的原始形状（live catalog 里没有裸 gpt-5.6、没有裸 gpt-6）
    LIVE = ["gpt-5.5", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-astra",
            "gpt-5.6-sol", "gpt-5.3-codex-spark"]
    got3, _a, _s = mc.topup_to_market_top(C, ["gpt-5.6-sol"], remote=LIVE)
    assert "gpt-5.6-sol" in got3, (
        f"站方报过、名录里还在售的名字被换成了它没报过的：{got3}")
    assert "gpt-6-astra" in got3, got3


def test_reenable_targets_only_for_real_disable_markers():
    """`reenable_targets` 只认两种真停用标记，别的一概不算。

    用户 2026-09-16：「无论原来是否被关闭，如果探测可用就要打开」。
    停用在本部署有两种表达（见 entry_out_of_pool）：
      · compat 段 `disabled: true`
      · 任意段 `excluded-models: ["*"]`
    其余「像停用但不是」的形态必须**不被**当成停用，否则会去改用户
    手工设的东西：`disabled: false`（显式启用）、`excluded-models: ["gpt-4"]`
    （正常的模型排除，与停用无关）、`weight: 0`（权重表达，不是开关）。
    """
    from cpa_probe.plan import reenable_targets

    assert reenable_targets("openai-compatibility", {"disabled": True}) == ["disabled"]
    assert reenable_targets("openai-compatibility", {"disabled": False}) == []
    assert reenable_targets("openai-compatibility", {}) == []
    assert reenable_targets(C, {"excluded-models": ["*"]}) == ["excluded-models"]
    assert reenable_targets(C, {"excluded-models": ["*", "gpt-4"]}) == ["excluded-models"]
    assert reenable_targets(C, {"excluded-models": ["gpt-4"]}) == []
    assert reenable_targets(C, {"excluded-models": []}) == []
    assert reenable_targets(C, {"weight": 0}) == []


def test_drop_disabling_lines_handles_both_excluded_shapes():
    """清停用标记要认 `excluded-models` 的**两种**写法，且只摘 `*`。

    `excluded-models` 不在 `_RENDERED_KEYS` 里，所以既有条目走 carry
    （`_dump_fields`）—— 而 PyYAML 对列表写的是**块序列**，序列项与父键
    **同级缩进**。第一版按「缩进必须更深」收集续行，于是 `- '*'` 落单被
    原样留下，写回后出现一个值为 null 的空 `excluded-models:`：没重开成功
    还多一处 diff。inline 写法则是另一种形态。两种都得覆盖。

    只摘 `*` 不删整行：同一行里可能还有操作员手工排除的模型名，那是独立
    语义（「别把请求分给这些模型」），不是停用。
    """
    from cpa_probe.writeback import _dump_fields, _drop_disabling_lines

    # 块序列（PyYAML 的产物，常态）
    assert _drop_disabling_lines(_dump_fields({"excluded-models": ["*"]}, "    "),
                                 ["excluded-models"]) == []
    kept = _drop_disabling_lines(
        _dump_fields({"excluded-models": ["*", "gpt-4"]}, "    "),
        ["excluded-models"])
    assert kept and "gpt-4" in kept[0] and "*" not in kept[0], kept
    # inline
    assert _drop_disabling_lines(['    excluded-models: ["*"]\n'],
                                 ["excluded-models"]) == []
    # compat 的布尔字段整行删
    assert _drop_disabling_lines(["    disabled: true\n"], ["disabled"]) == []
    # 空值（null）与「没有这一行」等价，删
    assert _drop_disabling_lines(["    excluded-models:\n"], ["excluded-models"]) == []
    # 注释行不误伤，其它字段原样留
    assert _drop_disabling_lines(['    # excluded-models: ["*"]\n'],
                                 ["excluded-models"]) == ['    # excluded-models: ["*"]\n']
    assert _drop_disabling_lines(["    priority: 10\n"], ["disabled"]) == ["    priority: 10\n"]


def test_catalog_registration_not_probe_budget():
    names = ["gpt-6"] + [f"gpt-6-peer{i}" for i in range(20)]
    sp = build([verdict(catalog=names)], names).sections[C]
    assert set(sp.models) == set(names)
    assert all(v == "inferred" for v in sp.model_provenance.values())


def test_prior_lower_models_do_not_override_policy_or_mutate_config():
    cfg = {C: [{"base-url": "https://site.example/a/v1",
                "api-key": "fixture-key", "priority": 0,
                "prefix": "KEEP", "headers": {"x-fixture": "kept"},
                "models": [{"name": "gpt-5.5", "alias": "old"},
                           {"name": "gpt-6", "alias": "primary"},
                           {"name": "gpt-6", "alias": "secondary"}]}]}
    before = copy.deepcopy(cfg)
    for models in (["gpt-5.5", "gpt-6"], []):
        sp = build([verdict(models=models, base_url="https://site.example/a/v1")],
                   ["gpt-6"], cfg, rebuild=True).sections[C]
        assert sp.models == ["gpt-6"]
        assert isinstance(sp.models, list)
        assert cfg == before


def test_compat_independent_families_and_custom_names():
    names = ["gpt-5.6-astra", "gpt-6", "claude-opus-4-8",
             "claude-sonnet-5", "gemini-3.1-pro", "gemini-4-pro",
             "kimi-k2", "kimi-k3"]
    sp = build([verdict(O, names)], names).sections[O]
    assert set(sp.models) == set(names[1::2])
    sp = build([verdict(O, catalog=["grok-4.6", "grok-5"])], []).sections[O]
    assert sp.models == ["grok-5"]


def test_compat_gemini_also_requires_highest_pro():
    names = ["gemini-3.1-pro-special", "gemini-4-pro",
             "gemini-5-flash", "gpt-6"]
    sp = build([verdict(O, names)], names).sections[O]
    assert set(sp.models) == {"gemini-4-pro", "gpt-6"}
    selected, _ = mc.latest_models(O, remote=names, limit=0)
    assert set(selected) == {"gemini-4-pro", "gpt-6"}


def test_runtime_proxy_choice_legacy_verdict():
    v = verdict(models=["gpt-6"], need_proxy=True)
    row = ParsedRow(1, "", "https://site.example", "fixture-key")
    result = SimpleNamespace(sections={C: v}, proxy="http://192.0.2.22:8888")
    with patch.object(mc, "remote_names", return_value=(["gpt-6"], "")), \
            patch.object(pp, "_proxy_url_for_config", return_value="http://untested:8888"):
        sp = pp.build_plan(row, result, {}).sections[C]
    assert sp.proxy_url == result.proxy
    assert any("代理" in w and "证据" in w for w in sp.warnings)


def test_lower_generation_removal_warning_is_truthful():
    cfg = {C: [{"base-url": "https://site.example/a/v1", "api-key": "fixture-key",
                "models": [{"name": "gpt-5.5"}]}]}
    sp = build([verdict(models=["gpt-5.4", "gpt-5.5", "gpt-6"],
                       base_url="https://site.example/a/v1")],
               ["gpt-6"], cfg, rebuild=True).sections[C]
    assert sp.models == ["gpt-6"]
    assert not any("原条目本来就在用的不会被删" in w for w in sp.warnings)
    assert any("2 个低世代" in w for w in sp.warnings)


def test_exact_prior_highest_peer_is_not_lost_on_success():
    cfg = {C: [
        {"base-url": "https://site.example/a/v1", "api-key": "fixture-key",
         "models": [{"name": "gpt-6-sol", "alias": "kept"}]},
        {"base-url": "https://site.example/b/v1", "api-key": "fixture-key",
         "models": [{"name": "gpt-6-other-path"}]},
        {"base-url": "https://site.example/a/v1", "api-key": "fixture-other",
         "models": [{"name": "gpt-6-other-key"}]}]}
    sp = build([verdict(models=["gpt-6"], base_url="https://site.example/a/v1")],
               ["gpt-6"], cfg, rebuild=True).sections[C]
    assert set(sp.models) == {"gpt-6", "gpt-6-sol"}
    assert sp.model_provenance["gpt-6-sol"] == "inferred"


def test_loopback_range_requires_cpa_warning():
    v = verdict(models=["gpt-6"], need_proxy=True)
    v.successful_proxy_url = "http://127.0.0.2:8888"
    sp = build([v], ["gpt-6"]).sections[C]
    assert any("CPA" in w and "映射" in w for w in sp.warnings)


def test_claude_identity_warning_both_orders():
    for order in ((L,), SECTIONS):
        models = {G: "gemini-4-pro", C: "gpt-6",
                  L: "claude-opus-5", O: "kimi-k3"}
        vs = [verdict(s, [models[s]],
                      min_body_kind="identity+system" if s == L else "")
              for s in order]
        plan = build(vs, list(models.values()))
        assert plan.sections[L].cloak_mode == "always"
        assert any("cloak.mode=always" in w for w in plan.sections[L].warnings)
        for s in order:
            if s != L:
                assert not any("cloak.mode=always" in w for w in plan.sections[s].warnings)


def test_implicit_priority_zero_participates_in_impact():
    cfg = {C: [{"base-url": "https://old.example/v1",
                "api-key": "fixture-old", "models": [{"name": "gpt-6"}]}]}
    band = pp.build_band(cfg, C)
    assert band.tiers == [0]
    assert band.model_top == {"gpt-6": 0}
    assert pp.compute_impact(band, ["gpt-6"], 100)[0].hijacks


def test_cross_section_site_priority_check_keeps_evidence_distinct():
    plans = []
    for i, (section, priority) in enumerate(((C, 0), (L, 100), (O, 200))):
        p = pp.ImportPlan(host="site.example", masked_key="fixture", line_no=i)
        p.sections[section] = pp.SectionPlan(
            section, f"https://site.example/path{i}", f"fixture-{i}",
            models=["fixture-model"], priority=priority)
        plans.append(p)
    before = copy.deepcopy(plans)
    warnings = pp.priority_split_within_host(plans)
    assert len(warnings) == 1
    assert all(f"priority {p}" in warnings[0] for p in (0, 100, 200))
    assert all(f"fixture-{i}" not in warnings[0] for i in range(3))
    assert plans == before


def test_native_prior_models_do_not_cross_paths_or_credentials():
    cfg = {L: [
        {"base-url": "https://site.example/a", "api-key": "fixture-key",
         "models": [{"name": "claude-opus-5"}]},
        {"base-url": "https://site.example/b", "api-key": "fixture-key",
         "models": [{"name": "claude-sonnet-5"}]}]}
    assert pp.existing_models_for(cfg, L, "https://site.example/b", "fixture-key") == [
        "claude-sonnet-5"]
    assert pp.existing_models_for(cfg, L, "https://site.example/b", "other-fixture") == []


def test_successful_proxy_overrides_untested_candidates():
    v = verdict(models=["gpt-6"], need_proxy=True)
    v.successful_proxy_url = "http://192.0.2.20:8888"
    with patch.object(pp, "_proxy_url_for_config", return_value="http://untested:8888"):
        sp = build([v], ["gpt-6"]).sections[C]
    assert sp.proxy_url == v.successful_proxy_url


def test_explicit_numeric_proxy_and_no_deployment_fallback():
    with patch.dict(os.environ, {"PROBE_PROXY": "http://192.0.2.21:8888"}, clear=True):
        assert pp._proxy_url_for_config() == "http://192.0.2.21:8888"
    with patch.dict(os.environ, {}, clear=True):
        assert pp._proxy_url_for_config() == ""


def test_localhost_proxy_mapping_or_warning():
    v = verdict(models=["gpt-6"], need_proxy=True)
    v.successful_proxy_url = "http://127.0.0.1:8888"
    with patch.dict(os.environ, {}, clear=True):
        sp = build([v], ["gpt-6"]).sections[C]
    assert any("CPA" in w and "代理" in w for w in sp.warnings)
    v.cpa_proxy_url = "http://reachable-proxy:8888"
    sp = build([v], ["gpt-6"]).sections[C]
    assert sp.proxy_url == v.cpa_proxy_url




def test_retry_budget_interval_is_per_round_not_per_credential():
    """轮间等待是**每轮一次**，不是每个凭据之间都等一次。

    2026-09-12：`CPA配置修改需求.md` 建议把 max-retry-credentials 压到 4，
    算式写的是「12 × (连接+等待) + 11 × 最多 8 秒退避」—— 把
    `max-retry-interval` 当成每个凭据之间都等一次，于是算出 88 秒退避。

    但 config.yaml:249 的原注释写明那是「各重试**轮次**之间等待凭证冷却的
    最长秒数」，轮数 = request-retry + 1。当前 request-retry: 1 → 只有一次
    轮间等待，退避总量 8 秒而不是 88 秒。

    照那份算式压预算的后果是确定的：预算 < 顶层池 → 有可用凭据却没机会试 →
    预算耗尽后 CPA 原样透传最后那个错误（conductor_execution.go:325-330），
    挂机时客户端看到 403。所以这条算式必须钉死。
    """
    from cpa_probe import tuning

    # 12 个凭据、2 轮、每次 2.9 秒、轮间 8 秒
    got = tuning.worst_case_seconds(budget=12, rounds=2, attempt_sec=2.9,
                                    interval_sec=8)
    assert abs(got - (2 * 12 * 2.9 + 8)) < 0.01, got
    # 那份 .md 的算法会得出 12*2.9 + 11*8 = 122.8 —— 必须不等于本函数结果
    assert abs(got - (12 * 2.9 + 11 * 8)) > 1, "退避被按每个凭据算了"
    # 单轮（request-retry: 0）没有轮间等待
    assert tuning.worst_case_seconds(budget=4, rounds=1, attempt_sec=2.0,
                                     interval_sec=30) == 8.0


def test_tier_facts_counts_credentials_and_same_host_runs():
    """顶层池实况：按凭据数（不是条目数）算，并认出连续同站。

    连续同站是真实的坑：权重相同时轮询严格按数组顺序（selector.go:539-560），
    预算越大越可能连打同一个站 —— 实测会触发站方前面 Cloudflare 的速率限制，
    返回 HTML 挑战页而不是真正的错误码。
    """
    import yaml
    from cpa_probe import tuning

    cfg = yaml.safe_load("""
claude-api-key:
  - {api-key: a1, base-url: "https://one.example", priority: 900, models: [{name: claude-opus-5}]}
  - {api-key: a2, base-url: "https://one.example", priority: 900, models: [{name: claude-opus-5}]}
  - {api-key: a3, base-url: "https://one.example", priority: 900, models: [{name: claude-opus-5}]}
  - {api-key: b1, base-url: "https://two.example", priority: 900, models: [{name: claude-opus-5}]}
  - {api-key: c1, base-url: "https://low.example", priority: 100, models: [{name: claude-opus-5}]}
  - {api-key: d1, base-url: "https://off.example", priority: 900,
     models: [{name: claude-opus-5}], excluded-models: ["*"]}
  - {api-key: e1, base-url: "https://kept.example", priority: 900, disabled: true,
     models: [{name: claude-opus-5}]}
openai-compatibility:
  - name: p
    base-url: "https://p.example/v1"
    priority: 500
    api-key-entries: [{api-key: k1}, {api-key: k2}]
    models: [{name: gpt-6, alias: ""}]
""")
    facts = tuning.tier_facts(cfg)
    cl = facts["claude-api-key"]
    assert cl.top_priority == 900, cl.top_priority
    # 5 个凭据。两处判据各自的理由：
    #   · 低档那个（priority 100）不在顶层，不算
    #   · `excluded-models: ["*"]` 的不算 —— 那是 CPA 管理面板「停用一个
    #     config 型凭据」的实现，任意段都生效
    #   · 但 `disabled: true` 在 claude 段**仍然算**：那个字段只对 compat
    #     段生效（本项目从 CPA 源码提取的 compat_field="disabled"），
    #     写在前三段是个 CPA 不认识的字段，凭据照样在调度池里轮询。
    #     把它当停用会让预算算小 —— 而预算 < 顶层池正是 403 透传的成因。
    #   · 每条都写了 models：`models` 为空时 CPA 不给该条目注册任何模型，
    #     它同样不在调度池里（entry_out_of_pool 的第三条判据）。夹具漏写
    #     这个字段会让整段算出 0 个凭据。
    assert cl.credentials == 5, cl.credentials
    assert cl.longest_same_host_run == 3, cl.longest_same_host_run
    # 明确钉住上面那条：disabled 在非 compat 段不等于停用
    from cpa_probe.plan import entry_out_of_pool
    assert entry_out_of_pool("claude-api-key", {"api-key": "e1",
                                               "base-url": "https://kept.example",
                                               "disabled": True}) == ""
    assert entry_out_of_pool("openai-compatibility", {"disabled": True,
                                                     "base-url": "https://x/v1"})
    assert cl.run_host == "one.example", cl.run_host
    # compat 段的 Key 挂在 api-key-entries 下 —— 按 Key 数算，不是条目数
    assert facts["openai-compatibility"].credentials == 2


def test_advice_covers_stream_bootstrap_and_never_shrinks_below_top_pool():
    """两个方向的判据都要在：不压到顶层池以下，也不越过窗口。

    `stream-bootstrap-buffering: false` 是确定的缺陷（0 事件的 200，即客户端
    报的 StreamNoEventsError），必须给 blocker 级建议。
    """
    import yaml
    from cpa_probe import tuning

    cfg = yaml.safe_load("""
debug: true
request-retry: 1
max-retry-credentials: 3
max-retry-interval: 8
streaming:
  bootstrap-retries: 1
codex:
  stream-bootstrap-buffering: false
claude-api-key:
  - {api-key: a1, base-url: "https://one.example", priority: 900}
  - {api-key: b1, base-url: "https://two.example", priority: 900}
  - {api-key: c1, base-url: "https://three.example", priority: 900}
  - {api-key: d1, base-url: "https://four.example", priority: 900}
""")
    advices, notes, _facts = tuning.advise(cfg, attempt_sec=2.0,
                                          attempt_why="测试固定值")
    by_key = {a.label: a for a in advices}
    # 顶层池 4 个而预算 3 —— 必须建议抬到 4，且判为 blocker
    assert by_key["max-retry-credentials"].want == 4, by_key["max-retry-credentials"].want
    assert by_key["max-retry-credentials"].severity == "blocker"
    # 流式引导缓冲：blocker
    sbb = by_key["codex.stream-bootstrap-buffering"]
    assert sbb.want is True and sbb.severity == "blocker", sbb
    assert "StreamNoEventsError" in sbb.why
    # 引导重试与 debug 各自有建议，但不是 blocker
    assert by_key["streaming.bootstrap-retries"].want == 3
    assert by_key["debug"].want is False
    assert by_key["debug"].severity == "info"
    # 每条都要带 why —— 不许只给数字让人凭信任点确认
    for adv in advices:
        assert adv.why.strip(), adv.label


def test_advice_refuses_to_shrink_budget_when_window_is_too_small():
    """顶层池装不进窗口时，**不**自动压预算 —— 那会制造 403 透传。

    压预算是「透传 403」那一侧的后果，而正确的解法是压顶层池（本项目做得到）。
    这种冲突要报出来交给操作员决定，不许工具自己选一边。
    """
    import yaml
    from cpa_probe import tuning

    rows = "\n".join(
        f'  - {{api-key: k{i}, base-url: "https://s{i}.example", priority: 900}}'
        for i in range(40))
    cfg = yaml.safe_load(
        "request-retry: 1\nmax-retry-credentials: 40\nmax-retry-interval: 8\n"
        "claude-api-key:\n" + rows)
    advices, notes, _f = tuning.advise(cfg, attempt_sec=2.9,
                                       attempt_why="测试固定值")
    joined = " ".join(notes)
    assert "冲突" in joined, notes
    assert "压顶层池" in joined or "顶层凭据数压" in joined, notes
    # 给出的那条建议必须说清代价，而不是默默压小
    adv = next((a for a in advices if a.label == "max-retry-credentials"), None)
    assert adv is not None and adv.want < 40, adv
    assert "永远轮不到" in adv.why, adv.why
if __name__ == "__main__":
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            with patch("socket.socket.connect",
                       side_effect=AssertionError("live network forbidden")) as connect:
                fn()
                assert connect.call_count == 0, "unexpected network attempt"
            print("PASS", name)
        except Exception:
            failed += 1
            print("FAIL", name)
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed; {failed} failed")
    sys.exit(bool(failed))
